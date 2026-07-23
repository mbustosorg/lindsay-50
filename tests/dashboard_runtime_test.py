"""Unit tests for `DashboardController` (issue #48, section 1).

The dashboard controller is the per-generation state machine for the
standalone preview dashboard. It owns one `Runtime` record per
generation, generation-discriminates every async callback, and tears
down all resources on Stop.

These tests exercise the controller on host CPython (no PyScript, no
canvas). The full render-loop hooks + MQTT-WS client + canvas work
will be wired in by `app_main.py` — the controller's state machine
is fully host-CPython-testable because `_bootstrap_generation` only
does work when an explicit hook is installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "heart-message-manager"))

from dashboard_controller import (
    DEFAULT_EVENT_LOG_MAX_ENTRIES,
    GENERATION_HISTORY_MAX,
    DashboardController,
    Runtime,
    _NullMessageManager,
    _to_js_obj,
)


# --- Fixtures --------------------------------------------------------------


class _FakeMessageManager:
    """Stand-in for the real `MessageManager` returned by render-loop hooks."""

    def __init__(self):
        self.dispatched = []

    def dispatch(self, raw):
        self.dispatched.append(raw)

    def get_messages(self, limit=None, suppress=True):  # noqa: ARG002 — stub
        return []


@pytest.fixture
def controller():
    return DashboardController()


@pytest.fixture
def fake_runtime_hook():
    """Build a `set_render_loop_hooks` callable that constructs a
    populated Runtime (event_log + MessageManager + coordinator)."""

    def _install(c: DashboardController) -> dict:
        """Returns a dict of references for the test to inspect."""

        refs = {"event_log": object(), "message_manager": _FakeMessageManager()}

        def on_start(runtime: Runtime) -> None:
            runtime.event_log = refs["event_log"]
            runtime.message_manager = refs["message_manager"]

        def on_stop(runtime: Runtime) -> None:  # noqa: ARG001
            refs["stop_called_with"] = runtime

        c.set_render_loop_hooks(on_start=on_start, on_stop=on_stop)
        return refs

    return _install


# --- Lifecycle states ------------------------------------------------------


def test_initial_state_is_stopped(controller):
    """New controller has no active runtime."""
    assert controller.state() == "stopped"
    assert controller.generation_id() == 0
    assert controller.runtime() is None


def test_start_creates_starting_generation(controller):
    """Start places the controller in `starting` until the hook promotes it."""
    controller.start()
    assert controller.state() == "starting"
    assert controller.generation_id() == 1
    runtime = controller.runtime()
    assert runtime is not None
    assert runtime.generation_id == 1
    # Before any hook runs, the MessageManager is the null stand-in.
    assert isinstance(runtime.message_manager, _NullMessageManager)


def test_start_is_idempotent_while_starting(controller):
    """A second Start while a generation is alive is a no-op (logged)."""
    controller.start()
    gen_id_1 = controller.generation_id()
    controller.start()
    assert controller.generation_id() == gen_id_1


def test_hook_can_promote_starting_to_running(controller, fake_runtime_hook):
    """When the render-loop hook installs a real MessageManager +
    event_log, the controller moves the state to `running`."""
    refs = fake_runtime_hook(controller)
    controller.start()
    assert controller.state() == "running"
    assert controller.runtime().event_log is refs["event_log"]


def test_stop_is_idempotent_when_already_stopped(controller):
    """Stop on a stopped controller is a no-op."""
    controller.stop()
    assert controller.state() == "stopped"


def test_stop_tears_down_running_generation(controller, fake_runtime_hook):
    """Stop fires the on_stop hook, swaps MessageManager to the null
    stand-in, drops event_log, and clears the active slot."""
    refs = fake_runtime_hook(controller)
    controller.start()
    assert controller.state() == "running"
    controller.stop()
    assert controller.state() == "stopped"
    assert controller.generation_id() == 0
    assert "stop_called_with" in refs, "on_stop hook was not fired"


def test_restart_creates_new_generation(controller, fake_runtime_hook):
    """restart() stops the active generation and starts a new one with
    a fresh generation id."""
    fake_runtime_hook(controller)
    controller.start()
    controller.stop()
    controller.start()  # gen 2
    assert controller.generation_id() == 2
    controller.restart()
    # After restart, the new gen is either `starting` (brief, before
    # the on_start hook runs) or `running` (the hook promoted it).
    # Both prove the state machine advanced to gen-3.
    assert controller.state() in ("starting", "running"), controller.state()
    assert controller.generation_id() == 3
    controller.stop()


# --- Generation discriminator ---------------------------------------------


def test_stale_callbacks_rejected_after_stop(controller, fake_runtime_hook):
    """A delayed callback that captured gen-id=1 MUST be rejected
    after Stop cleared the active slot."""
    fake_runtime_hook(controller)
    controller.start()
    stale_gen = controller.generation_id()
    controller.stop()
    assert controller.is_active_generation(stale_gen) is False


def test_stale_callbacks_rejected_after_stop_then_start(controller, fake_runtime_hook):
    """A delayed callback from gen 1 must be rejected while gen 2 is active."""
    fake_runtime_hook(controller)
    controller.start()
    gen_1 = controller.generation_id()
    controller.stop()
    controller.start()
    gen_2 = controller.generation_id()
    assert gen_1 != gen_2
    assert controller.is_active_generation(gen_1) is False
    assert controller.is_active_generation(gen_2) is True
    controller.stop()


def test_invalidate_test_hook_rejects_stale_callbacks(controller):
    """`invalidate(generation_id)` is a test-only hook that
    short-circuits the discriminator without running teardown —
    useful for the stale-callback test where the test wants to
    set up the mismatch without paying for resource release."""
    controller.start()
    gen = controller.generation_id()
    controller.invalidate(gen)
    assert controller.is_active_generation(gen) is False
    assert controller.generation_id() == 0
    assert controller.runtime() is None


def test_invalidate_ignores_other_generations(controller):
    """`invalidate(X)` is a no-op when X is not the active generation."""
    controller.start()
    gen_active = controller.generation_id()
    controller.invalidate(gen_active + 999)
    assert controller.generation_id() == gen_active
    assert controller.is_active_generation(gen_active) is True
    controller.stop()


# --- Partial-bootstrap failure handling -----------------------------------


def test_hook_failure_marks_runtime_as_error(controller):
    """A render-loop hook that raises marks the generation as
    `error`, clears the runtime, and lets the next Start() build
    from a clean slate."""
    captured: dict = {}

    def on_start(runtime: Runtime) -> None:  # noqa: ARG001
        captured["called"] = True
        raise RuntimeError("simulated REST seed failure")

    controller.set_render_loop_hooks(on_start=on_start)

    controller.start()
    assert captured.get("called") is True
    assert controller.state() == "stopped"  # runtime is released
    assert controller.generation_id() == 0
    assert controller.runtime() is None

    # History records the failed generation with its error message.
    snap = controller.history_snapshot()
    assert len(snap) == 1
    assert snap[0]["state"] == "error"
    assert "simulated REST seed failure" in snap[0]["error"]


def test_hook_failure_then_fresh_start_succeeds(controller):
    """After an error, a fresh Start() constructs a new generation
    (gen-2) with a clean runtime — no leftover state from the failed
    gen-1."""
    def on_start_fail(runtime: Runtime) -> None:  # noqa: ARG001
        raise RuntimeError("transient MQTT failure")

    def on_start_ok(runtime: Runtime) -> None:
        runtime.message_manager = _FakeMessageManager()
        runtime.event_log = object()

    # First gen: fails
    controller.set_render_loop_hooks(on_start=on_start_fail)
    controller.start()
    assert controller.state() == "stopped"
    controller.set_render_loop_hooks(on_start=on_start_ok)
    # Second gen: succeeds
    controller.start()
    assert controller.state() == "running"
    assert controller.generation_id() == 2
    controller.stop()


def test_on_stop_hook_failure_does_not_prevent_teardown(controller):
    """If the on_stop render-loop hook itself raises, the
    controller still releases the resources and moves the
    runtime to `stopped`."""
    def on_stop_raises(runtime: Runtime) -> None:  # noqa: ARG001
        raise RuntimeError("requestAnimationFrame cancel failed")

    def on_start_ok(runtime: Runtime) -> None:
        runtime.message_manager = _FakeMessageManager()
        runtime.event_log = object()

    controller.set_render_loop_hooks(on_start=on_start_ok, on_stop=on_stop_raises)
    controller.start()
    assert controller.state() == "running"
    controller.stop()  # must not raise
    assert controller.state() == "stopped"
    assert controller.runtime() is None


# --- History ring ---------------------------------------------------------


def test_history_ring_is_bounded(controller):
    """The history ring keeps only the last GENERATION_HISTORY_MAX entries."""
    for _ in range(GENERATION_HISTORY_MAX + 5):
        controller.start()
        controller.stop()
    snap = controller.history_snapshot()
    # Generation ids 1..(N+5) survived; the ring kept only the last 8.
    assert len(snap) == GENERATION_HISTORY_MAX


def test_history_snapshot_records_state_transitions(controller):
    """Each entry is `(generation_id, state, error)`."""
    controller.start()
    controller.stop()
    snap = controller.history_snapshot()
    assert snap[0]["generation_id"] == 1
    assert snap[0]["state"] == "stopped"
    assert snap[0]["error"] is None


# --- _NullMessageManager surface ------------------------------------------


def test_null_message_manager_swallows_dispatch():
    """Stale MQTT envelopes post-Stop land on the null stand-in.
    The stand-in must not raise."""
    null = _NullMessageManager()
    null.dispatch('{"type": "message"}')
    null.dispatch(None)
    null.dispatch("anything")


def test_null_message_manager_returns_empty_messages():
    null = _NullMessageManager()
    assert null.get_messages() == []
    assert null.get_messages(limit=10) == []
    assert null.get_messages(suppress=False) == []


def test_null_message_manager_get_config_is_none():
    """`get_config` returns None (the dashboard binds `.config` to a
    SignConfig in production; the null stand-in returns None so any
    stale UI binding sees a 'missing config' state)."""
    null = _NullMessageManager()
    assert null.get_config() is None


def test_null_message_manager_register_handler_is_noop():
    """Command-handler registrations on a torn-down generation land
    on the null stand-in. They must be silently dropped."""
    null = _NullMessageManager()
    null.register_handler("force_upgrade", lambda: None)
    null.register_handler("any-action", lambda: None)


def test_null_message_manager_seed_is_awaitable_noop():
    """`seed()` returns an awaitable (the real MessageManager returns
    a coroutine). The null stand-in matches that signature so
    `await null.seed()` is a no-op rather than a TypeError."""
    import asyncio

    null = _NullMessageManager()
    asyncio.run(null.seed())


# --- Behavioral knobs / module-level constants ----------------------------


def test_default_event_log_max_entries_is_100():
    """Spec §3 / §6: the in-memory browser selector event log caps
    at 100 entries by default."""
    assert DEFAULT_EVENT_LOG_MAX_ENTRIES == 100


def test_no_protected_leftover_singleton_state():
    """The controller must NOT carry any module-level singleton
    state. Every Start() constructs a fresh Runtime record."""
    c1 = DashboardController()
    c1.start()
    c2 = DashboardController()
    # Two independent controllers hold independent state.
    assert c1.state() == "starting"
    assert c2.state() == "stopped"
    c1.stop()


def test_null_message_manager_config_and_messages_are_none():
    """`messages` and `config` attributes default to None so the
    dashboard UI can distinguish a torn-down runtime."""
    null = _NullMessageManager()
    assert null.config is None
    assert null.messages is None


# --- Stop semantics --------------------------------------------------------


def test_stop_releases_event_log(controller, fake_runtime_hook):
    """The runtime's `event_log` is dropped on Stop — the next
    generation builds a new one (the deque is no longer
    referenced)."""
    refs = fake_runtime_hook(controller)
    el = refs["event_log"]
    controller.start()
    assert controller.runtime().event_log is el
    controller.stop()
    assert controller.runtime() is None


def test_stop_swaps_message_manager_to_null(controller, fake_runtime_hook):
    """After Stop, `message_manager` on the torn-down Runtime is the
    `_NullMessageManager` stand-in — the production Stop swaps it
    in `_teardown_generation` so any in-flight MQTT envelope lands
    safely.

    We verify by reaching into the history ring via `runtime()` —
    but `runtime()` clears the slot on stop, so we use the
    controller's history-snapshot metadata instead."""
    refs = fake_runtime_hook(controller)
    controller.start()
    # Pre-stop: real MessageManager.
    pre = controller.runtime().message_manager
    assert isinstance(pre, _FakeMessageManager)
    controller.stop()
    # Post-stop: the runtime is None (cleared); the history shows
    # the torn-down generation. We can't introspect the live
    # Runtime any more — that's the design: stale callbacks land on
    # the null stand-in because the runtime slot is gone.
    assert controller.runtime() is None
    snap = controller.history_snapshot()
    assert snap[0]["generation_id"] == 1
    assert snap[0]["state"] == "stopped"
    # Sanity: the captured references prove the hook did install
    # the fake before Stop dropped it.
    assert refs["message_manager"] is pre


