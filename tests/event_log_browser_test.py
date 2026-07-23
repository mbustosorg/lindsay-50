"""Unit tests for the browser-side in-memory `EventLog` (issue #48, section 3).

The browser dashboard owns one `EventLog` per runtime generation.
The class mirrors the Pi-side JSONL `EventLog` contract
(`append` / `query` / `last_for` / `__len__` / `max_entries`) but
backs the queue with `collections.deque(maxlen=N)` so the log is
ephemeral by construction — there's no IndexedDB persistence to
wipe, no file to delete, just a Python object the dashboard
controller drops on Stop.

Schema contract (locked — same as the Pi's):

    {
        "event_type":   "text_display" | ...,
        "message_id":   "<uuid>",
        "timestamp":    1752080123.45,
        "received_at":  1752000000.0
    }
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from heart_message_manager.event_log import EventLog  # noqa: E402


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def small_log():
    """EventLog with a tiny cap so eviction is observable in tests."""
    return EventLog(max_entries=3)


def _evt(event_type: str, message_id, ts: float, received_at: float) -> dict:
    """Helper: build a well-formed event dict."""
    return {
        "event_type": event_type,
        "message_id": message_id,
        "timestamp": ts,
        "received_at": received_at,
    }


# --- Construction ----------------------------------------------------------


def test_default_max_entries_is_100():
    """Default cap matches the Pi-side JSONL default."""
    log = EventLog()
    assert log.max_entries == 100


def test_zero_max_entries_rejected():
    """A non-positive cap is a programming error — raise early."""
    with pytest.raises(ValueError):
        EventLog(max_entries=0)
    with pytest.raises(ValueError):
        EventLog(max_entries=-1)


def test_empty_log_has_length_zero():
    log = EventLog()
    assert len(log) == 0


# --- append ----------------------------------------------------------------


def test_append_adds_row(small_log):
    """A well-formed event lands in the queue."""
    small_log.append(_evt("text_display", "m1", 100.0, 50.0))
    assert len(small_log) == 1


def test_append_drops_extra_fields(small_log):
    """The schema is locked to four keys. Extra fields (including
    `favorite`) must be silently dropped so a buggy caller cannot
    poison the queue."""
    small_log.append(
        {
            "event_type": "text_display",
            "message_id": "m1",
            "timestamp": 100.0,
            "received_at": 50.0,
            "favorite": True,
            "garbage": "ignored",
        }
    )
    rows = small_log.query()
    assert len(rows) == 1
    assert "favorite" not in rows[0]
    assert "garbage" not in rows[0]
    assert rows[0]["event_type"] == "text_display"


def test_append_rejects_missing_required_keys(small_log):
    """An event missing any required key is dropped."""
    small_log.append({"event_type": "text_display", "message_id": "m1", "timestamp": 1.0})  # no received_at
    small_log.append({"event_type": "text_display", "message_id": "m1", "received_at": 1.0})  # no timestamp
    small_log.append({"message_id": "m1", "timestamp": 1.0, "received_at": 1.0})  # no event_type
    small_log.append({"event_type": "text_display", "timestamp": 1.0, "received_at": 1.0})  # no message_id
    small_log.append(None)  # not a dict
    small_log.append("not a dict")  # not a dict
    assert len(small_log) == 0


def test_append_normalizes_string_ids():
    """message_id comes in as a string-like; coerce to str so query
    by string-id works regardless of caller-supplied type."""
    log = EventLog()
    log.append(_evt("text_display", 12345, 100.0, 50.0))
    rows = log.query(message_id="12345")
    assert len(rows) == 1


def test_append_normalizes_numeric_fields_to_float():
    """timestamp / received_at coerce to float so `since` filtering works."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100, 50))  # ints, not floats
    rows = log.query(since=99.5)
    assert len(rows) == 1
    rows = log.query(since=100.5)
    assert len(rows) == 0


# --- FIFO eviction ---------------------------------------------------------


def test_deque_drops_oldest_at_cap(small_log):
    """The bounded deque evicts the oldest entry at the cap (FIFO)."""
    for i in range(5):
        small_log.append(_evt("text_display", f"m{i}", float(i), 0.0))
    assert len(small_log) == 3
    rows = small_log.query()
    # First two (`m0`, `m1`) evicted; last three survive.
    assert [r["message_id"] for r in rows] == ["m2", "m3", "m4"]


def test_deque_drops_oldest_one_at_a_time():
    """Slow drain — eviction is one entry per append past the cap."""
    log = EventLog(max_entries=5)
    for i in range(8):
        log.append(_evt("text_display", f"m{i}", float(i), 0.0))
    # Cap=5: m3..m7 survive.
    rows = log.query()
    assert [r["message_id"] for r in rows] == ["m3", "m4", "m5", "m6", "m7"]


# --- query -----------------------------------------------------------------


def test_query_no_filters_returns_all(small_log):
    """An unfiltered query returns the whole queue in append order."""
    for i in range(3):
        small_log.append(_evt("text_display", f"m{i}", float(i), 0.0))
    rows = small_log.query()
    assert [r["message_id"] for r in rows] == ["m0", "m1", "m2"]


