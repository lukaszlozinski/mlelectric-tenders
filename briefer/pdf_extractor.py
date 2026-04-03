"""Extract text from PDF files using pdfplumber."""
import os
import pdfplumber
from pathlib import Path


def extract_all_pdfs(input_dir: str) -> tuple[str, list[str]]:
    """Extract text from all PDFs in directory.

    Returns:
        (combined_text, list_of_filenames)
    """
    input_path = Path(input_dir)
    pdf_files = sorted(input_path.glob("*.pdf"))

    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in {input_dir}")

    sections = []
    filenames = []

    for pdf_file in pdf_files:
        filenames.append(pdf_file.name)
        text = extract_single_pdf(pdf_file)
        sections.append(f"\n{'='*60}\n=== DOKUMENT: {pdf_file.name} ===\n{'='*60}\n\n{text}")

    combined = "\n".join(sections)
    return combined, filenames


def extract_single_pdf(pdf_path: Path) -> str:
    """Extract text from a single PDF, handling tables specially."""
    pages_text = []

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            # Try table extraction first
            tables = page.extract_tables()
            text = page.extract_text() or ""

            if tables:
                # If tables found, render them as structured text
                table_texts = []
                for table in tables:
                    table_texts.append(_format_table(table))

                # Combine regular text + table text
                page_content = f"--- Strona {i} ---\n{text}\n"
                if table_texts:
                    page_content += "\n[TABELA]\n" + "\n[/TABELA]\n\n[TABELA]\n".join(table_texts) + "\n[/TABELA]\n"
            else:
                page_content = f"--- Strona {i} ---\n{text}"

            pages_text.append(page_content)

    return "\n\n".join(pages_text)


def _format_table(table: list[list]) -> str:
    """Format an extracted table as readable text."""
    if not table:
        return ""

    rows = []
    for row in table:
        cells = [str(cell).strip() if cell else "" for cell in row]
        rows.append(" | ".join(cells))

    return "\n".join(rows)


def count_chars(text: str) -> int:
    """Return character count of text."""
    return len(text)