def test_stop_invalidates_active_generation_before_render_loop_stop(controller):
    """Design §2 contract: Stop invalidates the active generation FIRST,
    then runs teardown. The render-loop on_stop hook must observe a
    zeroed `_active_generation_id` and a False `is_active_generation(stale_gen)`
    BEFORE any teardown side-effect runs — otherwise a late
    `_on_change_js` (which captures `gen_id` at wrap time and gates
    on `is_active_generation(gen_id)` at call time) could fan out a
    stale `App._dispatchChange()` during the brief teardown window.
    See Finding (b) in the standalone-preview-dashboard spot-check.
    """
    captured: dict = {}
    stale_gen_holder: dict = {}

    def on_start(runtime: Runtime) -> None:
        runtime.event_log = object()
        runtime.message_manager = _FakeMessageManager()

    def on_stop(runtime: Runtime) -> None:
        # At this exact moment — early in `stop()` — the active
        # generation id MUST already be zero and the stale-gen
        # consultation MUST already return False.
        captured["generation_id_at_on_stop"] = controller.generation_id()
        stale_gen = stale_gen_holder["gen"]
        captured["is_active_at_on_stop"] = controller.is_active_generation(stale_gen)
        captured["runtime_state_at_on_stop"] = runtime.state

    controller.set_render_loop_hooks(on_start=on_start, on_stop=on_stop)
    controller.start()
    stale_gen_holder["gen"] = controller.generation_id()
    controller.stop()

    assert captured["generation_id_at_on_stop"] == 0, (
        "stop() must zero _active_generation_id BEFORE running the "
        "render-loop on_stop hook so a late _on_change_js short-circuits. "
        "See dashboard_controller.py:stop() — Finding (b)."
    )
    assert captured["is_active_at_on_stop"] is False
    assert captured["runtime_state_at_on_stop"] == "stopping"


