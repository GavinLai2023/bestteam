from __future__ import annotations

from pathlib import Path

from ..exceptions import ConfigurationError

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}


def parse_file(path: str) -> str:
    """Extract text content from a file.

    Supports PDF (text extraction), Excel (.xlsx/.xls, rendered as CSV rows),
    and common plain-text formats (.txt, .md, .csv, .json, .yaml).

    Args:
        path: Absolute or relative path to the file to parse.

    Returns:
        Extracted text content as a single string.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigurationError(f"File not found: {path}")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return _parse_pdf(file_path)
    if suffix in (".xlsx", ".xls", ".xlsm"):
        return _parse_excel(file_path)
    if suffix in _TEXT_SUFFIXES:
        return file_path.read_text(encoding="utf-8")

    raise ConfigurationError(
        f"Unsupported file type '{suffix}'. "
        f"Supported types: .pdf, .xlsx, .xls, {', '.join(sorted(_TEXT_SUFFIXES))}"
    )


def _parse_pdf(path: Path) -> str:
    try:
        import pypdf
    except ImportError as exc:
        raise ConfigurationError(
            "Parsing PDF files requires the 'pypdf' package. "
            "Install it with: pip install 'bestteam[tools-files]'"
        ) from exc

    reader = pypdf.PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    header = f"[PDF: {path.name} — {len(pages)} page(s)]\n"
    return header + "\n\n".join(pages)


def _parse_excel(path: Path) -> str:
    try:
        import openpyxl
    except ImportError as exc:
        raise ConfigurationError(
            "Parsing Excel files requires the 'openpyxl' package. "
            "Install it with: pip install 'bestteam[tools-files]'"
        ) from exc

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(",".join("" if v is None else str(v) for v in row))
        parts.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows))
    wb.close()
    return f"[Excel: {path.name}]\n\n" + "\n\n".join(parts)
