"""Wire models for the form-browser service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PageKind = Literal["spreadsheet", "form", "unknown"]


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4000)


class SessionCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str


class PageInfo(BaseModel):
    """What the service learned by opening the URL.

    ``login_required`` is the signal the Agent turns into a takeover request:
    the page is readable but refuses to be edited until the owner signs in.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str
    title: str = ""
    kind: PageKind = "unknown"
    login_required: bool = False
    logged_in: bool = False
    can_edit: bool = False
    # Identifies the surface well enough for the Agent to pick a strategy.
    editor: str = ""
    body_text_sample: str = ""


class GridCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    value: str = ""


class GridSnapshot(BaseModel):
    """The sheet as the Agent sees it: a header row plus data rows."""

    model_config = ConfigDict(extra="forbid")

    range: str
    start_cell: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    row_count: int = 0
    column_count: int = 0
    truncated: bool = False


class GridWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_cell: str = Field(min_length=1, max_length=16)
    # A row-per-entry matrix, mirroring how a spreadsheet row is filled.
    values: list[list[str]] = Field(min_length=1)


class GridWriteResult(BaseModel):
    """The write is read back before it is reported, so a silent miss cannot
    be mistaken for a successful fill."""

    model_config = ConfigDict(extra="forbid")

    written_range: str
    # What those cells held before the paste. Snapshotting here is what makes
    # an undo possible without the Agent having to keep session state around.
    previous: list[list[str]] = Field(default_factory=list)
    requested: list[list[str]]
    observed: list[list[str]]
    verified: bool


class GridClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    range: str = Field(min_length=1, max_length=64)


class ActionAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    detail: str = ""


class FormFieldInfo(BaseModel):
    """One fillable control on an ordinary HTML form.

    ``ref`` is the locator the write step uses; it is derived from the id or
    the name so it survives a re-render, rather than from a DOM path that
    breaks the moment the page reorders.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    name: str = ""
    label: str = ""
    type: str = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    value: str = ""


class FormSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = ""
    fields: list[FormFieldInfo] = Field(default_factory=list)
    submit_ref: str = ""
    submit_label: str = ""


class FormFillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``ref`` -> value, exactly as the owner confirmed on the card.
    values: dict[str, str] = Field(default_factory=dict)


class FormFillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filled: dict[str, str] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)


class FormSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = ""


class FormSubmitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submitted: bool = False
    final_url: str = ""
    title: str = ""
    body_text_sample: str = ""


class TakeoverState(BaseModel):
    """Whether the service needs the owner to finish something by hand."""

    model_config = ConfigDict(extra="forbid")

    required: bool
    reason: str = ""
    url: str = ""


class InputRequest(BaseModel):
    """One owner action forwarded into the live browser during a takeover.

    The owner sees a screenshot stream and clicks on it; this carries that
    click (or keystroke) back into the real page, so a login or captcha is
    completed by the person, not by the Agent.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["click", "type", "key", "scroll"]
    x: float | None = None
    y: float | None = None
    # Text for ``type``, a key name for ``key``.
    text: str = ""
    delta_x: float = 0
    delta_y: float = 0


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    classification: str = "permanent_error"
    detail: dict[str, Any] = Field(default_factory=dict)
