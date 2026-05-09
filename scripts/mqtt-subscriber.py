#!/usr/bin/env python3
"""Local MQTT subscriber for testing Flask MQTT publishing.

Subscribes to a topic and prints messages as they arrive.
Run with: python3 scripts/mqtt-subscriber.py

Configure via settings.toml in the project root:
  MQTT_HOST     - broker host (default: localhost)
  MQTT_PORT     - broker port (default: 1883)
  MQTT_USERNAME - username (default: test-user)
  MQTT_PASSWORD - password (default: test-key)
  AIO_USERNAME  - used to build the topic: {username}/feeds/{feed}
  AIO_FEED      - feed name to subscribe to (default: test-feed)
"""

import sys
import tomllib
from pathlib import Path

import paho.mqtt.client as mqtt


def _load_settings() -> dict:
    settings_path = Path(__file__).parent.parent / "heart-sms-receiver" / "settings.toml"
    if not settings_path.exists():
        print("WARNING: settings.toml not found, using defaults", file=sys.stderr)
        return {}
    with open(settings_path, "rb") as f:
        return tomllib.load(f)


def _topic(username: str, feed: str) -> str:
    if "/feeds/" in feed:
        return feed
    return f"{username}/feeds/{feed}"


def main() -> None:
    cfg = _load_settings()

    host = cfg.get("MQTT_HOST", "localhost")
    port = int(cfg.get("MQTT_PORT", 1883))
    username = cfg.get("MQTT_USERNAME", "test-user")
    password = cfg.get("MQTT_PASSWORD", "test-key")
    aio_user = cfg.get("AIO_USERNAME", "test-user")
    feed = cfg.get("AIO_FEED", "test-feed")

    topic = _topic(aio_user, feed)

    print(f"Connecting to MQTT broker {host}:{port} ...")
    print(f"Subscribing to: {topic}")

    def on_connect(_client, _userdata, _flags, rc):
        if rc == 0:
            print("Connected OK")
            _client.subscribe(topic)
            print(f"Subscribed to {topic}")
        else:
            print(f"Connection failed: rc={rc}", file=sys.stderr)

    def on_message(_client, _userdata, msg):
        payload = msg.payload.decode(errors="replace")
        print(f"[{msg.topic}] {payload}")

    def on_disconnect(_client, _userdata, rc):
        print(f"Disconnected: rc={rc}")

    client = mqtt.Client()
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    try:
        client.connect(host, port, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nExiting...")
        client.disconnect()


if __name__ == "__main__":
    main()
