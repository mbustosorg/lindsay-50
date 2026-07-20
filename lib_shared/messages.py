"""In-memory message service shared by the Flask app and the Raspberry Pi.

Classes:
    FilteredMessages: Abstract base; applies filter rules and sender-name resolution.
    InMemoryMessages: Ring-buffer implementation with O(1) deduplication.
"""

import re
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib_shared.models import MessageView, SignConfig
from lib_shared.name_utils import format_display_name, parse_name
from lib_shared.phone_utils import normalize_phone


def should_render_sender(
    sender: object,
    senders: dict,
    enforce_allowed_senders: bool = True,
) -> bool:
    """Decide whether a message from `sender` should render.

    Issue #6 / implement-senders-filtering. The decision is a pure
    function of three inputs:

      - `sender` — the incoming sender string (any common format).
      - `senders` — the allowlist dict (`cfg.senders`):
        `dict[str, dict]` mapping normalized phone to
        `{"name", "allowed", "phone"}`.
      - `enforce_allowed_senders` — the master toggle
        (cfg.sign_settings.enforce_allowed_senders).

    The decision rule:

      1. `not enforce_allowed_senders` → True (master toggle off; every
         sender renders, regardless of per-entry state — names still
         resolve for display).
      2. sender NOT in `senders` → False (allowlist mode is exclusive
         when enforcement is on; unlisted = blocked).
      3. sender in `senders` with `allowed=True` → True.
      4. sender in `senders` with `allowed=False` → False.

    Args:
        sender: The incoming sender string (any format — gets normalized
            via `phone_utils.normalize_phone` before lookup).
        senders: The `cfg.senders` dict (mapping normalized phone → value
            dict with `"allowed"` field).
        enforce_allowed_senders: The master enforcement toggle (default True).

    Returns:
        True iff the message should render; False if the sender list
        decided to suppress it.
    """
    if not enforce_allowed_senders:
        return True
    if not isinstance(sender, str):
        return False
    normalized = normalize_phone(sender)
    entry = senders.get(normalized) if isinstance(senders, dict) else None
    if entry is None:
        return False
    return bool(entry.get("allowed", False))


def _format_display_time(received_at: str, timezone: str) -> str:
    """Format a UTC ISO timestamp for display in the sign's configured timezone.

    The offset is computed at read-time via ``zoneinfo.ZoneInfo`` from the
    IANA ``timezone`` string on the config (DST-aware). On an unknown /
    invalid timezone the function falls back to ``US/Pacific`` so a bad
    config value never raises.

    Args:
        received_at: UTC ISO 8601 timestamp, e.g. ``"2026-05-22T14:30:00Z"``.
        timezone:    IANA timezone name, e.g. ``"America/Los_Angeles"``.

    Returns:
        Formatted string, e.g. ``"2026-05-22 10:30 AM"``, or the original
        string on parse failure.
    """
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("US/Pacific")
    try:
        utc_dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime("%Y-%m-%d %I:%M %p %Z").lower()
    except Exception:
        return received_at


