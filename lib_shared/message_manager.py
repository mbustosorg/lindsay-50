"""MessageManager — owns config + message storage, handles dispatch and seeding.

Both Flask and ESP32 instantiate this at boot. Seed URLs come from cfg internally.
"""

import json
import logging
from datetime import datetime, timezone

from lib_shared.config_reader import get_config
cfg = get_config()

from lib_shared.models import MessageEnvelope, Message, SignConfig
from lib_shared.messages import InMemoryMessages

logger = logging.getLogger(__name__)


class MessageManager:
    """Owns SignConfig + InMemoryMessages; handles dispatch, seeding, and storage.

    On Flask: instantiated at boot. Seeds from own REST API.
    On ESP32: same — seeds from the Flask server's REST API.
    """

    def __init__(self, on_message=None):
        """Create MessageManager with its own config and message storage.

        Args:
            on_message: callback(msg: Message) — called when a "message" envelope
                        arrives over MQTT. ESP32 uses this to trigger display updates.
        """
        self._config = SignConfig()
        self._messages = InMemoryMessages(self._config, maxlen=100)
        self._on_message = on_message

    @property
    def config(self) -> SignConfig:
        return self._config

    @property
    def messages(self) -> InMemoryMessages:
        return self._messages

    def dispatch(self, raw: str) -> None:
        """Parse MessageEnvelope from raw MQTT payload, update internal state."""
        try:
            envelope = MessageEnvelope.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Invalid MessageEnvelope: %s", e)
            return

        if envelope.type == "message":
            self._handle_message(envelope.payload)
        elif envelope.type == "config":
            self._handle_config(envelope.payload)
        else:
            logger.warning("Unknown envelope type: %r", envelope.type)

    def _handle_message(self, payload: dict) -> None:
        msg = Message(
            id=payload.get("id", ""),
            sender=payload.get("sender", ""),
            body=payload.get("body", ""),
            received_at=payload.get("received_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self._messages.add(msg, source="mqtt")
        logger.info("MessageManager routed message id=%s body=%r", msg.id, msg.body[:40])
        if self._on_message:
            self._on_message(msg)

    def _handle_config(self, payload: dict) -> None:
        self._config.update_from_dict(payload)
        logger.info("MessageManager applied config update")

    def seed(self) -> None:
        """Back-populate config and messages from the Flask REST API."""
        cfg_api = cfg.get("CONFIG_API_URL")
        msgs_api = cfg.get("MESSAGES_API_URL")

        if msgs_api:
            try:
                import requests as req
                resp = req.get(msgs_api, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    self._messages.clear()
                    msgs = [
                        Message(
                            id=item.get("id", ""),
                            sender=item.get("sender", ""),
                            body=item.get("body", ""),
                            received_at=item.get("received_at", ""),
                        )
                        for item in data[-100:]
                    ]
                    self._messages.add_many(msgs, source="rest")
                logger.info("MessageManager seeded %d messages", len(data) if isinstance(data, list) else 0)
            except Exception as e:
                logger.warning("MessageManager message seed failed: %s", e)

        if cfg_api:
            try:
                import requests as req
                resp = req.get(cfg_api, timeout=10)
                resp.raise_for_status()
                self._config.update_from_dict(resp.json())
                logger.info("MessageManager seeded config")
            except Exception as e:
                logger.warning("MessageManager config seed failed: %s", e)

    def get_messages(self, limit: int = 100):
        return self._messages.get_messages(limit)

    def get_config(self) -> SignConfig:
        return self._config
