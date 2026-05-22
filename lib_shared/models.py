"""Plain Python models shared between Flask app and ESP32.

No dataclasses or ABCs - CircuitPython compatible.
"""

import json


class MessageEnvelope:
    """JSON envelope for the unified MQTT feed.

    Attributes:
        type:    "message" | "config"
        payload: dict — Message.to_dict() or SignConfig.to_dict()
    """

    def __init__(self, type: str, payload: dict):
        self.type = type
        self.payload = payload

    @classmethod
    def from_json(cls, raw: str) -> "MessageEnvelope":
        d = json.loads(raw)
        return cls(type=d["type"], payload=d["payload"])

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))


class Message:
    def __init__(self, id, sender, body, received_at):
        self.id = id
        self.sender = sender
        self.body = body
        self.received_at = received_at  # ISO 8601 UTC

    @classmethod
    def from_dict(cls, d):
        return cls(id=d["id"], sender=d["sender"], body=d["body"], received_at=d["received_at"])

    def to_dict(self):
        return {"id": self.id, "sender": self.sender, "body": self.body, "received_at": self.received_at}


class MessageView:
    """Message with source and computed suppression status."""
    def __init__(self, message, source="rest", suppressed=False, rules=None, sender_name=None):
        self.message = message        # Message object
        self.source = source        # "rest" | "mqtt"
        self.suppressed = suppressed
        self.rules = rules or []    # list of FilterRule dicts
        self.sender_name = sender_name

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
        }


class FilterRule:
    def __init__(self, type, pattern, action="suppress"):
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

    Thread-safe: uses a reentrant lock when threading is available.
    CircuitPython has no threading module so lock is None there.
    """
    def __init__(self, filters=None, senders=None, rendering=None, sign=None, timezone="US/Pacific", version=1, tz_offset_mins: int = 0):
        self.filters = filters or []
        self.senders = senders or {}  # dict: phone -> name
        self.rendering = rendering if isinstance(rendering, RenderingSettings) else RenderingSettings.from_dict(rendering or {})
        self.sign = sign if isinstance(sign, SignSettings) else SignSettings.from_dict(sign or {})
        self.timezone = timezone
        self.version = version
        self.tz_offset_mins = tz_offset_mins
        try:
            import threading as _th
            self._lock = _th.RLock()
        except ImportError:
            self._lock = None

    def _with_lock(self, fn):
        """Run fn under the config lock (no-op if lock unavailable)."""
        if self._lock:
            with self._lock:
                return fn()
        return fn()

    @classmethod
    def default(cls):
        """Return a default config."""
        return cls()

    @classmethod
    def from_dict(cls, data):
        return cls(
            filters=[FilterRule.from_dict(f) for f in data.get("filters", [])],
            senders={s["phone"]: s["name"] for s in data.get("senders", [])},
            rendering=RenderingSettings.from_dict(data.get("rendering")),
            sign=SignSettings.from_dict(data.get("sign")) if data.get("sign") else SignSettings(),
            timezone=data.get("timezone", "US/Pacific"),
            version=data.get("version", 1),
            tz_offset_mins=data.get("tz_offset_mins", 0),
        )

    def to_dict(self):
        return self._with_lock(lambda: {
            "filters": [f.to_dict() for f in self.filters],
            "senders": [{"phone": p, "name": n} for p, n in self.senders.items()],
            "rendering": self.rendering.to_dict(),
            "sign": self.sign.to_dict(),
            "timezone": self.timezone,
            "tz_offset_mins": self.tz_offset_mins,
            "version": self.version,
        })

    def update(self, other: "SignConfig") -> None:
        """Update fields from another SignConfig object.

        Subclasses can override this and change behavior
        (e.g. SqliteConfig persists to storage).
        Thread-safe.
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
        """Update config from a dict (mutates self). Thread-safe."""
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


# ---------------------------------------------------------------------------
# FilteredMessages — base class for message services
# ---------------------------------------------------------------------------

import re

class FilteredMessages:
    """Base class for message services.

    Subclasses must implement:
      - add(message, source="rest")
      - add_many(messages, source="rest")
      - clear()
      - get_messages(limit=100) -> list[MessageView]

    The base class provides _apply_suppression() for use by subclasses.
    """

    def __init__(self, config):
        self._config = config

    def _apply_filter(self, msg, rules):
        """Apply filter rules to a message.

        Args:
            msg:   Message object
            rules: list of FilterRule objects

        Returns:
            List of FilterRule objects that suppress the message (in evaluation order).
        """
        suppressing = []
        for rule in rules:
            if rule.action != "suppress":
                continue
            if self._matches(msg, rule):
                suppressing.append(rule)
        return suppressing

    def _matches(self, msg, rule):
        """Return True if message matches the filter rule."""
        if rule.type == "keyword":
            return rule.pattern.lower() in msg.body.lower()
        elif rule.type == "regex":
            try:
                return bool(re.fullmatch(rule.pattern, msg.body))
            except re.error:
                return False
        elif rule.type == "sender":
            return msg.sender == rule.pattern
        elif rule.type == "message":
            return msg.id == rule.pattern
        return False

    def _apply_suppression(self, entries):
        """Fill suppressed, rules, sender_name on each MessageView entry.

        Called by get_messages() before returning. Override to customize.
        """
        for entry in entries:
            suppressing = self._apply_filter(entry.message, self._config.filters)
            entry.suppressed = bool(suppressing)
            entry.rules = [r.to_dict() for r in suppressing]
            entry.sender_name = self._config.senders.get(entry.message.sender)

    def add(self, message, source="rest"):
        """Add a single message."""
        raise NotImplementedError()

    def add_many(self, messages, source="rest"):
        """Add multiple messages."""
        raise NotImplementedError()

    def clear(self):
        """Clear all messages."""
        raise NotImplementedError()

    def get_messages(self, limit=100):
        """Return messages, newest first, with suppression applied."""
        raise NotImplementedError()
