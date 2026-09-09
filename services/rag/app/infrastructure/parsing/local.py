from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from app.domain.models import CanonicalBlock, ParsedDocument


SUPPORTED_LOCAL = {"txt", "md", "json", "csv", "tsv"}


class LocalParseError(ValueError):
    """A deterministic source-format error that must not be retried blindly."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_newlines(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


class LocalDocumentParser:
    def parse(self, path: Path, extension: str) -> ParsedDocument:
        extension = extension.lower().lstrip(".")
        if extension not in SUPPORTED_LOCAL:
            raise ValueError(f"unsupported local extension: {extension}")
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        structured_blocks: list[CanonicalBlock] | None = None
        if extension == "json":
            try:
                value = json.loads(text)
                text = json.dumps(value, ensure_ascii=False, indent=2)
                structured_blocks = json_blocks(value)
            except json.JSONDecodeError as exc:
                raise LocalParseError("MALFORMED_JSON", "JSON attachment is not valid JSON") from exc
        elif extension in {"csv", "tsv"}:
            text = _table_markdown(text, delimiter="\t" if extension == "tsv" else ",")
            structured_blocks = [CanonicalBlock(None, 0, "table", text, metadata={"format": extension})] if text else []
        else:
            text = normalize_newlines(text)
        blocks = structured_blocks if structured_blocks is not None else markdown_blocks(text)
        if not blocks and text:
            blocks = [CanonicalBlock(page_number=None, order=0, type="text", text=text)]
        return ParsedDocument(
            markdown=text,
            blocks=blocks,
            parser=f"local-{extension}",
            parser_version="v1",
        )


def markdown_blocks(markdown: str) -> list[CanonicalBlock]:
    """Turn Markdown/plain text into ordered blocks while retaining headings."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    blocks: list[CanonicalBlock] = []
    heading_path: list[str] = []
    paragraph: list[str] = []
    order = 0
    in_code = False
    code_language = ""
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal order
        value = "\n".join(paragraph).strip()
        paragraph.clear()
        if value:
            block_type = "list" if all(re.match(r"^\s*(?:[-*+] |\d+[.)] )", line) for line in value.splitlines()) else "text"
            blocks.append(CanonicalBlock(None, order, block_type, value, heading_path=tuple(heading_path)))
            order += 1

    def flush_code() -> None:
        nonlocal order
        value = "\n".join(code_lines).rstrip()
        code_lines.clear()
        if value:
            blocks.append(CanonicalBlock(None, order, "code", value, heading_path=tuple(heading_path), metadata={"language": code_language}))
            order += 1

    for line in lines:
        fence = re.match(r"^\s*```\s*([\w+-]*)\s*$", line)
        if fence:
            if in_code:
                flush_code()
                in_code = False
                code_language = ""
            else:
                flush_paragraph()
                in_code = True
                code_language = fence.group(1) or ""
            continue
        if in_code:
            code_lines.append(line)
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_path[:] = heading_path[: level - 1]
            heading_path.append(title)
            blocks.append(CanonicalBlock(None, order, "title", title, heading_path=tuple(heading_path), metadata={"level": level}))
            order += 1
        elif not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line)
    if in_code:
        flush_code()
    flush_paragraph()
    return blocks


def _table_markdown(value: str, *, delimiter: str) -> str:
    rows = list(csv.reader(io.StringIO(value), delimiter=delimiter))
    if not rows:
        return ""
    cleaned = [[cell.replace("\n", " ").strip() for cell in row] for row in rows]
    if not any(cleaned):
        return ""
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    lines = ["| " + " | ".join(cleaned[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in cleaned[1:])
    return "\n".join(lines)


def json_blocks(value: object) -> list[CanonicalBlock]:
    """Flatten JSON leaves into path-aware blocks so long objects stay coherent."""
    blocks: list[CanonicalBlock] = []
    order = 0

    def visit(node: object, path: tuple[str, ...]) -> None:
        nonlocal order
        if isinstance(node, dict):
            if not node:
                blocks.append(CanonicalBlock(None, order, "text", f"{' / '.join(path)}: {{}}".strip(), heading_path=path, metadata={"format": "json"}))
                order += 1
            for key, child in node.items():
                visit(child, path + (str(key),))
            return
        if isinstance(node, list):
            if not node:
                blocks.append(CanonicalBlock(None, order, "text", f"{' / '.join(path)}: []".strip(), heading_path=path, metadata={"format": "json"}))
                order += 1
            for index, child in enumerate(node):
                visit(child, path + (f"[{index}]",))
            return
        label = " / ".join(path)
        blocks.append(CanonicalBlock(None, order, "text", f"{label}: {json.dumps(node, ensure_ascii=False)}".strip(), heading_path=path[:-1], metadata={"format": "json", "json_path": label}))
        order += 1

    visit(value, ())
    return blocks
