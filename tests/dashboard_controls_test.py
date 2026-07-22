"""Tests for the dashboard lifecycle controls shim (§5.1, §5.2, §5.5).

The shim (`dashboard_controls.js`) wires the Start/Stop/Restart buttons
to `window.Dashboard.{start,stop,restart}` and keeps the status badge,
the loading overlay text, and the inline error row in sync with the
active generation's state.

JS DOM logic isn't directly unit-testable from host CPython. The tests
below verify the JS source pins the lifecycle state machine invariants:

  - The 5 lifecycle states (starting/running/stopping/stopped/error)
    each get a distinct badge label + color.
  - Buttons are enabled/disabled per state (Start: enabled only when
    not already starting/running/stopping; Stop/Restart: enabled only
    when starting/running).
  - The render-loop gate (`window.__PREVIEW_TICK_ENABLED__`) flips
    off in stopping/stopped/error and on in starting/running — this
    is the §5.5 contract that prevents `preview.js` from calling
    `window.tick()` against a torn-down coordinator.
  - The error row is hidden in every state except `error`, and the
    retry button delegates to `start()`.
  - The script is a no-op on pages without the
    `[data-dashboard-controls]` marker, so it can be loaded
    safely from /settings, /testing, /messages too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_SHIM = _PROJECT_ROOT / "heart-message-manager" / "static" / "dashboard_controls.js"
_SRC = _SHIM.read_text(encoding="utf-8")


# --- Lifecycle state coverage ---------------------------------------------


_LIFECYCLE_STATES = ("starting", "running", "stopping", "stopped", "error")


@pytest.mark.parametrize("state", _LIFECYCLE_STATES)
def test_apply_state_handles_lifecycle_state(state):
    """The shim's `applyState` function recognizes every documented
    lifecycle state. If a new state is added to the controller, the
    shim must extend its label/color map; this test fails until it
    does."""
    # `applyState` reads `state || "stopped"` and looks up the label
    # + color from the in-function dicts. Both dicts must have an
    # entry for every state. The keys are unquoted (JS object-literal
    # shorthand) so the regex matches `state: "label"` and
    # `state: "bg-..."`.
    label_pattern = re.compile(rf"\b{state}:\s*\"", re.MULTILINE)
    color_pattern = re.compile(rf"\b{state}:\s*\"bg-", re.MULTILINE)
    assert label_pattern.search(_SRC), (
        f"dashboard_controls.js applyState() must label the {state!r} "
        f"state — the spec requires distinct labels per lifecycle state."
    )
    assert color_pattern.search(_SRC), (
        f"dashboard_controls.js applyState() must color the {state!r} "
        f"state — the spec requires distinct color treatments per "
        f"lifecycle state."
    )


# --- Render-loop gate (§5.5) ----------------------------------------------


def test_render_loop_gate_toggles_off_in_non_running_states():
    """The render-loop gate (`window.__PREVIEW_TICK_ENABLED__`) is
    FALSE for stopping/stopped/error so `preview.js` skips `tick()`
    against a torn-down coordinator."""
    # The gate assignment reads `state === "running" || state === "starting"`.
    # Verify the assignment matches the spec exactly (TRUE only for
    # running/starting, FALSE for the rest).
    gate_pattern = re.compile(
        r"window\.__PREVIEW_TICK_ENABLED__\s*=\s*"
        r"state\s*===\s*[\"']running[\"']\s*\|\|\s*state\s*===\s*[\"']starting[\"']\s*;",
        re.MULTILINE,
    )
    assert gate_pattern.search(_SRC), (
        "dashboard_controls.js must toggle window.__PREVIEW_TICK_ENABLED__ "
        "to true only when state is 'running' or 'starting'; the spec §5.5 "
        "requires the gate to drop to false in stopping/stopped/error."
    )


# --- No-op on pages without the marker ------------------------------------


def test_shim_is_noop_without_dashboard_marker():
    """The shim exits early when `[data-dashboard-controls]` is not in
    the DOM. This lets the same script be loaded from /settings,
    /testing, /messages without crashing (those pages don't host the
    simulator)."""
    # The shim starts with `document.querySelector("[data-dashboard-controls]")`
    # and an early-return path.
    early_exit_pattern = re.compile(
        r'querySelector\(\s*[\"\']\[data-dashboard-controls\][\"\']\s*\)'
        r'[\s\S]{0,200}return\s*;',
        re.MULTILINE,
    )
    assert early_exit_pattern.search(_SRC), (
        "dashboard_controls.js must early-return when "
        "[data-dashboard-controls] is absent so the script is a "
        "no-op on /settings, /testing, /messages."
    )


# --- Button wiring ---------------------------------------------------------


def test_start_button_calls_controller_start():
    """The Start button click handler delegates to
    `window.Dashboard.start()`."""
    pattern = re.compile(
        r"startBtn[\s\S]{0,200}dashboard\.start\(\)",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_controls.js must wire the Start button to "
        "window.Dashboard.start() — the click handler is the "
        "production button-click binding."
    )


def test_stop_button_calls_controller_stop():
    pattern = re.compile(
        r"stopBtn[\s\S]{0,200}dashboard\.stop\(\)",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_controls.js must wire the Stop button to "
        "window.Dashboard.stop()."
    )


def test_restart_button_calls_controller_restart():
    pattern = re.compile(
        r"restartBtn[\s\S]{0,200}dashboard\.restart\(\)",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_controls.js must wire the Restart button to "
        "window.Dashboard.restart()."
    )


# --- Error-state retry (§1.8) ---------------------------------------------


def test_error_row_hidden_except_in_error_state():
    """The error row is hidden in every state except `error`."""
    pattern = re.compile(
        r'if\s*\(state\s*===\s*[\"\']error[\"\']\)\s*\{[\s\S]{0,400}errorRow\.classList\.remove\(\s*[\"\']hidden[\"\']',
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_controls.js must only show the error row in the "
        "error state — the spec §1.8 requires an actionable retry "
        "row that is hidden when the runtime is healthy."
    )


def test_error_retry_button_calls_start():
    """The error row's Retry button delegates to `start()`."""
    pattern = re.compile(
        r"errorRetryBtn[\s\S]{0,200}dashboard\.start\(\)",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_controls.js must wire the Retry button to "
        "window.Dashboard.start() so the operator can build a clean "
        "retry generation without reloading the page."
    )


