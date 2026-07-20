"""Plain Python models shared between the Flask app and the Raspberry Pi display."""

import json
import logging
import threading
from typing import List, Optional

log = logging.getLogger("heart")


class MessageEnvelope:
    """JSON envelope for the unified MQTT feed.

    Attributes:
        type:    "message" | "config"
        payload: dict — Message.to_dict() or SignConfig.to_dict()
    """

    def __init__(self, type: str, payload: dict) -> None:
        """Initialize a MessageEnvelope.

        Args:
            type: Envelope type — "message" or "config".
            payload: Dict payload — Message.to_dict() or SignConfig.to_dict().
        """
        self.type = type
        self.payload = payload

    @classmethod
    def from_json(cls, raw: str) -> "MessageEnvelope":
        d = json.loads(raw)
        return cls(type=d["type"], payload=d["payload"])

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))


class Message:
    """Represents an incoming SMS message.

    Stored to S3 as JSON and published over MQTT as part of a MessageEnvelope.

    `media` carries the optional list of MMS attachments that landed alongside
    the text. Each entry is `{"type": str, "url": str}` — `type` is the MIME
    type (e.g. ``"image/jpeg"``) and `url` is the S3 key under our bucket
    (e.g. ``"media/images/2026-07/media-2026-07-09T15-30-00Z.jpg"``). SMS-only
    messages carry ``media == []``. S3 keys are the wire format
    (design D2) — never a Twilio MediaUrl, never a pre-signed URL. The 1-hour
    signed URL is regenerated on every Flask 302 (see
    ``GET /api/media/<path:key>``).
    """

    def __init__(
        self,
        id: str,
        sender: str,
        body: str,
        received_at: str,
        media: Optional[list[dict]] = None,
    ) -> None:
        """Initialize a Message.

        Args:
            id: Unique message identifier (UUID string).
            sender: Phone number of the sender.
            body: Text content of the message.
            received_at: ISO 8601 UTC timestamp when received.
            media: Optional list of ``{"type": str, "url": str}`` entries
                representing MMS attachments already copied to OUR S3.
                Defaults to an empty list (legacy 4-field wire shape
                round-trips unchanged).
        """
        self.id = id
        self.sender = sender
        self.body = body
        self.received_at = received_at
        # `media` MUST always round-trip through to_dict/from_dict with the
        # exact list the caller passes in — empty for SMS, populated for MMS.
        # Defensive copy via list(...) keeps mutating `d["media"]` after
        # construction from leaking into self.media.
        self.media: list[dict] = list(media) if media is not None else []

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            sender=d["sender"],
            body=d["body"],
            received_at=d["received_at"],
            media=d.get("media", []),
        )

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender,
            "body": self.body,
            "received_at": self.received_at,
            "media": self.media,
        }

    def received_at_epoch(self) -> float:
        """Return the message's `received_at` as epoch seconds (float).

        Used by the message selector (issue #26) to compute the
        eligibility filter (via `build_eligible_messages`'s
        `lookback_seconds` cutoff) and the send-recency normalization.
        The Message dataclass stores
        `received_at` as an ISO 8601 UTC string; this method is the
        canonical conversion to epoch seconds for selectors and event-log
        writes (the event schema carries `received_at` as a float, see
        `heart-matrix-controller/event_log.py`).

        Returns 0.0 on parse failure so a malformed `received_at`
        causes the message to be filtered out of the eligible set
        (the rotation pauses on None rather than crashing). Returns
        a non-negative float on success.
        """
        raw = self.received_at
        if not raw or not isinstance(raw, str):
            return 0.0
        try:
            # `fromisoformat` accepts the trailing "Z" only on 3.11+;
            # normalize for 3.10 (the project's pinned runtime is 3.12
            # per `.python-version`, but tests may run elsewhere).
            normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            from datetime import datetime

            dt = datetime.fromisoformat(normalized)
            return dt.timestamp()
        except (ValueError, TypeError, ImportError):
            return 0.0


