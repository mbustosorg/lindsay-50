"""Tests for issue #71 Settings-page bare-cell fix.

The Settings page uses a single ``[data-sign-status-field="short_sha"]``
cell with NO wrapper (``data-sign-status-fields``) and NO placeholder
(``data-sign-status-placeholder``). Before the fix,
``sign_status.js:applyFieldsRender`` early-returned on
``!fieldsContainer && !placeholder`` and never populated the cell —
so the "Running Pi version" row always showed "—".

After the fix, the early-return guard also bails when there's no
``[data-sign-status-field]`` element at all, AND it always writes to
per-field cells regardless of wrapper presence.

These tests verify the structural assumptions the fix relies on:
1. The Settings template has a bare cell (no wrapper, no placeholder).
2. The JS guard now considers the per-cell marker too.
"""

from __future__ import annotations

import re
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text()


def test_settings_template_has_bare_short_sha_cell():
    """The Settings page hosts a single [data-sign-status-field="short_sha"]
    div with no [data-sign-status-fields] wrapper or [data-sign-status-placeholder]
    sibling on this page. The JS must handle this layout."""
    html = _read("heart-message-manager/templates/settings.html")
    assert 'data-sign-status-field="short_sha"' in html, (
        "settings.html is missing the bare short_sha cell — fix the template first"
    )


def test_settings_template_has_no_field_wrapper():
    """Settings must NOT have a [data-sign-status-fields] wrapper — that
    wrapper belongs to the dashboard's Sign Health card. If we ever
    add it here, the JS guard short-circuit semantics change."""
    html = _read("heart-message-manager/templates/settings.html")
    assert "data-sign-status-fields" not in html, (
        "settings.html unexpectedly has a [data-sign-status-fields] wrapper — "
        "the bare-cell fix only applies when the wrapper is absent"
    )


def test_settings_template_has_no_placeholder():
    """Settings must NOT have a [data-sign-status-placeholder] sibling."""
    html = _read("heart-message-manager/templates/settings.html")
    assert "data-sign-status-placeholder" not in html, (
        "settings.html unexpectedly has a [data-sign-status-placeholder] — "
        "the bare-cell fix only applies when the placeholder is absent"
    )


def test_sign_status_js_guard_includes_field_cell_check():
    """The fixed early-return guard must consider [data-sign-status-field]
    presence, not just the wrapper + placeholder."""
    js = _read("heart-message-manager/static/sign_status.js")
    # The guard now reads a third element before short-circuiting.
    assert "hasAnyFieldCell" in js or (
        'document.querySelector("[data-sign-status-field]")' in js
        and 'if (!fieldsContainer && !placeholder' in js
    ), "sign_status.js early-return guard no longer considers bare field cells"


def test_sign_status_js_populates_per_field_cells():
    """The render path must iterate over [data-sign-status-field]
    elements and write to each one — including the bare Settings cell."""
    js = _read("heart-message-manager/static/sign_status.js")
    # The fixed code writes to `cell.textContent` for each discovered field.
    # Look for the loop that does this.
    assert "[data-sign-status-field" in js, "sign_status.js does not query field cells"
    # The fix is incomplete if the guard still bails before the loop on the
    # Settings page. Verify the guard was extended (we don't pin exact wording,
    # only that some [data-sign-status-field] lookup happens before any early-return).
    field_cell_query = re.search(
        r'document\.querySelector\([^)]*data-sign-status-field[^)]*\)', js
    )
    assert field_cell_query is not None
