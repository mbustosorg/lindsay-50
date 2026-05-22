"""MQTT subscriber for ESP32 / CircuitPython.

MqttSubscriber is a simple MQTT-to-callback bridge.
MessagesSubscriber creates two MqttSubscriber instances and maps callbacks
to InMemoryMessages and Config methods.

This module MUST NOT import from lib.storage (SQLite) or any other module
that depends on SQLite.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from lib_shared.config_reader import get_config
cfg = get_config()

from lib_shared.messages import InMemoryMessages
from lib_shared.models import SignConfig, Message

logger = logging.getLogger(__name__)


def _feed_topic(feed: str, username: str) -> str:
    """Build the Adafruit IO MQTT topic for a feed."""
    if "/feeds/" in feed:
        return feed
    return f"{username}/feeds/{feed}"


# ---------------------------------------------------------------------------
# MqttSubscriber
# ---------------------------------------------------------------------------

class MqttSubscriber:
    """Simple MQTT-to-callback bridge.

    Args:
        feed: Adafruit IO feed name to subscribe to.
        on_message: Callback(raw: str) called when MQTT message arrives.
    """

    def __init__(self, feed: str = "", on_message=None):
        self._feed = feed
        self._on_message_cb = on_message
        self._topic = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Start the background MQTT subscriber thread."""
        username = cfg.AIO_USERNAME
        host = cfg.AIO_HOST
        port = int(cfg.AIO_PORT)

        self._topic = _feed_topic(self._feed, username)

        self._thread = threading.Thread(target=self._run, name="mqtt-subscriber", daemon=True)
        self._thread.start()
        logger.info("MqttSubscriber started on %s", self._topic)

    def stop(self) -> None:
        """Stop the background MQTT subscriber thread."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            host = cfg.AIO_HOST
            port = int(cfg.AIO_PORT)
            username = cfg.AIO_USERNAME
            password = cfg.AIO_KEY

            client = mqtt.Client(
                client_id=f"lindsay-subscriber-{id(self)}",
                clean_session=True,
            )
            if username:
                client.username_pw_set(username, password)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect

            try:
                logger.info("MqttSubscriber connecting to %s:%d...", host, port)
                client.connect(host, port, keepalive=60)
                client.loop_forever()
            except Exception as e:
                if not self._stop.is_set():
                    logger.warning("MqttSubscriber error: %s. Reconnecting in 5s...", e)
                    time.sleep(5)

    def _on_connect(self, _client, _userdata, _flags, rc: int) -> None:
        if rc == 0:
            _client.subscribe(self._topic)
            logger.info("MqttSubscriber subscribed to %s", self._topic)
        else:
            logger.warning("MqttSubscriber connection failed: rc=%s", rc)

    def _on_message(self, _client, _userdata, msg: mqtt.MQTTMessage) -> None:
        if self._on_message_cb:
            self._on_message_cb(msg.payload.decode(errors="replace"))

    def _on_disconnect(self, _client, _userdata, rc: int) -> None:
        if rc != 0:
            logger.warning("MqttSubscriber disconnected unexpectedly: rc=%s", rc)


# ---------------------------------------------------------------------------
# MqttConfig — MQTT config subscriber
# ---------------------------------------------------------------------------

class MqttConfig:
    """MQTT config subscriber wrapping a Config object.

    Owns its own MqttSubscriber for the config feed.
    Exposes the underlying Config as .config.
    """

    def __init__(self, config_feed: str, config_api_url: str = ""):
        self._config = SignConfig()
        self._config_api_url = config_api_url

        def on_message(raw: str) -> None:
            try:
                data = json.loads(raw)
                if "value" in data:
                    inner = json.loads(data["value"])
                    self._config.update_from_dict(inner)
                else:
                    self._config.update_from_dict(data)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Invalid config payload")

        self._sub = MqttSubscriber(feed=config_feed, on_message=on_message)

    def start(self) -> None:
        """Start the MQTT subscriber thread."""
        self._sub.start()

    @property
    def config(self) -> SignConfig:
        return self._config

    def seed(self) -> bool:
        """Seed config from REST API. Spawns a thread so caller isn't blocked."""
        if not self._config_api_url:
            return True
        def _do():
            try:
                import requests as req
                resp = req.get(self._config_api_url, timeout=10)
                resp.raise_for_status()
                self._config.update_from_dict(resp.json())
                logger.info("MqttConfig seeded config")
            except Exception as e:
                logger.warning("MqttConfig seed failed: %s", e)
        threading.Thread(target=_do, daemon=True).start()
        return True


