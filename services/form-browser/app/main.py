"""HTTP surface for the form-browser service.

The Agent drives this over the internal network; the token is configuration,
mirroring the Crawl4AI sidecar. Operations map one-to-one onto what a form
fill needs: open, read, write, verify, hand back to the owner.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from .cells import format_range, parse_range, to_ref
from .config import Settings, load_settings
from .forms import FormDriver, FormError
from .grid import GridDriver, GridError, GridUnavailable
from .models import (
    ActionAck,
    FormFillRequest,
    FormFillResult,
    FormSnapshot,
    FormSubmitRequest,
    FormSubmitResult,
    GridClearRequest,
    GridSnapshot,
    GridWriteRequest,
    GridWriteResult,
    InputRequest,
    OpenRequest,
    PageInfo,
    SessionCreated,
    TakeoverState,
)
from .sessions import SessionManager, SessionNotFound, TooManySessions

# Strings a kdocs/WPS page shows when the document is readable but not
# editable until the owner signs in. This is the takeover trigger.
LOGIN_MARKERS = (
    "立即登录加入编辑",
    "快捷登录加入编辑",
    "立即登录继续编辑",
    "登录后即可编辑",
    "登录以继续",
)

settings: Settings = load_settings()
manager = SessionManager(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="info-agent form-browser", lifespan=lifespan)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """No token configured means 'internal only', same as the renderer."""

    if not settings.api_token:
        return
    expected = f"Bearer {settings.api_token}"
    if (authorization or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid service token")


def _error(status: int, code: str, message: str, classification: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "classification": classification,
        },
    )


async def _page_info(page: Any) -> PageInfo:
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        body = ""
    title = ""
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = ""

    # Editors render their chrome long after `load`; give the grid the whole
    # settle budget, then fall back to a form check that is already rendered.
    driver = GridDriver(page, settle_ms=settings.settle_ms)
    is_grid = await driver.detect(timeout_ms=settings.settle_ms)
    login_required = any(marker in body for marker in LOGIN_MARKERS)
    kind = "spreadsheet" if is_grid else "unknown"
    editor = "kdocs-grid" if is_grid else ""
    sample = body[:1500] if body else ""

    # A form page is one that actually exposes editable fields and no grid.
    if not is_grid:
        if await FormDriver(page, settle_ms=settings.settle_ms).detect(timeout_ms=1000):
            kind = "form"
            editor = "html-form"

    return PageInfo(
        url=str(page.url),
        final_url=str(page.url),
        title=title,
        kind=kind,  # type: ignore[arg-type]
        login_required=login_required,
        logged_in=not login_required,
        can_edit=not login_required,
        editor=editor,
        body_text_sample=sample,
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "sessions": manager.count()}


@app.post("/sessions", response_model=SessionCreated, dependencies=[Depends(require_token)])
async def create_session() -> SessionCreated:
    try:
        session = await manager.create()
    except TooManySessions as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return SessionCreated(session_id=session.session_id)


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_token)])
async def close_session(session_id: str) -> ActionAck:
    await manager.close(session_id)
    return ActionAck(detail="closed")


@app.post(
    "/sessions/{session_id}/open",
    response_model=PageInfo,
    dependencies=[Depends(require_token)],
)
async def open_url(session_id: str, body: OpenRequest) -> PageInfo:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    try:
        await session.page.goto(
            body.url,
            wait_until="domcontentloaded",
            timeout=settings.nav_timeout_seconds * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"navigation failed: {exc}") from exc
    # Give the editor a chance to stop re-rendering before anything is clicked.
    try:
        await session.page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001 - a chatty page still gets a readiness check
        pass
    # Readiness is polled below, so this is only a beat for the first paint.
    await session.page.wait_for_timeout(800)
    info = await _page_info(session.page)
    session.url = info.final_url
    session.login_required = info.login_required
    if not info.login_required:
        await manager.persist_state(session)
    return info


@app.get(
    "/sessions/{session_id}/grid",
    response_model=GridSnapshot,
    dependencies=[Depends(require_token)],
)
async def read_grid(session_id: str) -> GridSnapshot:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    grid = session.grid
    if not await grid.detect():
        raise HTTPException(status_code=409, detail="the open page has no spreadsheet grid")
    try:
        headers, rows, span = await grid.header_and_rows()
    except GridError as exc:
        return _error(409 if not isinstance(exc, GridUnavailable) else 503, exc.code, str(exc), exc.classification)
    truncated = len(rows) > settings.max_grid_rows
    if truncated:
        rows = rows[: settings.max_grid_rows]
    top, left, bottom, right = parse_range(span) if span else (1, 1, 1, 1)
    if truncated:
        span = format_range(top, left, top + len(rows), right)
    return GridSnapshot(
        range=span,
        start_cell=to_ref(top, left),
        headers=headers,
        rows=rows,
        row_count=len(rows),
        column_count=max((len(row) for row in rows), default=len(headers)),
        truncated=truncated,
    )


@app.post(
    "/sessions/{session_id}/grid/write",
    response_model=GridWriteResult,
    dependencies=[Depends(require_token)],
)
async def write_grid(session_id: str, body: GridWriteRequest) -> GridWriteResult:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    if session.login_required:
        return _error(
            409,
            "login_required",
            "the document needs the owner to sign in before editing",
            "permanent_error",
        )
    grid = session.grid
    if not await grid.detect():
        raise HTTPException(status_code=409, detail="the open page has no spreadsheet grid")
    try:
        span, observed, previous = await grid.write(body.start_cell, body.values)
    except GridError as exc:
        return _error(503 if isinstance(exc, GridUnavailable) else 409, exc.code, str(exc), exc.classification)
    verified = await grid.matches(span, body.values)
    return GridWriteResult(
        written_range=span,
        previous=previous,
        requested=body.values,
        observed=observed,
        verified=verified,
    )


@app.post(
    "/sessions/{session_id}/grid/clear",
    response_model=ActionAck,
    dependencies=[Depends(require_token)],
)
async def clear_grid(session_id: str, body: GridClearRequest) -> ActionAck:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    try:
        await session.grid.clear(body.range)
    except GridError as exc:
        return _error(503 if isinstance(exc, GridUnavailable) else 409, exc.code, str(exc), exc.classification)
    return ActionAck(detail=f"cleared {body.range}")


@app.get(
    "/sessions/{session_id}/screenshot",
    dependencies=[Depends(require_token)],
)
async def screenshot(session_id: str) -> Response:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    # A takeover click can be mid-navigation; capturing immediately would send
    # the owner a blank frame and look like the browser died.
    try:
        await session.page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:  # noqa: BLE001 - a slow page still gets a frame
        pass
    await session.page.wait_for_timeout(400)
    try:
        data = await session.page.screenshot()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"screenshot failed: {exc}") from exc
    return Response(content=data, media_type="image/png")


@app.get(
    "/sessions/{session_id}/takeover",
    response_model=TakeoverState,
    dependencies=[Depends(require_token)],
)
async def takeover_state(session_id: str) -> TakeoverState:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    info = await _page_info(session.page)
    session.login_required = info.login_required
    if not info.login_required:
        await manager.persist_state(session)
    return TakeoverState(
        required=info.login_required,
        reason="登录后才能编辑该文档" if info.login_required else "",
        url=info.final_url,
    )


@app.post(
    "/sessions/{session_id}/input",
    response_model=ActionAck,
    dependencies=[Depends(require_token)],
)
async def send_input(session_id: str, body: InputRequest) -> ActionAck:
    """Forward one owner action into the live page during a takeover.

    Coordinates come from the screenshot the owner clicked on, so the page
    must be at the same viewport size the stream was captured at.
    """

    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    page = session.page
    try:
        if body.kind == "click":
            await page.mouse.click(float(body.x or 0), float(body.y or 0))
        elif body.kind == "type":
            await page.keyboard.type(body.text)
        elif body.kind == "key":
            await page.keyboard.press(body.text or "Enter")
        elif body.kind == "scroll":
            await page.mouse.wheel(body.delta_x, body.delta_y)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"input failed: {exc}") from exc
    await page.wait_for_timeout(400)
    return ActionAck(detail=body.kind)


@app.get(
    "/sessions/{session_id}/form",
    response_model=FormSnapshot,
    dependencies=[Depends(require_token)],
)
async def read_form(session_id: str) -> FormSnapshot:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    driver = FormDriver(session.page, settle_ms=settings.settle_ms)
    if not await driver.detect():
        raise HTTPException(status_code=409, detail="the open page has no HTML form")
    try:
        return await driver.snapshot()
    except FormError as exc:
        return _error(503 if exc.classification == "retryable_error" else 409, exc.code, str(exc), exc.classification)


@app.post(
    "/sessions/{session_id}/form/fill",
    response_model=FormFillResult,
    dependencies=[Depends(require_token)],
)
async def fill_form(session_id: str, body: FormFillRequest) -> FormFillResult:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    if session.login_required:
        return _error(
            409,
            "login_required",
            "the page needs the owner to sign in before editing",
            "permanent_error",
        )
    try:
        return await FormDriver(session.page, settle_ms=settings.settle_ms).fill(body.values)
    except FormError as exc:
        return _error(503 if exc.classification == "retryable_error" else 409, exc.code, str(exc), exc.classification)


@app.post(
    "/sessions/{session_id}/form/submit",
    response_model=FormSubmitResult,
    dependencies=[Depends(require_token)],
)
async def submit_form(session_id: str, body: FormSubmitRequest) -> FormSubmitResult:
    try:
        session = await manager.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown session") from exc
    if session.login_required:
        return _error(
            409,
            "login_required",
            "the page needs the owner to sign in before submitting",
            "permanent_error",
        )
    try:
        return await FormDriver(session.page, settle_ms=settings.settle_ms).submit(body.ref)
    except FormError as exc:
        return _error(503 if exc.classification == "retryable_error" else 409, exc.code, str(exc), exc.classification)
