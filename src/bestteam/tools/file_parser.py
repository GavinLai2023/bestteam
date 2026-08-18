from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ..exceptions import ConfigurationError

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}

SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".xlsx", ".xlsm", ".docx", ".xml"} | _TEXT_SUFFIXES
)


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

    # `lenient_text` deliberately left at its strict default: a mis-encoded
    # file here is an operator's own document, and a knowledge base is better
    # served by skipping it with a warning than by silently ingesting mojibake
    # chunks nobody can search. Attachments make the opposite trade -- see
    # `parse_bytes`.
    return parse_bytes(file_path.read_bytes(), file_path.name)


def parse_bytes(data: bytes, filename: str, *, lenient_text: bool = False) -> str:
    """Extract text from a file's bytes, dispatching on `filename`'s suffix.

    The byte-based entry point exists because email attachments are named by
    whoever sent the message. `parse_file` reads whatever path it is given,
    with no sandboxing -- a contract that is fine for knowledge bases, where an
    operator chooses the paths, and exactly wrong for mail. Parsing from bytes
    means there is no path for an attacker-chosen name to become.

    `filename` is used for dispatch and for the rendered header only; it is
    never resolved, opened, or joined to anything.

    Args:
        data: The file's raw bytes.
        filename: The file's name, used only for suffix dispatch and headers.
        lenient_text: Replace undecodable bytes in a plain-text file instead of
            raising. Off by default, so this entry point and `parse_file` agree
            and a mis-encoded document is reported rather than silently
            mangled. The email attachment path turns it on: a sender can name
            anything `.txt`, and one bad attachment must not fail a customer's
            whole run. Affects only plain text -- the binary parsers raise
            either way.

    Returns:
        Extracted text content as a single string.
    """
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        return _parse_pdf_bytes(data, filename)
    if suffix in (".xlsx", ".xlsm"):
        return _parse_excel_bytes(data, filename)
    if suffix == ".docx":
        return _parse_docx_bytes(data, filename)
    if suffix == ".xml":
        return _parse_xml_bytes(data, filename)
    if suffix in _TEXT_SUFFIXES:
        return _decode_text(data, lenient=lenient_text)

    raise ConfigurationError(
        f"Unsupported file type '{suffix}'. "
        f"Supported types: .pdf, .xlsx, .xlsm, .docx, .xml, {', '.join(sorted(_TEXT_SUFFIXES))}"
    )


def _decode_text(data: bytes, *, lenient: bool = False) -> str:
    """Decode plain-text bytes the way `Path.read_text` would.

    Strict by default, which is what `Path.read_text(encoding="utf-8")` did: a
    mis-encoded document should be reported to whoever owns it, not stored as
    mojibake. `lenient` swaps in `errors="replace"` for the attachment path,
    where a sender can name anything `.txt` and a UnicodeDecodeError escaping
    into the poller would fail a whole run over one bad file.

    Line endings are translated either way, because the path-based reader this
    replaces opened files in text mode (universal newlines) and its output must
    not change.
    """
    errors = "replace" if lenient else "strict"
    decoded = data.decode("utf-8", errors=errors)
    return decoded.replace("\r\n", "\n").replace("\r", "\n")

def _parse_pdf_bytes(data: bytes, name: str) -> str:
    try:
        import pypdf
    except ImportError as exc:
        raise ConfigurationError(
            "Parsing PDF files requires the 'pypdf' package. "
            "Install it with: pip install 'bestteam[tools-files]'"
        ) from exc

    reader = pypdf.PdfReader(io.BytesIO(data))
    # Pages are joined with a form feed rather than a blank line, so the page
    # boundary survives into the extracted text and a knowledge base can chunk
    # per page and cite an exact `p.N` (see `core/knowledge_base.py`'s
    # `_chunk_document`). That makes the form feed a delimiter, so a page whose
    # own extracted text contains one has to give it up -- otherwise it reads
    # as a page break and silently shifts the number of every page after it,
    # which is the one way the "exact p.N" guarantee could be quietly false.
    # Replaced with a space, not dropped, so words either side stay separated.
    # The email attachment path reads the same string; a form feed is
    # whitespace, so nothing there changes but the separator a model sees
    # between two pages.
    pages = [(page.extract_text() or "").replace("\f", " ") for page in reader.pages]
    header = f"[PDF: {name} — {len(pages)} page(s)]\n"
    return header + "\f".join(pages)


def _parse_docx_bytes(data: bytes, name: str) -> str:
    try:
        import docx
    except ImportError as exc:
        raise ConfigurationError(
            "Parsing Word files requires the 'python-docx' package. "
            "Install it with: pip install 'bestteam[tools-files]'"
        ) from exc

    document = docx.Document(io.BytesIO(data))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]

    table_parts = []
    for i, table in enumerate(document.tables, 1):
        rows = [
            ",".join(cell.text.strip() for cell in row.cells)
            for row in table.rows
        ]
        table_parts.append(f"[Table {i}]\n" + "\n".join(rows))

    header = f"[Word: {name}]\n"
    body = "\n".join(paragraphs)
    if table_parts:
        body += "\n\n" + "\n\n".join(table_parts)
    return header + body


def _parse_xml_bytes(data: bytes, name: str) -> str:
    try:
        # A separate pass collects the document's own namespace declarations;
        # the element tree itself only carries Clark-notation `{uri}local`
        # names, so the prefixes have to come from these start-ns events.
        ns_prefixes = {
            uri: prefix
            for _, (prefix, uri) in ET.iterparse(
                io.BytesIO(data), events=("start-ns",)
            )
        }
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConfigurationError(
            f"Failed to parse XML file '{name}': {exc}"
        ) from exc

    lines = [f"[XML: {name}]"]
    _render_xml_tree(root, lines, ns_prefixes)
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


def _parse_excel_bytes(data: bytes, name: str) -> str:
    try:
        import openpyxl
    except ImportError as exc:
        raise ConfigurationError(
            "Parsing Excel files requires the 'openpyxl' package. "
            "Install it with: pip install 'bestteam[tools-files]'"
        ) from exc

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(",".join("" if v is None else str(v) for v in row))
        parts.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows))
    wb.close()
    return f"[Excel: {name}]\n\n" + "\n\n".join(parts)