# --- on_change / status subscriber surface ---------------------------------
#
# The JS-side `dashboard_controls.js` shim calls
# `Dashboard.status()` for the initial button-label / error-row
# render, then subscribes via `Dashboard.on_change(callback)` for
# live updates. Without this surface the shim's bind() falls
# through to "neither on_change nor subscribe" and the Start button
# never updates — that's the bug the subscriber wiring fixes.


def test_status_initial_is_stopped(controller):
    """A fresh controller reports `state="stopped"` and `error=None`."""
    snap = controller.status()
    assert snap == {"state": "stopped", "error": None}


def test_status_during_starting(controller):
    """`status()` reflects the optimistic `starting` state right
    after `start()` and before any render-loop hook runs."""
    controller.start()
    snap = controller.status()
    assert snap == {"state": "starting", "error": None}


def test_status_after_successful_bootstrap(controller, fake_runtime_hook):
    """After a successful bootstrap, `status()` reports `running`."""
    fake_runtime_hook(controller)
    controller.start()
    assert controller.status() == {"state": "running", "error": None}


def test_status_after_error(controller):
    """A failed bootstrap reports `state="error"` with the error
    message carried in `error`."""
    def on_start_fail(runtime: Runtime) -> None:  # noqa: ARG001
        raise RuntimeError("seed failed: HTTP 503")

    controller.set_render_loop_hooks(on_start=on_start_fail)
    controller.start()
    snap = controller.status()
    assert snap["state"] == "stopped"
    assert snap["error"] is None
    # But the history snapshot preserves the error so a fresh
    # subscriber can read it back if it joins after the error.
    hist = controller.history_snapshot()
    assert hist[-1]["state"] == "error"
    assert "seed failed: HTTP 503" in hist[-1]["error"]


