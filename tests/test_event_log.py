"""Tests for the Pi-local event log (issue #26).

Covers tasks 6.1-6.5, 6.14, and 6.17. The selector tests live in
`test_selector.py`; the event-log integration tests in
`test_event_log_integration.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add the repo root + heart-matrix-controller to sys.path so the
# `event_log` module (which lives in `heart-matrix-controller/`)
# imports cleanly.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-matrix-controller"))

from event_log import EventLog, _REQUIRED_KEYS  # noqa: E402


def _make_event(message_id: str, timestamp: float, received_at: float, event_type: str = "text_display") -> dict:
    """Build a well-formed event dict."""
    return {
        "event_type": event_type,
        "message_id": message_id,
        "timestamp": timestamp,
        "received_at": received_at,
    }


# --- 6.1 Unit test: EventLog.append writes a parseable JSONL line and updates the in-memory cache ---


def test_append_writes_parseable_jsonl_and_updates_cache(tmp_path):
    """6.1: a fresh append produces one parseable JSON line and the cache picks it up."""
    log_path = tmp_path / "events.jsonl"
    log = EventLog(path=str(log_path), max_entries=10)
    log.append(_make_event("m1", 100.0, 50.0))

    # In-memory cache reflects the append.
    assert len(log) == 1
    assert log.last_for("m1", "text_display") == {
        "event_type": "text_display",
        "message_id": "m1",
        "timestamp": 100.0,
        "received_at": 50.0,
    }

    # On-disk file contains a parseable JSON line.
    with open(log_path, "r", encoding="utf-8") as f:
        line = f.readline()
    parsed = json.loads(line)
    assert parsed == _make_event("m1", 100.0, 50.0)


def test_append_writes_one_line_per_event(tmp_path):
    """Multiple appends produce one line each, in append order."""
    log_path = tmp_path / "events.jsonl"
    log = EventLog(path=str(log_path), max_entries=10)
    for i in range(5):
        log.append(_make_event(f"m{i}", 100.0 + i, 50.0 + i))
    with open(log_path, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 5
    assert [e["message_id"] for e in lines] == ["m0", "m1", "m2", "m3", "m4"]


# --- 6.2 Unit test: EventLog.query(event_type="text_display") returns only events with that event_type ---


def test_query_filters_by_event_type(tmp_path):
    """6.2: query(event_type=...) returns only events of that type."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=20)
    log.append(_make_event("m1", 100.0, 50.0, event_type="text_display"))
    log.append(_make_event("m1", 110.0, 50.0, event_type="image_display"))
    log.append(_make_event("m2", 120.0, 60.0, event_type="text_display"))
    log.append(_make_event("m1", 130.0, 50.0, event_type="video_display"))

    text_events = list(log.query(event_type="text_display"))
    assert len(text_events) == 2
    assert all(e["event_type"] == "text_display" for e in text_events)

    image_events = list(log.query(event_type="image_display"))
    assert len(image_events) == 1
    assert image_events[0]["message_id"] == "m1"
    assert image_events[0]["timestamp"] == 110.0

    video_events = list(log.query(event_type="video_display"))
    assert len(video_events) == 1


# --- 6.3 Unit test: EventLog.query(message_id="X") returns only events for X ---


def test_query_filters_by_message_id(tmp_path):
    """6.3: query(message_id=...) returns only events for that id."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=20)
    log.append(_make_event("m1", 100.0, 50.0))
    log.append(_make_event("m2", 110.0, 60.0))
    log.append(_make_event("m1", 120.0, 50.0))
    log.append(_make_event("m3", 130.0, 70.0))

    m1_events = list(log.query(message_id="m1"))
    assert len(m1_events) == 2
    assert all(e["message_id"] == "m1" for e in m1_events)

    m2_events = list(log.query(message_id="m2"))
    assert len(m2_events) == 1

    missing_events = list(log.query(message_id="never-existed"))
    assert missing_events == []


def test_query_combines_filters_with_and_semantics(tmp_path):
    """Filters are AND'd when both event_type and message_id are supplied."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=20)
    log.append(_make_event("m1", 100.0, 50.0, event_type="text_display"))
    log.append(_make_event("m1", 110.0, 50.0, event_type="image_display"))
    log.append(_make_event("m2", 120.0, 60.0, event_type="text_display"))

    matches = list(log.query(event_type="text_display", message_id="m1"))
    assert len(matches) == 1
    assert matches[0]["message_id"] == "m1"
    assert matches[0]["event_type"] == "text_display"


