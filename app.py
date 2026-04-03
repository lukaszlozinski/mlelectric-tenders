"""Tenders Briefer + Matcher — Streamlit App.

Upload tender PDFs → get DOCX briefing + XLSX reference match report.

Storage backend: local filesystem (swappable to GDrive later).
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

# Add briefer/matcher to path
sys.path.insert(0, str(BRIEFER_DIR))
sys.path.insert(0, str(MATCHER_DIR))

# Load API key: Streamlit secrets (cloud) or .env (local)
try:
    import streamlit as _st_check
    if hasattr(_st_check, "secrets") and "ANTHROPIC_API_KEY" in _st_check.secrets:
        os.environ["ANTHROPIC_API_KEY"] = _st_check.secrets["ANTHROPIC_API_KEY"]
except Exception:
    pass
from dotenv import load_dotenv
load_dotenv(BRIEFER_DIR / ".env", override=True)

# --- Imports from briefer/matcher ---
from prompt import SYSTEM_PROMPT, build_pdf_user_prompt
from llm_providers import AnthropicProvider
from docx_writer import generate_briefing_docx
from reference_matcher import load_reference_db, match_all_references, _extract_tender_requirements, _generate_llm_explanations, write_report_xlsx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
MONTHLY_CAP_USD = 30.0
USAGE_FILE = APP_DIR / "_usage.json"
# Password: from Streamlit secrets (cloud) or env var (local)
try:
    PASSWORD = st.secrets.get("APP_PASSWORD", os.environ.get("APP_PASSWORD", "mlelectric2026"))
except Exception:
    PASSWORD = os.environ.get("APP_PASSWORD", "mlelectric2026")
BRIEFER_MODEL = "claude-opus-4-20250514"

# --- Usage tracking ---

def _load_usage() -> dict:
    if USAGE_FILE.exists():
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)
        # Reset monthly counter if new month
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

def compute_input_hash(pdf_files: list) -> str:
    """Compute SHA256 hash of uploaded PDF content."""
    h = hashlib.sha256()
    for f in sorted(pdf_files, key=lambda x: x.name):
        h.update(f.getvalue())
    return h.hexdigest()[:12]


def run_briefer(pdf_files: list, progress_callback=None) -> tuple[dict | None, bytes | None, float]:
    """Run the briefer on uploaded PDFs.

    Returns (parsed_data, docx_bytes, cost_usd).
    """
    import base64
    import anthropic

    client = anthropic.Anthropic()
    provider_name = "Opus"

    # Build content blocks
    content = []
    for pdf_file in pdf_files:
        pdf_data = pdf_file.getvalue()
        b64_data = base64.standard_b64encode(pdf_data).decode("utf-8")
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
            "title": pdf_file.name,
        })

    user_prompt = build_pdf_user_prompt()
    content.append({"type": "text", "text": user_prompt})

    if progress_callback:
        progress_callback("Wysyłanie dokumentów do AI...")

    # Call API
    try:
        response = client.messages.create(
            model=BRIEFER_MODEL,
            max_tokens=16000,
            temperature=0.1,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        st.error(f"Błąd API: {e}. Spróbuj ponownie za kilka minut.")
        return None, None, 0.0

    raw = response.content[0].text
    inp_tokens = response.usage.input_tokens
    out_tokens = response.usage.output_tokens
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
        filenames = [f.name for f in pdf_files]
        generate_briefing_docx(parsed, provider_name, tmp_path, filenames)
        docx_bytes = tmp_path.read_bytes()
    except Exception as e:
        st.error(f"Błąd generowania DOCX: {e}")
        docx_bytes = None
    finally:
        tmp_path.unlink(missing_ok=True)

    return parsed, docx_bytes, cost


def run_matcher(tender_data: dict, progress_callback=None) -> tuple[bytes | None, float]:
    """Run the matcher against reference_db.

    Returns (xlsx_bytes, cost_usd).
    """
    if progress_callback:
        progress_callback("Ładowanie bazy referencji...")

    references = load_reference_db()
    if not references:
        st.warning("Baza referencji jest pusta. Dodaj referencje do reference_db/.")
        return None, 0.0

    if progress_callback:
        progress_callback(f"Dopasowywanie {len(references)} referencji...")

    all_matches = match_all_references(tender_data, references)

    # LLM explanations for top 3
    if progress_callback:
        progress_callback("Generowanie opisów dla TOP 3 referencji...")

    reqs = _extract_tender_requirements(tender_data)
    llm_explanations = {}
    top3 = all_matches[:3]
    cost = 0.0

    if top3:
        try:
            llm_explanations = _generate_llm_explanations(top3, tender_data, reqs)
            # Estimate LLM explanation cost (Sonnet: ~$0.01 per explanation)
            cost = 0.03  # 3 explanations
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


# --- Streamlit UI ---

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

# File upload
st.header("1. Wgraj dokumenty przetargowe (PDF)")
uploaded_files = st.file_uploader(
    "Wybierz pliki PDF (SWZ, OPZ, odpowiedzi na pytania, załączniki)",
    type=["pdf"],
    accept_multiple_files=True,
)

if uploaded_files:
    st.success(f"Wgrano {len(uploaded_files)} plików: {', '.join(f.name for f in uploaded_files)}")
    total_size = sum(f.size for f in uploaded_files) / 1024 / 1024
    st.caption(f"Łączny rozmiar: {total_size:.1f} MB")

    # Cost estimate
    est_pages = int(total_size * 5)  # ~5 pages per MB
    est_input_tokens = est_pages * 1500 + 5000  # ~1500 tokens/page + prompt
    est_cost = (est_input_tokens * 15 + 4000 * 75) / 1_000_000
    st.info(f"Szacowany koszt analizy: **${est_cost:.2f}** (~{est_pages} stron, model Opus)")

    if est_cost > remaining:
        st.error(f"Niewystarczający budżet. Pozostało ${remaining:.2f}, potrzeba ~${est_cost:.2f}.")
        st.stop()

st.divider()

# Reference DB status
st.header("2. Baza referencji")
ref_count = len(list(REFERENCE_DB_DIR.glob("*.json"))) if REFERENCE_DB_DIR.exists() else 0
st.info(f"Załadowanych referencji: **{ref_count}**")

st.divider()

# Run analysis
st.header("3. Uruchom analizę")

if not uploaded_files:
    st.warning("Najpierw wgraj dokumenty przetargowe (krok 1).")
    st.stop()

if ref_count == 0:
    st.warning("Baza referencji jest pusta. Dodaj pliki JSON do matcher/reference_db/.")
    st.stop()

if st.button("🔍 Analizuj przetarg", type="primary", use_container_width=True):
    status = st.status("Trwa analiza przetargu...", expanded=True)

    # Step 1: Briefer
    with status:
        st.write("**Etap 1/2:** Analiza dokumentów przetargowych (Claude Opus)...")
        progress = st.empty()

        parsed_data, docx_bytes, briefer_cost = run_briefer(
            uploaded_files,
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
            progress_callback=lambda msg: progress.caption(msg),
        )

        total_cost = briefer_cost + matcher_cost
        _record_cost(total_cost)

        if xlsx_bytes:
            st.write(f"✅ Raport referencji gotowy")
        else:
            st.write("⚠️ Raport referencji nie został wygenerowany")

        status.update(label=f"Analiza zakończona (koszt: ${total_cost:.2f})", state="complete")

    # Results
    st.divider()
    st.header("4. Wyniki")

    # Summary from parsed data
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

        # Experience requirements
        st.subheader("Wymagania doświadczenia")
        for req in parsed_data.get("wymagania_doswiadczenie", []):
            st.write(f"- {req}")

    # Download buttons
    st.divider()
    col1, col2 = st.columns(2)

    tender_name = (parsed_data.get("nazwa", "przetarg") or "przetarg")[:50]
    date_str = datetime.now().strftime("%Y-%m-%d")

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

    # Store in session for re-download
    st.session_state["last_parsed"] = parsed_data
    st.session_state["last_docx"] = docx_bytes
    st.session_state["last_xlsx"] = xlsx_bytes
