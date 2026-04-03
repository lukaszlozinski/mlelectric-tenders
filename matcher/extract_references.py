"""Extract structured metadata from reference PDFs using Claude Sonnet (Batches API).

Incremental: only processes PDFs that don't have a corresponding JSON in reference_db/.
Handles scans via Claude's native PDF vision.
Uses Anthropic Batches API for 50% cost savings.

Usage:
    python extract_references.py              # Process new PDFs only
    python extract_references.py --force      # Reprocess all PDFs
    python extract_references.py --dry-run    # Show what would be processed
    python extract_references.py --file X.pdf # Process a single file (sequential, no batch)
"""
import argparse
import base64
import json
import logging
import sys
import io
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from dotenv import load_dotenv

# Load API key from briefer's .env
load_dotenv(Path(__file__).parent.parent / "briefer" / ".env", override=True)

import anthropic

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REFS_DIR = Path(__file__).parent / "references"
DB_DIR = Path(__file__).parent / "reference_db"
MODEL = "claude-sonnet-4-20250514"

EXTRACTION_PROMPT = """Przeanalizuj załączony dokument referencyjny (list referencyjny / poświadczenie wykonania robót).

Wyodrębnij WSZYSTKIE dostępne informacje i zwróć JSON:

{
  "operator": "Nazwa zamawiającego / inwestora (np. PGE Dystrybucja, TAURON, ENEA, ENERGA, PSE)",
  "wykonawca": "Nazwa wykonawcy (firma która otrzymała referencję)",
  "contract_value_pln_netto": null lub liczba (wartość kontraktu netto w PLN, jeśli podana),
  "contract_value_pln_brutto": null lub liczba (wartość brutto, jeśli podana),
  "currency_note": "Jeśli kwota w innej walucie niż PLN — podaj oryginalną kwotę i walutę. null jeśli PLN.",
  "voltage": "Napięcie linii (np. '110 kV', '220 kV', '400 kV') — null jeśli nie podane",
  "scope_summary": "Krótki opis zakresu prac (1-2 zdania, po polsku)",
  "scope_elements": ["lista elementów zakresu: np. 'słupy kratowe', 'fundamenty', 'przewody fazowe', 'OPGW', 'izolatory', 'uziemienia', 'demontaż'"],
  "work_type": "Typ prac: przebudowa / modernizacja / budowa / wymiana izolacji / wymiana OPGW / inne",
  "line_route": "Relacja linii (np. 'GPZ Sędziszów – GPZ Szczekociny') — null jeśli nie podana",
  "pole_count": null lub liczba (ilość słupów/stanowisk),
  "line_length_km": null lub liczba (długość linii w km),
  "pole_weight_tonnes": null lub liczba (masa słupów w tonach, jeśli podana),
  "execution_period": "Okres realizacji (np. '2022-2023', 'od 01.2021 do 06.2022')",
  "completion_date": "Data zakończenia/odbioru (np. '2023-10-15') — null jeśli nie podana",
  "quality_note": "Uwagi o jakości (np. 'bez uwag', 'należycie', 'usterki') — null jeśli nie podane",
  "document_type": "Typ dokumentu: list_referencyjny / poswiadczenie / protokol_odbioru / inny",
  "is_scan": true/false,
  "confidence": "wysoka / średnia / niska (pewność ekstrakcji danych)",
  "notes": "Dodatkowe uwagi, np. brakujące dane, nieczytelne fragmenty"
}

ZASADY:
- Jeśli dokument jest skanem — odczytaj tekst z obrazu
- Podaj DOKŁADNE kwoty jeśli widoczne (nie zaokrąglaj)
- Jeśli kwota jest w EUR lub innej walucie, podaj oryginalną kwotę w currency_note i zostaw contract_value_pln_netto jako null
- Jeśli dokument jest nieczytelny — ustaw confidence: "niska" i opisz problem w notes
- scope_elements to LISTA konkretnych elementów prac (słupy, fundamenty, przewody, izolatory, OPGW, uziemienia, demontaż, drogi dojazdowe, itp.)
- Zwróć WYŁĄCZNIE JSON, bez żadnego tekstu przed ani po, bez markdown code blocks
"""


