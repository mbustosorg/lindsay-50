"""Server-only time helpers. zoneinfo is available here but not on CircuitPython."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def to_utc_datetime(dt_iso: str) -> datetime:
    """Parse an ISO 8601 timestamp and return it as a UTC datetime."""
    dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def now_utc_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tz_offset_mins(tz_name: str) -> int:
    """Compute the UTC offset in minutes for a timezone, including DST.

    Returns 0 as a fallback if the timezone is unknown or unavailable.
    """
    try:
        now = datetime.now(ZoneInfo(tz_name))
        offset = ZoneInfo(tz_name).utcoffset(now)
        return int(offset.total_seconds() / 60) if offset else 0
    except Exception:
        return 0


def format_timestamp_display(dt_iso: str, tz_name: str = "US/Pacific") -> str:
    """Format an ISO 8601 timestamp for display in the local timezone.

    Returns e.g. "2026-05-10 14:32:01 PST".
    """
    dt_utc = to_utc_datetime(dt_iso)
    dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
    return dt_local.strftime("%Y-%m-%d %H:%M:%S %Z")


def format_from_iso(dt_iso: str, tz_name: str = "UTC") -> str:
    """Format an ISO 8601 timestamp for display in the given timezone.

    Returns e.g. "May 10 02:32 PM".
    """
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(tz_name))
        return local.strftime("%b %d %I:%M %p")
    except Exception:
        return dt_iso
