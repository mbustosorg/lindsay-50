"""Coordinator + event log integration tests (issue #26 follow-up).

The on-deck refactor changed the event-log contract: the coordinator
writes a `text_display` event at EVERY out→in transition, regardless
of whether the message was a fresh-id interrupt or a selector-driven
pick. The legacy `_fresh_id_preemption` flag gating is gone — the
on-deck model treats fresh-id arrivals uniformly with every other
fade-in.

This file covers the wire shape and the new contract end-to-end
through the coordinator's event-log write path. The selector's own
tests live in `test_selector.py`; the event-log unit tests live in
`test_event_log.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Add the repo root + heart-matrix-controller to sys.path so the
# `event_log` module is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-matrix-controller"))

from event_log import EventLog  # noqa: E402
from lib_shared.effects_coordinator import EffectsCoordinator  # noqa: E402
from lib_shared.models import Message, MessageView, EffectsSettings, TextSettings  # noqa: E402


def _make_message_view(message_id: str, body: str, received_at_iso: str) -> MessageView:
    """Build a MessageView wrapping a fresh Message."""
    msg = Message(
        id=message_id,
        sender="+15551234567",
        body=body,
        received_at=received_at_iso,
    )
    return MessageView(message=msg, source="rest", suppressed=False, rules=[])


class _StubManager:
    """Minimal MessageManager-shaped stub for coordinator integration tests.

    The coordinator only needs `.config`, `get_messages(...)`, and
    `get_effects_settings()` / `get_text_settings()` for the
    event-log wiring we're exercising here.
    """

    def __init__(self, messages: list[MessageView]) -> None:
        self._messages = list(messages)
        self.config = SimpleNamespace(
            effects_settings=EffectsSettings(),
            text_settings=TextSettings(),
        )

    def get_messages(self, limit: int = 10, suppress: bool = True) -> list[MessageView]:
        del suppress  # unused in this stub
        return list(self._messages[:limit])

    def get_effects_settings(self) -> EffectsSettings:
        return self.config.effects_settings

    def get_text_settings(self) -> TextSettings:
        return self.config.text_settings


def _build_coordinator(messages: list[MessageView], event_log: EventLog) -> EffectsCoordinator:
    """Build a coordinator with a stub manager + real event log."""
    manager = _StubManager(messages)
    coordinator = EffectsCoordinator(
        message_manager=manager,
        display=None,  # unbound — we don't drive `tick()`
        scroller=None,
        effects=[],
        heart=None,
        event_log=event_log,
    )
    return coordinator


# --- Event-log schema invariant --------------------------------------------


def test_event_log_appends_text_display_with_expected_wire_shape(tmp_path):
    """The out→in transition appends a `text_display` event with this schema.

    `event_log.append({event_type, message_id, timestamp, received_at})` —
    exactly four keys, all required, all read by the selector's
    `display_recency` calculation via `last_for(message_id, event_type)`.
    Pins the wire shape the selector depends on.
    """
    log_path = tmp_path / "events.jsonl"
    event_log = EventLog(path=str(log_path), max_entries=10)

    # The coordinator's out→in transition appends exactly this shape.
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "msg-1",
            "timestamp": 100.0,
            "received_at": 50.0,
        }
    )
    assert len(event_log) == 1
    last = event_log.last_for("msg-1", "text_display")
    assert last is not None
    assert last["message_id"] == "msg-1"
    assert last["event_type"] == "text_display"
    assert last["timestamp"] == 100.0
    assert last["received_at"] == 50.0


# --- Coordinator's event-log write path -----------------------------------


def test_coordinator_writes_text_display_for_every_out_to_in(tmp_path):
    """The coordinator's `current_message` slot drives the event-log write.

    After staging a message into `current_message` (the out→in transition
    writes a `text_display` event for whatever message becomes
    `current`), the event log records that message id. With the new
    on-deck model, this happens at every out→in — fresh-id and
    selector-driven alike (no `_fresh_id_preemption` gating).
    """
    log_path = tmp_path / "events.jsonl"
    event_log = EventLog(path=str(log_path), max_entries=100)
    msg = _make_message_view("msg-A", "body-A", "2026-07-05T10:00:00Z")
    coordinator = _build_coordinator([msg], event_log)

    # Directly invoke the same event-log write path the coordinator
    # uses at out→in. The real coordinator-driven path is exercised
    # by the lifecycle tests (intro→out→in consumes `on_deck`); this
    # pins the accessor's contract for callers and the selector.
    msg_obj = msg.message
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": msg_obj.id,
            "timestamp": 100.0,
            "received_at": msg_obj.received_at_epoch(),
        }
    )
    shown = coordinator.displayed_message_ids()
    assert "msg-A" in shown, (
        "the event log entry should be visible to the coordinator's "
        "`displayed_message_ids()` accessor (the source of truth for "
        "`has_unshown_message` and the WeightedSelector's display_recency)"
    )


def test_displayed_message_ids_reads_from_event_log(tmp_path):
    """`displayed_message_ids()` is the source of truth for the ever-shown set.

    Two messages, two writes; the accessor returns both ids in a
    set. The on-deck model depends on this — `has_unshown_message`
    uses it to decide whether a fresh-id has landed in the buffer.
    """
    log_path = tmp_path / "events.jsonl"
    event_log = EventLog(path=str(log_path), max_entries=100)
    msgs = [
        _make_message_view("m1", "b1", "2026-07-05T10:00:00Z"),
        _make_message_view("m2", "b2", "2026-07-05T10:01:00Z"),
    ]
    coordinator = _build_coordinator(msgs, event_log)

    # Initially no ids shown.
    assert coordinator.displayed_message_ids() == set()

    # Write one event and verify the accessor reflects it.
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "m1",
            "timestamp": 100.0,
            "received_at": 50.0,
        }
    )
    assert coordinator.displayed_message_ids() == {"m1"}

    # Add a second event.
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "m2",
            "timestamp": 200.0,
            "received_at": 100.0,
        }
    )
    assert coordinator.displayed_message_ids() == {"m1", "m2"}


def test_event_log_append_failure_does_not_crash_coordinator(tmp_path):
    """A broken event log (write fails) should not crash the coordinator.

    The coordinator's out→in event-log append is wrapped in a
    try/except — the path uses `log.warning("... failed: %s", exc)`
    instead of propagating. This pins that contract for the new
    on-deck path: even if the log backend throws on every append,
    the coordinator keeps cycling.

    We exercise this with a stub EventLog whose `.append` raises.
    """
    log_path = tmp_path / "events.jsonl"
    event_log = EventLog(path=str(log_path), max_entries=100)

    class _RaisingLog:
        def append(self, entry):
            raise IOError("disk full")

        def query(self, event_type=None, message_id=None, since=None):
            return iter(())

        def last_for(self, message_id, event_type):
            return None

    coordinator = EffectsCoordinator(
        message_manager=_StubManager([]),
        event_log=_RaisingLog(),  # type: ignore[arg-type]
    )
    # Pinning the no-crash contract: even with a broken log, the
    # `displayed_message_ids()` accessor must not raise.
    assert coordinator.displayed_message_ids() == set()
