"""Paho MQTT subscriber for Flask (local dev with Mosquitto, plain TCP).

Wraps raw paho-mqtt.client. On each incoming message calls
dispatch_callback(raw_payload).
"""

import logging
import paho.mqtt.client as mqtt

from lib_shared.config_reader import get_config
cfg = get_config()

logger = logging.getLogger(__name__)


class PahoMqttClient:
    """Thin adapter: owns the paho-mqtt client lifecycle.

    Calls dispatch_callback(raw_payload) for each incoming message.
    """

    def __init__(self, dispatch_callback, feed: str):
        self._dispatch = dispatch_callback
        self._feed = feed
        self._thread = None
        self._stop = None

    def start(self) -> None:
        import threading
        import time

        username = cfg.AIO_USERNAME
        host = cfg.AIO_HOST
        port = int(cfg.AIO_PORT)

        if "/feeds/" in self._feed:
            topic = self._feed
        else:
            topic = f"{username}/feeds/{self._feed}"

        def on_connect(_client, _userdata, _flags, rc):
            if rc == 0:
                _client.subscribe(topic)
                logger.info("PahoMqttClient subscribed to %s", topic)
            else:
                logger.warning("PahoMqttClient connection failed: rc=%s", rc)

        def on_message(_client, _userdata, msg):
            self._dispatch(msg.payload.decode(errors="replace"))

        def on_disconnect(_client, _userdata, rc):
            if rc != 0:
                logger.warning("PahoMqttClient disconnected: rc=%s", rc)

        self._stop = threading.Event()

        def _run():
            stop = self._stop
            assert stop is not None
            while not stop.is_set():
                try:
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, clean_session=True)  # type: ignore[reportPrivateImportUsage]
                    client.username_pw_set(username, cfg.AIO_KEY)
                    client.on_connect = on_connect  # type: ignore[reportAttributeAccessIssue]
                    client.on_message = on_message  # type: ignore[reportAttributeAccessIssue]
                    client.on_disconnect = on_disconnect  # type: ignore[reportAttributeAccessIssue]
                    # Adafruit IO broker only supports MQTT 3.1.1, not v5
                    client.protocol = mqtt.MQTTv311  # type: ignore[reportAttributeAccessIssue]
                    # TLS required for port 8883
                    if port == 8883:
                        client.tls_set_context()
                    logger.info("PahoMqttClient connecting to %s:%d...", host, port)
                    client.connect(host, port, keepalive=60)
                    client.loop_forever()
                except Exception as e:
                    if not stop.is_set():
                        logger.warning("PahoMqttClient error: %s. Reconnecting in 5s...", e)
                        time.sleep(5)

        self._thread = threading.Thread(target=_run, name="paho-mqtt", daemon=True)
        self._thread.start()
        logger.info("PahoMqttClient started for feed %s", self._feed)

    def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
