"""Plain Python models shared between the Flask app and the Raspberry Pi display."""

import json
import threading


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


class RenderingSettings:
    """LED rendering defaults: mode, speed, color."""

    def __init__(self, mode: str = "scroll", speed: float = 0.5, color: int = 0xFFFFFF):
        """Initialize RenderingSettings.

        Args:
            mode: LED effect — "scroll" (default), "fireworks", "flame", etc.
            speed: Scroll/animation speed from 0.0 (slow) to 1.0 (fast).
            color: 24-bit RGB color value (default 0xFFFFFF white).
        """
        self.mode = mode
        self.speed = speed
        self.color = color

    @classmethod
    def from_dict(cls, d):
        if d is None:
            return cls()
        return cls(
            mode=d.get("mode", "scroll"),
            speed=d.get("speed", 0.5),
            color=d.get("color", 0xFFFFFF),
        )

    def to_dict(self):
        return {"mode": self.mode, "speed": self.speed, "color": self.color}


class SignConfig:
    """Configuration data model for the sign.

    filters: list of FilterRule objects
    senders: dict of phone -> name
    rendering: RenderingSettings
    sign: Sign object with .name attribute

    Thread-safe: guards mutations with a reentrant lock.
    """

    def __init__(
        self,
        filters=None,
        senders=None,
        rendering=None,
        sign=None,
        timezone="US/Pacific",
        version=1,
        tz_offset_mins: int = 0,
        allowed_senders=None,
    ):
        """Initialize a SignConfig.

        Args:
            filters: List of FilterRule objects (default empty).
            senders: Dict mapping phone number -> display name (default empty).
            rendering: RenderingSettings instance or dict (default built from empty dict).
            sign: SignSettings instance or dict (default built from empty dict).
            timezone: IANA timezone string (default "US/Pacific").
            version: Config schema version (default 1).
            tz_offset_mins: Manual UTC offset in minutes (default 0).
            allowed_senders: Deprecated, ignored (kept for backward compat with tests).
        """
        self.filters = filters or []
        self.senders = senders or {}
        self.rendering = (
            rendering if isinstance(rendering, RenderingSettings) else RenderingSettings.from_dict(rendering or {})
        )
        self.sign = sign if isinstance(sign, SignSettings) else SignSettings.from_dict(sign or {})
        self.timezone = timezone
        self.version = version
        self.tz_offset_mins = tz_offset_mins
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

        Args:
            data: dict with optional keys: filters, senders, rendering, sign,
                  timezone, version, tz_offset_mins.

        Returns:
            A new SignConfig instance.
        """
        return cls(
            filters=[FilterRule.from_dict(f) for f in data.get("filters", [])],
            senders={s["phone"]: s["name"] for s in data.get("senders", [])},
            rendering=RenderingSettings.from_dict(data.get("rendering")),
            sign=(SignSettings.from_dict(data.get("sign")) if data.get("sign") else SignSettings()),
            timezone=data.get("timezone", "US/Pacific"),
            version=data.get("version", 1),
            tz_offset_mins=data.get("tz_offset_mins", 0),
        )

    def to_dict(self):
        """Serialize the config to a dict suitable for JSON or S3 storage.

        Returns:
            dict with keys: filters, senders, rendering, sign, timezone,
            tz_offset_mins, version.
        """
        return self._with_lock(
            lambda: {
                "filters": [f.to_dict() for f in self.filters],
                "senders": [{"phone": p, "name": n} for p, n in self.senders.items()],
                "rendering": self.rendering.to_dict(),
                "sign": self.sign.to_dict(),
                "timezone": self.timezone,
                "tz_offset_mins": self.tz_offset_mins,
                "version": self.version,
            }
        )

    def update(self, other: "SignConfig") -> None:
        """Replace all fields with values from another SignConfig (thread-safe).

        Subclasses can override this to persist to storage (e.g. SqliteConfig).
        """

        def _do():
            self.filters = other.filters
            self.senders = other.senders
            self.rendering = other.rendering
            self.sign = other.sign
            self.timezone = other.timezone
            self.version = other.version
            self.tz_offset_mins = other.tz_offset_mins

        self._with_lock(_do)

    def update_from_dict(self, data: dict) -> None:
        """Replace all fields from a dict (mutates self). Thread-safe."""

        def _do():
            self.filters = [FilterRule.from_dict(f) for f in data.get("filters", [])]
            self.senders = {s["phone"]: s["name"] for s in data.get("senders", [])}
            self.rendering = RenderingSettings.from_dict(data.get("rendering"))
            sign_data = data.get("sign")
            self.sign = SignSettings.from_dict(sign_data) if sign_data else SignSettings()
            self.timezone = data.get("timezone", "US/Pacific")
            self.version = data.get("version", 1)
            self.tz_offset_mins = data.get("tz_offset_mins", 0)

        self._with_lock(_do)