def get_pending_pdfs(force: bool = False, single_file: str | None = None) -> list[Path]:
    """Find PDFs that need processing."""
    DB_DIR.mkdir(parents=True, exist_ok=True)

    if single_file:
        pdf_path = REFS_DIR / single_file
        if not pdf_path.exists():
            logger.error(f"File not found: {pdf_path}")
            return []
        return [pdf_path]

    pdf_files = sorted(REFS_DIR.glob("*.pdf"))

    # Dedup: skip SKAN_ORYGINAL if a TLUMACZENIE_PRZYSIEGLE exists for same date prefix
    deduped = []
    for pdf_path in pdf_files:
        if "SKAN_ORYGINAL" in pdf_path.name:
            # Check if a translation pair exists
            tlum_name = pdf_path.name.replace("SKAN_ORYGINAL", "TLUMACZENIE_PRZYSIEGLE")
            if (REFS_DIR / tlum_name).exists():
                logger.info(f"Skipping {pdf_path.name} (translation pair exists)")
                continue
        deduped.append(pdf_path)

    if force:
        return deduped

    pending = []
    for pdf_path in deduped:
        json_path = DB_DIR / (pdf_path.stem + ".json")
        if json_path.exists():
            if pdf_path.stat().st_mtime <= json_path.stat().st_mtime:
                continue
        pending.append(pdf_path)

    return pending


def _build_request_content(pdf_path: Path) -> list[dict]:
    """Build the API content blocks for a single PDF."""
    pdf_data = pdf_path.read_bytes()
    b64_data = base64.standard_b64encode(pdf_data).decode("utf-8")
    return [
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
            "title": pdf_path.name,
        },
        {"type": "text", "text": EXTRACTION_PROMPT},
    ]


