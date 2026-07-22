"""Tests for the secondary pages preserved by issue #48, §9.

The dashboard at `/` now owns the simulator + live MQTT + test
injection. The /settings, /testing, and /messages pages are
**transitionally** preserved — operators can reach them via
bookmarks, the new-tab nav links in the dashboard header, or the
sidebar-free `/messages` / `/settings` / `/testing` URL bar.

The spec §9 forbids removing them in this change. The page-level
contracts are pinned here:

  - §9.1: /settings and /testing still render their full forms
    (sign-health, effects list, sign-upgrade controls, inject
    form, info cards, messages feed) when opened directly.
  - §9.2: /settings, /testing, /messages do NOT load the
    dashboard's PyScript runtime, message-topic MQTT subscriber,
    or preview canvas.
  - §9.3: closing a /settings, /testing, or /messages tab must
    not stop the dashboard. This is a cross-tab contract; the
    test pins the absence of any `BroadcastChannel` /
    `localStorage` / `SharedWorker` coordination that would let
    one tab tear down the dashboard.
  - §9.4: /testing surfaces a "transitional" banner that links to
    the dashboard, and the page is intentionally retained (the
    template + route are NOT removed in this change).
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
_TESTING = _PROJECT_ROOT / "heart-message-manager" / "templates" / "testing.html"
_SETTINGS = _PROJECT_ROOT / "heart-message-manager" / "templates" / "settings.html"
_MESSAGES = _PROJECT_ROOT / "heart-message-manager" / "templates" / "messages.html"

_BASE_SRC = _BASE.read_text(encoding="utf-8")
_DASHBOARD_SRC = _DASHBOARD.read_text(encoding="utf-8")
_TESTING_SRC = _TESTING.read_text(encoding="utf-8")
_SETTINGS_SRC = _SETTINGS.read_text(encoding="utf-8")
_MESSAGES_SRC = _MESSAGES.read_text(encoding="utf-8")


# --- §9.1: /settings + /testing retain their full forms --------------------


def test_settings_template_renders_sign_health_section():
    """The Settings template still renders the sign-health pill /
    Settings Health section when opened directly (no dashboard
    context required)."""
    pattern = re.compile(
        r"data-sign-status-field|sign-health|Sign Health",
        re.IGNORECASE,
    )
    assert pattern.search(_SETTINGS_SRC), (
        "/settings template must retain the sign-health section / "
        "data-sign-status-field contract — §9.1 requires Settings "
        "to work standalone."
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


def test_testing_template_renders_inject_form():
    """The Testing page still renders the test-injection form."""
    assert 'id="inject-form"' in _TESTING_SRC, (
        "/testing template must retain #inject-form — §9.1 "
        "requires the injection form to work standalone (the "
        "dashboard has a copy too, but /testing remains "
        "authoritative for operators on the old nav)."
    )


def test_testing_template_renders_info_cards():
    """The Testing page still renders the Current Config / Active
    Filters / S3 Bucket diagnostic cards."""
    for marker in ("cfg-section", "filters-section", "s3-section"):
        assert marker in _TESTING_SRC, (
            f"/testing template must retain #{marker} — §9.1 " "requires the diagnostic cards to work standalone."
        )


def test_testing_template_renders_messages_feed():
    """The Testing page still renders the Messages Feed."""
    pattern = re.compile(r"msg-count|refreshFeed|messages-feed")
    assert pattern.search(_TESTING_SRC), (
        "/testing template must retain the Messages Feed — §9.1 " "requires the live feed to work standalone."
    )


# --- §9.2: secondary pages do NOT load the dashboard simulator -----------


def test_base_does_not_load_pyscript_for_secondary_pages():
    """`base.html` no longer inlines the PyScript runtime — it's
    scoped to the dashboard (issue #48 §4.10). The Settings /
    Testing / Messages pages therefore avoid the cold-PyScript-load
    cost when opened directly."""
    assert "<py-config" not in _BASE_SRC, (
        "base.html must not inline <py-config> — the simulator "
        "is dashboard-scoped (§4.10) and base.html is shared by "
        "/settings /testing /messages."
    )
    assert "pyscript.net" not in _BASE_SRC, (
        "base.html must not reference pyscript.net — the simulator " "runtime is dashboard-scoped (§4.10)."
    )


def test_testing_template_does_not_load_pyscript():
    """/testing does not load the dashboard's PyScript runtime —
    it stays an SSR page with selective JS shims."""
    assert "<py-script" not in _TESTING_SRC, (
        "testing.html must not include <py-script> — the simulator " "is dashboard-scoped (§9.2)."
    )
    assert "pyscript.net" not in _TESTING_SRC


def test_settings_template_does_not_load_pyscript():
    assert "<py-script" not in _SETTINGS_SRC, (
        "settings.html must not include <py-script> — the simulator " "is dashboard-scoped (§9.2)."
    )
    assert "pyscript.net" not in _SETTINGS_SRC


def test_testing_template_does_not_load_dashboard_controls():
    """/testing loads dashboard_modals.js / dashboard_recent.js /
    dashboard_controls.js ONLY if the [data-dashboard-controls]
    marker is present — which it is not on /testing. Each shim is
    a no-op without the marker, so loading is safe."""
    # The shims are loaded from base.html or a {% block scripts %}.
    # If they're loaded on every page (including /testing), the
    # no-op guard ensures correctness — but we don't actually need
    # to load them on /testing at all. The strict assertion is
    # that /testing does NOT carry the marker.
    assert "data-dashboard-controls" not in _TESTING_SRC, (
        "/testing must not declare [data-dashboard-controls] — " "the simulator lifecycle is dashboard-only (§9.2)."
    )


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


# --- §9.4: Testing documents itself as transitional ----------------------


def test_testing_template_documents_transitional_status():
    """/testing surfaces a "transitional" banner so operators
    arriving via bookmark see the new dashboard path."""
    pattern = re.compile(
        r"data-testing-transitional-banner|Transitional",
        re.MULTILINE,
    )
    assert pattern.search(_TESTING_SRC), (
        "/testing must surface a 'Transitional' banner linking to "
        "the dashboard — §9.4 documents Testing as transitional."
    )


def test_testing_route_is_retained():
    """The /testing route is intentionally retained in this change.
    No follow-up issue has removed it."""
    main_src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text(encoding="utf-8")
    assert '"/testing"' in main_src or "'/testing'" in main_src, (
        "/testing route must be retained — §9.4 forbids removing " "the route in this change."
    )
    assert "testing.html" in main_src, (
        "/testing template must be referenced by main.py — §9.4 " "forbids removing it in this change."
    )


def test_testing_banner_links_to_dashboard():
    """The transitional banner's anchor points at the dashboard."""
    pattern = re.compile(
        r"url_for\([\"\']dashboard[\"\']\)|/[\"\'].{0,40}dashboard",
        re.MULTILINE,
    )
    assert pattern.search(_TESTING_SRC), (
        "/testing transitional banner must link to url_for('dashboard') "
        "— §9.4 directs operators to the new dashboard."
    )


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


def test_settings_health_section_uses_sign_status_field():
    """The Settings page's health section is rendered via
    [data-sign-status-field] which the sign_status.js shim fills
    in — independent of the dashboard simulator."""
    pattern = re.compile(r"data-sign-status-field")
    assert pattern.search(_SETTINGS_SRC), (
        "/settings must expose [data-sign-status-field] so the "
        "physical-sign status subscription fills the health section "
        "even when the dashboard simulator is stopped (§9.5)."
    )


# --- Auth still required --------------------------------------------------


def test_testing_route_requires_auth():
    """/testing must remain behind the login_required gate. The
    §9 changes don't loosen auth."""
    main_src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text(encoding="utf-8")
    # Find the testing route + its decorators. The pattern is
    # `@app.route("/testing")` followed by `@login_required`.
    pattern = re.compile(
        r"@app\.route\([\"\']/testing[\"\']\)[\s\S]{0,200}@login_required",
        re.MULTILINE,
    )
    assert pattern.search(main_src), (
        "/testing route must be @login_required — §9 forbids " "removing auth from the secondary pages."
    )


def test_testing_includes_mqtt_header_partial():
    """/testing includes the `_mqtt_header.html` partial.

    Companion to `test_settings_does_not_render_mqtt_status_header`
    (settings_template_test.py): the dashboard hosts the simulator,
    the transitional Testing page still owns its own simulator, and
    both surface the MQTT status pill at the top of the page. The
    base shell went from "always render" to "never render"; pages
    that need the pill now opt in by including the partial.
    """
    assert '{% include "_mqtt_header.html" %}' in _TESTING_SRC, (
        "testing.html must include the _mqtt_header.html partial "
        "explicitly (issue #48, §4.10) — the Testing page owns the "
        "simulator runtime and surfaces the MQTT status pill."
    )
    # The partial itself must declare the elements — pin this so a
    # user who deletes the partial source drops a useful error
    # pointing at the actual contract instead of a vague "include
    # didn't expand" failure.
    partial_path = _PROJECT_ROOT / "heart-message-manager" / "templates" / "_mqtt_header.html"
    assert partial_path.exists(), (
        "_mqtt_header.html partial must exist so testing.html (and "
        "any future simulator-hosting page) can include the MQTT "
        "status pill explicitly."
    )
    partial_src = partial_path.read_text(encoding="utf-8")
    assert 'id="mqtt-status"' in partial_src
    assert 'id="mqtt-ws-target"' in partial_src
    assert 'id="mqtt-sub-topic"' in partial_src
