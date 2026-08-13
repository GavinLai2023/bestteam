from __future__ import annotations

from pathlib import Path

from ..exceptions import ConfigurationError

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}


def parse_file(path: str) -> str:
    """Extract text content from a file.

    Supports PDF (text extraction), Excel (.xlsx/.xlsm, rendered as CSV rows),
    Word (.docx, including tables), XML (structural rendering of tags,
    attributes and text), and common plain-text formats (.txt, .md, .csv,
    .json, .yaml). Legacy .xls (BIFF) is not supported -- the openpyxl
    backend reads only the modern Office Open XML formats.

    This tool reads whatever local path it is given, with no sandboxing —
    the same trust boundary as `http_get` fetching arbitrary URLs. If this
    tool is exposed to an LLM agent, the caller is responsible for
    constraining which paths the agent can be prompted to access.

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
    if suffix in (".xlsx", ".xlsm"):
        return _parse_excel(file_path)
    if suffix == ".docx":
        return _parse_docx(file_path)
    if suffix == ".xml":
        return _parse_xml(file_path)
    if suffix in _TEXT_SUFFIXES:
        return file_path.read_text(encoding="utf-8")

    raise ConfigurationError(
        f"Unsupported file type '{suffix}'. "
        f"Supported types: .pdf, .xlsx, .xlsm, .docx, .xml, {', '.join(sorted(_TEXT_SUFFIXES))}"
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


def _parse_docx(path: Path) -> str:
    try:
        import docx
    except ImportError as exc:
        raise ConfigurationError(
            "Parsing Word files requires the 'python-docx' package. "
            "Install it with: pip install 'bestteam[tools-files]'"
        ) from exc

    document = docx.Document(str(path))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]

    table_parts = []
    for i, table in enumerate(document.tables, 1):
        rows = [
            ",".join(cell.text.strip() for cell in row.cells)
            for row in table.rows
        ]
        table_parts.append(f"[Table {i}]\n" + "\n".join(rows))

    header = f"[Word: {path.name}]\n"
    body = "\n".join(paragraphs)
    if table_parts:
        body += "\n\n" + "\n\n".join(table_parts)
    return header + body


def _parse_xml(path: Path) -> str:
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        raise ConfigurationError(
            f"Failed to parse XML file '{path.name}': {exc}"
        ) from exc

    lines = [f"[XML: {path.name}]"]
    _render_xml_element(tree.getroot(), lines, depth=0)
    return "\n".join(lines)


def _render_xml_element(elem, lines: list, depth: int) -> None:
    indent = "  " * depth
    attrs = " ".join(f'{k}="{v}"' for k, v in elem.attrib.items())
    line = f"{indent}<{elem.tag}" + (f" {attrs}" if attrs else "") + ">"
    text = (elem.text or "").strip()
    if text:
        line += f" {text}"
    lines.append(line)
    for child in elem:
        _render_xml_element(child, lines, depth + 1)


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
