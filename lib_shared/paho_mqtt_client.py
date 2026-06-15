"""Paho MQTT client shared by the Flask server and the Raspberry Pi display.

Wraps paho-mqtt. The constructor takes connection params (host, port,
username, password, topic) explicitly so the client doesn't read from
any config singleton — callers wire these from whatever config source
they have (TOML, env, secrets manager, hardcoded test values, etc.).
Subscribes in a daemon thread with automatic reconnect and calls
dispatch_callback(raw_payload) for each incoming message. The Flask
server also uses publish_envelope() to push envelopes to the broker;
the Pi is subscribe-only and simply never calls it. TLS is enabled
automatically on port 8883 (e.g. io.adafruit.com).
"""

import logging
import threading
import time

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class PahoMqttClient:
    """Owns the paho-mqtt client lifecycle.

    Calls dispatch_callback(raw_payload) for each incoming message.
    """

    def __init__(self, dispatch_callback, *, host, port, username, password, topic):
        """Initialize the client.

        Args:
            dispatch_callback: Callable that accepts a raw MQTT payload string.
            host: MQTT broker host.
            port: MQTT broker port (int or numeric string — coerced to int).
            username: MQTT broker username.
            password: MQTT broker password.
            topic: Wire-format topic to subscribe to and publish on. The
                client does no broker-specific translation; for Adafruit
                IO this must be the full "{username}/feeds/{feedname}" path.
        """
        self._dispatch = dispatch_callback
        self._thread = None
        self._stop = None

        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._topic = topic

    def start(self) -> None:
        """Connect to the broker and run the subscriber loop in a daemon thread."""
        topic = self._topic
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
            logger.info("PahoMqttClient received: topic=%s payload=%r", msg.topic, msg.payload)
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
                    logger.info("PahoMqttClient connecting to %s:%d...", self._host, self._port)
                    client.connect(self._host, self._port, keepalive=60)
                    client.loop_forever()
                except Exception as e:
                    if not stop.is_set():
                        logger.warning("PahoMqttClient error: %s. Reconnecting in 5s...", e)
                        time.sleep(5)
                    else:
                        logger.info("PahoMqttClient thread stopping")

        self._thread = threading.Thread(target=_run, name="paho-mqtt", daemon=True)
        self._thread.start()
        logger.info("PahoMqttClient started for feed %s", self._topic)

    def publish_envelope(self, envelope) -> bool:
        """Publish a MessageEnvelope to the broker. Returns True on success.

        Waits for the MQTT CONNACK (rc==0) before publishing, then blocks
        for up to 5 seconds waiting for the QoS 1 PUBACK. Mirrors the
        browser-side MqttWsClient pattern: the publish is sent only after
        the broker has acknowledged the connection, not when the socket
        opens. Without loop_start() the paho network thread never runs and
        the queued publish dies in the outgoing buffer.
        """
        topic = self._topic
        payload = envelope.to_json()
        try:
            client = mqtt.Client(clean_session=True)
            client.username_pw_set(self._username, self._password)
            if self._port == 8883:
                client.tls_set_context()
            # Wire on_connect so we can fail fast on CONNACK refusal
            # (wrong creds, broker down, etc) instead of waiting 5s for
            # the publish-timeout. Same pattern as the SUBSCRIBER's
            # on_connect above — paho calls it from the network thread.
            connect_event = threading.Event()

            def _on_connect(_client, _userdata, _flags, rc):
                if rc == 0:
                    connect_event.set()
                else:
                    logger.warning("PahoMqttClient CONNACK refused: rc=%s", rc)

            client.on_connect = _on_connect  # type: ignore[reportAttributeAccessIssue]
            client.connect(self._host, self._port, keepalive=30)
            client.loop_start()
            if not connect_event.wait(timeout=5):
                logger.warning("PahoMqttClient CONNACK not received within 5s")
                client.loop_stop()
                client.disconnect()
                return False
            result = client.publish(topic, payload.encode(), qos=1)
            result.wait_for_publish(timeout=5)
            client.loop_stop()
            client.disconnect()
            if not result.is_published() or result.rc != mqtt.MQTT_ERR_SUCCESS:
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    logger.warning("PahoMqttClient publish failed: rc=%s", result.rc)
                else:
                    logger.warning(
                        "PahoMqttClient publish not confirmed within 5s (rc=%s, mid=%s)",
                        result.rc,
                        result.mid,
                    )
                return False
            logger.info("PahoMqttClient confirmed publish to %s", topic)
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
