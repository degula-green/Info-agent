"""Drive a canvas-rendered spreadsheet (kdocs/WPS, Tencent Docs, ...).

These editors paint the grid to ``<canvas>``, so there are no cell elements to
query. Two stable handles exist instead, and both were verified against a live
kdocs document:

* the **name box** (``input.edit-box``) accepts a reference such as ``C12`` or
  ``A1:J20`` and moves the selection there;
* the **clipboard** carries the selected block as TSV in both directions.

So reads are "select, copy, parse" and writes are "select, paste, read back".
Every write is read back before it is reported, which is what keeps a silent
miss from being mistaken for a successful fill.
"""

from __future__ import annotations

import time
from typing import Any

from .cells import block_range, format_range, parse_range

NAME_BOX = "input.edit-box"


class GridError(RuntimeError):
    """Base error; ``classification`` is what the Agent kernel reads."""

    classification = "permanent_error"
    code = "grid_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class GridUnavailable(GridError):
    classification = "retryable_error"
    code = "grid_unavailable"


def parse_tsv(text: str) -> list[list[str]]:
    """Clipboard TSV -> a matrix, without a trailing blank row."""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    if not normalized:
        return []
    return [line.split("\t") for line in normalized.split("\n")]


def to_tsv(values: list[list[str]]) -> str:
    return "\n".join("\t".join(str(cell) for cell in row) for row in values)


def trim_matrix(matrix: list[list[str]]) -> list[list[str]]:
    """Drop trailing blank rows and columns from a scanned block.

    A whole-sheet read has to name a generous range, so the result always
    carries padding. Leaving it in would make the header look like it extends
    to column Z.
    """

    rows = [list(row) for row in matrix]
    while rows and not any(str(cell).strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        return []
    width = max(len(row) for row in rows)
    while width > 0:
        if any(
            len(row) >= width and str(row[width - 1]).strip() for row in rows
        ):
            break
        width -= 1
    return [row[:width] for row in rows]


class GridDriver:
    """Bound to one page; the session owns the lifetime."""

    def __init__(
        self,
        page: Any,
        *,
        settle_ms: int = 8000,
        max_rows: int = 200,
        scan_columns: str = "Z",
    ) -> None:
        self.page = page
        self.settle_ms = max(int(settle_ms), 0)
        self.max_rows = max(int(max_rows), 1)
        self.scan_columns = str(scan_columns or "Z").strip().upper() or "Z"

    async def _settle(self, factor: float = 1.0) -> None:
        await self.page.wait_for_timeout(int(self.settle_ms * factor))

    async def detect(self, *, timeout_ms: int = 0) -> bool:
        """True when the page exposes the name box / canvas pair.

        A spreadsheet editor renders its chrome well after ``load``, so with a
        budget the check polls instead of sampling once: a fixed sleep is a
        race that fails exactly when the page is slow.
        """

        deadline = time.monotonic() + max(int(timeout_ms), 0) / 1000
        while True:
            try:
                if (
                    await self.page.locator(NAME_BOX).count() > 0
                    and await self.page.locator("canvas").count() > 0
                ):
                    return True
            except Exception:  # noqa: BLE001 - detection never raises
                pass
            if time.monotonic() >= deadline:
                return False
            await self.page.wait_for_timeout(250)

    async def select(self, ref: str) -> str:
        """Move the selection to ``ref`` and return what the name box shows."""

        box = self.page.locator(NAME_BOX).first
        last: Exception | None = None
        # A freshly painted editor keeps re-rendering, and Playwright refuses
        # to click an element that is not stable. Retrying rides that out.
        for _ in range(3):
            try:
                await box.click(timeout=5000)
                await box.press("Control+a")
                await box.fill(ref)
                await box.press("Enter")
                await self._settle(0.15)
                return (await box.input_value()).strip()
            except Exception as exc:  # noqa: BLE001
                last = exc
                await self.page.wait_for_timeout(1200)
        raise GridUnavailable(f"could not select {ref}: {last}")

    async def _set_clipboard(self, text: str) -> None:
        await self.page.evaluate(
            "async (value) => { await navigator.clipboard.writeText(value); }",
            text,
        )

    async def _read_clipboard(self) -> str:
        for _ in range(3):
            try:
                text = await self.page.evaluate("() => navigator.clipboard.readText()")
            except Exception as exc:  # noqa: BLE001
                raise GridUnavailable(f"clipboard read failed: {exc}") from exc
            if text:
                return str(text)
            await self._settle(0.2)
        return ""

    async def read(self, span: str | None = None, *, start_cell: str = "A1") -> list[list[str]]:
        """Read a block; without ``span`` the whole used sheet is returned."""

        # Clicking the name box and pressing Ctrl+A selects the *text* inside
        # it, not the sheet, so a whole-sheet read names a generous range and
        # trims the padding afterwards.
        ref = span or f"{start_cell}:{self.scan_columns}{self.max_rows}"
        # The name box can exist before the sheet paints, and copying a grid
        # that has not painted yields an empty clipboard. Retry rather than
        # report "the sheet is empty" for a page that is merely slow.
        matrix: list[list[str]] = []
        for attempt in range(3):
            await self.select(ref)
            await self.page.keyboard.press("Control+c")
            await self._settle(0.2)
            matrix = parse_tsv(await self._read_clipboard())
            if matrix:
                return matrix
            if attempt < 2:
                await self.page.wait_for_timeout(1000)
        return matrix

    async def header_and_rows(self, *, header_span: str = "A1:Z1") -> tuple[list[str], list[list[str]], str]:
        """Split the sheet into a header row, data rows, and the used range."""

        matrix = trim_matrix(await self.read(None))
        if not matrix:
            return [], [], ""
        width = max(len(row) for row in matrix)
        padded = [row + [""] * (width - len(row)) for row in matrix]
        headers = [str(cell).strip() for cell in padded[0]]
        rows = padded[1:]
        # The used range starts at A1 because that is where the read began.
        span = format_range(1, 1, len(padded), width)
        return headers, rows, span

    async def write(
        self, start_cell: str, values: list[list[str]]
    ) -> tuple[str, list[list[str]], list[list[str]]]:
        """Paste a block, then return (span, observed, previous).

        ``previous`` is read before the paste so the caller can undo the write
        by restoring it — the only safety net a live document has.
        """

        if not values or not values[0]:
            raise GridError("nothing to write")
        rows = len(values)
        columns = max(len(row) for row in values)
        padded = [list(row) + [""] * (columns - len(row)) for row in values]
        span = block_range(start_cell, rows, columns)

        previous = await self.read(span)
        await self.select(start_cell)
        await self._set_clipboard(to_tsv(padded))
        await self.page.keyboard.press("Control+v")
        await self._settle(0.5)
        observed = await self.read(span)
        return span, observed, previous

    async def clear(self, span: str) -> None:
        top, left, bottom, right = parse_range(span)
        await self.read(format_range(top, left, bottom, right))  # ensure selection
        await self.page.keyboard.press("Delete")
        await self._settle(0.3)

    async def matches(self, span: str, expected: list[list[str]]) -> bool:
        observed = await self.read(span)
        rows = len(expected)
        columns = max((len(row) for row in expected), default=0)
        if len(observed) < rows or any(len(row) < columns for row in observed[:rows]):
            return False
        for index, row in enumerate(expected):
            for col, cell in enumerate(row):
                if str(observed[index][col]).strip() != str(cell).strip():
                    return False
        return True