class FilteredMessages:
    """Abstract in-memory message store with filter-rule support.

    Subclasses must implement:
      - add(message, source="rest")
      - add_many(messages, source="rest")
      - clear()
      - get_messages(limit=100) -> list[MessageView]

    The base class provides _apply_filter(), _matches(), and _enrich_messages()
    for use by subclasses. Thread-safety is the caller's responsibility.
    """

    def __init__(self, config: SignConfig) -> None:
        """Initialize with a SignConfig (provides .filters and .senders)."""
        self._config = config

    def _apply_filter(self, msg, rules):
        """Apply filter rules to a message.

        Disabled rules (status="disabled") are treated as absent and
        do NOT contribute to the suppressing list — they're the
        "disable it vs. delete it" affordance the issue asks for.

        Args:
            msg:   Message object
            rules: list of FilterRule objects

        Returns:
            List of FilterRule objects that suppress the message (in evaluation order).
        """
        suppressing = []
        for rule in rules:
            if getattr(rule, "status", "enabled") != "enabled":
                # Disabled rules are skipped (per-rule lifecycle — the
                # operator can mute without losing the rule).
                continue
            if rule.action != "suppress":
                continue
            if self._matches(msg, rule):
                suppressing.append(rule)
        return suppressing

    def _matches(self, msg, rule):
        """Return True if message matches the filter rule.

        v3 (issue #6): no `type == "sender"` branch — sender matching
        moved to `cfg.senders` (per-entry `allowed` + master
        `sign_settings.enforce_allowed_senders` toggle). A rule whose
        `type` is `"sender"` will fall through to `False` here; the
        `_v2_to_v3` migration converts such rules into senders list
        entries before they reach this code.
        """
        if rule.type == "keyword":
            return rule.pattern.lower() in msg.body.lower()
        elif rule.type == "regex":
            try:
                return bool(re.fullmatch(rule.pattern, msg.body))
            except re.error:
                return False
        elif rule.type == "message":
            return msg.id == rule.pattern
        return False

    def _enrich_messages(self, entries):
        """Enrich each MessageView entry with suppressed, rules, sender_name, and display_time.

        Called by `MessageManager._handle_message` (single-entry) and
        `_handle_config` (whole-list re-enrich) at event time. Reads
        (`get_messages`) do not call this — the derived fields are already
        populated on the buffered views. Override to customize.

        v3 (issue #6): after the FilterRule pass, consults
        `should_render_sender` to apply the master `enforce_allowed_senders`
        toggle + per-entry `allowed` flag. When the senders list
        suppresses a message AND no FilterRule matched, appends a
        synthetic `sender_action` rule to `entry.rules` so the admin
        UI can render a "Suppressed by sender action" badge. When
        `enforce_allowed_senders` is False, no suppression decision is
        made (the master toggle bypasses the filter); no synthetic
        marker is added.

        The display-name resolution works regardless of `allowed` —
        the operator sees "From: Alice" (or whatever the format
        produces) for disallowed senders in the admin UI.
        """
        timezone = self._config.sign_settings.timezone
        enforce_allowed_senders = bool(self._config.sign_settings.enforce_allowed_senders)
        senders = self._config.senders
        name_format = self._config.text_settings.name_display_format
        # Precompute `all_first_names` once per call — stable across
        # the buffer's messages. Empty/missing names contribute an
        # empty string to the list (no name to format either way).
        all_first_names = [(parse_name((entry or {}).get("name", ""))[0]) for entry in senders.values()]

        for entry in entries:
            suppressing = self._apply_filter(entry.message, self._config.filters)
            rule_dicts = [r.to_dict() for r in suppressing]
            sender = entry.message.sender
            # Display name: lookup the normalized entry's stored name and
            # apply the configured format. Works regardless of `allowed` —
            # even a disallowed sender's name resolves for display.
            sender_entry = senders.get(normalize_phone(sender)) if isinstance(sender, str) else None
            stored_name = (sender_entry or {}).get("name", "") if sender_entry else ""
            entry.sender_name = format_display_name(stored_name, name_format, all_first_names)

            # Apply senders list decision (master toggle + per-entry allowed).
            sender_passes = should_render_sender(sender, senders, enforce_allowed_senders)
            if not sender_passes:
                entry.suppressed = True
                if not suppressing:
                    # No FilterRule matched — add the synthetic marker so
                    # the admin UI can render "Suppressed by sender action".
                    entry.rules = rule_dicts + [
                        {
                            "type": "sender_action",
                            "pattern": normalize_phone(sender) if isinstance(sender, str) else "",
                            "action": "suppress",
                        }
                    ]
                # If a FilterRule already matched, leave entry.rules alone
                # (the real rule wins for display — no synthetic marker
                # when a real rule already accounted for the suppression).
                else:
                    entry.rules = rule_dicts
            else:
                entry.suppressed = bool(suppressing)
                entry.rules = rule_dicts
            entry.display_time = _format_display_time(entry.message.received_at, timezone)

    def add(self, message, source="rest"):
        """Add a single message to the store."""
        raise NotImplementedError()

    def add_many(self, messages, source="rest"):
        """Add multiple messages in order."""
        raise NotImplementedError()

    def clear(self):
        """Clear all messages from the store."""
        raise NotImplementedError()

    def get_messages(self, limit=100, suppress=True):
        """Return MessageView entries, newest first.

        Args:
            limit: Maximum number of messages to return.
            suppress: If True (default), excluded suppressed messages from the result.
        """
        raise NotImplementedError()


class InMemoryMessages(FilteredMessages):
    """In-memory ring buffer with O(1) deduplication.

    Uses a deque for the ring buffer and a set for fast seen-id lookup.
    Duplicates are dropped silently on add().
    """

    def __init__(self, config: SignConfig, maxlen: int = 100) -> None:
        """Initialize with a config and optional ring-buffer max length.

        Args:
            config: SignConfig instance providing .filters and .senders.
            maxlen: Maximum number of messages to retain (default 100).
        """
        super().__init__(config)
        self._msgs: deque = deque(maxlen=maxlen)
        self._seen_ids: set = set()

    def add(self, message, source="rest") -> MessageView | None:
        """Add a single message. Skips silently if id already seen (O(1) check).

        Returns the appended `MessageView`, or `None` if the message was
        a duplicate. Enrichment of the returned view is the caller's
        responsibility — see `MessageManager._handle_message`
        (single-entry) and `_handle_config` (whole-list re-enrich).
        Keeping enrichment out of `add()` avoids a second pass during
        batch hydrates like `add_many` / cache seed.
        """
        if message.id in self._seen_ids:
            return None
        self._seen_ids.add(message.id)
        view = MessageView(message, source=source)
        self._msgs.append(view)
        return view

    def add_many(self, messages, source="rest") -> None:
        """Add multiple messages in insertion order. Skips duplicates."""
        for msg in messages:
            self.add(msg, source)

    def clear(self) -> None:
        """Clear all messages and the seen-id set."""
        self._msgs.clear()
        self._seen_ids.clear()

    def get_messages(self, limit: int | None = 100, suppress: bool = True) -> list[MessageView]:
        """Return the most recent N messages, newest first (sorted by received_at desc).

        Thin read: returns the already-enriched `MessageView` instances from
        the ring buffer, sorted, and optionally filtered by the precomputed
        `suppressed` flag. Does NOT call `_apply_filter`, `_matches`, or
        `_format_display_time` — enrichment happens on the event that
        mutates the inputs (a new message arriving or a config change), not
        on every read.

        Args:
            limit: Maximum number of messages to return (default 100).
            suppress: If True (default), exclude suppressed messages from the result.
        """
        entries = list(self._msgs)
        if suppress:
            entries = [e for e in entries if not e.suppressed]
        return sorted(entries, key=lambda e: e.message.received_at, reverse=True)[:limit]