def _parse_response(raw: str) -> dict | None:
    """Parse LLM response, stripping markdown code blocks if present."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        logger.error(f"Raw: {raw[:500]}")
        return None


def extract_single(client: anthropic.Anthropic, pdf_path: Path) -> dict | None:
    """Extract one PDF sequentially (for --file mode)."""
    content = _build_request_content(pdf_path)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.RateLimitError:
        logger.warning("Rate limited, waiting 30s...")
        time.sleep(30)
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            temperature=0.1,
            messages=[{"role": "user", "content": content}],
        )

    data = _parse_response(response.content[0].text)
    if data is None:
        return None

    data["_source_file"] = pdf_path.name
    data["_source_size_kb"] = round(pdf_path.stat().st_size / 1024, 1)
    data["_extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["_model"] = MODEL
    data["_tokens_input"] = response.usage.input_tokens
    data["_tokens_output"] = response.usage.output_tokens
    return data


def extract_batch(client: anthropic.Anthropic, pdf_paths: list[Path]) -> dict[str, dict]:
    """Extract all PDFs via Batches API (50% cheaper).

    Returns dict mapping filename -> extracted data.
    """
    # Build batch requests
    requests = []
    id_to_path = {}  # Map sanitized custom_id back to path
    for idx, pdf_path in enumerate(pdf_paths):
        # custom_id must match ^[a-zA-Z0-9_-]{1,64}$
        custom_id = f"ref_{idx:03d}"
        id_to_path[custom_id] = pdf_path
        content = _build_request_content(pdf_path)
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL,
                "max_tokens": 2000,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": content}],
            },
        })

    logger.info(f"Submitting batch of {len(requests)} requests...")
    batch = client.messages.batches.create(requests=requests)
    batch_id = batch.id
    logger.info(f"Batch created: {batch_id}")

    # Poll for completion
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        total = counts.processing + counts.succeeded + counts.errored + counts.canceled + counts.expired
        logger.info(
            f"  Status: {batch.processing_status} | "
            f"done: {counts.succeeded}/{total}, "
            f"errors: {counts.errored}, "
            f"processing: {counts.processing}"
        )

        if batch.processing_status == "ended":
            break

        time.sleep(10)

    # Retrieve results
    results = {}

    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        pdf_path = id_to_path.get(custom_id)
        if not pdf_path:
            logger.warning(f"Unknown custom_id in batch results: {custom_id}")
            continue

        if result.result.type == "succeeded":
            msg = result.result.message
            raw_text = msg.content[0].text
            data = _parse_response(raw_text)
            if data:
                data["_source_file"] = pdf_path.name
                data["_source_size_kb"] = round(pdf_path.stat().st_size / 1024, 1)
                data["_extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                data["_model"] = MODEL
                data["_tokens_input"] = msg.usage.input_tokens
                data["_tokens_output"] = msg.usage.output_tokens
                data["_batch_id"] = batch_id
                results[pdf_path.name] = data
            else:
                logger.error(f"  {pdf_path.name}: JSON parse failed")
        elif result.result.type == "errored":
            err = result.result.error
            logger.error(f"  {pdf_path.name}: API error — {err}")
        else:
            logger.warning(f"  {pdf_path.name}: status={result.result.type}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Extract reference metadata using Claude (Batches API)")
    parser.add_argument("--force", action="store_true", help="Reprocess all PDFs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--file", type=str, help="Process a single PDF file (sequential, no batch)")
    args = parser.parse_args()

    pending = get_pending_pdfs(force=args.force, single_file=args.file)

    if not pending:
        logger.info("All reference PDFs already processed. Use --force to reprocess.")
        return

    logger.info(f"PDFs to process: {len(pending)}")
    for p in pending:
        logger.info(f"  - {p.name} ({p.stat().st_size / 1024:.0f} KB)")

    # Cost estimate (batch = 50% of standard pricing)
    total_kb = sum(p.stat().st_size for p in pending) / 1024
    est_input_tokens = int(total_kb * 50) + len(EXTRACTION_PROMPT) // 4 * len(pending)
    est_output_tokens = 600 * len(pending)  # ~600 output tokens per reference
    is_batch = len(pending) > 1 and not args.file
    discount = 0.5 if is_batch else 1.0
    # Sonnet pricing: $3/MTok input, $15/MTok output
    est_cost = ((est_input_tokens * 3 + est_output_tokens * 15) / 1_000_000) * discount
    mode = "batch (50% discount)" if is_batch else "sequential"
    logger.info(f"Mode: {mode}")
    logger.info(f"Estimated cost: ~${est_cost:.2f} ({est_input_tokens:,} input + {est_output_tokens:,} output tokens)")

    if args.dry_run:
        logger.info("Dry run — no API calls made.")
        return

    DB_DIR.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic()

    if args.file or len(pending) == 1:
        # Single file — sequential mode
        pdf_path = pending[0]
        logger.info(f"Extracting: {pdf_path.name}")
        data = extract_single(client, pdf_path)
        if data:
            json_path = DB_DIR / (pdf_path.stem + ".json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            val = data.get("contract_value_pln_netto")
            val_str = f"{val:,.0f} PLN" if val else "N/A"
            logger.info(f"  OK: {data.get('operator', '?')} | {data.get('voltage', '?')} | {val_str}")
        else:
            logger.error(f"  FAILED: {pdf_path.name}")
    else:
        # Multiple files — batch mode
        results = extract_batch(client, pending)

        # Save results
        total_in = 0
        total_out = 0
        for filename, data in results.items():
            stem = Path(filename).stem
            json_path = DB_DIR / (stem + ".json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            total_in += data.get("_tokens_input", 0)
            total_out += data.get("_tokens_output", 0)
            val = data.get("contract_value_pln_netto")
            val_str = f"{val:,.0f} PLN" if val else "N/A"
            logger.info(f"  Saved: {filename} | {data.get('operator', '?')} | {val_str}")

        logger.info(f"\nDone: {len(results)}/{len(pending)} extracted")
        logger.info(f"Tokens: {total_in:,} input + {total_out:,} output")
        actual_cost = ((total_in * 3 + total_out * 15) / 1_000_000) * 0.5
        logger.info(f"Actual cost (batch 50%): ~${actual_cost:.2f}")

        if len(results) < len(pending):
            failed = [p.name for p in pending if p.name not in results]
            logger.warning(f"Failed: {failed}")


if __name__ == "__main__":
    main()