def test_query_by_event_type():
    """Filter on `event_type` only."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.append(_evt("hold_complete", "m1", 101.0, 50.0))
    log.append(_evt("text_display", "m2", 102.0, 51.0))
    rows = log.query(event_type="text_display")
    assert [r["message_id"] for r in rows] == ["m1", "m2"]


def test_query_by_message_id():
    """Filter on `message_id` only — used by the selector's
    `last_for` shortcut and by debug tools."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.append(_evt("text_display", "m2", 101.0, 51.0))
    log.append(_evt("text_display", "m1", 102.0, 50.0))
    rows = log.query(message_id="m1")
    assert [r["message_id"] for r in rows] == ["m1", "m1"]


def test_query_by_since():
    """`since` is a timestamp lower bound (inclusive)."""
    log = EventLog()
    for ts in (10.0, 20.0, 30.0, 40.0):
        log.append(_evt("text_display", f"m{ts}", ts, 0.0))
    rows = log.query(since=25.0)
    assert [r["message_id"] for r in rows] == ["m30.0", "m40.0"]


def test_query_combined_filters():
    """All three filters compose (AND semantics)."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.append(_evt("hold_complete", "m1", 101.0, 50.0))
    log.append(_evt("text_display", "m1", 102.0, 50.0))
    log.append(_evt("text_display", "m2", 103.0, 50.0))
    rows = log.query(event_type="text_display", message_id="m1", since=101.5)
    assert len(rows) == 1
    assert rows[0]["timestamp"] == 102.0


def test_query_returns_empty_when_no_match():
    """An empty result is `[]`, never `None` — the selector loops
    over it without a None guard."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    rows = log.query(message_id="missing")
    assert rows == []


def test_query_returns_defensive_copy():
    """Mutating the returned list does not mutate the queue."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    rows = log.query()
    rows.clear()
    assert len(log) == 1


# --- last_for --------------------------------------------------------------


def test_last_for_returns_most_recent_matching():
    """`last_for(message_id, event_type)` returns the newest matching
    entry (the one the selector cares about for "when was this last
    shown?")."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.append(_evt("hold_complete", "m1", 101.0, 50.0))
    log.append(_evt("text_display", "m1", 102.0, 50.0))
    latest = log.last_for("m1", "text_display")
    assert latest is not None
    assert latest["timestamp"] == 102.0


def test_last_for_returns_none_when_no_match():
    """No matching event → None (the selector treats None as "never
    shown, full send-recency weight")."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    assert log.last_for("m1", "hold_complete") is None
    assert log.last_for("missing", "text_display") is None


def test_last_for_returns_none_when_log_empty():
    log = EventLog()
    assert log.last_for("anything", "text_display") is None


# --- clear -----------------------------------------------------------------


def test_clear_empties_the_queue():
    """`clear()` is the dashboard controller's Stop hook for the
    selector event log — the next generation sees a fresh queue."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.append(_evt("text_display", "m2", 101.0, 51.0))
    assert len(log) == 2
    log.clear()
    assert len(log) == 0
    assert log.query() == []


def test_clear_preserves_max_entries():
    """The cap is a class-level constant; `clear()` only resets the
    queue contents, not the bound."""
    log = EventLog(max_entries=5)
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.clear()
    assert log.max_entries == 5


def test_clear_then_reuse_does_not_inherit_state():
    """After clear, the queue is fresh — appends land from the top."""
    log = EventLog(max_entries=3)
    log.append(_evt("text_display", "old1", 100.0, 50.0))
    log.append(_evt("text_display", "old2", 101.0, 51.0))
    log.clear()
    log.append(_evt("text_display", "new1", 200.0, 100.0))
    rows = log.query()
    assert [r["message_id"] for r in rows] == ["new1"]


def test_clear_does_not_break_existing_query_returns():
    """`clear()` replaces the deque in place so any held reference
    to the prior deque sees a fresh empty queue."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.clear()
    # A subsequent append uses the new (replacement) deque; the
    # cap is the same.
    for i in range(105):
        log.append(_evt("text_display", f"m{i}", float(i), 0.0))
    assert len(log) == 100


# --- reload ----------------------------------------------------------------


def test_reload_is_a_noop_for_in_memory_backend():
    """`reload()` exists for API compatibility with the Pi-side
    JSONL `EventLog`. The in-memory backend has no I/O to refresh —
    the deque IS the canonical state."""
    log = EventLog()
    log.append(_evt("text_display", "m1", 100.0, 50.0))
    log.reload()
    rows = log.query()
    assert len(rows) == 1


# --- Thread-safety ---------------------------------------------------------


def test_concurrent_appends_are_safe():
    """The lock guards mutations and reads. Two threads appending in
    parallel must not lose entries or corrupt the queue."""
    log = EventLog(max_entries=1000)
    n_threads = 8
    per_thread = 50

    def append_batch(start_idx):
        for i in range(per_thread):
            log.append(_evt("text_display", f"t{start_idx}-m{i}", float(i), 0.0))

    threads = [
        threading.Thread(target=append_batch, args=(t,))
        for t in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(log) == n_threads * per_thread
