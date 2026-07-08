"""Tests for `heart-matrix-controller/status_publisher.py`.

Covers the long-lived paho publisher that pushes StatusSnapshot
JSON to MQTT_STATUS_TOPIC every 5s. Lifecycle assertions:
connect_async + loop_start at construction, publish() at QoS 0,
thread-safe concurrent publish, reconnect timer on non-success
rc, close() pairs loop_stop with loop_start.

Uses a Mock client injected via `client_factory` so no real paho
network calls run during the test. paho's own `MQTT_ERR_SUCCESS`
is referenced for the rc-comparison logic.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest

from status_publisher import StatusPublisher


@pytest.fixture
def mock_client_factory():
    """Return a (factory, client) pair.

    `factory(clean_session=True)` returns the mock `client`; the
    StatusPublisher constructor uses the factory to acquire its
    paho client.
    """
    client = MagicMock()
    factory = MagicMock(return_value=client)
    return factory, client


class TestStatusPublisherLifecycle:
    def test_constructor_calls_connect_async_and_loop_start(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="mqtt.example.com",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/status",
            client_factory=factory,
        )
        # Factory was called with clean_session=True
        factory.assert_called_once_with(clean_session=True)
        # connect_async was called with the configured host/port
        client.connect_async.assert_called_once_with("mqtt.example.com", 1883, keepalive=60)
        # loop_start was called once
        client.loop_start.assert_called_once()
        # The publisher is now usable.
        assert pub is not None
        pub.close()

    def test_constructor_enables_tls_for_port_8883(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="io.adafruit.com",
            port=8883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        # tls_set_context was called (TLS for 8883)
        client.tls_set_context.assert_called_once()
        pub.close()

    def test_constructor_skips_tls_for_non_8883_port(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="localhost",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        # TLS not enabled on 1883.
        client.tls_set_context.assert_not_called()
        pub.close()

    def test_constructor_sets_credentials(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="alice",
            password="secret",
            topic="t",
            client_factory=factory,
        )
        client.username_pw_set.assert_called_once_with("alice", "secret")
        pub.close()

    def test_port_coerced_to_int(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="h",
            port="8883",
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        # connect_async was called with an int port.
        args, _ = client.connect_async.call_args
        assert args[1] == 8883
        assert isinstance(args[1], int)
        pub.close()


class TestPublish:
    def test_publish_encodes_payload_and_calls_client_publish(self, mock_client_factory):
        factory, client = mock_client_factory
        # Default MQTT publish rc on a MagicMock is a Mock, not the
        # paho ERR_SUCCESS constant. Make the rc match.
        client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t/feeds/status",
            client_factory=factory,
        )
        ok = pub.publish({"active_sha": "abc1234", "uptime_seconds": 7})
        assert ok is True
        # publish was called with the topic (positional), encoded
        # payload (positional), qos=0 (keyword in this implementation).
        # The contract is "topic, payload, qos=0" — assert the encoded
        # payload and the qos are correct; the topic is the second
        # positional in some paho signatures and the first here.
        call = client.publish.call_args
        # The StatusPublisher code calls `client.publish(topic, payload, qos=0)`,
        # so topic + payload are positional, qos is keyword. Use `.args`
        # and `.kwargs` for a robust assertion.
        assert call.args[0] == "t/feeds/status"
        assert call.args[1] == b'{"active_sha":"abc1234","uptime_seconds":7}'
        assert call.kwargs.get("qos") == 0
        pub.close()

    def test_publish_returns_false_on_non_success_rc(self, mock_client_factory):
        factory, client = mock_client_factory
        # Simulate a failed publish (e.g. broker disconnect).
        client.publish.return_value.rc = mqtt.MQTT_ERR_NO_CONN
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
            reconnect_interval_s=0.05,
        )
        ok = pub.publish({"active_sha": "x"})
        assert ok is False
        # Reconnect timer should be scheduled — give the timer a
        # moment to fire, then verify a second connect_async attempt.
        time.sleep(0.1)
        assert client.connect_async.call_count >= 2
        pub.close()

    def test_publish_swallows_client_publish_exception(self, mock_client_factory):
        factory, client = mock_client_factory
        client.publish.side_effect = RuntimeError("network error")
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
            reconnect_interval_s=0.05,
        )
        ok = pub.publish({"active_sha": "x"})
        assert ok is False
        # Reconnect timer fires — close() should not raise.
        pub.close()

    def test_publish_is_thread_safe(self, mock_client_factory):
        """Concurrent publish() calls from multiple threads must not raise."""
        factory, client = mock_client_factory
        client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        errors: list[Exception] = []
        iterations = 50

        def publish_many(label: str) -> None:
            try:
                for i in range(iterations):
                    pub.publish({"active_sha": f"{label}-{i}"})
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=publish_many, args=("T1",))
        t2 = threading.Thread(target=publish_many, args=("T2",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not errors
        # Total publishes = at least iterations per thread.
        assert client.publish.call_count >= iterations * 2
        pub.close()


class TestClose:
    def test_close_calls_loop_stop_and_disconnect(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        # Sanity: loop_start was called once at construction.
        client.loop_start.assert_called_once()
        pub.close()
        # loop_stop pairs loop_start.
        client.loop_stop.assert_called_once()
        client.disconnect.assert_called_once()

    def test_close_is_idempotent(self, mock_client_factory):
        factory, client = mock_client_factory
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        pub.close()
        # Second close is a no-op — loop_stop and disconnect are not
        # called a second time.
        client.loop_stop.reset_mock()
        client.disconnect.reset_mock()
        pub.close()
        client.loop_stop.assert_not_called()
        client.disconnect.assert_not_called()

    def test_close_swallows_loop_stop_exception(self, mock_client_factory):
        factory, client = mock_client_factory
        client.loop_stop.side_effect = RuntimeError("loop stop boom")
        pub = StatusPublisher(
            host="h",
            port=1883,
            username="u",
            password="p",
            topic="t",
            client_factory=factory,
        )
        # Should not raise.
        pub.close()
        # disconnect is still attempted.
        client.disconnect.assert_called_once()
