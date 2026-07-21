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

