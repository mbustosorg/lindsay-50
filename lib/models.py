from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


@dataclass
class Message:
    id: str
    sender: str
    body: str
    received_at: str  # ISO 8601 timestamp

    @classmethod
    def create(cls, sender: str, body: str) -> "Message":
        return cls(
            id=str(uuid.uuid4()),
            sender=sender,
            body=body,
            received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )


@dataclass
class AllowedSender:
    name: str
    phone: str  # E.164 format


@dataclass
class FilterRule:
    type: str  # "keyword" | "regex" | "sender" | "message"
    pattern: str
    action: str = "suppress"


@dataclass
class RenderingConfig:
    mode: str = "scroll"
    speed: float = 0.04
    color: int = 16711680  # Red (0xFF0000)


@dataclass
class SignConfig:
    name: str = "Lindsay's Heart"


@dataclass
class Config:
    version: int = 1
    allowed_senders: list[AllowedSender] = field(default_factory=list)
    filters: list[FilterRule] = field(default_factory=list)
    rendering: RenderingConfig = field(default_factory=RenderingConfig)
    sign: SignConfig = field(default_factory=SignConfig)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "allowed_senders": [
                {"name": s.name, "phone": s.phone} for s in self.allowed_senders
            ],
            "filters": [
                {"type": f.type, "pattern": f.pattern, "action": f.action}
                for f in self.filters
            ],
            "rendering": {
                "mode": self.rendering.mode,
                "speed": self.rendering.speed,
                "color": self.rendering.color,
            },
            "sign": {"name": self.sign.name},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        allowed_senders = [
            AllowedSender(name=s["name"], phone=s["phone"])
            for s in data.get("allowed_senders", [])
        ]
        filters = [
            FilterRule(type=f["type"], pattern=f["pattern"], action=f.get("action", "suppress"))
            for f in data.get("filters", [])
        ]
        rendering_data = data.get("rendering", {})
        rendering = RenderingConfig(
            mode=rendering_data.get("mode", "scroll"),
            speed=rendering_data.get("speed", 0.04),
            color=rendering_data.get("color", 16711680),
        )
        sign_data = data.get("sign", {})
        sign = SignConfig(name=sign_data.get("name", "Lindsay's Heart"))

        return cls(
            version=data.get("version", 1),
            allowed_senders=allowed_senders,
            filters=filters,
            rendering=rendering,
            sign=sign,
        )

    @classmethod
    def default(cls) -> "Config":
        return cls()
