"""Tests for `lib_shared/paho_mqtt_client.py`.

Covers the v2 invariant: `PahoMqttClient` does NOT accept
`on_connect_callback`. The previous v1 design had Flask publish
a `command=reboot` envelope on every MQTT reconnect, turning
network flakiness into a reboot hint. v2 publishes the
`check-for-update` envelope exactly once at Flask startup and
reconnects publish nothing.

We don't actually open network sockets in these tests — the
`start()` method spawns a daemon thread that connects to the
broker, but the constructor doesn't talk to anything. So we
can assert on the constructor signature and the stored fields
without spinning up a real MQTT broker.
"""

from __future__ import annotations

import inspect


def test_constructor_does_not_accept_on_connect_callback():
    """PahoMqttClient.__init__ must not accept an on_connect_callback kwarg.

    v2 design: the `command=check-for-update` hint is published once
    at Flask startup; reconnects publish nothing. A misconfigured
    caller passing the old kwarg is a regression we want to catch
    in unit tests, not in production.
    """
    from lib_shared.paho_mqtt_client import PahoMqttClient

    sig = inspect.signature(PahoMqttClient.__init__)
    assert "on_connect_callback" not in sig.parameters


def test_constructor_accepts_required_kwargs():
    """PahoMqttClient.__init__ accepts dispatch_callback + the connection kwargs."""
    from lib_shared.paho_mqtt_client import PahoMqttClient

    sig = inspect.signature(PahoMqttClient.__init__)
    for name in ("dispatch_callback", "host", "port", "username", "password", "topic"):
        assert name in sig.parameters, f"missing kwarg {name!r}"


def test_constructor_stores_connection_params():
    """The constructor stores the connection params on `self` without touching the network."""
    from lib_shared.paho_mqtt_client import PahoMqttClient

    client = PahoMqttClient(
        dispatch_callback=lambda *_: None,
        host="mqtt.example.com",
        port=1883,
        username="u",
        password="p",
        topic="t/feeds/sign",
    )
    assert client._host == "mqtt.example.com"
    assert client._port == 1883
    assert client._username == "u"
    assert client._password == "p"
    assert client._topic == "t/feeds/sign"


def test_port_coerced_to_int():
    """`port` accepts int or numeric string — coerced to int."""
    from lib_shared.paho_mqtt_client import PahoMqttClient

    client = PahoMqttClient(
        dispatch_callback=lambda *_: None,
        host="x",
        port="8883",
        username="u",
        password="p",
        topic="t",
    )
    assert client._port == 8883
    assert isinstance(client._port, int)


def test_start_does_not_block():
    """start() returns immediately — the actual MQTT loop runs in a daemon thread."""
    from lib_shared.paho_mqtt_client import PahoMqttClient

    client = PahoMqttClient(
        dispatch_callback=lambda *_: None,
        host="localhost",
        port=1883,
        username="u",
        password="p",
        topic="t",
    )
    # Must not raise even with no broker listening on localhost
    # (the thread will error and retry, but the daemon itself
    # returns control).
    client.start()
    # Stop immediately to avoid leaving a connection dangling.
    client.stop()