def test_query_with_since_filter(tmp_path):
    """`since` filters events whose timestamp is >= since."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=20)
    log.append(_make_event("m1", 100.0, 50.0))
    log.append(_make_event("m1", 200.0, 50.0))
    log.append(_make_event("m1", 300.0, 50.0))

    matches = list(log.query(message_id="m1", since=150.0))
    assert len(matches) == 2
    assert [e["timestamp"] for e in matches] == [200.0, 300.0]


# --- 6.4 Unit test: a corrupt JSONL line is skipped without breaking subsequent reads ---


def test_corrupt_line_skipped_other_events_survive(tmp_path):
    """6.4: corrupt lines are skipped, other events persist."""
    log_path = tmp_path / "e.jsonl"
    # Pre-populate with a mix of valid and corrupt lines.
    log_path.write_text(
        json.dumps(_make_event("m1", 100.0, 50.0))
        + "\n"
        + "{not valid json\n"
        + json.dumps(_make_event("m2", 110.0, 60.0))
        + "\n"
        + "\n"  # empty line
        + json.dumps(_make_event("m3", 120.0, 70.0))
        + "\n"
        + '{"event_type": "text_display"}\n'  # missing required keys
    )
    log = EventLog(path=str(log_path), max_entries=20)
    # Three valid events loaded (m1, m2, m3); the two bad lines skipped.
    assert len(log) == 3
    assert log.last_for("m1", "text_display") is not None
    assert log.last_for("m2", "text_display") is not None
    assert log.last_for("m3", "text_display") is not None


def test_file_with_only_corrupt_lines_yields_empty_cache(tmp_path):
    """All lines corrupt → empty cache, no exception."""
    log_path = tmp_path / "e.jsonl"
    log_path.write_text("{not json\n{still bad\n")
    log = EventLog(path=str(log_path), max_entries=10)
    assert len(log) == 0


# --- 6.5 Unit test: bounded ring — when the log has EVENT_LOG_MAX_ENTRIES entries and a new event is appended, the oldest entry is dropped and the on-disk file holds exactly N entries ---


def test_bounded_ring_drops_oldest_entry(tmp_path):
    """6.5: at-capacity append drops the oldest entry, on-disk file holds exactly N entries."""
    log_path = tmp_path / "e.jsonl"
    cap = 5
    log = EventLog(path=str(log_path), max_entries=cap)

    # Fill the log to capacity.
    for i in range(cap):
        log.append(_make_event(f"m{i}", 100.0 + i, 50.0 + i))
    assert len(log) == cap

    # Append one more — oldest (m0) should be dropped.
    log.append(_make_event("m5", 200.0, 150.0))
    assert len(log) == cap

    # On-disk file holds exactly N entries.
    with open(log_path, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) == cap

    # In-memory cache reflects the eviction: m0 is gone, m5 is the most recent.
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["message_id"] == "m1"
    assert parsed[-1]["message_id"] == "m5"

    # Query for the dropped message returns nothing.
    assert list(log.query(message_id="m0")) == []
    assert log.last_for("m5", "text_display") is not None


def test_bounded_ring_capacity_one(tmp_path):
    """Edge case: max_entries=1 retains only the most recent event."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=1)
    log.append(_make_event("m0", 100.0, 50.0))
    log.append(_make_event("m1", 110.0, 60.0))
    log.append(_make_event("m2", 120.0, 70.0))
    assert len(log) == 1
    assert log.last_for("m2", "text_display") is not None
    assert list(log.query(message_id="m0")) == []


def test_bounded_ring_construct_with_existing_oversize_log(tmp_path):
    """A log file larger than the cap is trimmed at construction time."""
    log_path = tmp_path / "e.jsonl"
    with open(log_path, "w", encoding="utf-8") as f:
        for i in range(10):
            f.write(json.dumps(_make_event(f"m{i}", 100.0 + i, 50.0 + i)) + "\n")

    # Construct with cap=3 — keeps the most recent 3 (m7, m8, m9).
    log = EventLog(path=str(log_path), max_entries=3)
    assert len(log) == 3
    remaining = [e["message_id"] for e in log.query()]
    assert remaining == ["m7", "m8", "m9"]


def test_bounded_ring_rewrite_is_atomic(tmp_path):
    """The on-disk file is consistent after eviction — no partial rewrite."""
    log_path = tmp_path / "e.jsonl"
    log = EventLog(path=str(log_path), max_entries=3)
    for i in range(10):
        log.append(_make_event(f"m{i}", 100.0 + i, 50.0 + i))

    # File should hold exactly 3 valid JSON lines, no temp residue.
    assert log_path.exists()
    with open(log_path, "r", encoding="utf-8") as f:
        raw = f.read()
    # No leftover .tmp files in the parent directory.
    tmp_residue = [p for p in tmp_path.iterdir() if p.name.startswith(".events.") and p.name.endswith(".jsonl.tmp")]
    assert tmp_residue == []

    # Every line parses as JSON.
    parsed = [json.loads(line) for line in raw.split("\n") if line.strip()]
    assert len(parsed) == 3
    assert all(set(e.keys()) == _REQUIRED_KEYS for e in parsed)


