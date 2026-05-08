"""Shared data models for Flask and ESP32."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Message:
    """Represents an inbound SMS message."""

    id: str
    sender: str
    body: str
    received_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender": self.sender,
            "body": self.body,
            "received_at": self.received_at.isoformat(),
        }

    @classmethod
    def from_row(cls, row: tuple) -> "Message":
        return cls(
            id=row[0],
            sender=row[1],
            body=row[2],
            received_at=datetime.fromisoformat(row[3]),
        )


@dataclass
class AllowedSender:
    name: str
    phone: str


@dataclass
class FilterRule:
    """A single filter rule."""

    type: str  # "keyword" | "regex" | "sender" | "message"
    pattern: str
    action: str  # "suppress"

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "pattern": self.pattern, "action": self.action}

    @classmethod
    def from_dict(cls, d: dict) -> "FilterRule":
        return cls(type=d["type"], pattern=d["pattern"], action=d["action"])


@dataclass
class RenderingConfig:
    mode: str = "scroll"
    speed: float = 0.04
    color: int = 16711680


@dataclass
class SignConfig:
    name: str = "Lindsay's Heart"


@dataclass
class Config:
    """Application configuration stored in SQLite as JSON."""

    version: int = 1
    allowed_senders: list[AllowedSender] = field(default_factory=list)
    filters: list[FilterRule] = field(default_factory=list)
    rendering: RenderingConfig = field(default_factory=RenderingConfig)
    sign: SignConfig = field(default_factory=SignConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "allowed_senders": [
                {"name": s.name, "phone": s.phone} for s in self.allowed_senders
            ],
            "filters": [f.to_dict() for f in self.filters],
            "rendering": {
                "mode": self.rendering.mode,
                "speed": self.rendering.speed,
                "color": self.rendering.color,
            },
            "sign": {"name": self.sign.name},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        return cls(
            version=d.get("version", 1),
            allowed_senders=[
                AllowedSender(name=s["name"], phone=s["phone"])
                for s in d.get("allowed_senders", [])
            ],
            filters=[FilterRule.from_dict(f) for f in d.get("filters", [])],
            rendering=RenderingConfig(**d.get("rendering", {})),
            sign=SignConfig(**d.get("sign", {})),
        )

    @classmethod
    def default(cls) -> "Config":
        return cls()
