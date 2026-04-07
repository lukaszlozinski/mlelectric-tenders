"""Tenders Briefer + Matcher — Streamlit App.

Select tender from GDrive → get DOCX briefing + XLSX reference match report.
Outputs saved back to GDrive and available for download.
"""
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# Fix Polish encoding on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# --- Path setup ---
APP_DIR = Path(__file__).parent.resolve()
BRIEFER_DIR = APP_DIR / "briefer"
MATCHER_DIR = APP_DIR / "matcher"
REFERENCE_DB_DIR = MATCHER_DIR / "reference_db"

sys.path.insert(0, str(BRIEFER_DIR))
sys.path.insert(0, str(MATCHER_DIR))
sys.path.insert(0, str(APP_DIR))

# Load API key from secrets or .env
try:
    if "ANTHROPIC_API_KEY" in st.secrets:
        os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    pass
from dotenv import load_dotenv
load_dotenv(BRIEFER_DIR / ".env", override=True)

# --- Imports ---
from prompt import SYSTEM_PROMPT, build_pdf_user_prompt
from docx_writer import generate_briefing_docx
from reference_matcher import (
    match_all_references,
    _extract_tender_requirements, _generate_llm_explanations, write_report_xlsx,
)
from gdrive import get_drive_service, list_input_folders, list_folder_pdfs, download_pdf, save_output, load_reference_db_from_gdrive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
MONTHLY_CAP_USD = 30.0
USAGE_FILE = APP_DIR / "_usage.json"
BRIEFER_MODEL = "claude-opus-4-20250514"

try:
    PASSWORD = st.secrets.get("APP_PASSWORD", "mlelectric2026")
except Exception:
    PASSWORD = os.environ.get("APP_PASSWORD", "mlelectric2026")

# --- Usage tracking ---

def _load_usage() -> dict:
    if USAGE_FILE.exists():
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)
        if data.get("month") != datetime.now().strftime("%Y-%m"):
            return {"month": datetime.now().strftime("%Y-%m"), "total_cost_usd": 0.0, "runs": 0}
        return data
    return {"month": datetime.now().strftime("%Y-%m"), "total_cost_usd": 0.0, "runs": 0}


def _save_usage(usage: dict):
    with open(USAGE_FILE, "w") as f:
        json.dump(usage, f, indent=2)


def _check_budget() -> tuple[bool, float]:
    usage = _load_usage()
    remaining = MONTHLY_CAP_USD - usage["total_cost_usd"]
    return remaining > 0, remaining


def _record_cost(cost_usd: float):
    usage = _load_usage()
    usage["total_cost_usd"] += cost_usd
    usage["runs"] += 1
    _save_usage(usage)


# --- Core functions ---

def run_briefer(pdf_data_list: list[tuple[str, bytes]], progress_callback=None) -> tuple[dict | None, bytes | None, float]:
    """Run the briefer on PDF data.

    Args:
        pdf_data_list: list of (filename, pdf_bytes) tuples

    Returns (parsed_data, docx_bytes, cost_usd).
    """
    import base64
    import anthropic

    client = anthropic.Anthropic()

    # Build content blocks
    content = []
    for filename, pdf_bytes in pdf_data_list:
        b64_data = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
            "title": filename,
        })

    user_prompt = build_pdf_user_prompt()
    content.append({"type": "text", "text": user_prompt})

    if progress_callback:
        progress_callback("Wysyłanie dokumentów do AI...")

    # Call API (streaming for long Opus requests)
    try:
        result_parts = []
        with client.messages.stream(
            model=BRIEFER_MODEL,
            max_tokens=16000,
            temperature=0.1,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)
            final = stream.get_final_message()
        raw = "".join(result_parts)
        inp_tokens = final.usage.input_tokens
        out_tokens = final.usage.output_tokens
    except Exception as e:
        st.error(f"Błąd API: {e}. Spróbuj ponownie za kilka minut.")
        return None, None, 0.0

    # Opus pricing: $15/MTok input, $75/MTok output
    cost = (inp_tokens * 15 + out_tokens * 75) / 1_000_000

    if progress_callback:
        progress_callback("Przetwarzanie odpowiedzi...")

    # Parse JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        st.error(f"Błąd parsowania odpowiedzi AI: {e}")
        return None, None, cost

    # Generate DOCX
    if progress_callback:
        progress_callback("Generowanie dokumentu DOCX...")

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        filenames = [name for name, _ in pdf_data_list]
        generate_briefing_docx(parsed, "Opus", tmp_path, filenames)
        docx_bytes = tmp_path.read_bytes()
    except Exception as e:
        st.error(f"Błąd generowania DOCX: {e}")
        docx_bytes = None
    finally:
        tmp_path.unlink(missing_ok=True)

    return parsed, docx_bytes, cost


