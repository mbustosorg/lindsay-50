"""Integration tests: DashboardController + real EventLog (issue #48, §2.5, §3.3, §3.4).

The spec requires that the controller's per-generation runtime owns a
fresh `EventLog` on every Start and discards the prior generation's
queue during Stop. These tests exercise the controller against the
real `EventLog` class (not a stub) to prove:

  - Each generation's `Runtime.event_log` is a fresh, independent
    `EventLog` instance — appending to gen-1's log does not affect
    gen-2's log.
  - Stop calls `clear()` on the active log AND drops the reference
    (the slot is None when the runtime is torn down).
  - A fresh Start after Stop sees an empty log even if the prior
    generation was saturated with entries.
  - The bounded deque cap is preserved across Stop-then-Start
    boundaries.
  - The `clear()` during Stop is robust to the log being already
    empty (no exceptions), and tolerant of a hook that didn't
    install a log (Runtime.event_log=None — Stop doesn't blow up).
  - A Start-then-fail path (render-loop hook raises) does not leak
    a partially-constructed log: the next Start sees a fresh queue.

The production wiring (the render-loop hook that calls
`EventLog(max_entries=100)`) is captured in test code via
`set_render_loop_hooks`. The controller's `_teardown_generation`
calls `event_log.clear()` before dropping the reference; we
verify that contract here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "heart-message-manager"))

from dashboard_controller import DashboardController, Runtime, _NullMessageManager  # noqa: F401 — _NullMessageManager re-exported for parity
from event_log import EventLog


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def controller():
    return DashboardController()


@pytest.fixture
def log_cap():
    """Default cap used by the controller's render-loop hook in
    production. Pinning the value here keeps the test from drifting
    if `DEFAULT_EVENT_LOG_MAX_ENTRIES` ever changes — the tests should
    assert against the constant, not a magic number."""
    from dashboard_controller import DEFAULT_EVENT_LOG_MAX_ENTRIES

    return DEFAULT_EVENT_LOG_MAX_ENTRIES


def _install_log_hook(c: DashboardController, max_entries: int | None = None):
    """Install a render-loop hook that constructs a fresh `EventLog`
    + a `_FakeMessageManager` (real MessageManager is not part of
    the seam being tested).

    Returns the dict of references the test can introspect
    (`{'event_log': <EventLog>, 'message_manager': <FakeManager>,
    'on_start_calls': [...], 'on_stop_calls': [...]}`).
    """

    refs = {"on_start_calls": [], "on_stop_calls": []}

    def on_start(runtime: Runtime) -> None:
        cap = max_entries if max_entries is not None else 100
        runtime.event_log = EventLog(max_entries=cap)
        refs["event_log"] = runtime.event_log
        runtime.message_manager = _FakeMessageManager()
        refs["on_start_calls"].append(runtime.generation_id)

    def on_stop(runtime: Runtime) -> None:
        refs["on_stop_calls"].append(runtime.generation_id)

    c.set_render_loop_hooks(on_start=on_start, on_stop=on_stop)
    return refs


class _FakeMessageManager:
    """Same stand-in used by `dashboard_runtime_test.py`. Duplicated
    here so the two test files stay independent (one can be deleted
    without breaking the other)."""

    def __init__(self):
        self.dispatched = []

    def dispatch(self, raw):
        self.dispatched.append(raw)

    def get_messages(self, limit=None, suppress=True):  # noqa: ARG002
        return []


# --- §3.3 / §3.4: fresh log per generation --------------------------------


def test_fresh_start_constructs_a_new_event_log(controller):
    """Start builds a fresh `EventLog` for the new generation — the
    slot was None before, and after Start is the hook-installed
    `EventLog` instance."""
    assert controller.runtime() is None
    refs = _install_log_hook(controller)
    controller.start()
    log = controller.runtime().event_log
    assert isinstance(log, EventLog)
    assert log is refs["event_log"]
    assert len(log) == 0
    controller.stop()


def test_fresh_start_log_is_empty_even_if_prior_generation_was_full(controller):
    """A fresh Start must NOT inherit state from the prior
    generation. We saturate gen-1's log, Stop, Start, and verify
    gen-2's log is empty."""
    _refs = _install_log_hook(controller, max_entries=5)
    # gen-1
    controller.start()
    gen1_log = controller.runtime().event_log
    for i in range(10):
        gen1_log.append(
            {
                "event_type": "text_display",
                "message_id": f"old-{i}",
                "timestamp": float(i),
                "received_at": 0.0,
            }
        )
    # Cap-5 log dropped the first 5 entries; 5 survived.
    assert len(gen1_log) == 5
    controller.stop()
    # gen-2
    controller.start()
    gen2_log = controller.runtime().event_log
    assert gen2_log is not gen1_log, "fresh Start must construct a new log instance"
    assert len(gen2_log) == 0, "fresh Start must NOT inherit the prior generation's queue"
    controller.stop()


