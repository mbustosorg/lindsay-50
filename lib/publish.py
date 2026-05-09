"""Publish messages and config JSON to Adafruit IO over MQTT.

Uses the standard MQTT topic format that Adafruit IO uses:
  {username}/feeds/{feedname}

Flask publishes via MQTT so the ESP32 can receive messages in real-time
via its existing MQTT subscription.
"""

import json
import logging
import os
import threading
import time
import tomllib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Live message ring buffer (last N messages received from MQTT subscriber)
# ---------------------------------------------------------------------------

_LIVE_MESSAGES: deque = deque(maxlen=100)
_LIVE_LOCK = threading.Lock()


def record_live_message(body: str, topic: str, source: str = "mqtt",
                          received_at: str | None = None,
                          msg_id: str | None = None) -> None:
    """Record a message for the live feed display.

    Args:
        body:        The message body.
        topic:       The MQTT topic it was received on (or the feed topic if published).
        source:      Where this message came from: "mqtt" (from broker) or "rest" (from API back-populate).
        received_at:  ISO8601 timestamp string. Defaults to now UTC.
        msg_id:      Optional message UUID for deduplication.
    """
    with _LIVE_LOCK:
        _LIVE_MESSAGES.append({
            "body": body,
            "topic": topic,
            "source": source,
            "received_at": received_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "msg_id": msg_id,
        })


def get_live_messages(limit: int = 20) -> list[dict]:
    """Return the most recent N messages received from MQTT, newest first."""
    with _LIVE_LOCK:
        return list(reversed(list(_LIVE_MESSAGES)))[:limit]


def seed_from_rest_messages(messages: list[dict], feed_topic: str) -> None:
    """Seed the live message ring buffer from REST API messages.

    Clears the ring buffer first, then back-populates with messages already
    in SQLite (simulating what MQTT would have delivered).
    Each message is marked source="rest" so it's clear it came from the API.
    Uses message UUID for deduplication when called multiple times.
    """
    with _LIVE_LOCK:
        _LIVE_MESSAGES.clear()
    for msg in reversed(messages):
        record_live_message(
            body=msg.get("body", ""),
            topic=feed_topic,
            source="rest",
            received_at=msg.get("received_at"),
            msg_id=msg.get("id"),
        )


# ---------------------------------------------------------------------------
# Config (loaded from settings.toml)
# ---------------------------------------------------------------------------

