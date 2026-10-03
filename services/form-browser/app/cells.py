"""A1-notation helpers.

Small and boring on purpose: the grid driver locates cells by reference, so
every conversion between a reference and an (row, column) pair lives here.
"""

from __future__ import annotations

import re

_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")


def parse_ref(ref: str) -> tuple[int, int]:
    """``"C12"`` -> ``(12, 3)``; row is 1-based, column is 1-based."""

    match = _REF.match(str(ref or "").strip())
    if not match:
        raise ValueError(f"invalid cell reference: {ref!r}")
    letters, digits = match.groups()
    column = 0
    for char in letters.upper():
        column = column * 26 + (ord(char) - ord("A") + 1)
    return int(digits), column


def to_ref(row: int, column: int) -> str:
    if row < 1 or column < 1:
        raise ValueError(f"invalid cell position: row={row} column={column}")
    letters = ""
    value = column
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return f"{letters}{row}"


def parse_range(span: str) -> tuple[int, int, int, int]:
    """``"A1:C4"`` -> ``(1, 1, 4, 3)`` as (top, left, bottom, right)."""

    text = str(span or "").strip()
    if ":" in text:
        start, end = text.split(":", 1)
    else:
        start = end = text
    top, left = parse_ref(start)
    bottom, right = parse_ref(end)
    if bottom < top:
        top, bottom = bottom, top
    if right < left:
        left, right = right, left
    return top, left, bottom, right


def format_range(top: int, left: int, bottom: int, right: int) -> str:
    return f"{to_ref(top, left)}:{to_ref(bottom, right)}"


def block_range(start_cell: str, rows: int, columns: int) -> str:
    """The range a ``rows x columns`` block covers starting at ``start_cell``."""

    if rows < 1 or columns < 1:
        raise ValueError("a block needs at least one row and one column")
    top, left = parse_ref(start_cell)
    return format_range(top, left, top + rows - 1, left + columns - 1)
