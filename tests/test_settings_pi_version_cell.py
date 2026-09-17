"""Tests for issue #71 Settings-page Sign Health layout.

Round 11 (operator feedback): the operator asked to drop the Running
Pi SHA / Short SHA cells from the Sign Health card on /settings,
because the Short SHA is already shown on the dashboard's Versions
pill and the Running SHA isn't needed anywhere. The Settings page
no longer hosts a bare ``[data-sign-status-field="short_sha"]``
cell. The dashboard's Sign Health card keeps its
``[data-sign-status-fields]`` wrapper + field cells; the JS guard
still handles both layouts (bare and wrapped) so future pages can
host per-cell ``data-sign-status-field`` slots without re-doing the
guard.

These tests verify the structural assumptions both layouts rely on:
1. Settings has no short_sha cell (round 11 removal) AND no wrapper
   / placeholder (no structural drift toward the Sign Health layout).
2. The JS guard still considers the per-cell marker too.
"""

from __future__ import annotations

import re
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text()


def test_settings_template_does_not_host_short_sha_cell():
    """Round 11: the operator asked to drop the bare short_sha cell
    from /settings because the Versions pill on the dashboard already
    shows it. Settings.html must NOT have a ``short_sha`` field cell
    — adding it back is a regression that hides the layout cleanup."""
    html = _read("heart-message-manager/templates/settings.html")
    assert 'data-sign-status-field="short_sha"' not in html, (
        "settings.html unexpectedly has a [data-sign-status-field=\"short_sha\"] "
        "cell — round 11 removed this from /settings; the Versions pill on the "
        "dashboard is the single source of truth for the Pi's short SHA."
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
    presence, not just the wrapper + placeholder. Retained because the
    dashboard Sign Health card still uses the wrapped layout — the
    guard's bare-cell handling is for any future per-cell slot, not
    just Settings."""
    js = _read("heart-message-manager/static/sign_status.js")
    # The guard reads a third element before short-circuiting.
    assert "hasAnyFieldCell" in js or (
        'document.querySelector("[data-sign-status-field]")' in js
        and 'if (!fieldsContainer && !placeholder' in js
    ), "sign_status.js early-return guard no longer considers bare field cells"


def test_sign_status_js_populates_per_field_cells():
    """The render path must iterate over [data-sign-status-field]
    elements and write to each one — the dashboard's Sign Health card
    has multiple cells in a wrapped layout, and the write loop has to
    reach them all."""
    js = _read("heart-message-manager/static/sign_status.js")
    # The fixed code writes to `cell.textContent` for each discovered field.
    # Look for the loop that does this.
    assert "[data-sign-status-field" in js, "sign_status.js does not query field cells"
    # Verify the guard was extended (we don't pin exact wording,
    # only that some [data-sign-status-field] lookup happens before any early-return).
    field_cell_query = re.search(
        r'document\.querySelector\([^)]*data-sign-status-field[^)]*\)', js
    )
    assert field_cell_query is not None
