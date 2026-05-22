"""Adafruit IO MQTT subscriber for Flask (Heroku, TLS on port 8883).

Wraps Adafruit_IO.MQTTClient. On each incoming message calls
dispatch_callback(raw_payload).
"""

import logging
import threading
import time
from Adafruit_IO import MQTTClient

from lib_shared.config_reader import get_config
cfg = get_config()

logger = logging.getLogger(__name__)


class AdafruitMqttClient:
    """Thin adapter: owns the Adafruit_IO.MQTTClient lifecycle.

    Calls dispatch_callback(raw_payload) for each incoming message.
    """

    def __init__(self, dispatch_callback, feed: str):
        self._dispatch = dispatch_callback
        self._feed = feed
        self._thread = None
        self._stop = None

    def start(self) -> None:
        username = cfg.AIO_USERNAME
        key = cfg.AIO_KEY

        def on_connect(_client):
            _client.subscribe(self._feed)
            logger.info("AdafruitMqttClient subscribed to %s/%s", username, self._feed)

        def on_disconnect(_client, rc):
            if rc != 0:
                logger.warning("AdafruitMqttClient disconnected: rc=%s", rc)

        def on_message(_client, topic, payload):
            self._dispatch(payload)

        self._stop = threading.Event()

        def _run():
            stop = self._stop
            assert stop is not None
            while not stop.is_set():
                try:
                    client = MQTTClient(username, key, service_host=cfg.AIO_HOST, secure=True)
                    client.on_connect = on_connect  # type: ignore[reportAttributeAccessIssue]
                    client.on_disconnect = on_disconnect  # type: ignore[reportAttributeAccessIssue]
                    client.on_message = on_message  # type: ignore[reportAttributeAccessIssue]
                    logger.info("AdafruitMqttClient connecting to %s...", cfg.AIO_HOST)
                    client.connect()
                    client.loop_background()
                    while not stop.is_set() and client.is_connected():
                        time.sleep(1)
                except Exception as e:
                    if not stop.is_set():
                        logger.warning("AdafruitMqttClient error: %s. Reconnecting in 5s...", e)
                        time.sleep(5)

        self._thread = threading.Thread(target=_run, name="adafruit-mqtt", daemon=True)
        self._thread.start()
        logger.info("AdafruitMqttClient started for feed %s", self._feed)

    def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
