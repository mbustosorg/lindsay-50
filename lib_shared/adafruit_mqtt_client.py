"""Adafruit IO MQTT client for the Flask server (Heroku, TLS on port 8883).

Wraps Adafruit_IO.MQTTClient. On each incoming message calls
dispatch_callback(raw_payload).
"""

import logging
from Adafruit_IO import MQTTClient  # type: ignore[import-untyped]  # Adafruit IO ships no type stubs

from lib_shared.config_reader import get_config

cfg = get_config()

logger = logging.getLogger(__name__)


class AdafruitMqttClient:
    """Thin adapter: owns the Adafruit_IO.MQTTClient lifecycle.

    Calls dispatch_callback(raw_payload) for each incoming message.
    """

    def __init__(self, dispatch_callback):
        """Initialize the client.

        Args:
            dispatch_callback: Callable that accepts a raw MQTT payload string.
        """
        self._dispatch = dispatch_callback
        self._client: MQTTClient | None = None
        self._username = cfg.MQTT_USERNAME
        self._key = cfg.MQTT_PASSWORD
        self._feed = cfg.MQTT_TOPIC

    def start(self) -> None:
        """Connect to Adafruit IO and start the background loop."""

        def on_connect(_client):
            logger.info(
                "AdafruitMqttClient connected, subscribing to %s/%s",
                self._username,
                self._feed,
            )
            _client.subscribe(self._feed)

        def on_disconnect(_client, rc):
            logger.warning("AdafruitMqttClient disconnected: rc=%s", rc)

        def on_message(_client, feed_id, payload):
            logger.info("AdafruitMqttClient on_message: feed_id=%r payload=%r", feed_id, payload)
            self._dispatch(payload)

        self._client = MQTTClient(self._username, self._key, secure=True)
        self._client.on_connect = on_connect  # type: ignore[reportAttributeAccessIssue]
        self._client.on_disconnect = on_disconnect  # type: ignore[reportAttributeAccessIssue]
        self._client.on_message = on_message  # type: ignore[reportAttributeAccessIssue]

        logger.info("AdafruitMqttClient connecting to %s...", cfg.MQTT_HOST)
        self._client.connect()
        self._client.loop_background()
        logger.info("AdafruitMqttClient started for feed %s", self._feed)

    def publish_envelope(self, envelope) -> bool:
        """Publish a MessageEnvelope to the AIO feed. Returns True on success."""
        payload = envelope.to_json()
        try:
            client = MQTTClient(cfg.MQTT_USERNAME, cfg.MQTT_PASSWORD, secure=True)
            client.connect()
            client.publish(self._feed, payload)
            client.disconnect()
            logger.info("AdafruitMqttClient published envelope to %s", self._feed)
            return True
        except Exception as e:
            logger.warning("AdafruitMqttClient publish failed: %s", e)
            return False

    def stop(self) -> None:
        """Disconnect the MQTT client."""
        if self._client:
            self._client.disconnect()