# ---------------------------------------------------------------------------
# MqttMessages — MQTT message subscriber
# ---------------------------------------------------------------------------

class MqttMessages:
    """MQTT message subscriber wrapping InMemoryMessages.

    Owns its own MqttSubscriber for the message feed.
    Exposes the underlying InMemoryMessages as .messages.
    """

    def __init__(self, feed: str, api_url: str = "", config: SignConfig | None = None):
        self._msgs = InMemoryMessages(config if config is not None else SignConfig(), maxlen=100)
        self._api_url = api_url

        def on_message(raw: str) -> None:
            msg_id = None
            received_at = None
            sender = None
            display_body = raw
            try:
                data = json.loads(raw)
                if "body" in data:
                    msg_id = data.get("id")
                    received_at = data.get("received_at")
                    sender = data.get("sender")
                    display_body = data.get("body", raw)
                elif "value" in data:
                    display_body = data.get("value", raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            msg_obj = Message(
                id=msg_id or f"{time.time()}-{display_body[:20]}",
                sender=sender or "",
                body=display_body,
                received_at=received_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            self._msgs.add(msg_obj, source="mqtt")

        self._sub = MqttSubscriber(feed=feed, on_message=on_message)

    def start(self) -> None:
        """Start the MQTT subscriber thread."""
        self._sub.start()

    @property
    def messages(self) -> InMemoryMessages:
        return self._msgs

    def seed(self) -> bool:
        """Seed messages from REST API. Spawns a thread so caller isn't blocked."""
        if not self._api_url:
            return True
        def _do():
            try:
                import requests as req
                resp = req.get(self._api_url, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    self._msgs.clear()
                    msgs = [
                        Message(
                            id=item.get("id", ""),
                            sender=item.get("sender", ""),
                            body=item.get("body", ""),
                            received_at=item.get("received_at", ""),
                        )
                        for item in data[-100:]
                    ]
                    self._msgs.add_many(msgs, source="rest")
                logger.info("MqttMessages seeded %d messages", len(data) if isinstance(data, list) else 0)
            except Exception as e:
                logger.warning("MqttMessages seed failed: %s", e)
        threading.Thread(target=_do, daemon=True).start()
        return True


# ---------------------------------------------------------------------------
# MessagesSubscriber — thin orchestrator over MqttConfig + MqttMessages
# ---------------------------------------------------------------------------

class MessagesSubscriber:
    """Thin orchestrator over MqttConfig and MqttMessages.

    __init__ creates objects. start() starts MQTT threads and seeds from REST APIs.
    start() is idempotent - calling multiple times is safe.
    """

    def __init__(self, feed: str, config_feed: str, api_url: str = "", config_api_url: str = ""):
        self._config = MqttConfig(config_feed, config_api_url)
        # Share the same Config between MqttConfig and MqttMessages so
        # filter rules apply to messages as soon as they arrive.
        self._messages = MqttMessages(feed, api_url, config=self._config.config)
        self._started = False

    def start(self) -> None:
        """Start MQTT subscribers and seed from REST APIs. Idempotent."""
        if self._started:
            return
        self._started = True
        self._config.start()
        self._messages.start()
        # Seed with retry loop - give Flask time to start responding
        for delay in (0.5, 1.0, 2.0):
            logger.info("MessagesSubscriber seed attempt...")
            if self.seed():
                break
            logger.info("MessagesSubscriber seed failed, retrying in %ss...", delay)
            time.sleep(delay)
        logger.info("MessagesSubscriber start done. Buffer has %d messages",
                    len(self._messages._msgs._msgs))

    @property
    def config(self) -> SignConfig:
        return self._config.config

    @property
    def messages(self) -> InMemoryMessages:
        return self._messages.messages

    def get_messages(self, limit: int = 100):
        return self.messages.get_messages(limit)

    def seed(self) -> bool:
        cfg_ok = self._config.seed()
        msgs_ok = self._messages.seed()
        return cfg_ok and msgs_ok

    def update_config(self, config_dict: dict) -> None:
        """Update the shared config (e.g. from the config MQTT topic)."""
        self.config.update_from_dict(config_dict)