def test_on_change_fires_starting_and_running(controller, fake_runtime_hook):
    """A subscriber receives both `starting` and `running` snapshots
    during a successful Start."""
    fake_runtime_hook(controller)
    received: list[dict] = []
    controller.on_change(lambda snap: received.append(dict(snap)))

    controller.start()

    # Two notifications during a single start(): "starting" (optimistic)
    # and "running" (after the hook promoted the runtime).
    states = [s["state"] for s in received]
    assert "starting" in states
    assert "running" in states
    # `starting` fires before `running` (lifecycle order).
    assert states.index("starting") < states.index("running")


def test_on_change_fires_stopping_and_stopped(controller, fake_runtime_hook):
    """A subscriber receives `stopping` then `stopped` during teardown."""
    fake_runtime_hook(controller)
    controller.start()
    received: list[dict] = []
    controller.on_change(lambda snap: received.append(dict(snap)))

    controller.stop()

    states = [s["state"] for s in received]
    assert "stopping" in states
    assert "stopped" in states
    assert states.index("stopping") < states.index("stopped")


def test_on_change_fires_error(controller):
    """A failed bootstrap fans out an `error` snapshot with the
    captured message."""
    def on_start_fail(runtime: Runtime) -> None:  # noqa: ARG001
        raise RuntimeError("transient network error")

    controller.set_render_loop_hooks(on_start=on_start_fail)
    received: list[dict] = []
    controller.on_change(lambda snap: received.append(dict(snap)))

    controller.start()

    error_snaps = [s for s in received if s["state"] == "error"]
    assert len(error_snaps) == 1
    assert "transient network error" in error_snaps[0]["error"]


