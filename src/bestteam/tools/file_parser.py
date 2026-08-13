from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ..exceptions import ConfigurationError

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}


def parse_file(path: str) -> str:
    """Extract text content from a file.

    Supports PDF (text extraction), Excel (.xlsx/.xlsm, rendered as CSV rows),
    Word (.docx, including tables), XML (structural rendering of tags,
    attributes, and text, including mixed-content tail text and namespace
    prefixes -- comments and processing instructions are dropped, matching
    ElementTree's default parser), and common plain-text formats (.txt, .md,
    .csv, .json, .yaml). Legacy .xls (BIFF) is not supported -- the openpyxl
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
    try:
        ns_prefixes = {
            uri: prefix
            for _, (prefix, uri) in ET.iterparse(str(path), events=("start-ns",))
        }
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        raise ConfigurationError(
            f"Failed to parse XML file '{path.name}': {exc}"
        ) from exc

    lines = [f"[XML: {path.name}]"]
    _render_xml_tree(tree.getroot(), lines, ns_prefixes)
    return "\n".join(lines)


def _qualified_name(name: str, ns_prefixes: dict) -> str:
    """Render a Clark-notation `{uri}local` name as `prefix:local` using the
    document's own namespace declarations, falling back to the bare local
    name when no prefix is known (e.g. the default namespace)."""
    if not name.startswith("{"):
        return name
    uri, _, local = name[1:].partition("}")
    prefix = ns_prefixes.get(uri)
    return f"{prefix}:{local}" if prefix else local


def _normalize_xml_text(text: "str | None") -> str:
    return " ".join(text.split()) if text else ""


def _render_element_open_line(elem, depth: int, ns_prefixes: dict) -> str:
    indent = "  " * depth
    tag = _qualified_name(elem.tag, ns_prefixes)
    attrs = " ".join(
        f"{_qualified_name(k, ns_prefixes)}={quoteattr(v)}"
        for k, v in elem.attrib.items()
    )
    line = f"{indent}<{tag}" + (f" {attrs}" if attrs else "") + ">"
    text = _normalize_xml_text(elem.text)
    if text:
        line += f" {escape(text)}"
    return line


def _render_xml_tree(root, lines: list, ns_prefixes: dict) -> None:
    """Depth-first render of the element tree, including each element's tail
    text (mixed content following a child's closing tag). Iterative rather
    than recursive so a pathologically deep-but-narrow document can't blow
    the Python call stack -- an explicit stack is bounded only by memory,
    matching what `ET.parse` itself can already handle."""
    RENDER, TAIL = 0, 1
    stack = [(RENDER, root, 0)]
    while stack:
        kind, payload, depth = stack.pop()
        if kind == TAIL:
            lines.append(f"{'  ' * depth}{payload}")
            continue

        elem = payload
        lines.append(_render_element_open_line(elem, depth, ns_prefixes))

        items = []
        for child in elem:
            items.append((RENDER, child, depth + 1))
            tail = _normalize_xml_text(child.tail)
            if tail:
                items.append((TAIL, escape(tail), depth + 1))
        stack.extend(reversed(items))


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
