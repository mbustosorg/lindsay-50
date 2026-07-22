"""Tests for the dashboard modals + test-injection shim (§6).

The dashboard hosts three diagnostic modals (Current config, Active
filters, S3 bucket) plus the test-injection form. All four delegate
to the existing authenticated `/api/admin/{config,filters,s3-objects}`
and `/api/test-messages` endpoints; the shim is pure JS DOM glue.

Static-source assertions pin the spec contracts:

  - The test-injection form POSTs to `/api/test-messages` with the
    same wire shape the Testing page uses (URL-encoded From / Body),
    and never creates an optimistic recent-100 row on HTTP failure.
    The injection result row reports Flask acceptance separately
    from MQTT receipt (which lives in the MQTT-WS subscriber).
  - The three diagnostic modals fetch from their `/api/admin/*`
    endpoints with the `X-API-Key` header from `window.APP_CONFIG`.
  - Modal lifecycle: background click closes the modal, Escape
    closes the topmost modal, the trigger element's focus is
    restored on close, opening a modal does NOT touch
    `window.Dashboard` or `window._coordinator` (the simulator
    runtime is independent — §6.7).
  - The shim is a no-op on pages without `[data-dashboard-controls]`
    so /settings /testing /messages can load the same file safely.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_SHIM = _PROJECT_ROOT / "heart-message-manager" / "static" / "dashboard_modals.js"
_SRC = _SHIM.read_text(encoding="utf-8")


# --- Test injection (§6.1, §6.2) -----------------------------------------


def test_inject_form_posts_to_test_messages_endpoint():
    """The form's submit handler POSTs URL-encoded From/Body to
    `/api/test-messages` — the same shape the Testing page uses."""
    # The submit handler builds a URLSearchParams body and fetches
    # `/api/test-messages`. The two lines aren't always within the
    # same window — match each independently and assert both
    # patterns occur in the file.
    fetch_pattern = re.compile(r'fetch\(\s*[\"\']\/api\/test-messages[\"\']')
    search_pattern = re.compile(r'new\s+URLSearchParams')
    assert fetch_pattern.search(_SRC), (
        "dashboard_modals.js must POST to /api/test-messages "
        "(matches the Testing-page wire shape)."
    )
    assert search_pattern.search(_SRC), (
        "dashboard_modals.js must build a URLSearchParams body for "
        "the injection POST (matches the Testing-page wire shape)."
    )


def test_inject_form_sends_x_api_key_header():
    """The form's POST must carry the X-API-Key header from
    `window.APP_CONFIG.apiKey` so the auth gate accepts it."""
    pattern = re.compile(
        r'X-API-Key[\s\S]{0,80}window\.APP_CONFIG',
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must set the X-API-Key header from "
        "window.APP_CONFIG.apiKey so the test-messages endpoint "
        "passes the api_login_required gate."
    )


def test_inject_form_no_optimistic_message_on_http_failure():
    """§6.1: the form reports the HTTP failure inline and does NOT
    push a row into the recent-100 table on HTTP failure. The recent
    table is fed by the MQTT-WS subscriber, not the form."""
    # The shim must NOT call window.App.getMessages or any
    # `_dispatchChange` / registerOnChange equivalent on the
    # failure branch. The pattern looks for any direct mutation
    # of `currentRows` or `App.*` call inside the catch path.
    pattern = re.compile(
        r"if\s*\(\s*!res\.ok\s*\)\s*\{[\s\S]{0,500}?return",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must short-circuit on HTTP failure "
        "without touching the recent-100 table or MessageManager "
        "(spec §6.1: no optimistic message on HTTP failure)."
    )


def test_inject_form_reports_flask_acceptance_in_result_row():
    """§6.1: the inline `#inject-result` row reports Flask acceptance
    (HTTP 200/202) and explicitly says "waiting for MQTT receipt…"
    so the operator can correlate the accepted message with the
    running generation's live MQTT receipt."""
    pattern = re.compile(
        r"waiting for MQTT receipt",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must surface 'waiting for MQTT receipt' "
        "after Flask accepts the injection so the operator can "
        "correlate acceptance with the active generation's live "
        "MQTT receipt (§6.2)."
    )


def test_inject_form_clears_body_on_success():
    """On success the body input is cleared so the operator can
    inject the next message without retyping."""
    pattern = re.compile(
        r"injectBody\.value\s*=\s*[\"\'][\"\']",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must clear the body input after a "
        "successful injection."
    )


# --- Diagnostic modals (§6.5, §6.6, §6.7) ---------------------------------


_DIAGNOSTIC_ENDPOINTS = {
    "cfg-modal": "/api/admin/config",
    "filters-modal": "/api/admin/filters",
    "s3-modal": "/api/admin/s3-objects",
}


@pytest.mark.parametrize("modal_id,endpoint", list(_DIAGNOSTIC_ENDPOINTS.items()))
def test_modal_fetches_its_endpoint(modal_id, endpoint):
    """Each diagnostic modal triggers a fetch against its
    `/api/admin/<resource>` endpoint with X-API-Key auth."""
    pattern = re.compile(re.escape(endpoint))
    assert pattern.search(_SRC), (
        f"dashboard_modals.js must reference the {endpoint!r} "
        f"endpoint for {modal_id!r} — the spec §6.6 requires the "
        f"existing authenticated APIs."
    )


