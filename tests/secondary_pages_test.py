"""Tests for the secondary pages preserved by issue #48, §9.

The dashboard at `/` now owns the simulator + live MQTT + test
injection. The /settings and /messages pages are **transitionally**
preserved — operators can reach them via bookmarks, the new-tab
nav links in the dashboard header, or the sidebar-free
`/messages` / `/settings` URL bar.

The /testing page was removed on 2026-07-23 — its diagnostic
surface (test injection, filters, config inspector, messages
feed) was folded into the dashboard per the issue #48 design.
The /testing route is RETAINED as a redirect to `/` so that any
bookmarks, inbound links, or curl scripts that point at it
don't 404 — they land on the dashboard instead. The redirect
preserves the URL contract while the rest of the surface lives
on `/`.

The page-level contracts are pinned here:

  - §9.1: /settings still renders its full forms (effects list,
    sign-upgrade controls) when opened directly. The messages
    page still renders its archive.
  - §9.2: /settings and /messages do NOT load the dashboard's
    PyScript runtime, message-topic MQTT subscriber, or preview
    canvas.
  - §9.3: closing a /settings or /messages tab must not stop the
    dashboard. This is a cross-tab contract; the test pins the
    absence of any `BroadcastChannel` / `localStorage` /
    `SharedWorker` coordination that would let one tab tear down
    the dashboard.
  - §9.4 (legacy): /testing surfaced a "transitional" banner —
    that page is now removed. The redirect-to-dashboard covers
    the URL contract for any pre-removal bookmark.
  - §9.5: the physical-sign status pill / Settings health section
    still receive the physical MQTT_STATUS_TOPIC independently of
    simulator lifecycle. The base shell still loads
    sign_status.js / mqtt_ws_client.js; the dashboard simulator
    loads `app_main.py` but does NOT clobber the status
    subscriber.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_BASE = _PROJECT_ROOT / "heart-message-manager" / "templates" / "base.html"
_DASHBOARD = _PROJECT_ROOT / "heart-message-manager" / "templates" / "dashboard.html"
_SETTINGS = _PROJECT_ROOT / "heart-message-manager" / "templates" / "settings.html"
_MESSAGES = _PROJECT_ROOT / "heart-message-manager" / "templates" / "messages.html"

_BASE_SRC = _BASE.read_text(encoding="utf-8")
_DASHBOARD_SRC = _DASHBOARD.read_text(encoding="utf-8")
_SETTINGS_SRC = _SETTINGS.read_text(encoding="utf-8")
_MESSAGES_SRC = _MESSAGES.read_text(encoding="utf-8")


# --- §9.1: /settings retains its full forms ------------------------------


def test_dashboard_template_renders_sign_health_card():
    """The Dashboard template renders the Sign Health card
    (post-2026-07-22 follow-up — Sign Health moved off Settings).
    The card exposes `data-sign-status-field` cells so the
    sign_status.js shim fills them with the physical-sign status
    snapshot whenever one arrives. Operators no longer need a
    Settings tab to read Pi health."""
    pattern = re.compile(
        r"data-sign-status-field|Sign Health",
        re.IGNORECASE,
    )
    assert pattern.search(_DASHBOARD_SRC), (
        "/ (Dashboard) template must expose the Sign Health card "
        "with [data-sign-status-field] cells — Sign Health moved "
        "here from Settings in the post-2026-07-22 follow-up so "
        "operators don't have to keep a Settings tab open."
    )


def test_settings_template_no_longer_renders_sign_health_section():
    """Companion to the above: the Sign Health CARD moved off
    `/settings` to the Dashboard (post-2026-07-22 follow-up).

    Note: the Pi Upgrade section on /settings legitimately keeps a
    `data-sign-status-field="short_sha"` cell so operators can see
    "is the Pi running what I targeted?" next to the apply button.
    That cell is a separate concern from the moved Sign Health
    card and is NOT covered by this assertion.

    The moved card is uniquely identifiable by the
    `active_sha` / `uptime_seconds` / `mqtt_connected` /
    `last_error` / `received_at_browser` fields — none of those
    appear on the Pi Upgrade section, so their absence on
    `/settings` proves the card moved."""
    unique_field_pattern = re.compile(
        r"data-sign-status-field=\"(?:active_sha|uptime_seconds|"
        r"mqtt_connected|last_error|received_at_browser|started_at)\"",
    )
    assert not unique_field_pattern.search(_SETTINGS_SRC), (
        "/settings template must not still carry the moved Sign "
        "Health card fields (active_sha / uptime_seconds / "
        "mqtt_connected / last_error / received_at_browser / "
        "started_at) — the card moved to the Dashboard in the "
        "post-2026-07-22 follow-up. The Pi Upgrade section's "
        "`short_sha` cell is a separate, retained cell."
    )


def test_settings_template_renders_effects_list_form():
    """The Settings page still renders the effects-list form."""
    assert "effects-list" in _SETTINGS_SRC or "effects_list" in _SETTINGS_SRC, (
        "/settings template must retain the effects-list form — " "§9.1 requires Settings to work standalone."
    )


def test_settings_template_renders_sign_upgrade_controls():
    """The Settings page still renders the sign-upgrade controls."""
    pattern = re.compile(r"data-upgrade-settings-field")
    assert pattern.search(_SETTINGS_SRC), (
        "/settings template must retain data-upgrade-settings-field " "— §9.1 requires Settings to work standalone."
    )


def test_testing_route_redirects_to_dashboard():
    """The /testing route is RETAINED as a redirect to / so that
    any pre-removal bookmark, inbound link, or curl script lands
    on the dashboard instead of 404'ing. The page itself was
    removed on 2026-07-23 — the diagnostic surface lives on /
    now."""
    main_src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text(encoding="utf-8")
    assert '"/testing"' in main_src or "'/testing'" in main_src, (
        "/testing route must be retained as a redirect (post-removal "
        "follow-up): the URL contract outlives the page."
    )
    # The retained route must NOT render testing.html — the
    # template is gone. It must redirect to dashboard instead.
    assert "testing.html" not in main_src, (
        "/testing template was removed (2026-07-23) — main.py must "
        "not still reference testing.html. The retained route is a "
        "redirect."
    )
    assert "redirect(url_for(\"dashboard\"))" in main_src or "redirect(url_for('dashboard'))" in main_src, (
        "/testing route must redirect to the dashboard — the page "
        "is gone, the redirect preserves the URL contract."
    )


# --- §9.2: secondary pages do NOT load the dashboard simulator -----------


def test_base_does_not_load_pyscript_for_secondary_pages():
    """`base.html` no longer inlines the PyScript runtime — it's
    scoped to the dashboard (issue #48 §4.10). The Settings /
    Messages pages therefore avoid the cold-PyScript-load cost
    when opened directly. /testing is a redirect so it's
    automatically exempt."""
    assert "<py-config" not in _BASE_SRC, (
        "base.html must not inline <py-config> — the simulator "
        "is dashboard-scoped (§4.10) and base.html is shared by "
        "/settings /messages (and the /testing redirect)."
    )
    assert "pyscript.net" not in _BASE_SRC, (
        "base.html must not reference pyscript.net — the simulator " "runtime is dashboard-scoped (§4.10)."
    )


def test_settings_template_does_not_load_pyscript():
    assert "<py-script" not in _SETTINGS_SRC, (
        "settings.html must not include <py-script> — the simulator " "is dashboard-scoped (§9.2)."
    )
    assert "pyscript.net" not in _SETTINGS_SRC


def test_messages_template_does_not_load_pyscript():
    assert "<py-script" not in _MESSAGES_SRC, (
        "messages.html must not include <py-script> — the simulator " "is dashboard-scoped (§9.2)."
    )


# --- §9.3: closing a secondary tab does not affect the dashboard ---------


def test_dashboard_does_not_use_shared_worker_for_state():
    """The dashboard's runtime is per-tab (per-generation). No
    SharedWorker or cross-tab BroadcastChannel is used to
    coordinate teardown — closing a secondary tab cannot reach
    into the dashboard's generation."""
    # Pull the dashboard's script list (py-config + .py files +
    # dashboard_*.js) — none should reference SharedWorker or
    # BroadcastChannel.
    for name in (
        "app_main.py",
        "preview_main.py",
        "preview.js",
        "dashboard_controls.js",
        "dashboard_modals.js",
        "dashboard_recent.js",
    ):
        path = _PROJECT_ROOT / "heart-message-manager" / "static"
        candidate_paths = [
            path / name,
            path / "preview" / name,
        ]
        for candidate in candidate_paths:
            if candidate.exists():
                src = candidate.read_text(encoding="utf-8")
                assert "SharedWorker" not in src, (
                    f"{candidate.name} must not use SharedWorker — " f"cross-tab coordination is forbidden (§9.3)."
                )
                assert "BroadcastChannel" not in src, (
                    f"{candidate.name} must not use BroadcastChannel "
                    f"— cross-tab teardown would let /settings "
                    f"stop the dashboard (§9.3)."
                )


def test_dashboard_does_not_use_localstorage_for_runtime_state():
    """Per §2.4 the dashboard does not hydrate state from
    localStorage / sessionStorage / IndexedDB. This is the §9.3
    cross-tab guarantee: each tab gets its own fresh generation
    with no shared state to mutate."""
    # Any usage of localStorage / sessionStorage in dashboard JS
    # would surface as cross-tab persistence — the dashboard
    # therefore has zero references.
    for name in (
        "app_main.py",
        "preview_main.py",
        "preview.js",
        "dashboard_controls.js",
        "dashboard_modals.js",
        "dashboard_recent.js",
    ):
        path = _PROJECT_ROOT / "heart-message-manager" / "static"
        candidate_paths = [
            path / name,
            path / "preview" / name,
        ]
        for candidate in candidate_paths:
            if candidate.exists():
                src = candidate.read_text(encoding="utf-8")
                assert "localStorage" not in src, (
                    f"{candidate.name} must not use localStorage — " f"the dashboard runtime is per-tab (§9.3)."
                )
                assert "sessionStorage" not in src, (
                    f"{candidate.name} must not use sessionStorage — " f"the dashboard runtime is per-tab (§9.3)."
                )


# --- §9.4 (legacy): /testing page is gone — see test_testing_route_redirects_to_dashboard above ---


# --- §9.5: physical-sign status independent of simulator lifecycle --------


def test_base_loads_sign_status_js_on_every_page():
    """The physical-sign status client (sign_status.js) is loaded
    on every authenticated page so the Settings health section /
    dashboard pill can render even when the simulator is stopped."""
    assert "sign_status.js" in _BASE_SRC, (
        "base.html must load sign_status.js — the physical-sign "
        "status pill is independent of the simulator lifecycle (§9.5)."
    )


def test_base_loads_mqtt_ws_client_on_every_page():
    """The MQTT-WS client shim is loaded on every authenticated page
    so the status topic subscription survives Stop/Start."""
    assert "mqtt_ws_client.js" in _BASE_SRC, (
        "base.html must load mqtt_ws_client.js — the physical-sign "
        "status topic subscription is independent of simulator "
        "lifecycle (§9.5)."
    )


def test_status_topic_is_separate_from_message_topic():
    """The paho client takes an optional `status_topic` parameter
    — distinct from `topic` (the message envelope flow). The
    Flask routes wire both subscribe paths so a simulator Stop
    does NOT cut the status subscription."""
    paho_src = (_PROJECT_ROOT / "lib_shared" / "paho_mqtt_client.py").read_text(encoding="utf-8")
    assert "status_topic" in paho_src, (
        "paho_mqtt_client.py must accept a separate status_topic "
        "subscribe path — the status flow is independent of the "
        "MQTT_TOPIC envelope flow (§9.5)."
    )
    assert "status_dispatch_callback" in paho_src, (
        "paho_mqtt_client.py must accept a separate "
        "status_dispatch_callback — the status flow has its own "
        "callback path so a raise in the envelope callback does "
        "not affect status delivery (§9.5)."
    )


def test_dashboard_health_card_uses_sign_status_field():
    """The Dashboard's Sign Health card (post-2026-07-22 follow-up —
    moved here from /settings) is rendered via
    [data-sign-status-field] which the sign_status.js shim fills
    in — independent of the dashboard simulator. This is the §9.5
    contract: status pills must render even when the simulator is
    stopped, and the same shim must drive the cell population."""
    pattern = re.compile(r"data-sign-status-field")
    assert pattern.search(_DASHBOARD_SRC), (
        "/ (Dashboard) must expose [data-sign-status-field] so the "
        "physical-sign status subscription fills the Sign Health "
        "card even when the simulator is stopped (§9.5). Sign "
        "Health moved to / from /settings in the post-2026-07-22 "
        "follow-up; the field contract is unchanged."
    )


# --- /testing page removal (post-2026-07-23 follow-up) ------------------

# (The single test_testing_route_redirects_to_dashboard declaration
# lives in the §9.1 section above.)
