"""Drive an ordinary HTML form.

Unlike the spreadsheet path this one *can* read the DOM, so fields are located
by their own id or name. ``ref`` is derived from those rather than from a DOM
path, which is what lets the write step reopen the page in a fresh session and
still find the same control.
"""

from __future__ import annotations

from typing import Any

from .models import FormFieldInfo, FormFillResult, FormSnapshot, FormSubmitResult

CONTROL_SELECTOR = (
    "input:not([type=hidden]):not([type=submit]):not([type=button])"
    ":not([type=reset]):not([type=image]), textarea, select"
)
SUBMIT_SELECTOR = (
    "button[type=submit], input[type=submit], button:not([type]), "
    "input[type=button][value*='提交'], button:has-text('提交'), button:has-text('Submit')"
)

# Runs in the page: one pass over the controls, no per-field round trips.
COLLECT_JS = """
() => {
  const sel = %s;
  const nodes = Array.from(document.querySelectorAll(sel));
  const seen = new Map();
  return nodes.map((el, index) => {
    const name = el.getAttribute('name') || '';
    let ref;
    if (el.id) {
      ref = '#' + el.id;
    } else if (name) {
      const n = seen.get(name) || 0;
      seen.set(name, n + 1);
      ref = 'name:' + name + '#' + n;
    } else {
      ref = 'index:' + index;
    }
    const labelNode =
      (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) ||
      el.closest('label');
    const label = (labelNode ? labelNode.innerText : '') ||
      el.getAttribute('aria-label') || el.getAttribute('placeholder') || name || el.id || '';
    const type = el.tagName === 'SELECT' ? 'select' : (el.getAttribute('type') || 'text');
    const options = el.tagName === 'SELECT'
      ? Array.from(el.options).map((o) => o.value)
      : Array.from(el.querySelectorAll ? el.querySelectorAll('option') : []).map((o) => o.value);
    return {
      ref,
      name,
      label: String(label).trim().slice(0, 200),
      type,
      required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
      options,
      value: el.type === 'checkbox' || el.type === 'radio' ? (el.checked ? 'true' : '') : (el.value || ''),
    };
  });
}
""" % repr(CONTROL_SELECTOR)


class FormError(RuntimeError):
    classification = "permanent_error"
    code = "form_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class FormUnavailable(FormError):
    classification = "retryable_error"
    code = "form_unavailable"


def _locator(page: Any, ref: str) -> Any:
    """Resolve a derived ref back to a locator."""

    text = str(ref or "").strip()
    if text.startswith("#"):
        return page.locator(text).first
    if text.startswith("name:"):
        body = text[5:]
        name, _, nth = body.rpartition("#")
        target = f'[name="{name}"]'
        return page.locator(target).nth(int(nth) if nth.isdigit() else 0)
    if text.startswith("index:"):
        return page.locator(CONTROL_SELECTOR).nth(int(text[6:] or 0))
    raise FormError(f"unknown field reference: {ref!r}")


class FormDriver:
    def __init__(self, page: Any, *, settle_ms: int = 8000) -> None:
        self.page = page
        self.settle_ms = max(int(settle_ms), 0)

    async def detect(self) -> bool:
        try:
            return await self.page.locator(CONTROL_SELECTOR).count() > 0
        except Exception:  # noqa: BLE001
            return False

    async def snapshot(self) -> FormSnapshot:
        try:
            raw = await self.page.evaluate(COLLECT_JS)
        except Exception as exc:  # noqa: BLE001
            raise FormUnavailable(f"could not read the form: {exc}") from exc
        fields = [FormFieldInfo(**item) for item in (raw or [])]

        submit_ref = ""
        submit_label = ""
        try:
            button = self.page.locator(SUBMIT_SELECTOR)
            if await button.count():
                first = button.first
                submit_label = (await first.inner_text()).strip()[:60]
                submit_ref = "submit:0"
        except Exception:  # noqa: BLE001
            submit_ref = ""
        return FormSnapshot(
            url=str(self.page.url),
            title=await self.page.title(),
            fields=fields,
            submit_ref=submit_ref,
            submit_label=submit_label,
        )

    async def fill(self, values: dict[str, str]) -> FormFillResult:
        filled: dict[str, str] = {}
        failed: dict[str, str] = {}
        for ref, value in (values or {}).items():
            try:
                locator = _locator(self.page, ref)
                tag = await locator.evaluate("e => e.tagName")
                if tag == "SELECT":
                    await locator.select_option(value)
                else:
                    kind = (await locator.get_attribute("type") or "text").lower()
                    if kind in {"checkbox", "radio"}:
                        if str(value).strip().lower() in {"true", "1", "on", "yes"}:
                            await locator.check()
                        else:
                            await locator.uncheck()
                    else:
                        await locator.fill(str(value))
                filled[ref] = str(value)
            except Exception as exc:  # noqa: BLE001
                failed[ref] = f"{type(exc).__name__}: {exc}"
        await self.page.wait_for_timeout(min(self.settle_ms, 2000))
        return FormFillResult(filled=filled, failed=failed)

    async def submit(self, ref: str = "") -> FormSubmitResult:
        locator = (
            _locator(self.page, ref)
            if ref and not ref.startswith("submit:")
            else self.page.locator(SUBMIT_SELECTOR).first
        )
        try:
            await locator.click(timeout=10000)
        except Exception as exc:  # noqa: BLE001
            raise FormError(f"submit control could not be clicked: {exc}") from exc
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001 - a client-side form may not navigate
            pass
        await self.page.wait_for_timeout(min(self.settle_ms, 4000))
        try:
            body = await self.page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""
        return FormSubmitResult(
            submitted=True,
            final_url=str(self.page.url),
            title=await self.page.title(),
            body_text_sample=body[:1500],
        )
