"""Tests for the dashboard recent-100 table shim (§7).

The recent-100 table is a thin DOM renderer over the in-browser
MessageManager's view. The shim (`dashboard_recent.js`) reads up to
100 records (suppressed included), renders a 20-row client-paginated
table, and wires single-flight suppress / unsuppress actions.

Static-source assertions pin the spec contracts:

  - Reads 100 records from `window.App.getMessages(100, true)` —
    `true` is the `suppress` argument so suppressed rows are
    visible (the dashboard shows them with a `suppressed` badge).
  - Preserves `MessageView.source` (REST seed / MQTT live) for the
    source-badge rendering.
  - 20-row pagination: page count is `Math.max(1, ceil(N / 20))`,
    current page is clamped after live updates so a stale page
    pointer doesn't render an empty table.
  - Single-flight suppress / unsuppress: button is disabled while
    the POST is in flight; on success the table re-renders from
    the authoritative server view; on failure the button is
    re-enabled (non-destructive).
  - Subscribes to `window.App.registerOnChange` so live MQTT
    receipts refresh the table.
  - No-op on pages without `[data-dashboard-controls]`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_SHIM = _PROJECT_ROOT / "heart-message-manager" / "static" / "dashboard_recent.js"
_SRC = _SHIM.read_text(encoding="utf-8")


# --- Recent-100 wire shape (§7.1) -----------------------------------------


def test_recent_table_requests_100_records():
    """The shim reads 100 records from the in-browser MessageManager.
    The hard cap of 100 is the §7.1 invariant — it's the ring size
    the dashboard's spec binds to."""
    pattern = re.compile(
        r"App\.getMessages\(\s*100\s*,\s*true\s*\)",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_recent.js must request 100 records from "
        "window.App.getMessages(100, true) — the spec §7.1 binds "
        "the dashboard's ring to 100 records with suppressed rows "
        "included."
    )


def test_recent_table_preserves_messageview_source_field():
    """The source field is rendered as a badge (REST seed / MQTT
    live). The shim must read `msg.source` from each row."""
    pattern = re.compile(r"msg\.source")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must read msg.source from each row "
        "to render the source badge (REST seed / MQTT live)."
    )


def test_recent_table_renders_source_badges():
    """Both `rest` and `mqtt` source values render as distinct badges."""
    rest_pattern = re.compile(r'source\s*===\s*[\"\']rest[\"\']')
    mqtt_pattern = re.compile(r'source\s*===\s*[\"\']mqtt[\"\']')
    assert rest_pattern.search(_SRC), (
        "dashboard_recent.js must render a distinct badge when "
        "msg.source === 'rest' (REST seed)."
    )
    assert mqtt_pattern.search(_SRC), (
        "dashboard_recent.js must render a distinct badge when "
        "msg.source === 'mqtt' (MQTT live)."
    )


def test_recent_table_renders_rule_chips():
    """`msg.rules` (filter rules that touched the record) renders
    as inline chips next to the body."""
    pattern = re.compile(r"msg\.rules")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must read msg.rules from each row "
        "to render rule chips."
    )


def test_recent_table_renders_media_count():
    """`msg.media` (MMS attachments) renders as a count badge."""
    pattern = re.compile(r"msg\.media")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must read msg.media from each row "
        "to render the media count badge."
    )


def test_recent_table_sets_data_msg_id_attribute():
    """Each row carries `data-msg-id` for the existing #48 hook
    contract — operators / future tests reach the row by msg id."""
    pattern = re.compile(r'data-msg-id')
    assert pattern.search(_SRC), (
        "dashboard_recent.js must set data-msg-id on each row."
    )


def test_recent_table_sets_data_received_at_attribute():
    """Each row carries `data-received-at` so future-test code can
    correlate the rendered row with the wire shape."""
    pattern = re.compile(r'data-received-at')
    assert pattern.search(_SRC), (
        "dashboard_recent.js must set data-received-at on each row."
    )


# --- Pagination (§7.3, §7.4) ----------------------------------------------


def test_page_size_is_20():
    """Page size is 20 rows per page — the spec §7.4 contract."""
    pattern = re.compile(r"PAGE_SIZE\s*=\s*20")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must set PAGE_SIZE = 20 — the spec "
        "§7.4 binds dashboard pagination to 20 rows per page."
    )


def test_pagination_clamps_after_live_update():
    """After a live update drops the row count, the current page
    pointer is clamped to the new max — otherwise the table would
    render an empty page."""
    pattern = re.compile(r"clampPage|clamp_page")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must clamp the current page pointer "
        "after a live update — the spec §7.3 requires clamping "
        "so a stale pointer doesn't render an empty table."
    )


def test_page_count_floor_is_one():
    """An empty ring still shows page 1 (no division-by-zero /
    page-info crash)."""
    pattern = re.compile(r"Math\.max\(\s*1\s*,\s*Math\.ceil")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must floor page count at 1 so an "
        "empty ring doesn't crash the pagination controls."
    )


# --- Single-flight actions (§7.7) -----------------------------------------


def test_suppress_button_disabled_during_request():
    """The button is disabled while the POST is in flight so a
    double-click can't double-publish."""
    pattern = re.compile(
        r"btn\.disabled\s*=\s*true[\s\S]{0,200}fetch\(",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_recent.js must disable the suppress button "
        "during the in-flight POST so a double-click can't "
        "double-publish."
    )


def test_suppress_button_re_enabled_on_failure():
    """On HTTP failure, the button is re-enabled and the original
    label restored. Non-destructive: no document reload."""
    pattern = re.compile(
        r"!res\.ok[\s\S]{0,400}?btn\.disabled\s*=\s*false",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_recent.js must re-enable the suppress button on "
        "HTTP failure — the spec §7.7 requires non-destructive "
        "error handling."
    )


def test_suppress_endpoints_use_post_with_json_body():
    """Both suppress and unsuppress endpoints accept a JSON body
    with `{"id": ...}`."""
    suppress_pattern = re.compile(
        r"endpoint\s*=\s*[\s\S]{0,200}/api/admin/(?:suppress|unsuppress)-message",
        re.MULTILINE,
    )
    assert suppress_pattern.search(_SRC), (
        "dashboard_recent.js must route suppress / unsuppress "
        "actions to /api/admin/{suppress,unsuppress}-message."
    )
    # JSON body
    body_pattern = re.compile(r"JSON\.stringify")
    assert body_pattern.search(_SRC), (
        "dashboard_recent.js must JSON-encode the suppress action "
        "body."
    )


def test_suppress_uses_x_api_key_header():
    """Suppress / unsuppress POSTs include the X-API-Key header."""
    pattern = re.compile(
        r'X-API-Key[\s\S]{0,80}window\.APP_CONFIG',
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_recent.js must set X-API-Key on suppress / "
        "unsuppress POSTs from window.APP_CONFIG.apiKey."
    )


# --- Change-notification subscription (§7.2) ------------------------------


def test_recent_table_subscribes_to_registerOnChange():
    """The table subscribes to `window.App.registerOnChange` so live
    MQTT receipts refresh the view."""
    pattern = re.compile(r"App\.registerOnChange")
    assert pattern.search(_SRC), (
        "dashboard_recent.js must subscribe to "
        "window.App.registerOnChange so live MQTT receipts "
        "refresh the recent table."
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
        "dashboard_recent.js must early-return when "
        "[data-dashboard-controls] is absent so the script is a "
        "no-op on /settings, /testing, /messages."
    )