class MessageView:
    """Message with source and computed suppression status."""

    def __init__(
        self,
        message: "Message",
        source: str = "rest",
        suppressed: bool = False,
        rules: list["FilterRule"] | None = None,
        sender_name: str = "",
        display_time: str | None = None,
        media: list | None = None,
    ) -> None:
        """Initialize a MessageView.

        Args:
            message: Message object this view wraps.
            source: "rest" when loaded from storage, "mqtt" when received live.
            suppressed: True if any filter rule matched this message.
            rules: List of FilterRule objects that suppressed the message.
            sender_name: Display name for the sender (from the senders allowlist).
            display_time: Pre-formatted local time string, or None (set by _enrich_messages).
            media: Optional MMS attachments list. Defaults to
                ``message.media`` if not supplied — surface it as a
                flat top-level attribute so the JS-side Pyodide
                proxy exposes ``entry.media`` alongside ``source`` /
                ``display_time``. Without this, the testing page's
                modal (which ``JSON.stringify``s the entry) sees
                ``media`` nested under ``entry.message.media`` and
                the inline row click / the modal popup disagree.
        """
        self.message = message
        self.source = source
        self.suppressed = suppressed
        self.rules = list(rules) if rules is not None else []
        self.sender_name = sender_name
        self.display_time = display_time
        # Mirror the wrapped Message's `media` so it's a flat field on
        # the view — JS-side Pyodide proxies only expose instance
        # attributes set in __init__, not @property accessors, so this
        # has to be a real attribute for `item.media` and
        # `JSON.stringify(item).media` to work on the testing page.
        self.media = list(media) if media is not None else list(message.media)

    def to_dict(self):
        return {
            "id": self.message.id,
            "sender": self.message.sender,
            "body": self.message.body,
            "received_at": self.message.received_at,
            "media": self.message.media,
            "source": self.source,
            "suppressed": self.suppressed,
            "rules": self.rules,
            "sender_name": self.sender_name,
            "display_time": self.display_time,
        }


