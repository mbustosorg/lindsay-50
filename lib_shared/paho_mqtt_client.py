"""Paho MQTT client shared by the Flask server and the Raspberry Pi display.

Wraps paho-mqtt. Subscribes in a daemon thread with automatic reconnect and
calls dispatch_callback(raw_payload) for each incoming message. The Flask
server also uses publish_envelope() to push envelopes to the broker; the Pi is
subscribe-only and simply never calls it. TLS is enabled automatically on port
8883 (e.g. io.adafruit.com).
"""

import logging
import threading
import time

import paho.mqtt.client as mqtt

from lib_shared.config_reader import get_config

cfg = get_config()

logger = logging.getLogger(__name__)


class PahoMqttClient:
    """Owns the paho-mqtt client lifecycle.

    Calls dispatch_callback(raw_payload) for each incoming message.
    """

    def __init__(self, dispatch_callback):
        """Initialize the client.

        Args:
            dispatch_callback: Callable that accepts a raw MQTT payload string.
        """
        self._dispatch = dispatch_callback
        self._thread = None
        self._stop = None

        self._host = cfg.MQTT_HOST
        self._port = int(cfg.MQTT_PORT)
        self._username = cfg.MQTT_USERNAME
        self._password = cfg.MQTT_PASSWORD
        self._feed = cfg.MQTT_TOPIC

    def start(self) -> None:
        """Connect to the broker and run the subscriber loop in a daemon thread."""
        topic = self._feed
        logger.info(
            "PahoMqttClient will subscribe to topic=%r username=%r",
            topic,
            self._username,
        )

        def on_connect(_client, _userdata, _flags, rc):
            if rc == 0:
                _client.subscribe(topic)
                logger.info("PahoMqttClient connected, subscribed to %s", topic)
            else:
                logger.warning("PahoMqttClient connection failed: rc=%s", rc)

        def on_message(_client, _userdata, msg):
            logger.info(
                "PahoMqttClient received: topic=%s payload=%r", msg.topic, msg.payload
            )
            self._dispatch(msg.payload.decode(errors="replace"))

        def on_disconnect(_client, _userdata, rc):
            if rc != 0:
                logger.warning("PahoMqttClient unexpectedly disconnected: rc=%s", rc)

        self._stop = threading.Event()

        def _run():
            stop = self._stop
            assert stop is not None
            while not stop.is_set():
                try:
                    client = mqtt.Client(clean_session=True)  # type: ignore[reportPrivateImportUsage]
                    client.username_pw_set(self._username, self._password)
                    client.on_connect = on_connect  # type: ignore[reportAttributeAccessIssue]
                    client.on_message = on_message  # type: ignore[reportAttributeAccessIssue]
                    client.on_disconnect = on_disconnect  # type: ignore[reportAttributeAccessIssue]
                    # TLS required for port 8883 (e.g. io.adafruit.com).
                    if self._port == 8883:
                        client.tls_set_context()
                    logger.info(
                        "PahoMqttClient connecting to %s:%d...", self._host, self._port
                    )
                    client.connect(self._host, self._port, keepalive=60)
                    client.loop_forever()
                except Exception as e:
                    if not stop.is_set():
                        logger.warning(
                            "PahoMqttClient error: %s. Reconnecting in 5s...", e
                        )
                        time.sleep(5)
                    else:
                        logger.info("PahoMqttClient thread stopping")

        self._thread = threading.Thread(target=_run, name="paho-mqtt", daemon=True)
        self._thread.start()
        logger.info("PahoMqttClient started for feed %s", self._feed)

    def publish_envelope(self, envelope) -> bool:
        """Publish a MessageEnvelope to the broker feed. Returns True on success."""
        topic = self._feed
        payload = envelope.to_json()
        try:
            client = mqtt.Client(clean_session=True)
            client.username_pw_set(self._username, self._password)
            if self._port == 8883:
                client.tls_set_context()
            client.connect(self._host, self._port, keepalive=30)
            result = client.publish(topic, payload.encode(), qos=1)
            client.loop_stop()
            client.disconnect()
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning("PahoMqttClient publish failed: rc=%s", result.rc)
                return False
            logger.info("PahoMqttClient published envelope to %s", topic)
            return True
        except Exception as e:
            logger.warning("PahoMqttClient publish failed: %s", e)
            return False

    def stop(self) -> None:
        """Signal the subscriber thread to shut down and wait for it to join."""
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
