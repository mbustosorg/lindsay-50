"""Plain Python models shared between the Flask app and the Raspberry Pi display."""

import json
import threading
from typing import List, Optional


class MessageEnvelope:
    """JSON envelope for the unified MQTT feed.

    Attributes:
        type:    "message" | "config"
        payload: dict — Message.to_dict() or SignConfig.to_dict()
    """

    def __init__(self, type: str, payload: dict):
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
    """

    def __init__(self, id, sender, body, received_at):
        """Initialize a Message.

        Args:
            id: Unique message identifier (UUID string).
            sender: Phone number of the sender.
            body: Text content of the message.
            received_at: ISO 8601 UTC timestamp when received.
        """
        self.id = id
        self.sender = sender
        self.body = body
        self.received_at = received_at

    @classmethod
    def from_dict(cls, d):
        return cls(id=d["id"], sender=d["sender"], body=d["body"], received_at=d["received_at"])

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender,
            "body": self.body,
            "received_at": self.received_at,
        }


class MessageView:
    """Message with source and computed suppression status."""

    def __init__(
        self,
        message,
        source="rest",
        suppressed=False,
        rules=[],
        sender_name="",
        display_time=None,
    ):
        """Initialize a MessageView.

        Args:
            message: Message object this view wraps.
            source: "rest" when loaded from storage, "mqtt" when received live.
            suppressed: True if any filter rule matched this message.
            rules: List of FilterRule dicts that suppressed the message.
            sender_name: Display name for the sender (from the senders allowlist).
            display_time: Pre-formatted local time string, or None (set by _enrich_messages).
        """
        self.message = message
        self.source = source
        self.suppressed = suppressed
        self.rules = rules
        self.sender_name = sender_name
        self.display_time = display_time

    def to_dict(self):
        return {
            "id": self.message.id,
            "sender": self.message.sender,
            "body": self.message.body,
            "received_at": self.message.received_at,
            "source": self.source,
            "suppressed": self.suppressed,
            "rules": self.rules,
            "sender_name": self.sender_name,
            "display_time": self.display_time,
        }


class FilterRule:
    """A single filter rule that can suppress messages.

    Attributes:
        type: Rule type — "keyword", "regex", "sender", or "message".
        pattern: The value to match against (case-sensitive except for keyword).
        action: Always "suppress" in practice.
    """

    def __init__(self, type, pattern, action="suppress"):
        """Initialize a FilterRule.

        Args:
            type: Rule type — "keyword", "regex", "sender", or "message".
            pattern: Value to match against.
            action: Action to take when matched (default "suppress").
        """
        self.type = type
        self.pattern = pattern
        self.action = action

    @classmethod
    def from_dict(cls, d):
        return cls(type=d["type"], pattern=d["pattern"], action=d.get("action", "suppress"))

    def to_dict(self):
        return {"type": self.type, "pattern": self.pattern, "action": self.action}


class SignSettings:
    """Sign configuration with name attribute."""

    def __init__(self, name: str = "Lindsay's Heart"):
        """Initialize SignSettings.

        Args:
            name: Display name shown on the sign (default "Lindsay's Heart").
        """
        self.name = name

    @classmethod
    def from_dict(cls, d):
        if d is None:
            return cls()
        return cls(name=d.get("name", "Lindsay's Heart"))

    def to_dict(self):
        return {"name": self.name}


# ---------------------------------------------------------------------------
# EffectsSettings + TextSettings — v2 config blocks
# ---------------------------------------------------------------------------

# Canonical full 7-effect list. The 5 historically-defaulted effects are
# enabled; the 2 asset-dependent effects (VideoDisplay, PngDisplay) are
# disabled by default because they need operator-supplied asset files.
_DEFAULT_EFFECTS_LIST_FULL: List[dict] = [
    {"name": "Hyperspace", "enabled": True},
    {"name": "VideoDisplay", "enabled": False},
    {"name": "PngDisplay", "enabled": False},
    {"name": "Honeycomb", "enabled": True},
    {"name": "Flame", "enabled": True},
    {"name": "Fireworks", "enabled": True},
    {"name": "NightSky", "enabled": True},
]


class EffectsSettings:
    """Effects subsystem config: rotation list + pacing + recent_count.

    Groups every input the `EffectsCoordinator` consumes so the coordinator
    takes one focused argument instead of the full SignConfig.
    """

    def __init__(
        self,
        effects: Optional[List[dict]] = None,
        fade_seconds: float = 2.0,
        hold_seconds: float = 15.0,
        intro_seconds: float = 5.0,
        idle_seconds: float = 300.0,
        recent_count: int = 5,
    ):
        """Initialize EffectsSettings.

        Args:
            effects: List of `{"name": str, "enabled": bool}` dicts. Defaults
                to the canonical 7-entry list.
            fade_seconds: Seconds for one full fade (default 2.0).
            hold_seconds: Seconds to keep a message fully visible (default 15.0).
            intro_seconds: Seconds to show the boot-splash heart (default 5.0).
            idle_seconds: Seconds of idleness before a random message plays (default 300.0).
            recent_count: Size of the idle-rotation recent-messages pool (default 5).
        """
        self.effects = list(effects) if effects is not None else [dict(e) for e in _DEFAULT_EFFECTS_LIST_FULL]
        self.fade_seconds = fade_seconds
        self.hold_seconds = hold_seconds
        self.intro_seconds = intro_seconds
        self.idle_seconds = idle_seconds
        self.recent_count = recent_count

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: effects, fade_seconds, hold_seconds,
                intro_seconds, idle_seconds, recent_count.

        Returns:
            A new EffectsSettings instance.

        Raises:
            ValueError: on a malformed effects list (entries missing name/enabled,
                or non-dict entries). The Flask validation layer is responsible
                for rejecting unknown effect names; this method accepts any
                name string.
        """
        d = d or {}
        effects = d.get("effects", _DEFAULT_EFFECTS_LIST_FULL)
        if not isinstance(effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")
        return cls(
            effects=[{"name": n["name"], "enabled": n["enabled"]} for n in effects],
            fade_seconds=float(d.get("fade_seconds", 2.0)),
            hold_seconds=float(d.get("hold_seconds", 15.0)),
            intro_seconds=float(d.get("intro_seconds", 5.0)),
            idle_seconds=float(d.get("idle_seconds", 300.0)),
            recent_count=int(d.get("recent_count", 5)),
        )

    def to_dict(self):
        """Serialize to a dict (wire shape)."""
        return {
            "effects": self.effects,
            "fade_seconds": self.fade_seconds,
            "hold_seconds": self.hold_seconds,
            "intro_seconds": self.intro_seconds,
            "idle_seconds": self.idle_seconds,
            "recent_count": self.recent_count,
        }

    def validate(self):
        """Raise ValueError on out-of-range values.

        Raises:
            ValueError: on negative pacing durations, recent_count < 1, or
                a malformed effects list.
        """
        if self.fade_seconds < 0 or self.hold_seconds < 0 or self.intro_seconds < 0 or self.idle_seconds < 0:
            raise ValueError("pacing durations must be non-negative")
        if self.recent_count < 1:
            raise ValueError("recent_count must be a positive integer")
        if not isinstance(self.effects, list) or not all(
            isinstance(n, dict) and isinstance(n.get("name"), str) and isinstance(n.get("enabled"), bool)
            for n in self.effects
        ):
            raise ValueError("effects must be a list of {name: str, enabled: bool} objects")


class TextSettings:
    """Text rendering config: scroll speed, color, text_effect.

    `speed` is the user-facing knob (1=Low to 5=High). The underlying
    `frame_delay` / `offset_seconds` are derived from it by the scroller
    (see `ScrollerBase.SPEED_TABLE`). The wire shape stores `speed` only
    — the technical pacing values are device-local.

    Named "text_settings" (not "scroller_settings") because the scroller
    is just one text effect — future text effects (swirl, bounce) will
    share the same block.
    """

    # v1 supports "scroll" only; more values land as future text effects.
    TEXT_EFFECTS: tuple = ("scroll",)
    MIN_SPEED = 1
    MAX_SPEED = 5
    DEFAULT_SPEED = 3

    def __init__(
        self,
        speed: int = DEFAULT_SPEED,
        color: int = 0xFF0000,
        text_effect: str = "scroll",
    ):
        """Initialize TextSettings.

        Args:
            speed: 1..5 scroll speed (1=Low, 3=Medium default, 5=High).
            color: 24-bit RGB color value (default 0xFF0000 red).
            text_effect: One of TEXT_EFFECTS (currently "scroll").
        """
        self.speed = speed
        self.color = color
        self.text_effect = text_effect

    @classmethod
    def from_dict(cls, d):
        """Parse from a dict (wire shape).

        Args:
            d: dict with optional keys: speed, color, text_effect. Legacy
                `frame_delay` / `offset_seconds` keys are silently ignored —
                the new defaults are sensible and the user said v2 payloads
                are disposable.

        Returns:
            A new TextSettings instance.

        Raises:
            ValueError: on an unknown text_effect, or on an out-of-range
                or non-integer `speed` (callers like the admin validation
                helper catch and translate to a 400).
        """
        d = d or {}
        text_effect = d.get("text_effect", "scroll")
        if text_effect not in cls.TEXT_EFFECTS:
            raise ValueError(f"text_effect must be one of {cls.TEXT_EFFECTS}, got {text_effect!r}")
        speed = d.get("speed", cls.DEFAULT_SPEED)
        if isinstance(speed, bool) or not isinstance(speed, int) or not cls.MIN_SPEED <= speed <= cls.MAX_SPEED:
            raise ValueError(f"speed must be an integer in {cls.MIN_SPEED}..{cls.MAX_SPEED}, got {speed!r}")
        return cls(
            speed=speed,
            color=int(d.get("color", 0xFF0000)),
            text_effect=text_effect,
        )

    def to_dict(self):
        """Serialize to a dict (wire shape)."""
        return {
            "speed": self.speed,
            "color": self.color,
            "text_effect": self.text_effect,
        }

    def validate(self):
        """Raise ValueError on out-of-range values.

        Raises:
            ValueError: on speed outside 1..5, color outside 0..0xFFFFFF,
                or an unknown text_effect.
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


class SignConfig:
    """Configuration data model for the sign.

    filters: list of FilterRule objects
    senders: dict of phone -> name
    sign: SignSettings
    timezone: IANA timezone string
    effect_settings: EffectsSettings
    text_settings: TextSettings

    Thread-safe: guards mutations with a reentrant lock.
    """

    # Wire-format schema version. Bump on breaking changes; pair with
    # a new entry in lib_shared.config_migrations.MIGRATIONS.
    CURRENT_VERSION: int = 2

    def __init__(
        self,
        filters=None,
        senders=None,
        sign=None,
        timezone: str = "US/Pacific",
        version: int = CURRENT_VERSION,
        effect_settings=None,
        text_settings=None,
        allowed_senders=None,
    ):
        """Initialize a SignConfig.

        Args:
            filters: List of FilterRule objects (default empty).
            senders: Dict mapping phone number -> display name (default empty).
            sign: SignSettings instance or dict (default built from empty dict).
            timezone: IANA timezone string (default "US/Pacific").
            version: Config schema version (default CURRENT_VERSION = 2).
            effect_settings: EffectsSettings instance or dict (default built from empty dict).
            text_settings: TextSettings instance or dict (default built from empty dict).
            allowed_senders: Deprecated, ignored (kept for backward compat with tests).
        """
        self.filters = filters or []
        self.senders = senders or {}
        self.sign = sign if isinstance(sign, SignSettings) else SignSettings.from_dict(sign or {})
        self.timezone = timezone
        self.version = version
        self.effect_settings = (
            effect_settings
            if isinstance(effect_settings, EffectsSettings)
            else EffectsSettings.from_dict(effect_settings or {})
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
        """Return a default SignConfig with empty filters, senders, and US/Pacific timezone."""
        return cls()

    @classmethod
    def from_dict(cls, data):
        """Deserialize a SignConfig from a dict (the same shape as to_dict()).

        Runs the migration registry at the top so older wire shapes are
        transparently brought up to CURRENT_VERSION.

        Args:
            data: dict with optional keys: filters, senders, sign, timezone,
                version, effect_settings, text_settings.

        Returns:
            A new SignConfig instance.
        """
        # Defense-in-depth: bring older payloads forward before parsing.
        from lib_shared.config_migrations import migrate

        if data is not None:
            data = migrate(data, current_version=cls.CURRENT_VERSION)
        else:
            data = {}
        return cls(
            filters=[FilterRule.from_dict(f) for f in data.get("filters", [])],
            senders={s["phone"]: s["name"] for s in data.get("senders", [])},
            sign=(SignSettings.from_dict(data.get("sign")) if data.get("sign") else SignSettings()),
            timezone=data.get("timezone", "US/Pacific"),
            version=data.get("version", cls.CURRENT_VERSION),
            effect_settings=data.get("effect_settings"),
            text_settings=data.get("text_settings"),
        )

    def to_dict(self):
        """Serialize the config to a dict suitable for JSON or S3 storage.

        Returns:
            dict with keys: filters, senders, sign, timezone, effect_settings,
            text_settings, version.
        """
        return self._with_lock(
            lambda: {
                "filters": [f.to_dict() for f in self.filters],
                "senders": [{"phone": p, "name": n} for p, n in self.senders.items()],
                "sign": self.sign.to_dict(),
                "timezone": self.timezone,
                "version": self.version,
                "effect_settings": self.effect_settings.to_dict(),
                "text_settings": self.text_settings.to_dict(),
            }
        )

    def update(self, other: "SignConfig") -> None:
        """Replace all fields with values from another SignConfig (thread-safe).

        Subclasses can override this to persist to storage (e.g. SqliteConfig).
        """

        def _do():
            self.filters = other.filters
            self.senders = other.senders
            self.sign = other.sign
            self.timezone = other.timezone
            self.version = other.version
            self.effect_settings = other.effect_settings
            self.text_settings = other.text_settings

        self._with_lock(_do)

    def update_from_dict(self, data: dict) -> None:
        """Replace all fields from a dict (mutates self). Thread-safe.

        Runs the migration registry at the top so older wire shapes (a v1
        payload arriving over MQTT, for example) are transparently brought
        up to CURRENT_VERSION before the field-by-field update runs.
        """
        from lib_shared.config_migrations import migrate

        if data is not None:
            data = migrate(data, current_version=self.CURRENT_VERSION)
        else:
            data = {}

        def _do():
            self.filters = [FilterRule.from_dict(f) for f in data.get("filters", [])]
            self.senders = {s["phone"]: s["name"] for s in data.get("senders", [])}
            sign_data = data.get("sign")
            self.sign = SignSettings.from_dict(sign_data) if sign_data else SignSettings()
            self.timezone = data.get("timezone", "US/Pacific")
            self.version = data.get("version", self.CURRENT_VERSION)
            # Only overwrite the new blocks if the incoming payload carries them.
            # This keeps the existing in-memory values when a v1 partial update
            # arrives (the migration fills defaults, so the blocks are present
            # — but we still want the caller's intent to "leave it alone" honored).
            if "effect_settings" in data:
                es = data["effect_settings"]
                self.effect_settings = es if isinstance(es, EffectsSettings) else EffectsSettings.from_dict(es or {})
            if "text_settings" in data:
                ts = data["text_settings"]
                self.text_settings = ts if isinstance(ts, TextSettings) else TextSettings.from_dict(ts or {})

        self._with_lock(_do)