def _mqtt_config() -> dict:
    """Load MQTT config from settings.toml."""
    settings_path = Path(__file__).parent.parent / "heart-sms-receiver" / "settings.toml"
    if not settings_path.exists():
        return {}
    with open(settings_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# MQTT client factory
# ---------------------------------------------------------------------------

def _mqtt_client(for_subscribe: bool = False) -> mqtt.Client:
    """Build an MQTT client from settings.toml.

    Args:
        for_subscribe: if True, enable auto-reconnect and set a clean session.
    """
    cfg = _mqtt_config()
    username = cfg.get("MQTT_USERNAME") or cfg.get("AIO_USERNAME", "")
    password = cfg.get("MQTT_PASSWORD") or cfg.get("AIO_KEY", "")

    client = mqtt.Client(
        client_id=f"lindsay-flask-{os.getpid()}-{'sub' if for_subscribe else 'pub'}",
        clean_session=for_subscribe,
    )
    if username:
        client.username_pw_set(username, password)
    return client


# ---------------------------------------------------------------------------
# MQTT topic helpers
# ---------------------------------------------------------------------------

def _feed_topic(feed: str, username: str) -> str:
    """Build the Adafruit IO MQTT topic for a feed.

    Format: {username}/feeds/{feed}
    If feed already contains 'feeds/', use as-is.
    """
    if "/feeds/" in feed:
        return feed
    return f"{username}/feeds/{feed}"


# ---------------------------------------------------------------------------
# Background MQTT subscriber
# ---------------------------------------------------------------------------

class _MqttSubscriber:
    """Long-lived MQTT subscriber that records all messages received to the ring buffer."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        cfg = _mqtt_config()
        feed = cfg.get("AIO_FEED", "")
        username = cfg.get("AIO_USERNAME", "")
        if not feed or not username:
            logger.warning("MQTT subscriber not started: AIO_FEED or AIO_USERNAME not configured")
            return

        self._topic = _feed_topic(feed, username)
        self._thread = threading.Thread(target=self._run, name="mqtt-subscriber", daemon=True)
        self._thread.start()
        logger.info("MQTT subscriber started on topic: %s", self._topic)

    def _run(self) -> None:
        while not self._stop.is_set():
            client = _mqtt_client(for_subscribe=True)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect

            cfg = _mqtt_config()
            host = cfg.get("MQTT_HOST", "io.adafruit.com")
            port = int(cfg.get("MQTT_PORT", 8883))

            try:
                logger.info("MQTT subscriber connecting to %s:%d...", host, port)
                client.connect(host, port, keepalive=60)
                client.loop_forever()
            except Exception as e:
                if not self._stop.is_set():
                    logger.warning("MQTT subscriber error: %s. Reconnecting in 5s...", e)
                    time.sleep(5)

    def _on_connect(self, _client, _userdata, _flags, rc):
        if rc == 0:
            logger.info("MQTT subscriber connected")
            _client.subscribe(self._topic)
            logger.info("MQTT subscriber subscribed to %s", self._topic)
        else:
            logger.warning("MQTT subscriber connection failed: rc=%s", rc)

    def _on_message(self, _client, _userdata, msg):
        body = msg.payload.decode(errors="replace")
        record_live_message(body, msg.topic)
        logger.info("MQTT subscriber received: %s [%s]", body, msg.topic)

    def _on_disconnect(self, _client, _userdata, rc):
        if rc != 0:
            logger.warning("MQTT subscriber disconnected unexpectedly: rc=%s", rc)

    def stop(self) -> None:
        self._stop.set()


# Singleton subscriber instance
_mqtt_subscriber: _MqttSubscriber | None = None


def start_mqtt_subscriber() -> None:
    """Start the background MQTT subscriber. Call once from Flask app startup."""
    global _mqtt_subscriber
    _mqtt_subscriber = _MqttSubscriber()
    _mqtt_subscriber.start()


# ---------------------------------------------------------------------------
# Publish message (called by Flask on each inbound SMS)
# ---------------------------------------------------------------------------

def publish_message(body: str, feed: str | None = None) -> bool:
    """Publish a message body to the AIO feed over MQTT.

    Args:
        body:     The SMS message text.
        feed:     Feed name. If None, loaded from settings.toml as AIO_FEED.

    Returns:
        True on success, False on failure.
    """
    cfg = _mqtt_config()
    feed = feed or cfg.get("AIO_FEED", "")
    username = cfg.get("AIO_USERNAME", "")

    if not feed or not username:
        logger.warning("MQTT/AIO_FEED not configured; skipping publish")
        return False

    host = cfg.get("MQTT_HOST", "io.adafruit.com")
    port = int(cfg.get("MQTT_PORT", 8883))
    topic = _feed_topic(feed, username)

    client = _mqtt_client()
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, body.encode(), qos=0)
        client.loop_stop()
        client.disconnect()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish failed: rc=%s", result.rc)
            return False
        logger.info("Published to MQTT %s:%d/%s", host, port, topic)
        return True
    except Exception as e:
        logger.error("Failed to publish via MQTT: %s", e)
        return False


# ---------------------------------------------------------------------------
# Publish config JSON (called after any config change via admin UI)
# ---------------------------------------------------------------------------

def publish_config(config_dict: dict, feed: str | None = None) -> bool:
    """Publish config JSON to a feed over MQTT.

    Args:
        config_dict: The config dict to serialize and send.
        feed:        Feed name. If None, loaded from settings.toml as AIO_CONFIG_FEED.

    Returns:
        True on success, False on failure.
    """
    cfg = _mqtt_config()
    feed = feed or cfg.get("AIO_CONFIG_FEED", "")
    username = cfg.get("AIO_USERNAME", "")

    if not feed or not username:
        logger.warning("MQTT/AIO_CONFIG_FEED not configured; skipping config publish")
        return False

    host = cfg.get("MQTT_HOST", "io.adafruit.com")
    port = int(cfg.get("MQTT_PORT", 8883))
    topic = _feed_topic(feed, username)
    payload = json.dumps(config_dict, separators=(",", ":"))

    client = _mqtt_client()
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        result = client.publish(topic, payload.encode(), qos=0)
        client.loop_stop()
        client.disconnect()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT config publish failed: rc=%s", result.rc)
            return False
        logger.info("Published config to MQTT %s:%d/%s", host, port, topic)
        return True
    except Exception as e:
        logger.error("Failed to publish config via MQTT: %s", e)
        return False