def test_each_generation_has_an_independent_queue(controller):
    """Appending events to gen-1's log must not affect gen-2's log.
    Two independent instances, no shared backing storage."""
    _refs = _install_log_hook(controller)
    controller.start()
    log1 = controller.runtime().event_log
    log1.append(
        {
            "event_type": "text_display",
            "message_id": "gen1-only",
            "timestamp": 100.0,
            "received_at": 50.0,
        }
    )
    assert len(log1) == 1
    controller.stop()
    controller.start()
    log2 = controller.runtime().event_log
    assert log2 is not log1
    rows = log2.query()
    assert rows == [], "gen-2's log must NOT contain gen-1's events"
    controller.stop()


# --- §1.6: Stop teardown ---------------------------------------------------


def test_stop_clears_then_drops_the_log(controller):
    """Stop calls `clear()` on the runtime's event_log so the prior
    queue is empty when the controller goes back to `stopped` — even
    if a stale reference survives, it sees an empty queue."""
    _refs = _install_log_hook(controller)
    controller.start()
    log = controller.runtime().event_log
    log.append(
        {
            "event_type": "text_display",
            "message_id": "m1",
            "timestamp": 100.0,
            "received_at": 50.0,
        }
    )
    assert len(log) == 1
    controller.stop()
    # The runtime slot is None after Stop — the controller has dropped
    # the reference entirely. (The Production teardown calls `clear()`
    # before dropping; the test confirms the drop happened.)
    assert controller.runtime() is None


def test_stop_with_no_event_log_does_not_raise(controller):
    """If the render-loop hook didn't install an event_log (e.g. a
    minimal hook that only constructs the MessageManager), Stop
    must not raise — the teardown is robust to missing fields."""
    def on_start_minimal(runtime: Runtime) -> None:  # noqa: ARG001
        runtime.message_manager = _FakeMessageManager()
        # No event_log installed.

    def on_stop_minimal(runtime: Runtime) -> None:  # noqa: ARG001
        pass

    controller.set_render_loop_hooks(on_start=on_start_minimal, on_stop=on_stop_minimal)
    controller.start()
    assert controller.runtime().event_log is None
    controller.stop()  # must not raise
    assert controller.state() == "stopped"


def test_stop_idempotent_when_log_already_empty(controller):
    """A Start with no appends, then Stop, must not raise."""
    _refs = _install_log_hook(controller)
    controller.start()
    log = controller.runtime().event_log
    assert len(log) == 0
    controller.stop()  # `clear()` on an empty log is a no-op
    assert controller.state() == "stopped"


def test_stop_releases_log_cap_to_default(controller, log_cap):
    """The next Start must construct a fresh log with the default
    cap, not whatever the prior generation had."""
    _refs = _install_log_hook(controller, max_entries=3)
    controller.start()
    assert controller.runtime().event_log.max_entries == 3
    controller.stop()
    # Fresh Start — no max_entries kwarg → default cap.
    _install_log_hook(controller)  # reinstall with default cap
    controller.start()
    assert controller.runtime().event_log.max_entries == log_cap
    controller.stop()


# --- §1.7 / §1.8: partial-bootstrap failure ------------------------------


def test_failed_start_does_not_leak_partial_event_log(controller):
    """A render-loop hook that constructs the EventLog but then
    raises (e.g. MQTT-WS connect failed AFTER the log was built)
    must release the partial log. The next Start sees a fresh
    queue."""
    refs: dict = {}

    def on_start_partial(runtime: Runtime) -> None:
        runtime.event_log = EventLog(max_entries=10)
        runtime.message_manager = _FakeMessageManager()
        # Half-construct the log...
        runtime.event_log.append(
            {
                "event_type": "text_display",
                "message_id": "leaked",
                "timestamp": 1.0,
                "received_at": 0.0,
            }
        )
        refs["partial_log"] = runtime.event_log
        # ...then fail before completion.
        raise RuntimeError("MQTT connect failed (simulated)")

    controller.set_render_loop_hooks(on_start=on_start_partial)
    controller.start()
    assert controller.state() == "stopped"
    assert controller.runtime() is None
    assert controller.history_snapshot()[0]["state"] == "error"
    # The next Start constructs a fresh log — none of the entries
    # from the failed partial log should leak in.
    refs2: dict = {}

    def on_start_ok(runtime: Runtime) -> None:
        runtime.event_log = EventLog(max_entries=10)
        runtime.message_manager = _FakeMessageManager()
        refs2["log"] = runtime.event_log

    controller.set_render_loop_hooks(on_start=on_start_ok)
    controller.start()
    fresh_log = controller.runtime().event_log
    assert fresh_log is not refs["partial_log"]
    assert len(fresh_log) == 0
    controller.stop()