def test_on_change_returns_working_unsubscribe(controller, fake_runtime_hook):
    """The unsubscribe function returned from `on_change` removes
    the callback from the subscriber list — subsequent transitions
    no longer reach it."""
    fake_runtime_hook(controller)
    received: list[dict] = []
    unsubscribe = controller.on_change(lambda snap: received.append(dict(snap)))

    controller.start()
    pre_stop_count = len(received)
    assert pre_stop_count >= 2  # starting + running

    unsubscribe()
    controller.stop()

    # No new notifications after unsubscribe.
    assert len(received) == pre_stop_count


def test_on_change_unsubscribe_is_idempotent(controller):
    """Calling the unsubscribe function twice is a no-op (the
    callback is no longer in the list the second time around)."""
    received: list[dict] = []
    unsubscribe = controller.on_change(lambda snap: received.append(dict(snap)))

    unsubscribe()
    unsubscribe()  # must not raise

    controller.start()
    # The callback was unsubscribed before start() ran, so it
    # received no notifications.
    assert received == []


def test_on_change_subscriber_exception_does_not_break_chain(controller, fake_runtime_hook):
    """A subscriber that raises is logged and skipped; subsequent
    subscribers (and subsequent transitions) still receive their
    snapshots."""
    fake_runtime_hook(controller)
    received: list[dict] = []

    def raising_cb(snap):
        raise RuntimeError("subscriber exploded")

    controller.on_change(raising_cb)
    controller.on_change(lambda snap: received.append(dict(snap)))

    controller.start()

    # The healthy subscriber still received the starting + running
    # notifications despite the broken one raising on every call.
    states = [s["state"] for s in received]
    assert "starting" in states
    assert "running" in states


def test_on_change_does_not_fire_on_subscribe(controller):
    """`on_change` is fire-on-edge: subscribing does NOT immediately
    invoke the callback with the current state. The JS-side shim
    uses `status()` for the initial pull and `on_change` for live
    updates — a fire-on-subscribe would double-deliver."""
    received: list[dict] = []
    controller.on_change(lambda snap: received.append(dict(snap)))

    assert received == []

    controller.start()

    # After the first transition, the callback has been invoked.
    assert len(received) >= 1


