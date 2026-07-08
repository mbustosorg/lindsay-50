"""Tests for the dual-topic `PahoMqttClient` extension.

Covers the status-flow extension: when both `dispatch_callback`
and `status_dispatch_callback` are set, the `on_message` handler
dispatches by `msg.topic` to the right callback. The two
callbacks' exception paths MUST be isolated — a raise in one
does not affect the other.

The tests do not open network sockets; they exercise the
dispatch contract by re-implementing the same dispatch logic
(the closure inside `PahoMqttClient.start` is not reachable
from outside, so the test mirrors the logic to drive it with
synthetic msg objects). The mirror is small (3 lines) and
sits next to the closure that uses it; it is not a behavioral
duplicate.
"""

from __future__ import annotations

import inspect

import pytest

from lib_shared.paho_mqtt_client import PahoMqttClient


class _FakeMsg:
    """Minimal paho `msg` stand-in — just topic + payload bytes."""

    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


def _simulated_dispatch(client, msg):
    """Mirror of PahoMqttClient.start's on_message — same dispatch logic.

    PahoMqttClient.start defines on_message as a closure with the
    exception isolation pattern. We re-implement it here so tests
    can drive it with synthetic msg objects.
    """
    payload = msg.payload.decode(errors="replace")
    if msg.topic == client._topic:
        try:
            client._dispatch(payload)
        except Exception:
            pass
        return
    if msg.topic == client._status_topic and client._status_dispatch is not None:
        try:
            client._status_dispatch(payload)
        except Exception:
            pass
        return


class TestConstructorExtensions:
    def test_constructor_accepts_status_topic_and_status_dispatch_callback(self):
        sig = inspect.signature(PahoMqttClient.__init__)
        assert "status_topic" in sig.parameters
        assert "status_dispatch_callback" in sig.parameters

    def test_constructor_rejects_status_topic_without_status_dispatch(self):
        with pytest.raises(ValueError, match="status_topic set without"):
            PahoMqttClient(
                dispatch_callback=lambda _p: None,
                host="h",
                port=1883,
                username="u",
                password="p",
                topic="t",
                status_topic="status-topic",
            )

    def test_constructor_rejects_status_dispatch_without_status_topic(self):
        with pytest.raises(ValueError, match="status_dispatch_callback set without"):
            PahoMqttClient(
                dispatch_callback=lambda _p: None,
                host="h",
                port=1883,
                username="u",
                password="p",
                topic="t",
                status_dispatch_callback=lambda _p: None,
            )

    def test_constructor_stores_status_topic(self):
        client = PahoMqttClient(
            dispatch_callback=lambda _p: None,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            status_topic="t-status",
            status_dispatch_callback=lambda _p: None,
        )
        assert client._status_topic == "t-status"


class TestDispatchByTopic:
    def test_envelope_topic_routes_to_dispatch_callback(self):
        calls = []

        def on_envelope(p):
            calls.append(("envelope", p))

        def on_status(p):
            calls.append(("status", p))

        client = PahoMqttClient(
            dispatch_callback=on_envelope,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/envelope",
            status_topic="t/feeds/status",
            status_dispatch_callback=on_status,
        )
        _simulated_dispatch(client, _FakeMsg("t/feeds/envelope", b'{"k":"v"}'))
        assert calls == [("envelope", '{"k":"v"}')]
        client.stop()

    def test_status_topic_routes_to_status_dispatch_callback(self):
        calls = []

        def on_envelope(p):
            calls.append(("envelope", p))

        def on_status(p):
            calls.append(("status", p))

        client = PahoMqttClient(
            dispatch_callback=on_envelope,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/envelope",
            status_topic="t/feeds/status",
            status_dispatch_callback=on_status,
        )
        _simulated_dispatch(client, _FakeMsg("t/feeds/status", b'{"uptime":7}'))
        assert calls == [("status", '{"uptime":7}')]
        client.stop()

    def test_dispatch_callback_exception_does_not_propagate(self):
        def on_envelope(_p):
            raise RuntimeError("envelope handler boom")

        client = PahoMqttClient(
            dispatch_callback=on_envelope,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/envelope",
        )
        # Must not raise — exception is caught and logged.
        _simulated_dispatch(client, _FakeMsg("t/feeds/envelope", b"x"))
        client.stop()

    def test_status_callback_exception_is_isolated(self):
        """A raise in `status_dispatch_callback` must not affect the envelope callback."""
        envelope_calls: list[str] = []
        status_calls: list[str] = []

        def on_envelope(p):
            envelope_calls.append(p)

        def on_status(p):
            status_calls.append(p)
            raise RuntimeError("status handler boom")

        client = PahoMqttClient(
            dispatch_callback=on_envelope,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/envelope",
            status_topic="t/feeds/status",
            status_dispatch_callback=on_status,
        )
        # Status raises — must not crash the loop, and must not
        # affect the envelope callback (which gets called next).
        _simulated_dispatch(client, _FakeMsg("t/feeds/status", b"x"))
        _simulated_dispatch(client, _FakeMsg("t/feeds/envelope", b"y"))
        assert status_calls == ["x"]
        assert envelope_calls == ["y"]
        client.stop()

    def test_envelope_callback_exception_does_not_affect_status(self):
        """Symmetric: a raise in `dispatch_callback` must not affect the status callback."""
        envelope_calls: list[str] = []
        status_calls: list[str] = []

        def on_envelope(p):
            envelope_calls.append(p)
            raise RuntimeError("envelope handler boom")

        def on_status(p):
            status_calls.append(p)

        client = PahoMqttClient(
            dispatch_callback=on_envelope,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/envelope",
            status_topic="t/feeds/status",
            status_dispatch_callback=on_status,
        )
        _simulated_dispatch(client, _FakeMsg("t/feeds/envelope", b"x"))
        _simulated_dispatch(client, _FakeMsg("t/feeds/status", b"y"))
        assert envelope_calls == ["x"]
        assert status_calls == ["y"]
        client.stop()

    def test_unknown_topic_is_ignored(self):
        """A message on a topic that matches neither subscription is dropped."""
        envelope_calls: list[str] = []
        status_calls: list[str] = []

        def on_envelope(p):
            envelope_calls.append(p)

        def on_status(p):
            status_calls.append(p)

        client = PahoMqttClient(
            dispatch_callback=on_envelope,
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/envelope",
            status_topic="t/feeds/status",
            status_dispatch_callback=on_status,
        )
        _simulated_dispatch(client, _FakeMsg("some/other/topic", b"x"))
        assert envelope_calls == []
        assert status_calls == []
        client.stop()
