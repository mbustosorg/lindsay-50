"""CircuitPython MQTT client for heart-matrix-controller.

Wraps adafruit_io.adafruit_io.IO_MQTT. On each incoming message,
calls dispatch_callback(raw_payload).

Reconnection is handled by the caller (main loop in code.py).
"""

import os
import wifi
import socketpool
import adafruit_connection_manager
import adafruit_minimqtt.adafruit_minimqtt as MQTT
from adafruit_io.adafruit_io import IO_MQTT

from lib_shared.config_reader import get_config

cfg = get_config()


class CircuitPythonMqttClient:
    """Owns the IO_MQTT lifecycle; dispatches raw payload to callback."""

    def __init__(self, dispatch_callback):
        """Create client.

        Args:
            dispatch_callback: called with (body: str) for type="message" envelopes.
                             Called with None for type="config" envelopes (ESP32 ignores config).
            feed: Adafruit IO feed name (e.g. "lindsay50").
        """
        self._dispatch = dispatch_callback
        self._io = None
        self._mqtt = None
        self._host = cfg.MQTT_HOST
        self._port = int(cfg.MQTT_PORT)
        self._username = cfg.MQTT_USERNAME
        self._password = cfg.MQTT_PASSWORD
        self._feed = cfg.MQTT_TOPIC

    def start(self) -> None:
        """Set up MQTT and IO_MQTT, connect and subscribe."""
        """
        # IO_MQTT.subscribe() takes the feed name, not the full "{user}/feeds/{feed}" path.
        _feed = cfg.MQTT_TOPIC.rsplit("/feeds/", 1)[-1]

        if "/feeds/" in self._feed:
            topic = self._feed
        else:
            topic = f"{username}/feeds/{self._feed}"
        
        print("PahoMqttClient will subscribe to topic=%r feed=%r username=%r", topic, self._feed, username)
        """

        pool = socketpool.SocketPool(wifi.radio)
        ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)

        def on_connect(client):
            print("Connected to Adafruit IO")
            client.subscribe(self._feed)

        def on_disconnect(client, rc):
            print(f"Disconnected from Adafruit IO: rc={rc}")

        def on_subscribe(client, userdata, topic, granted_qos):
            print(f"Subscribed to {topic} with QOS {granted_qos}")

        def on_message(client, feed_id, payload):
            print(f"Feed {feed_id} received: {payload!r}")
            self._dispatch(payload)

        self._mqtt = MQTT.MQTT(
            broker=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            is_ssl=(self._port == 8883),
            socket_pool=pool,
            ssl_context=ssl_context,
            socket_timeout=0.001,
        )

        self._io = IO_MQTT(self._mqtt)
        self._io.on_connect = on_connect
        self._io.on_disconnect = on_disconnect
        self._io.on_subscribe = on_subscribe
        self._io.on_message = on_message

        print(f"Connecting to MQTT broker...")
        self._io.connect()

    def reconnect(self) -> None:
        """Attempt to reconnect the MQTT client."""
        if self._io is not None:
            print("Reconnecting MQTT client...")
            self._io.reconnect()

    def loop(self, timeout=0.001) -> None:
        """Process MQTT events. Call in the main loop."""
        if self._io is not None:
            self._io.loop(timeout=timeout)
