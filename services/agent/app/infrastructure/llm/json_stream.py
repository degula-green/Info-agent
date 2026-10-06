"""Incremental extraction of one known string field from streamed JSON.

The model is still asked for strict JSON. This module does not parse the whole
JSON incrementally and does not decide whether the document is valid; it only
locates one known top-level string field and decodes that string while chunks
arrive. The final, authoritative parse still belongs to ``parse_json_object``
plus the provider's Pydantic model once the stream ends.

The extractor is a deliberately explicit state machine because chunk boundaries
can fall anywhere, including between a key and its colon, between a colon and
the opening quote, inside an escape sequence, or inside a surrogate pair.
"""

from __future__ import annotations

from typing import Iterable, Iterator


_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class FirstStringFieldExtractor:
    """Decode the first top-level string field whose name matches ``field``.

    Usage::

        extractor = FirstStringFieldExtractor("answer")
        for chunk in model_chunks:
            piece = extractor.feed(chunk)
            if piece:
                sink(piece)
        piece = extractor.finish()

    If the field never appears, its value is not a string, or the stream is
    malformed before the value starts, ``feed`` simply returns no text. The
    caller then keeps buffering and lets the authoritative final parse decide.
    """

    def __init__(self, field: str) -> None:
        name = str(field or "").strip()
        if not name:
            raise ValueError("field must not be empty")
        self.field = name
        self._text = ""
        self._pos = 0
        self._depth = 0
        self._state = "scan"
        self._pending_key = False
        self._pending_value = False
        self._matched = False
        self._out: list[str] = []

    @property
    def matched(self) -> bool:
        """True once the target key was seen in the top-level object."""

        return self._matched

    @property
    def started(self) -> bool:
        """True once the extractor is inside the target string value."""

        return self._state == "value" or (self._state == "done" and self._matched)

    @property
    def done(self) -> bool:
        return self._state == "done"

    def feed(self, chunk: str) -> str:
        """Consume one streamed text chunk and return newly decoded text."""

        if not chunk:
            return ""
        self._text += chunk
        self._run(final=False)
        return self._flush()

    def finish(self) -> str:
        """Flush state that can only be resolved now that the stream ended."""

        self._run(final=True)
        return self._flush()

    # -- internals --------------------------------------------------------

    def _flush(self) -> str:
        if not self._out:
            return ""
        value = "".join(self._out)
        self._out.clear()
        return value

    def _skip_ws(self, index: int) -> int:
        text = self._text
        while index < len(text) and text[index].isspace():
            index += 1
        return index

    def _run(self, *, final: bool) -> None:
        text = self._text
        while self._pos < len(text) and self._state != "done":
            if self._pending_key:
                index = self._skip_ws(self._pos)
                if index >= len(text):
                    self._pos = index
                    return
                self._pending_key = False
                if text[index] == ":":
                    self._pending_value = True
                    self._pos = index + 1
                else:
                    self._pos = index
                continue

            if self._pending_value:
                index = self._skip_ws(self._pos)
                if index >= len(text):
                    self._pos = index
                    return
                self._pending_value = False
                if text[index] == '"':
                    self._state = "value"
                    self._pos = index + 1
                    continue
                # The matched field exists but is not a string; nothing to
                # stream, and the authoritative parser decides what it means.
                self._state = "done"
                self._pos = index
                continue

            if self._state == "scan":
                char = text[self._pos]
                if char.isspace() or char in ",:":
                    self._pos += 1
                    continue
                if char in "{[":
                    self._depth += 1
                    self._pos += 1
                    continue
                if char in "}]":
                    self._depth = max(0, self._depth - 1)
                    self._pos += 1
                    continue
                if char == '"':
                    token, end = self._read_string(self._pos)
                    if token is None:
                        return
                    if self._depth == 1 and token == self.field:
                        self._matched = True
                        index = self._skip_ws(end)
                        if index >= len(text):
                            self._pos = end
                            self._pending_key = True
                            return
                        if text[index] == ":":
                            self._pending_value = True
                            self._pos = index + 1
                            continue
                    self._pos = end
                    continue
                self._pos += 1
                continue

            # Inside the target string value.
            char = text[self._pos]
            if char == '"':
                self._state = "done"
                self._pos += 1
                return
            if char != "\\":
                self._out.append(char)
                self._pos += 1
                continue
            if self._pos + 1 >= len(text):
                if final:
                    self._state = "done"
                    self._pos = len(text)
                return
            escape = text[self._pos + 1]
            if escape in _SIMPLE_ESCAPES:
                self._out.append(_SIMPLE_ESCAPES[escape])
                self._pos += 2
                continue
            if escape != "u":
                self._out.append(escape)
                self._pos += 2
                continue
            if self._pos + 6 > len(text):
                if final:
                    self._state = "done"
                    self._pos = len(text)
                return
            digits = text[self._pos + 2 : self._pos + 6]
            try:
                code = int(digits, 16)
            except ValueError:
                self._out.append(text[self._pos : self._pos + 2])
                self._pos += 2
                continue
            if 0xD800 <= code <= 0xDBFF:
                # Wait for a possible low surrogate unless the stream ended.
                if self._pos + 12 > len(text):
                    if final:
                        self._out.append(chr(code))
                        self._pos = min(len(text), self._pos + 6)
                    return
                low = None
                if text[self._pos + 6 : self._pos + 8] == "\\u":
                    try:
                        low = int(text[self._pos + 8 : self._pos + 12], 16)
                    except ValueError:
                        low = None
                if low is not None and 0xDC00 <= low <= 0xDFFF:
                    codepoint = (
                        0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    )
                    self._out.append(chr(codepoint))
                    self._pos += 12
                else:
                    self._out.append(chr(code))
                    self._pos += 6
                continue
            self._out.append(chr(code))
            self._pos += 6

    def _read_string(self, start: int) -> tuple[str | None, int | None]:
        """Read a complete JSON string token, or ``(None, None)`` if partial."""

        text = self._text
        index = start + 1
        out: list[str] = []
        while index < len(text):
            char = text[index]
            if char == '"':
                return "".join(out), index + 1
            if char != "\\":
                out.append(char)
                index += 1
                continue
            if index + 1 >= len(text):
                return None, None
            escape = text[index + 1]
            if escape in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[escape])
                index += 2
                continue
            if escape == "u":
                if index + 6 > len(text):
                    return None, None
                try:
                    out.append(chr(int(text[index + 2 : index + 6], 16)))
                except ValueError:
                    out.append(text[index : index + 2])
                index += 6
                continue
            out.append(escape)
            index += 2
        return None, None


def extract_string_field(chunks: Iterable[str], field: str) -> str:
    """Convenience helper used by tests and non-streaming callers."""

    extractor = FirstStringFieldExtractor(field)
    parts = [extractor.feed(chunk) for chunk in chunks]
    parts.append(extractor.finish())
    return "".join(parts)
