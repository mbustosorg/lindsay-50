"""Shared data models for message storage and config."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Message:
    """Inbound SMS message with metadata."""
    id: str          # UUID v4
    sender: str       # E.164 phone number
    body: str
    received_at: str  # ISO 8601 timestamp

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "body": self.body,
            "received_at": self.received_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            id=d["id"],
            sender=d["sender"],
            body=d["body"],
            received_at=d["received_at"],
        )


@dataclass
class FilterRule:
    """A single filter rule."""
    type: str   # "keyword" | "regex" | "sender" | "message"
    pattern: str
    action: str = "suppress"

    def to_dict(self) -> dict:
        return {"type": self.type, "pattern": self.pattern, "action": self.action}

    @classmethod
    def from_dict(cls, d: dict) -> "FilterRule":
        return cls(type=d["type"], pattern=d["pattern"], action=d.get("action", "suppress"))


@dataclass
class AllowedSender:
    """A known sender with a human-readable name."""
    name: str
    phone: str  # E.164

    def to_dict(self) -> dict:
        return {"name": self.name, "phone": self.phone}

    @classmethod
    def from_dict(cls, d: dict) -> "AllowedSender":
        return cls(name=d["name"], phone=d["phone"])


@dataclass
class RenderingSettings:
    """LED sign rendering defaults."""
    mode: str = "scroll"
    speed: float = 0.04
    color: int = 0xFF0000  # Red

    def to_dict(self) -> dict:
        return {"mode": self.mode, "speed": self.speed, "color": self.color}

    @classmethod
    def from_dict(cls, d: dict) -> "RenderingSettings":
        return cls(mode=d.get("mode", "scroll"), speed=d.get("speed", 0.04), color=d.get("color", 0xFF0000))


@dataclass
class SignSettings:
    """Sign metadata."""
    name: str = "Lindsay's Heart"

    def to_dict(self) -> dict:
        return {"name": self.name}

    @classmethod
    def from_dict(cls, d: dict) -> "SignSettings":
        return cls(name=d.get("name", "Lindsay's Heart"))


@dataclass
class Config:
    """Application configuration stored in SQLite as JSON."""
    version: int = 1
    allowed_senders: list[AllowedSender] = field(default_factory=list)
    filters: list[FilterRule] = field(default_factory=list)
    rendering: RenderingSettings = field(default_factory=RenderingSettings)
    sign: SignSettings = field(default_factory=SignSettings)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "allowed_senders": [s.to_dict() for s in self.allowed_senders],
            "filters": [f.to_dict() for f in self.filters],
            "rendering": self.rendering.to_dict(),
            "sign": self.sign.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        return cls(
            version=d.get("version", 1),
            allowed_senders=[AllowedSender.from_dict(s) for s in d.get("allowed_senders", [])],
            filters=[FilterRule.from_dict(f) for f in d.get("filters", [])],
            rendering=RenderingSettings.from_dict(d.get("rendering", {})),
            sign=SignSettings.from_dict(d.get("sign", {})),
        )

    @classmethod
    def default(cls) -> "Config":
        """Return a default config with sensible defaults."""
        return cls(
            version=1,
            allowed_senders=[],
            filters=[],
            rendering=RenderingSettings(),
            sign=SignSettings(),
        )