def run_matcher(tender_data: dict, references: list[dict], progress_callback=None) -> tuple[bytes | None, float]:
    """Run the matcher against preloaded references."""
    if not references:
        st.warning("Baza referencji jest pusta.")
        return None, 0.0

    if progress_callback:
        progress_callback(f"Dopasowywanie {len(references)} referencji...")

    all_matches = match_all_references(tender_data, references)

    # LLM explanations for top 3
    if progress_callback:
        progress_callback("Generowanie opisów dla TOP 3 referencji...")

    reqs = _extract_tender_requirements(tender_data)
    llm_explanations = {}
    cost = 0.0

    top3 = all_matches[:3]
    if top3:
        try:
            llm_explanations = _generate_llm_explanations(top3, tender_data, reqs)
            cost = 0.03
        except Exception as e:
            logger.warning(f"LLM explanations failed: {e}")

    # Generate XLSX
    if progress_callback:
        progress_callback("Generowanie raportu XLSX...")

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        write_report_xlsx(all_matches, tender_data, tmp_path, llm_explanations)
        xlsx_bytes = tmp_path.read_bytes()
    except Exception as e:
        st.error(f"Błąd generowania XLSX: {e}")
        xlsx_bytes = None
    finally:
        tmp_path.unlink(missing_ok=True)

    return xlsx_bytes, cost


# =========================================================================
# Streamlit UI
# =========================================================================

st.set_page_config(
    page_title="Analiza Przetargów — MLElectric",
    page_icon="⚡",
    layout="wide",
)

# Password gate
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("⚡ Analiza Przetargów")
    pwd = st.text_input("Hasło dostępu:", type="password")
    if pwd:
        if pwd == PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Nieprawidłowe hasło")
    st.stop()

# Main app
st.title("⚡ Analiza Przetargów — MLElectric")

# Budget status
budget_ok, remaining = _check_budget()
usage = _load_usage()
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Budżet miesięczny", f"${MONTHLY_CAP_USD:.0f}")
with col2:
    st.metric("Wykorzystano", f"${usage['total_cost_usd']:.2f}")
with col3:
    st.metric("Analiz w tym miesiącu", usage["runs"])

if not budget_ok:
    st.error("Przekroczono miesięczny limit budżetu. Skontaktuj się z administratorem.")
    st.stop()

st.divider()

# ── Step 1: Select tender from GDrive ──
st.header("1. Wybierz przetarg z Google Drive")

try:
    drive_service = get_drive_service()
    input_folders = list_input_folders(drive_service)
except Exception as e:
    st.error(f"Błąd połączenia z Google Drive: {e}")
    st.info("Alternatywnie możesz wgrać pliki ręcznie poniżej.")
    input_folders = []
    drive_service = None

selected_folder = None
pdf_data_list = []

if input_folders:
    folder_names = [f["name"] for f in input_folders]
    selected_name = st.selectbox(
        "Wybierz folder z dokumentami przetargowymi:",
        options=folder_names,
        index=0,
    )
    selected_folder = next(f for f in input_folders if f["name"] == selected_name)

    # List PDFs in selected folder
    pdfs = list_folder_pdfs(selected_folder["id"], drive_service)

    if pdfs:
        st.success(f"Znaleziono {len(pdfs)} plików PDF:")
        total_size = 0
        for pdf in pdfs:
            size = int(pdf.get("size", 0))
            total_size += size
            st.caption(f"  📄 {pdf['name']} ({size / 1024:.0f} KB)")

        total_mb = total_size / 1024 / 1024
        est_pages = int(total_mb * 5)
        est_cost = (est_pages * 1500 * 15 + 4000 * 75) / 1_000_000
        st.info(f"Szacowany koszt analizy: **${est_cost:.2f}** (~{est_pages} stron, model Opus)")

        if est_cost > remaining:
            st.error(f"Niewystarczający budżet. Pozostało ${remaining:.2f}.")
            st.stop()
    else:
        st.warning("Brak plików PDF w wybranym folderze.")
        selected_folder = None