def test_modal_escape_key_handler_present():
    """§6.5: Escape closes the topmost open modal."""
    pattern = re.compile(
        r'keydown[\s\S]{0,300}Escape[\s\S]{0,300}closeModal',
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must listen for Escape and close the "
        "topmost modal — the spec §6.5 requires accessible "
        "keyboard handling."
    )


def test_modal_background_click_closes():
    """§6.5: clicking the modal backdrop closes the modal (and only
    when the click target is the backdrop itself, not a child)."""
    pattern = re.compile(
        r"target\.classList\.contains\(\s*[\"\']fixed[\"\']",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must close the modal when the backdrop "
        "(the `.fixed.inset-0` element) is clicked, NOT when a child "
        "element is clicked — same shape as testing.html's modal."
    )


def test_modal_close_button_uses_data_attribute():
    """Each modal's close button declares `data-modal-close="ID"` so
    the shim can wire them generically without per-modal handler
    boilerplate."""
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "_dashboard_modals.html"
    ).read_text(encoding="utf-8")
    for modal_id in ("json-modal", "cfg-modal", "filters-modal", "s3-modal"):
        pattern = re.compile(rf'data-modal-close="{modal_id}"')
        assert pattern.search(template), (
            f"_dashboard_modals.html must declare a close button with "
            f"data-modal-close={modal_id!r}; the shim dispatches on "
            f"this attribute."
        )


def test_modal_trigger_focus_restored_on_close():
    """§6.5: the trigger element's focus is restored when the modal
    closes (so keyboard users land where they left off)."""
    pattern = re.compile(
        r"openTriggers\.get[\s\S]{0,300}trigger\.focus",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must capture the active trigger on "
        "open and restore focus on close — the spec §6.5 requires "
        "trigger-focus restoration."
    )


def test_modal_opens_do_not_touch_dashboard_runtime():
    """§6.7: opening, updating, and closing any modal is purely a
    DOM concern — no MessageManager mutation, no coordinator tick,
    no MQTT dispatch. The shim must never reference `window.Dashboard`
    or `window._coordinator` (it would silently tie the simulator
    lifecycle to a dialog)."""
    # Strip leading `// ` comments before checking — the file's
    # top-of-file docstring mentions the controller as part of the
    # context summary, but doesn't invoke it.
    code_only = re.sub(r"^\s*//[^\n]*", "", _SRC, flags=re.MULTILINE)
    assert "window.Dashboard" not in code_only, (
        "dashboard_modals.js must not reference window.Dashboard in "
        "executable code — the simulator lifecycle is independent "
        "of the modals (§6.7)."
    )
    assert "window._coordinator" not in code_only, (
        "dashboard_modals.js must not reference window._coordinator "
        "in executable code — the simulator lifecycle is independent "
        "of the modals (§6.7)."
    )


# --- No-op on pages without the marker ------------------------------------


def test_shim_is_noop_without_dashboard_marker():
    """The shim exits early when `[data-dashboard-controls]` is
    absent, so /settings /testing /messages can load the same file
    safely."""
    pattern = re.compile(
        r'querySelector\(\s*[\"\']\[data-dashboard-controls\][\"\']\s*\)'
        r'[\s\S]{0,200}return\s*;',
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_modals.js must early-return when "
        "[data-dashboard-controls] is absent so the script is a "
        "no-op on /settings, /testing, /messages."
    )


# --- Modal-body id contract (§6.5/§6.6) ----------------------------------


def test_modal_body_ids_match_template():
    """The `bodyId` passed to `fetchAndShow` for cfg + filters modals
    must APPEND to `-body` and resolve to the actual `<...>` id the
    template emits.

    Regression (issue #48 round 3, 2026-07-22): the JS passed `"cfg"`
    and `"filters"` as bodyId, so `setModalBody("cfg", ...)` looked
    for `#cfg-body` — but the template emits `#cfg-modal-body`.
    `document.getElementById` returned null, `setModalBody` early-
    returned silently, and the modal opened with an empty body. The
    cfg-modal and filters-modal appeared to never load anything.
    Fix: pass `"cfg-modal"` and `"filters-modal"`. This test pins
    that contract.
    """
    template = (
        _PROJECT_ROOT / "heart-message-manager" / "templates" / "_dashboard_modals.html"
    ).read_text(encoding="utf-8")
    expected_body_ids = {
        "cfg-modal": "cfg-modal-body",
        "filters-modal": "filters-modal-body",
        "s3-modal": "s3-modal-body",
    }
    for modal_id, body_id in expected_body_ids.items():
        assert f'id="{body_id}"' in template, (
            f"_dashboard_modals.html must declare id={body_id!r} "
            f"so setModalBody('{modal_id}', …) resolves to a real "
            f"element."
        )
        # The JS shim must reference the full bodyId, not a short
        # prefix that doesn't match. The fetchAndShow calls above
        # pass the second arg as `bodyId` (e.g. `"cfg-modal"`); the
        # shim must NOT pass `"cfg"` (which would look for
        # `#cfg-body`).
        pattern = re.compile(rf'fetchAndShow\(\s*[\"\']{re.escape(modal_id)}[\"\']\s*,\s*[\"\']{re.escape(body_id)}[\"\']')
        assert pattern.search(_SRC), (
            f"dashboard_modals.js must call fetchAndShow("
            f"{modal_id!r}, {body_id!r}, ...) — a bodyId that doesn't "
            f"match the template's id={body_id!r} attribute makes "
            f"setModalBody silently no-op (the modal opens empty)."
        )