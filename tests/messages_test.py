"""Tests for lib_shared.messages — display-time formatting and message store.

Covers:
- _format_display_time: timezone-aware formatting via IANA names
- InMemoryMessages: ring buffer + dedup + filter enrichment
- The store reads timezone from the config (not tz_offset_mins)
"""

from lib_shared.messages import InMemoryMessages, _format_display_time
from lib_shared.models import (
    EffectsSettings,
    FilterRule,
    Message,
    SignConfig,
    TextSettings,
)

# --- _format_display_time ---


def test_format_display_time_uses_timezone_name():
    """_format_display_time formats the timestamp in the named IANA timezone."""
    out = _format_display_time("2026-05-22T14:30:00Z", "America/Los_Angeles")
    assert "2026" in out
    assert "May" in out or "05" in out
    # 14:30 UTC → 07:30 PDT in May (DST)
    assert "7:30" in out


def test_format_display_time_eastern():
    """Eastern timezone gets a different display than Pacific."""
    pacific = _format_display_time("2026-05-22T14:30:00Z", "America/Los_Angeles")
    eastern = _format_display_time("2026-05-22T14:30:00Z", "America/New_York")
    assert pacific != eastern
    # 14:30 UTC → 10:30 EDT in May
    assert "10:30" in eastern


def test_format_display_time_handles_dst_winter():
    """DST-aware: January in Pacific is PST (UTC-8), not PDT (UTC-7)."""
    out = _format_display_time("2026-01-15T14:30:00Z", "America/Los_Angeles")
    # 14:30 UTC → 06:30 PST
    assert "6:30" in out


def test_format_display_time_handles_dst_summer():
    """DST-aware: May in Pacific is PDT (UTC-7), not PST (UTC-8)."""
    out = _format_display_time("2026-05-15T14:30:00Z", "America/Los_Angeles")
    # 14:30 UTC → 07:30 PDT
    assert "7:30" in out


def test_format_display_time_utc():
    """UTC timezone formats without offset (12-hour format with AM/PM)."""
    out = _format_display_time("2026-05-22T14:30:00Z", "UTC")
    # 14:30 UTC → 2:30 PM in the 12-hour format the formatter uses.
    assert "2:30" in out.lower() or "14:30" in out


def test_format_display_time_unknown_timezone_falls_back():
    """An unknown timezone name falls back to US/Pacific, never raising."""
    out = _format_display_time("2026-05-22T14:30:00Z", "Mars/Olympus_Mons")
    # Should be the same as America/Los_Angeles.
    expected = _format_display_time("2026-05-22T14:30:00Z", "America/Los_Angeles")
    assert out == expected


def test_format_display_time_malformed_input_returns_input():
    """Malformed input is returned unchanged instead of raising."""
    out = _format_display_time("not a timestamp", "America/Los_Angeles")
    assert out == "not a timestamp"


def test_format_display_time_empty_string():
    """Empty input is returned as empty."""
    out = _format_display_time("", "America/Los_Angeles")
    assert out == ""


# --- InMemoryMessages timezone-driven display_time ---


def _make_config(timezone="America/Los_Angeles"):
    return SignConfig(
        effect_settings=EffectsSettings(),
        text_settings=TextSettings(),
        timezone=timezone,
    )


def test_in_memory_messages_enriches_display_time():
    """Each entry's display_time reflects the config's timezone, not tz_offset_mins."""
    cfg = _make_config(timezone="America/New_York")
    store = InMemoryMessages(cfg, maxlen=10)
    msg = Message(
        id="m1",
        sender="+1",
        body="hi",
        received_at="2026-05-22T14:30:00Z",
    )
    store.add(msg)
    # In production the manager drives enrichment at event time; in
    # this direct test we call it ourselves to populate display_time.
    store._enrich_messages(list(store._msgs))
    out = store.get_messages(limit=1)
    assert len(out) == 1
    # 14:30 UTC → 10:30 EDT
    assert "10:30" in out[0].display_time


def test_in_memory_messages_no_tz_offset_mins_attribute():
    """The config object no longer carries tz_offset_mins."""
    cfg = _make_config()
    assert not hasattr(cfg, "tz_offset_mins")


def test_in_memory_messages_dedupes_by_id():
    """Adding the same id twice keeps a single entry in the ring buffer."""
    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="dup", sender="+1", body="x", received_at="2026-01-01T00:00:00Z"))
    store.add(Message(id="dup", sender="+1", body="x", received_at="2026-01-01T00:00:00Z"))
    assert len(store.get_messages(limit=10)) == 1


def test_in_memory_messages_respects_maxlen():
    """The ring buffer drops the oldest entry when full."""
    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=3)
    for i in range(5):
        store.add(Message(id=f"m{i}", sender="+1", body=str(i), received_at=f"2026-01-0{i + 1}T00:00:00Z"))
    out = store.get_messages(limit=10)
    # Only the most recent 3 are kept.
    assert len(out) == 3
    ids = [e.message.id for e in out]
    assert "m2" in ids
    assert "m3" in ids
    assert "m4" in ids


def test_in_memory_messages_orders_newest_first():
    """get_messages returns entries sorted by received_at descending."""
    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="old", sender="+1", body="x", received_at="2026-01-01T00:00:00Z"))
    store.add(Message(id="new", sender="+1", body="y", received_at="2026-06-01T00:00:00Z"))
    out = store.get_messages(limit=10)
    assert out[0].message.id == "new"


def test_in_memory_messages_filters_applied():
    """get_messages applies filter rules from the config."""
    cfg = _make_config()
    cfg.filters.append(FilterRule(type="keyword", pattern="bad", action="suppress"))
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="ok", sender="+1", body="good message", received_at="2026-01-01T00:00:00Z"))
    store.add(Message(id="bad", sender="+1", body="this is bad news", received_at="2026-01-02T00:00:00Z"))
    # Enrich at event time (the manager does this in production).
    store._enrich_messages(list(store._msgs))
    out = store.get_messages(limit=10, suppress=True)
    assert len(out) == 1
    assert out[0].message.id == "ok"
    # With suppress=False, the suppressed one is included.
    out_all = store.get_messages(limit=10, suppress=False)
    assert len(out_all) == 2


def test_in_memory_messages_clear():
    """clear() empties the buffer and the seen-ids set."""
    cfg = _make_config()
    store = InMemoryMessages(cfg, maxlen=10)
    store.add(Message(id="x", sender="+1", body="x", received_at="2026-01-01T00:00:00Z"))
    store.clear()
    assert store.get_messages(limit=10) == []
    # Adding the same id again works after clear (the seen set is reset).
    store.add(Message(id="x", sender="+1", body="x", received_at="2026-01-01T00:00:00Z"))
    assert len(store.get_messages(limit=10)) == 1