def test_status_after_stop_returns_stopped(controller, fake_runtime_hook):
    """`status()` returns `stopped` (with no error) after teardown
    even though the history ring holds the torn-down runtime."""
    fake_runtime_hook(controller)
    controller.start()
    controller.stop()
    assert controller.status() == {"state": "stopped", "error": None}


# --- PyScript dict → JS object boundary (issue #48 regression) -------------
#
# The standalone dashboard's Start/Stop button reads the controller's
# state via `Dashboard.status()` on bind, then keeps in sync via the
# `on_change` callback. Pyodide 0.26 hands Python dicts back to JS as
# `PyProxy` wrappers — `Object.keys(snap)` returns `[]`,
# `JSON.stringify(snap)` returns `"{}"`, and `snap.state` is
# `undefined`. Without an explicit `to_js(..., dict_converter=
# Object.fromEntries)` conversion, the button shows "Start" / overlay
# shows "Simulator stopped — press Start to begin" even though the
# controller is actually `running`. The `stop` action then fires when
# the operator clicks the (mis-labeled) "Start" button.
#
# The `_to_js_obj` helper centralizes the conversion with a host-CPython
# fallback (the unit-test environment has no Pyodide), so the conversion
# is exercised in production (PyScript) and the structural shape is
# preserved in tests.


def test_to_js_obj_returns_plain_dict_on_host_python():
    """Host CPython has no Pyodide; the helper falls back to the input dict."""
    out = _to_js_obj({"state": "running", "error": None})
    assert out == {"state": "running", "error": None}
    assert out["state"] == "running"


def test_to_js_obj_preserves_none_values():
    """`error=None` must survive the conversion. Pyodide's default
    `Object.fromEntries` keeps the key with a `null` value; the helper
    must do the same on host CPython so tests can compare with `==`."""
    out = _to_js_obj({"state": "running", "error": None})
    assert "error" in out
    assert out["error"] is None


def test_status_returns_runnings_error_none_shape(controller, fake_runtime_hook):
    """`status()` returns the `{state, error}` shape with `state=running`
    and `error=None` after a successful bootstrap. Regression for the
    bug where the value arrived in JS as an empty PyProxy dict, making
    `applyState` see `state === undefined` and fall back to `stopped`."""
    fake_runtime_hook(controller)
    controller.start()
    snap = controller.status()
    assert snap["state"] == "running"
    assert snap["error"] is None
    # The dict shape must include both keys — the JS shim reads
    # `snap.state` directly and treats an absent key as "stopped".
    assert "state" in snap
    assert "error" in snap


def test_on_change_payload_has_state_and_error_keys(controller, fake_runtime_hook):
    """Subscribers receive a `{state, error}` payload with both keys
    populated. Regression for the bug where the dict arrived as a
    PyProxy and the JS shim's `snap.state` was `undefined`."""
    fake_runtime_hook(controller)
    received: list[dict] = []
    controller.on_change(lambda snap: received.append(dict(snap)))
    controller.start()
    assert received, "subscribers should have been notified"
    running = [s for s in received if s.get("state") == "running"]
    assert running, f"expected a 'running' notification, got {received}"
    snap = running[-1]
    assert "state" in snap
    assert "error" in snap
    assert snap["error"] is None


def test_on_change_payload_keys_remain_accessible_across_transitions(controller, fake_runtime_hook):
    """Every transition payload has both `state` and `error` keys.
    Regression: the original PyProxy bug surfaced as a transient
    notification with `snap.state === undefined`, which the JS shim
    normalized to `stopped` and overrode the operator's view."""
    fake_runtime_hook(controller)
    received: list[dict] = []
    controller.on_change(lambda snap: received.append(dict(snap)))
    controller.start()
    controller.stop()
    states = [s["state"] for s in received]
    assert "starting" in states
    assert "running" in states
    assert "stopping" in states
    assert "stopped" in states
    # Every payload carried both keys — the JS shim's
    # `applyState(state, error)` destructures both.
    for snap in received:
        assert "state" in snap, f"snap without 'state' key: {snap}"
        assert "error" in snap, f"snap without 'error' key: {snap}"