# --- 6.14 Unit test: log survives a restart — write events, instantiate a fresh EventLog, query returns the prior events ---


def test_log_survives_restart(tmp_path):
    """6.14: a fresh EventLog over the same path sees the previously-written events."""
    log_path = tmp_path / "e.jsonl"

    # Write events with one instance.
    first = EventLog(path=str(log_path), max_entries=10)
    first.append(_make_event("m1", 100.0, 50.0))
    first.append(_make_event("m2", 110.0, 60.0))
    first.append(_make_event("m3", 120.0, 70.0))

    # "Restart" — instantiate a fresh EventLog over the same path.
    second = EventLog(path=str(log_path), max_entries=10)
    assert len(second) == 3
    assert second.last_for("m1", "text_display") is not None
    assert second.last_for("m2", "text_display") is not None
    assert second.last_for("m3", "text_display") is not None


# --- 6.17 Unit test: event schema contains exactly {event_type, message_id, timestamp, received_at} — adding favorite fails the test ---


def test_event_schema_has_exactly_required_keys(tmp_path):
    """6.17: the event schema has exactly 4 keys — favorite must not be present."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=10)
    # Append with a `favorite` extra field — must be silently dropped.
    log.append(
        {
            "event_type": "text_display",
            "message_id": "m1",
            "timestamp": 100.0,
            "received_at": 50.0,
            "favorite": True,
        }
    )
    persisted = log.last_for("m1", "text_display")
    assert persisted is not None
    assert set(persisted.keys()) == {"event_type", "message_id", "timestamp", "received_at"}
    assert "favorite" not in persisted


def test_event_schema_rejects_event_missing_required_keys(tmp_path):
    """An event missing any required key is silently dropped (not stored)."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=10)
    log.append({"event_type": "text_display", "message_id": "m1", "timestamp": 100.0})  # missing received_at
    log.append({"message_id": "m1", "timestamp": 100.0, "received_at": 50.0})  # missing event_type
    log.append({"event_type": "text_display", "timestamp": 100.0, "received_at": 50.0})  # missing message_id
    assert len(log) == 0


def test_event_schema_rejects_non_dict_input(tmp_path):
    """Non-dict input (string, list, None) is silently dropped."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=10)
    log.append("not a dict")
    log.append([1, 2, 3])
    log.append(None)
    assert len(log) == 0


def test_required_keys_constant_is_locked():
    """The required-keys constant is the documented 4-key set, no favorite."""
    assert _REQUIRED_KEYS == frozenset({"event_type", "message_id", "timestamp", "received_at"})


# --- Misc defensive tests ---


def test_constructor_validates_max_entries():
    """max_entries < 1 raises ValueError at construction time."""
    with pytest.raises(ValueError):
        EventLog(path="data/events.jsonl", max_entries=0)
    with pytest.raises(ValueError):
        EventLog(path="data/events.jsonl", max_entries=-1)


def test_query_is_iterator(tmp_path):
    """query() returns an iterator (generator), not a list."""
    log = EventLog(path=str(tmp_path / "e.jsonl"), max_entries=10)
    log.append(_make_event("m1", 100.0, 50.0))
    result = log.query(message_id="m1")
    import types as _types

    assert isinstance(result, _types.GeneratorType)


def test_log_file_created_lazily_on_first_append(tmp_path):
    """A fresh EventLog does NOT create the file until the first append."""
    log_path = tmp_path / "subdir" / "e.jsonl"  # parent doesn't exist yet
    log = EventLog(path=str(log_path), max_entries=10)
    assert not log_path.exists()
    log.append(_make_event("m1", 100.0, 50.0))
    assert log_path.exists()
    assert log_path.parent.is_dir()


def test_reload_picks_up_external_changes(tmp_path):
    """reload() re-reads the on-disk file into the cache."""
    log_path = tmp_path / "e.jsonl"
    log = EventLog(path=str(log_path), max_entries=10)
    # Append via a separate EventLog instance — the in-memory cache
    # of the first instance is stale until reload().
    other = EventLog(path=str(log_path), max_entries=10)
    other.append(_make_event("external", 100.0, 50.0))

    assert log.last_for("external", "text_display") is None
    log.reload()
    assert log.last_for("external", "text_display") is not None