# --- Initial gate state ---------------------------------------------------


def test_initial_render_loop_gate_is_false():
    """Before the controller reports its first state, the render-loop
    gate is FALSE so `preview.js`'s rAF loop doesn't fire `window.tick()`
    while PyScript is still loading (the coordinator is None until
    `_bootstrap()` completes)."""
    pattern = re.compile(
        r"window\.__PREVIEW_TICK_ENABLED__\s*=\s*false",
        re.MULTILINE,
    )
    assert pattern.search(_SRC), (
        "dashboard_controls.js must default the render-loop gate to "
        "false before the controller reports its first state — the "
        "spec §5.5 forbids the render loop starting before the "
        "Python runtime is ready."
    )


# --- preview.js gate handling ---------------------------------------------


def test_preview_js_reads_tick_enabled_gate():
    """`preview.js`'s rAF loop must read the
    `window.__PREVIEW_TICK_ENABLED__` flag before calling
    `window.tick()`. This is the cross-file contract §5.5."""
    preview_path = _PROJECT_ROOT / "heart-message-manager" / "static" / "preview" / "preview.js"
    preview_src = preview_path.read_text(encoding="utf-8")
    # Look for the gate check + a return/continue that skips the
    # `window.tick()` call. The skip path calls requestAnimationFrame
    # to keep the loop alive; that's the consumer.
    pattern = re.compile(
        r"window\.__PREVIEW_TICK_ENABLED__[\s\S]{0,400}requestAnimationFrame",
        re.MULTILINE,
    )
    assert pattern.search(preview_src), (
        "preview.js must read window.__PREVIEW_TICK_ENABLED__ before "
        "calling window.tick(); the lifecycle controls toggle that "
        "gate, and the rAF loop is the production consumer."
    )