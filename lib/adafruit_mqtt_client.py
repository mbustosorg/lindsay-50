"""Adafruit IO MQTT subscriber for Flask (Heroku, TLS on port 8883).

Wraps Adafruit_IO.MQTTClient. On each incoming message calls
dispatch_callback(raw_payload).
"""

import logging
import threading
import time
from Adafruit_IO import MQTTClient, errors as aio_errors

from lib_shared.config_reader import get_config
cfg = get_config()

logger = logging.getLogger(__name__)


class _AdafruitMQTTClient(MQTTClient):
    """Subclass of Adafruit_IO.MQTTClient that handles broker rc != 0 gracefully.

    The Adafruit IO broker sometimes returns non-zero CONNACK codes (e.g. rc=6
    "Message not found" when the feed is new). The base class raises on any
    non-zero rc, which causes the client to repeatedly reconnect. This subclass
    treats rc=6 as a successful connection so the client loop stays alive and
    the subscription succeeds once the feed is populated.
    """

    def _mqtt_connect(self, client, userdata, flags, rc):
        logger.debug("AdafruitMqttClient CONNACK rc=%s", rc)
        if rc == 0:
            self._connected = True
            logger.info("Connected to Adafruit IO!")
        elif rc == 6:
            # "Message not found (internal error)" — feed may not exist yet.
            # Treat as connected; subscribe will work once feed has messages.
            self._connected = True
            logger.warning("AdafruitMqttClient CONNACK rc=6 (feed may not exist yet)")
        elif rc == 2:
            # "Network protocol error" — transient, treat as connected.
            self._connected = True
            logger.warning("AdafruitMqttClient CONNACK rc=2 (transient)")
        else:
            raise aio_errors.MQTTError(rc)
        if self.on_connect is not None:
            self.on_connect(self)

    def _mqtt_disconnect(self, client, userdata, rc):
        # Adafruit IO base class raises on any non-zero rc.
        # rc=2 ("network protocol error") can occur transiently — log and continue.
        if rc != 0:
            logger.warning("AdafruitMqttClient broker disconnect rc=%s", rc)
        self._connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self, rc)


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
            logger.info("AdafruitMqttClient on_connect called")
            _client.subscribe(self._feed)
            logger.info("AdafruitMqttClient subscribed to %s/%s", username, self._feed)

        def on_disconnect(_client, rc):
            logger.info("AdafruitMqttClient on_disconnect called: rc=%s", rc)
            if rc != 0:
                logger.warning("AdafruitMqttClient disconnected: rc=%s", rc)

        def on_message(_client, topic, payload):
            logger.info("AdafruitMqttClient on_message called: topic=%r payload=%r", topic, payload)
            self._dispatch(payload)

        self._stop = threading.Event()

        def _run():
            stop = self._stop
            assert stop is not None
            while not stop.is_set():
                try:
                    client = _AdafruitMQTTClient(username, key, service_host=cfg.AIO_HOST, secure=True)
                    client.on_connect = on_connect  # type: ignore[reportAttributeAccessIssue]
                    client.on_disconnect = on_disconnect  # type: ignore[reportAttributeAccessIssue]
                    client.on_message = on_message  # type: ignore[reportAttributeAccessIssue]
                    logger.info("AdafruitMqttClient connecting to %s...", cfg.AIO_HOST)
                    client.connect()
                    logger.info("AdafruitMqttClient connect() returned, is_connected=%s", client.is_connected())
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
