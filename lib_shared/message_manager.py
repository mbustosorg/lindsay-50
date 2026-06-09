"""MessageManager — owns config + message storage, handles dispatch and seeding.

Both Flask and ESP32 instantiate this at boot. Seed URLs come from cfg internally.
"""

import json

try:
    import logging
except ImportError:
    import adafruit_logging as logging

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
        """Convert payload dict to Message, store it, and call _on_message callback."""
        msg = Message(
            id=payload.get("id", ""),
            sender=payload.get("sender", ""),
            body=payload.get("body", ""),
            received_at=payload.get("received_at", ""),
        )

        self._messages.add(msg, source="mqtt")
        logger.info(
            "MessageManager routed message id=%s body=%r", msg.id, msg.body[:40]
        )
        if self._on_message:
            self._on_message(msg)

    def _handle_config(self, payload: dict) -> None:
        """Apply a SignConfig dict to the in-memory config (thread-safe update)."""
        self._config.update_from_dict(payload)
        logger.info("MessageManager applied config update")

    def seed(self) -> None:
        """Back-populate config and messages from the Flask REST API."""
        try:
            import requests as req

            _requests_timeout = 10
            _requests_raise_for_status = lambda r: r.raise_for_status()
            # Flask-to-Flask seed calls need API key auth
            _api_key = cfg.if_exists("API_SECRET_KEY") or ""
            _headers = {"X-API-Key": _api_key} if _api_key else {}
        except ImportError:
            import adafruit_requests as req

            _requests_timeout = None  # adafruit_requests doesn't support timeout param
            _requests_raise_for_status = lambda r: None  # no-op on CircuitPython
            _headers = {}

        cfg_api = cfg.get("CONFIG_API_URL")
        msgs_api = cfg.get("MESSAGES_API_URL")

        if msgs_api:
            try:
                resp = req.get(msgs_api, timeout=_requests_timeout, headers=_headers)
                _requests_raise_for_status(resp)
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
                logger.info(
                    "MessageManager seeded %d messages",
                    len(data) if isinstance(data, list) else 0,
                )
            except Exception as e:
                logger.warning("MessageManager message seed failed: %s", e)

        if cfg_api:
            try:
                resp = req.get(cfg_api, timeout=_requests_timeout, headers=_headers)
                _requests_raise_for_status(resp)
                self._config.update_from_dict(resp.json())
                logger.info("MessageManager seeded config")
            except Exception as e:
                logger.warning("MessageManager config seed failed: %s", e)

    def get_messages(self, limit: int = 100, suppress: bool = True):
        """Return messages from the ring buffer.

        Args:
            limit: Maximum number of messages to return.
            suppress: If True (default), exclude suppressed messages.
        """
        return self._messages.get_messages(limit, suppress=suppress)

    def get_config(self) -> SignConfig:
        return self._config