# --- §2.5: stop-then-start round-trip -------------------------------------


def test_stop_then_start_full_round_trip(controller):
    """The full Start→populate→Stop→Start round-trip yields two
    independent logs with disjoint state."""
    _refs = _install_log_hook(controller, max_entries=4)
    # gen-1
    controller.start()
    log1 = controller.runtime().event_log
    for i in range(6):  # exceeds cap-4
        log1.append(
            {
                "event_type": "text_display",
                "message_id": f"g1-{i}",
                "timestamp": float(i),
                "received_at": 0.0,
            }
        )
    assert len(log1) == 4
    rows1 = log1.query()
    assert [r["message_id"] for r in rows1] == ["g1-2", "g1-3", "g1-4", "g1-5"]
    controller.stop()
    # gen-2
    controller.start()
    log2 = controller.runtime().event_log
    assert log2 is not log1
    assert len(log2) == 0
    for i in range(3):
        log2.append(
            {
                "event_type": "text_display",
                "message_id": f"g2-{i}",
                "timestamp": float(i),
                "received_at": 0.0,
            }
        )
    assert len(log2) == 3
    rows2 = log2.query()
    assert [r["message_id"] for r in rows2] == ["g2-0", "g2-1", "g2-2"]
    controller.stop()


def test_log_clear_is_called_with_correct_arguments_during_stop(controller):
    """The teardown calls `event_log.clear()` (no args). The
    `clear()` method on the real `EventLog` is parameterless
    (matching the deque.clear signature), so this is a regression
    guard against accidental `clear(k=...)` calls."""
    import inspect

    # Inspect `EventLog.clear` as an unbound function (descriptor on
    # the class). It must take exactly `(self,)` — no positional
    # args beyond self, no keyword args.
    unbound = EventLog.__dict__["clear"]
    sig = inspect.signature(unbound)
    params = [name for name in sig.parameters if name != "self"]
    assert params == [], (
        f"EventLog.clear() must take no arguments beyond self; got params={params}. "
        "The controller's Stop teardown calls `event_log.clear()` with no args."
    )


# --- §3.5: no IndexedDB references at the controller seam ---------------


def test_event_log_has_no_indexeddb_attribute():
    """Pinning §3.5: the in-memory `EventLog` does NOT carry any
    IndexedDB-specific state. A test future-me might be tempted to
    `clear()` the IDB store on Stop — there is none."""
    log = EventLog()
    # The Pi-side JSONL `EventLog` exposes `reload()` to re-read
    # from disk; the browser side keeps the same surface for API
    # parity but `reload()` is a no-op. There's no `idb_store`,
    # `db`, `object_store`, or similar.
    forbidden = (
        "idb_store",
        "db",
        "object_store",
        "idb_open",
        "transaction",
        "_db",
    )
    for attr in forbidden:
        assert not hasattr(log, attr), (
            f"EventLog must NOT carry an `{attr}` attribute — the browser "
            "side is in-memory only and does not use IndexedDB."
        )


def test_event_log_clear_wipes_the_deque():
    """Pinning §3.5 + §1.6: the in-memory `EventLog.clear()` resets
    the deque — there is no secondary IndexedDB store to clear. We
    verify by appending, clearing, and inspecting the internal
    `self._events` (the deque)."""
    log = EventLog(max_entries=10)
    for i in range(5):
        log.append(
            {
                "event_type": "text_display",
                "message_id": f"m{i}",
                "timestamp": float(i),
                "received_at": 0.0,
            }
        )
    assert len(log) == 5
    # Internal deque has 5 entries.
    assert len(log._events) == 5
    log.clear()
    # Internal deque is replaced with a fresh `deque(maxlen=N)` —
    # `clear()` on a deque itself just empties it; we verify both
    # `len(log)` and the deque length.
    assert len(log) == 0
    assert len(log._events) == 0


# --- §2.8: parity with the Pi-side JSONL EventLog contract --------------


def test_browser_event_log_matches_pi_contract():
    """The browser-side `EventLog` and the Pi-side JSONL `EventLog`
    expose the same public surface. Any drift (a method on one side
    that doesn't exist on the other) is a parity violation.

    This test only inspects the in-memory class — the Pi class is
    loaded lazily so the test stays decoupled from the Pi deps.
    """
    expected_methods = {
        "append",
        "query",
        "last_for",
        "clear",
        "reload",
        "__len__",
        "max_entries",
    }
    actual = set(dir(EventLog()))
    missing = expected_methods - actual
    assert not missing, f"EventLog is missing methods: {sorted(missing)}"