# Fallback: manual upload
st.caption("Lub wgraj pliki ręcznie:")
uploaded_files = st.file_uploader(
    "Wybierz pliki PDF",
    type=["pdf"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)

st.divider()

# Reference DB status — load from GDrive (cached in session)
st.header("2. Baza referencji")
if "references" not in st.session_state:
    try:
        st.session_state.references = load_reference_db_from_gdrive(drive_service)
    except Exception as e:
        st.warning(f"Nie udało się załadować referencji z GDrive: {e}")
        st.session_state.references = []

ref_count = len(st.session_state.references)
st.info(f"Załadowanych referencji: **{ref_count}**")

st.divider()

# ── Step 3: Run analysis ──
st.header("3. Uruchom analizę")

has_input = bool(selected_folder and pdfs) or bool(uploaded_files)

if not has_input:
    st.warning("Wybierz przetarg z Google Drive lub wgraj pliki PDF.")
    st.stop()

if ref_count == 0:
    st.warning("Baza referencji jest pusta.")
    st.stop()

if st.button("🔍 Analizuj przetarg", type="primary", use_container_width=True):
    status = st.status("Trwa analiza przetargu...", expanded=True)

    with status:
        # Download PDFs from GDrive or use uploaded files
        if selected_folder and pdfs and not uploaded_files:
            st.write("**Pobieranie plików z Google Drive...**")
            pdf_data_list = []
            for pdf in pdfs:
                st.caption(f"Pobieranie: {pdf['name']}...")
                data = download_pdf(pdf["id"], drive_service)
                pdf_data_list.append((pdf["name"], data))
            st.write(f"✅ Pobrano {len(pdf_data_list)} plików")
        else:
            pdf_data_list = [(f.name, f.getvalue()) for f in uploaded_files]

        # Step 1: Briefer
        st.write("**Etap 1/2:** Analiza dokumentów przetargowych (Claude Opus)...")
        progress = st.empty()

        parsed_data, docx_bytes, briefer_cost = run_briefer(
            pdf_data_list,
            progress_callback=lambda msg: progress.caption(msg),
        )

        if parsed_data:
            st.write(f"✅ Briefing gotowy (koszt: ${briefer_cost:.2f})")
        else:
            st.write("❌ Briefing nie powiódł się")
            status.update(label="Analiza nie powiodła się", state="error")
            st.stop()

        # Step 2: Matcher
        st.write("**Etap 2/2:** Dopasowanie referencji...")

        xlsx_bytes, matcher_cost = run_matcher(
            parsed_data,
            st.session_state.references,
            progress_callback=lambda msg: progress.caption(msg),
        )

        total_cost = briefer_cost + matcher_cost
        _record_cost(total_cost)

        if xlsx_bytes:
            st.write("✅ Raport referencji gotowy")
        else:
            st.write("⚠️ Raport referencji nie został wygenerowany")

        # Save to GDrive
        tender_name = selected_folder["name"] if selected_folder else "upload"
        date_str = datetime.now().strftime("%Y-%m-%d")
        subfolder_name = f"{tender_name}_{date_str}"
        gdrive_links = {}

        if drive_service:
            st.write("**Zapisywanie wyników na Google Drive...**")
            try:
                if docx_bytes:
                    link = save_output(
                        docx_bytes, f"briefing_{date_str}.docx", subfolder_name,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        drive_service,
                    )
                    gdrive_links["docx"] = link
                    st.write(f"✅ Briefing zapisany na GDrive")

                if xlsx_bytes:
                    link = save_output(
                        xlsx_bytes, f"referencje_{date_str}.xlsx", subfolder_name,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        drive_service,
                    )
                    gdrive_links["xlsx"] = link
                    st.write(f"✅ Raport referencji zapisany na GDrive")

                # Also save parsed JSON for reference
                parsed_bytes = json.dumps(parsed_data, ensure_ascii=False, indent=2).encode("utf-8")
                save_output(
                    parsed_bytes, f"parsed_{date_str}.json", subfolder_name,
                    "application/json", drive_service,
                )
            except Exception as e:
                st.warning(f"Nie udało się zapisać na GDrive: {e}. Pobierz pliki ręcznie poniżej.")

        status.update(label=f"Analiza zakończona (koszt: ${total_cost:.2f})", state="complete")

    # ── Results ──
    st.divider()
    st.header("4. Wyniki")

    if parsed_data:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Przetarg")
            st.write(f"**Nazwa:** {parsed_data.get('nazwa', '—')}")
            st.write(f"**Zamawiający:** {parsed_data.get('zamawiajacy', '—')}")
            st.write(f"**Nr postępowania:** {parsed_data.get('numer_postepowania', '—')}")
            terminy = parsed_data.get("terminy", {})
            st.write(f"**Termin składania ofert:** {terminy.get('skladanie_ofert', '—')}")
        with col2:
            st.subheader("Finanse")
            finanse = parsed_data.get("finanse", {})
            st.write(f"**Wadium:** {finanse.get('wadium', '—')}")
            st.write(f"**Szacunkowa wartość:** {finanse.get('szacunkowa_wartosc', '—')}")
            st.write(f"**Zabezpieczenie NWU:** {finanse.get('zabezpieczenie_nwu', '—')}")

        st.subheader("Wymagania doświadczenia")
        for req in parsed_data.get("wymagania_doswiadczenie", []):
            st.write(f"- {req}")

    # GDrive links
    if gdrive_links:
        st.divider()
        st.subheader("📁 Pliki na Google Drive")
        st.caption(f"Folder: 01_tenders/outputs/{subfolder_name}/")
        for label, link in gdrive_links.items():
            name = "Briefing DOCX" if label == "docx" else "Raport referencji XLSX"
            st.markdown(f"[🔗 {name}]({link})")

    # Download buttons
    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        if docx_bytes:
            st.download_button(
                "📄 Pobierz briefing (DOCX)",
                data=docx_bytes,
                file_name=f"briefing_{date_str}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )

    with col2:
        if xlsx_bytes:
            st.download_button(
                "📊 Pobierz raport referencji (XLSX)",
                data=xlsx_bytes,
                file_name=f"referencje_{date_str}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
