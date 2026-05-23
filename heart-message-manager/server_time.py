"""Server-only time helpers. zoneinfo is available here but not on CircuitPython."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def to_utc_datetime(dt_iso: str) -> datetime:
    """Parse an ISO 8601 timestamp and return it as a UTC datetime.

    Examples:
        >>> to_utc_datetime("2026-05-22T14:30:00-07:00")
        datetime.datetime(2026, 5, 22, 21, 30, tzinfo=datetime.timezone.utc)
        
        >>> to_utc_datetime("2026-05-22T21:30:00Z")
        datetime.datetime(2026, 5, 22, 21, 30, tzinfo=datetime.timezone.utc)
    """
    dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def now_utc_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Example:
        >>> now_utc_iso()  # doctest: +SKIP
        '2026-05-22T21:30:00Z'
    """
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


def format_from_iso(
    dt_iso: str, tz_name: str = "UTC", format: str = "%b %d %I:%M %p"
) -> str:
    """Format an ISO 8601 timestamp in the given timezone.

    Args:
        dt_iso: ISO 8601 timestamp string.
        tz_name: IANA timezone name (default "UTC").
        format: strftime format string (default "%b %d %I:%M %p" -> "May 10 02:32 PM").
                Common alternatives:
                    "%Y-%m-%d %H:%M:%S %Z"  -> "2026-05-10 14:32:01 PST"

    Returns:
        Formatted timestamp string, or the original string on parse failure.
    """
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(tz_name))
        return local.strftime(format)
    except Exception:
        return dt_iso