class FilterRule:
    """A single filter rule that can suppress messages.

    v3 taxonomy (issue #6 / implement-senders-filtering):
      - `type` is restricted to `"keyword"`, `"regex"`, or `"message"`.
        The `"sender"` type is REMOVED from the wire — sender matching
        is the `SignConfig.senders` list's job (see the senders-status
        spec). `from_dict` raises `ValueError` on `type="sender"` or
        any other unrecognized value.
      - `action` is restricted to `"suppress"` (the only v1 action).
        `from_dict` raises `ValueError` on any other value.
      - `status` is the per-RULE lifecycle (`"enabled"` | `"disabled"`).
        A rule with `status="disabled"` is treated as absent at apply
        time (see `FilteredMessages._apply_filter`). `from_dict`
        accepts a missing `status` key as a back-compat default of
        `"enabled"` so legacy v2 payloads still load.

    Attributes:
        type: Rule type — "keyword", "regex", or "message".
        pattern: The value to match against (case-sensitive except for keyword).
        action: Always "suppress" in practice (other values rejected at from_dict).
        status: Per-rule lifecycle — "enabled" (default) or "disabled".
    """

    VALID_TYPES = ("keyword", "regex", "message")
    VALID_ACTIONS = ("suppress",)
    VALID_STATUSES = ("enabled", "disabled")
    DEFAULT_STATUS = "enabled"

    def __init__(
        self,
        type: str,
        pattern: str,
        action: str = "suppress",
        status: str = DEFAULT_STATUS,
    ) -> None:
        """Initialize a FilterRule.

        Args:
            type: Rule type — "keyword", "regex", or "message".
            pattern: Value to match against.
            action: Action to take when matched (default "suppress"; other values rejected at from_dict).
            status: "enabled" or "disabled" (default "enabled").

        Raises:
            ValueError: on an unknown type, action, or status value.
        """
        if type not in FilterRule.VALID_TYPES:
            raise ValueError(f"FilterRule.type must be one of {FilterRule.VALID_TYPES}, got {type!r}")
        if action not in FilterRule.VALID_ACTIONS:
            raise ValueError(f"FilterRule.action must be one of {FilterRule.VALID_ACTIONS}, got {action!r}")
        if status not in FilterRule.VALID_STATUSES:
            raise ValueError(f"FilterRule.status must be one of {FilterRule.VALID_STATUSES}, got {status!r}")
        self.type = type
        self.pattern = pattern
        self.action = action
        self.status = status

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with required keys: type, pattern. Optional: action (default
                "suppress"), status (default "enabled").

        Returns:
            A new FilterRule instance.

        Raises:
            ValueError: on an unknown type, action, or status value. Notably,
                `type="sender"` raises (the type is REMOVED from the wire).
        """
        if not isinstance(d, dict):
            raise ValueError(f"FilterRule.from_dict requires a dict, got {type(d).__name__}")
        ftype = d.get("type")
        if ftype not in cls.VALID_TYPES:
            raise ValueError(f"FilterRule.type must be one of {cls.VALID_TYPES}, got {ftype!r}")
        action = d.get("action", "suppress")
        if action not in cls.VALID_ACTIONS:
            raise ValueError(f"FilterRule.action must be one of {cls.VALID_ACTIONS}, got {action!r}")
        status = d.get("status", cls.DEFAULT_STATUS)
        if status not in cls.VALID_STATUSES:
            raise ValueError(f"FilterRule.status must be one of {cls.VALID_STATUSES}, got {status!r}")
        return cls(type=ftype, pattern=d.get("pattern", ""), action=action, status=status)

    def to_dict(self):
        return {"type": self.type, "pattern": self.pattern, "action": self.action, "status": self.status}


class SignSettings:
    """Sign configuration: identity (sign_name), operational metadata (timezone),
    and the senders-allowlist master toggle.

    v3 layout (issue #6 / implement-senders-filtering):
      - `name` was renamed to `sign_name` (matches the HTML form
        field name and disambiguates "the sign's name" from generic
        "name" in the context of `SignConfig`).
      - `timezone` moved from a top-level field on `SignConfig`
        into this nested block.
      - `enforce_allowed_senders` (formerly `text_settings.enforcement_enabled`)
        is the master toggle for the senders allowlist filter. Lives here —
        alongside `sign_name` / `timezone` — because it's a sign-level policy
        knob (per-deployment), not a presentation knob (which live with the
        text/effects blocks).

    The block is renamed `SignSettings` → `SignConfig.sign_settings`
    (was `SignConfig.sign`) to match the `effects_settings` /
    `text_settings` naming convention.

    Attributes:
        sign_name: Display name shown on the sign (default "Lindsay's Heart").
        timezone: IANA timezone string (default "US/Pacific").
        enforce_allowed_senders: Master toggle for the senders allowlist
            filter. True (default) means `cfg.senders` governs which
            senders render; False bypasses the filter entirely (every
            message renders, display names still resolve).
    """

    DEFAULT_SIGN_NAME = "Lindsay's Heart"
    DEFAULT_TIMEZONE = "US/Pacific"
    DEFAULT_ENFORCE_ALLOWED_SENDERS = True

    def __init__(
        self,
        sign_name: str = DEFAULT_SIGN_NAME,
        timezone: str = DEFAULT_TIMEZONE,
        enforce_allowed_senders: bool = DEFAULT_ENFORCE_ALLOWED_SENDERS,
    ) -> None:
        """Initialize SignSettings.

        Args:
            sign_name: Display name shown on the sign (default "Lindsay's Heart").
            timezone: IANA timezone string (default "US/Pacific").
            enforce_allowed_senders: Master toggle for the senders
                allowlist filter (default True).
        """
        self.sign_name = sign_name
        self.timezone = timezone
        self.enforce_allowed_senders = enforce_allowed_senders

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: sign_name (default "Lindsay's Heart"),
                timezone (default "US/Pacific"),
                enforce_allowed_senders (default True). May also be `None`.

        Returns:
            A new SignSettings instance.
        """
        if d is None:
            return cls()
        enforce = d.get("enforce_allowed_senders", cls.DEFAULT_ENFORCE_ALLOWED_SENDERS)
        if not isinstance(enforce, bool):
            raise ValueError(f"enforce_allowed_senders must be a bool, got {type(enforce).__name__}")
        return cls(
            sign_name=d.get("sign_name", cls.DEFAULT_SIGN_NAME),
            timezone=d.get("timezone", cls.DEFAULT_TIMEZONE),
            enforce_allowed_senders=enforce,
        )

    def to_dict(self):
        return {
            "sign_name": self.sign_name,
            "timezone": self.timezone,
            "enforce_allowed_senders": self.enforce_allowed_senders,
        }


# ---------------------------------------------------------------------------
# EffectsSettings + TextSettings — v2 config blocks
# ---------------------------------------------------------------------------


def _default_effects_list() -> List[dict]:
    """Return the canonical effects list (loaded via the loader).

    Reads `lib_shared.effects_loader.load_effects_settings()["effects"]`
    on first call and strips the loader-only `module` / `class_name`
    fields (the dataclass only needs `name` and `enabled`). Caches the
    result on the function attribute so subsequent calls are cheap.

    Imports are deferred to the function body so the module-level
    import of `models.py` doesn't pull in the loader at the wrong
    moment (the loader module imports `os`, `pathlib`, etc., and we
    want `models.py` to remain importable in any environment).
    """
    cached = getattr(_default_effects_list, "_cache", None)
    if cached is not None:
        return cached
    from lib_shared.effects_loader import load_effects_settings

    entries = load_effects_settings().get("effects", [])
    cleaned = [{"name": e["name"], "enabled": e["enabled"]} for e in entries]
    _default_effects_list._cache = cleaned  # type: ignore[attr-defined]
    return cleaned


class EffectsSettings:
    """Effects subsystem config: rotation list + pacing + selector knobs.

    Groups every input the `EffectsCoordinator` consumes so the coordinator
    takes one focused argument instead of the full SignConfig.
    """

    # Selector registry mirrors `lib_shared.selector.VALID_SELECTOR_ALGORITHMS`.
    # Re-exported here so `EffectsSettings.validate` can sanity-check the
    # wire value without crossing the lib_shared selector module boundary.
    # New algorithms require updating both lists.
    VALID_SELECTOR_ALGORITHMS = ("weighted", "random")
    DEFAULT_SELECTOR_ALGORITHM = "weighted"

    # Eligibility-window bounds. The admin UI surfaces `lookback_days`
    # directly; the lower bound (1) keeps the operator from accidentally
    # filtering every message out, and the upper bound (365) caps the
    # effective window at the ring-buffer's natural `maxlen=100` —
    # anything larger is a config smell (no messages survive that long).
    MIN_LOOKBACK_DAYS = 1
    MAX_LOOKBACK_DAYS = 365
    DEFAULT_LOOKBACK_DAYS = 14

    def __init__(
        self,
        effects: Optional[List[dict]] = None,
        fade_seconds: Optional[float] = None,
        hold_seconds: Optional[float] = None,
        intro_seconds: Optional[float] = None,
        idle_seconds: Optional[float] = None,
        lookback_days: Optional[int] = None,
        selector_algorithm: Optional[str] = None,
    ):
        """Initialize EffectsSettings.

        Args:
            effects: List of `{"name": str, "enabled": bool}` dicts. Defaults
                to the canonical list loaded via `lib_shared.effects_loader`.
            fade_seconds: Seconds for one full fade. Default `None` falls
                through to the loader's value (canonical or operator
                override) so an `effects_settings` override honors its
                own pacing.
            hold_seconds: Seconds to keep a message fully visible. Default
                `None` falls through to the loader.
            intro_seconds: Seconds to show the boot-splash heart. Default
                `None` falls through to the loader.
            idle_seconds: Seconds of idleness before a random message
                plays. Default `None` falls through to the loader.
            lookback_days: Eligibility window (days) for the candidate
                pool — messages older than `now - lookback_days` are
                filtered out of the eligible set before any selector
                runs. Default `None` falls through to the loader.
                Bounds: 1..365. Shared by every selection algorithm.
            selector_algorithm: Which `MessageSelector` subclass the
                coordinator dispatches to via `make_selector(...)`.
                One of `VALID_SELECTOR_ALGORITHMS`. Default
                `DEFAULT_SELECTOR_ALGORITHM` ("weighted").

        The loader-driven defaults are what make an operator's override
        file (env var or `config_overrides/effects_settings.json`) take
        effect even when the device boots without any wire envelope —
        the device constructs `EffectsSettings()` directly and the
        pacing values come from the same source the loader writes.
        """
        # Read the loader output once so the override (env var or
        # config_overrides/) and canonical fall through identically for
        # the effects list and every pacing field. The same source-of-
        # truth path that `_default_effects_list` already uses.
        from lib_shared.effects_loader import load_effects_settings

        loader_cfg = load_effects_settings()

        self.effects = list(effects) if effects is not None else [dict(e) for e in _default_effects_list()]
        self.fade_seconds = fade_seconds if fade_seconds is not None else loader_cfg.get("fade_seconds", 2.0)
        self.hold_seconds = hold_seconds if hold_seconds is not None else loader_cfg.get("hold_seconds", 15.0)
        self.intro_seconds = intro_seconds if intro_seconds is not None else loader_cfg.get("intro_seconds", 5.0)
        self.idle_seconds = idle_seconds if idle_seconds is not None else loader_cfg.get("idle_seconds", 300.0)
        self.lookback_days = (
            lookback_days if lookback_days is not None else loader_cfg.get("lookback_days", self.DEFAULT_LOOKBACK_DAYS)
        )
        self.selector_algorithm = (
            selector_algorithm
            if selector_algorithm is not None
            else loader_cfg.get("selector_algorithm", self.DEFAULT_SELECTOR_ALGORITHM)
        )

    @property
    def lookback_seconds(self) -> float:
        """The eligibility window expressed in seconds (for arithmetic).

        `lookback_days` is the operator-facing knob; the selector code
        operates on epoch-seconds offsets. Multiply through this
        property rather than inlining the constant.
        """
        return float(self.lookback_days) * 86_400.0

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: effects, fade_seconds, hold_seconds,
                intro_seconds, idle_seconds, lookback_days, selector_algorithm.

        Returns:
            A new EffectsSettings instance.

        Raises:
            ValueError: on a malformed effects list (entries missing name/enabled,
                or non-dict entries). The Flask validation layer is responsible
                for rejecting unknown effect names; this method accepts any
                name string.
        """
        d = d or {}
        effects = d.get("effects", _default_effects_list())
        if not isinstance(effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")

        # Pacing fields are passed as `None` when absent so `__init__`
        # can fall through to the loader (canonical or operator
        # override). This way an empty wire envelope (`{}`) on a device
        # that boots with no config sync still gets the operator's
        # pacing values from the override file. When the field IS
        # present, it gets type-coerced here (str→float, str→int) so
        # the wire layer can tolerate JSON strings from older clients
        # while the explicit-value path in `__init__` gets a typed
        # value.
        def _f(key: str) -> Optional[float]:
            v = d.get(key)
            return None if v is None else float(v)

        def _i(key: str) -> Optional[int]:
            v = d.get(key)
            return None if v is None else int(v)

        return cls(
            effects=[{"name": n["name"], "enabled": n["enabled"]} for n in effects],
            fade_seconds=_f("fade_seconds"),
            hold_seconds=_f("hold_seconds"),
            intro_seconds=_f("intro_seconds"),
            idle_seconds=_f("idle_seconds"),
            lookback_days=_i("lookback_days"),
            selector_algorithm=d.get("selector_algorithm"),
        )

    def to_dict(self):
        """Serialize to a dict (wire shape)."""
        return {
            "effects": self.effects,
            "fade_seconds": self.fade_seconds,
            "hold_seconds": self.hold_seconds,
            "intro_seconds": self.intro_seconds,
            "idle_seconds": self.idle_seconds,
            "lookback_days": self.lookback_days,
            "selector_algorithm": self.selector_algorithm,
        }

    def validate(self):
        """Raise ValueError on out-of-range values.

        Raises:
            ValueError: on negative pacing durations, lookback_days
                outside [MIN_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS], an unknown
                selector_algorithm, or a malformed effects list.
        """
        if self.fade_seconds < 0 or self.hold_seconds < 0 or self.intro_seconds < 0 or self.idle_seconds < 0:
            raise ValueError("pacing durations must be non-negative")
        if not isinstance(self.lookback_days, int) or isinstance(self.lookback_days, bool):
            raise ValueError(f"lookback_days must be an integer, got {type(self.lookback_days).__name__}")
        if not (self.MIN_LOOKBACK_DAYS <= self.lookback_days <= self.MAX_LOOKBACK_DAYS):
            raise ValueError(
                f"lookback_days must be in {self.MIN_LOOKBACK_DAYS}..{self.MAX_LOOKBACK_DAYS}, "
                f"got {self.lookback_days}"
            )
        if self.selector_algorithm not in self.VALID_SELECTOR_ALGORITHMS:
            raise ValueError(
                f"selector_algorithm must be one of {self.VALID_SELECTOR_ALGORITHMS}, "
                f"got {self.selector_algorithm!r}"
            )
        if not isinstance(self.effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in self.effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")


class TextSettings:
    """Text rendering config: scroll speed, color, text_effect, name format.

    `speed` is the user-facing knob (1=Low to 5=High). The underlying
    `frame_delay` / `offset_seconds` are derived from it by the scroller
    (see `ScrollerBase.SPEED_TABLE`). The wire shape stores `speed` only
    — the technical pacing values are device-local.

    Named "text_settings" (not "scroller_settings") because the scroller
    is just one text effect — future text effects (swirl, bounce) will
    share the same block. The `name_display_format` field lives here
    alongside `color` / `text_effect` because it governs how sender names
    render (a presentation knob — pairs with the text rendering knobs).
    The senders allowlist master toggle is a sign-level policy gate (not
    a presentation knob) and lives in `sign_settings.enforce_allowed_senders`.
    """

    # v1 supports "scroll" only; more values land as future text effects.
    TEXT_EFFECTS: tuple = ("scroll",)
    MIN_SPEED = 1
    MAX_SPEED = 5
    DEFAULT_SPEED = 3

    # Name display format (issue #6 / implement-senders-filtering).
    # Governs how `MessageView.sender_name` is computed from the stored
    # `name` field. Lives here (next to `color` / `text_effect`) because
    # it's a presentation knob — display-format knobs group with other
    # presentation knobs. See `lib_shared/name_utils.format_display_name`
    # for the per-format semantics.
    VALID_NAME_DISPLAY_FORMATS = (
        "full",
        "first_initial",
        "first",
        "first_initial_if_duplicates",
    )
    DEFAULT_NAME_DISPLAY_FORMAT = "first_initial_if_duplicates"

    def __init__(
        self,
        speed: int = DEFAULT_SPEED,
        color: int = 0xFF0000,
        text_effect: str = "scroll",
        name_display_format: Optional[str] = None,
    ):
        """Initialize TextSettings.

        Args:
            speed: 1..5 scroll speed (1=Low, 3=Medium default, 5=High).
            color: 24-bit RGB color value (default 0xFF0000 red).
            text_effect: One of TEXT_EFFECTS (currently "scroll").
            name_display_format: One of VALID_NAME_DISPLAY_FORMATS.
                Default `None` falls through to DEFAULT_NAME_DISPLAY_FORMAT.
        """
        self.speed = speed
        self.color = color
        self.text_effect = text_effect
        self.name_display_format = (
            name_display_format if name_display_format is not None else self.DEFAULT_NAME_DISPLAY_FORMAT
        )

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: speed, color, text_effect,
                name_display_format. Legacy `frame_delay` / `offset_seconds`
                keys are silently ignored — the new defaults are sensible
                and the user said v2 payloads are disposable.

        Returns:
            A new TextSettings instance.

        Raises:
            ValueError: on an unknown text_effect or name_display_format,
                or on an out-of-range or non-integer `speed` (callers
                like the admin validation helper catch and translate
                to a 400).
        """
        d = d or {}
        text_effect = d.get("text_effect", "scroll")
        if text_effect not in cls.TEXT_EFFECTS:
            raise ValueError(f"text_effect must be one of {cls.TEXT_EFFECTS}, got {text_effect!r}")
        speed = d.get("speed", cls.DEFAULT_SPEED)
        if isinstance(speed, bool) or not isinstance(speed, int) or not cls.MIN_SPEED <= speed <= cls.MAX_SPEED:
            raise ValueError(f"speed must be an integer in {cls.MIN_SPEED}..{cls.MAX_SPEED}, got {speed!r}")
        name_display_format = d.get("name_display_format", cls.DEFAULT_NAME_DISPLAY_FORMAT)
        if name_display_format not in cls.VALID_NAME_DISPLAY_FORMATS:
            raise ValueError(
                f"name_display_format must be one of {cls.VALID_NAME_DISPLAY_FORMATS}, " f"got {name_display_format!r}"
            )
        return cls(
            speed=speed,
            color=int(d.get("color", 0xFF0000)),
            text_effect=text_effect,
            name_display_format=name_display_format,
        )

    def to_dict(self):
        """Serialize to a dict (wire shape)."""
        return {
            "speed": self.speed,
            "color": self.color,
            "text_effect": self.text_effect,
            "name_display_format": self.name_display_format,
        }

    def validate(self):
        """Raise ValueError on out-of-range values.

        Raises:
            ValueError: on speed outside 1..5, color outside 0..0xFFFFFF,
                an unknown text_effect, or an unknown name_display_format.
        """
        if (
            isinstance(self.speed, bool)
            or not isinstance(self.speed, int)
            or not self.MIN_SPEED <= self.speed <= self.MAX_SPEED
        ):
            raise ValueError(f"speed must be an integer in {self.MIN_SPEED}..{self.MAX_SPEED}")
        if not (0 <= self.color <= 0xFFFFFF):
            raise ValueError("color must be in range 0..0xFFFFFF")
        if self.text_effect not in self.TEXT_EFFECTS:
            raise ValueError(f"text_effect must be one of {self.TEXT_EFFECTS}")
        if self.name_display_format not in self.VALID_NAME_DISPLAY_FORMATS:
            raise ValueError(
                f"name_display_format must be one of {self.VALID_NAME_DISPLAY_FORMATS}, "
                f"got {self.name_display_format!r}"
            )


class SignConfig:
    """Configuration data model for the sign.

    v3 layout (issue #6 / implement-senders-filtering):
      - `sign_settings` (was `sign`) holds `sign_name` (was `name`) and
        `timezone` (was a top-level field), and the senders-allowlist
        master toggle `enforce_allowed_senders` (was a draft-schema
        top-level field).
      - `text_settings` gains `name_display_format` (presentation knob
        alongside `speed` / `color` / `text_effect`).
      - `senders` is a `dict[str, dict]` mapping NORMALIZED phone →
        `{"name", "allowed", "phone"}` (was `dict[str, str]`).
      - `filters` use `FilterRule.status: "enabled" | "disabled"` (was
        `enabled: bool`) and `type` is restricted to `keyword`, `regex`,
        `message` (sender type REMOVED — sender matching moved to the
        senders list).
      - No top-level `sign`, `timezone`, `enforce_allowed_senders`, or
        `name_display_format` keys. No top-level `allowed_senders` (it
        was already deprecated).

    Thread-safe: guards mutations with a reentrant lock.
    """

    # Wire-format schema version. Bump on breaking changes; pair with
    # a new entry in lib_shared.config_migrations.MIGRATIONS.
    CURRENT_VERSION: int = 3

    def __init__(
        self,
        filters: list["FilterRule"] | None = None,
        senders: "dict[str, dict] | None" = None,
        sign_settings: "SignSettings | dict | None" = None,
        version: int = CURRENT_VERSION,
        effects_settings: "EffectsSettings | dict | None" = None,
        text_settings: "TextSettings | dict | None" = None,
    ) -> None:
        """Initialize a SignConfig.

        Args:
            filters: List of FilterRule objects (default empty).
            senders: Dict mapping NORMALIZED phone (e.g. ``+15551234567``)
                to a value dict ``{"name": str, "allowed": bool, "phone": str}``
                (default empty dict).
            sign_settings: SignSettings instance or dict (default built
                from empty dict — sign_name="Lindsay's Heart",
                timezone="US/Pacific").
            version: Config schema version (default CURRENT_VERSION = 3).
            effects_settings: EffectsSettings instance or dict (default
                built from empty dict).
            text_settings: TextSettings instance or dict (default built
                from empty dict — name_display_format=
                "first_initial_if_duplicates" by default).
        """
        self.filters = filters or []
        self.senders = senders if senders is not None else {}
        self.sign_settings = (
            sign_settings if isinstance(sign_settings, SignSettings) else SignSettings.from_dict(sign_settings or {})
        )
        self.version = version
        self.effects_settings = (
            effects_settings
            if isinstance(effects_settings, EffectsSettings)
            else EffectsSettings.from_dict(effects_settings or {})
        )
        self.text_settings = (
            text_settings if isinstance(text_settings, TextSettings) else TextSettings.from_dict(text_settings or {})
        )
        self._lock = threading.RLock()

    def _with_lock(self, fn):
        """Run fn under the config lock (no-op if lock unavailable).

        Args:
            fn: a callable to execute inside the lock.

        Returns:
            The return value of fn().
        """
        if self._lock:
            with self._lock:
                return fn()
        return fn()

    @classmethod
    def default(cls):
        """Return a default SignConfig (CURRENT_VERSION, empty filters / senders)."""
        return cls()

    @classmethod
    def from_dict(cls, data):
        """Deserialize a SignConfig from a dict (the same shape as to_dict()).

        Runs the migration registry at the top so older wire shapes are
        transparently brought up to CURRENT_VERSION. The senders list is
        normalized via ``phone_utils.normalize_phone`` on ingest so the
        dict key is always the canonical form.

        Args:
            data: dict with optional keys: filters, senders, sign_settings,
                version, effects_settings, text_settings.

        Returns:
            A new SignConfig instance.

        Raises:
            ValueError: on malformed filter or senders entries.
        """
        # Defense-in-depth: bring older payloads forward before parsing.
        from lib_shared.config_migrations import migrate

        if data is not None:
            data = migrate(data, current_version=cls.CURRENT_VERSION)
        else:
            data = {}

        senders: dict[str, dict] = {}
        for entry in data.get("senders", []):
            if not isinstance(entry, dict):
                continue
            phone = entry.get("phone")
            name = entry.get("name", "")
            if not isinstance(phone, str):
                continue
            from lib_shared.phone_utils import normalize_phone

            key = normalize_phone(phone)
            senders[key] = {
                "name": name,
                "allowed": bool(entry.get("allowed", True)),
                "phone": phone,
            }

        return cls(
            filters=[FilterRule.from_dict(f) for f in data.get("filters", [])],
            senders=senders,
            sign_settings=data.get("sign_settings"),
            version=data.get("version", cls.CURRENT_VERSION),
            effects_settings=data.get("effects_settings"),
            text_settings=data.get("text_settings"),
        )

    def to_dict(self):
        """Serialize the config to a dict suitable for JSON or S3 storage.

        Returns:
            dict with keys: filters, senders, sign_settings, effects_settings,
            text_settings, version. No top-level `sign`, `timezone`,
            `enforce_allowed_senders`, or `name_display_format` keys (all
            live inside their respective nested settings blocks).
        """
        return self._with_lock(
            lambda: {
                "filters": [f.to_dict() for f in self.filters],
                "senders": sorted(
                    (
                        {"phone": entry["phone"], "name": entry["name"], "allowed": entry["allowed"]}
                        for entry in self.senders.values()
                    ),
                    key=lambda d: d["phone"],
                ),
                "sign_settings": self.sign_settings.to_dict(),
                "version": self.version,
                "effects_settings": self.effects_settings.to_dict(),
                "text_settings": self.text_settings.to_dict(),
            }
        )

    def update(self, other: "SignConfig") -> None:
        """Replace all fields with values from another SignConfig (thread-safe).

        Subclasses can override this to persist to storage (e.g. SqliteConfig).
        """

        def _do():
            self.filters = other.filters
            self.senders = dict(other.senders)
            self.sign_settings = other.sign_settings
            self.version = other.version
            self.effects_settings = other.effects_settings
            self.text_settings = other.text_settings

        self._with_lock(_do)

    def update_from_dict(self, data: dict) -> None:
        """Replace all fields from a dict (mutates self). Thread-safe.

        Runs the migration registry at the top so older wire shapes (a v1
        payload arriving over MQTT, for example) are transparently brought
        up to CURRENT_VERSION before the field-by-field update runs.

        If an `effects_settings` override is active on this process
        (`config_overrides/effects_settings.json` or
        `EFFECTS_SETTINGS_OVERRIDE` env var), the wire's `effects_settings`
        block is dropped here so EVERY entry point (seed fetch, MQTT
        dispatch, future callers) respects the override. The override owns
        the entire `EffectsSettings` — both the effects list AND the pacing
        fields. `MessageManager._handle_config` also strips defensively;
        that path stays (defense in depth, no-op when this layer has
        already stripped).
        """
        from lib_shared.config_migrations import migrate

        if data is not None:
            data = migrate(data, current_version=self.CURRENT_VERSION)
        else:
            data = {}

        # Strip wire `effects_settings` when an override is active. The
        # override lives in the loader (canonical repo-root JSON or
        # `EFFECTS_SETTINGS_OVERRIDE` env var) and is the single source of
        # truth for this block. We must not mutate the caller's dict, so
        # shallow-copy before pop.
        from lib_shared.effects_loader import is_effects_settings_override_active

        if is_effects_settings_override_active() and isinstance(data, dict) and "effects_settings" in data:
            data = dict(data)
            data.pop("effects_settings", None)
            log.debug("SignConfig.update_from_dict: dropped wire effects_settings (override active)")

        senders: dict[str, dict] = {}
        for entry in data.get("senders", []):
            if not isinstance(entry, dict):
                continue
            phone = entry.get("phone")
            name = entry.get("name", "")
            if not isinstance(phone, str):
                continue
            from lib_shared.phone_utils import normalize_phone

            key = normalize_phone(phone)
            senders[key] = {
                "name": name,
                "allowed": bool(entry.get("allowed", True)),
                "phone": phone,
            }

        def _do():
            self.filters = [FilterRule.from_dict(f) for f in data.get("filters", [])]
            self.senders = senders
            sign_data = data.get("sign_settings")
            self.sign_settings = SignSettings.from_dict(sign_data) if sign_data else SignSettings()
            self.version = data.get("version", self.CURRENT_VERSION)
            # Only overwrite the new blocks if the incoming payload carries them.
            # This keeps the existing in-memory values when a partial update
            # arrives (the migration fills defaults, so the blocks are present
            # — but we still want the caller's intent to "leave it alone" honored).
            if "effects_settings" in data:
                es = data["effects_settings"]
                self.effects_settings = es if isinstance(es, EffectsSettings) else EffectsSettings.from_dict(es or {})
            if "text_settings" in data:
                ts = data["text_settings"]
                self.text_settings = ts if isinstance(ts, TextSettings) else TextSettings.from_dict(ts or {})

        self._with_lock(_do)
