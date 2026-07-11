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

Two-topic support (status-flow extension): the constructor takes
optional `status_topic` + `status_dispatch_callback` parameters. When
both are set, the subscriber also subscribes to the status topic and
dispatches incoming messages to `status_dispatch_callback` based on
`msg.topic`. The two callbacks' exception paths are isolated — a
raise in one does not affect the other. The envelope publish path is
unchanged.
"""

import logging
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class PahoMqttClient:
    """Owns the paho-mqtt client lifecycle.

    Calls dispatch_callback(raw_payload) for each incoming message on
    the envelope topic. When `status_topic` + `status_dispatch_callback`
    are both configured, also dispatches incoming messages on the
    status topic to `status_dispatch_callback`. The two callbacks'
    exception paths are isolated.
    """

    def __init__(
        self,
        dispatch_callback: Callable[[str], None],
        *,
        host: str,
        port: int | str,
        username: str,
        password: str,
        topic: str,
        status_topic: Optional[str] = None,
        status_dispatch_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize the client.

        Args:
            dispatch_callback: Callable that accepts a raw MQTT payload string.
            host: MQTT broker host.
            port: MQTT broker port (int or numeric string — coerced to int).
            username: MQTT broker username.
            password: MQTT broker password.
            topic: Wire-format topic for the envelope flow. The client
                does no broker-specific translation; for Adafruit IO
                this must be the full "{username}/feeds/{feedname}" path.
            status_topic: Optional wire-format topic for the status flow.
                When set, the client also subscribes to it and dispatches
                incoming messages to `status_dispatch_callback`. Pass
                exactly the same string you'd pass as `topic`; resolved
                by `mqtt-status-topic-resolve` helper at the call site.
            status_dispatch_callback: Optional callable for the status
                flow. Required when `status_topic` is set.
        """
        if status_topic is not None and status_dispatch_callback is None:
            raise ValueError("PahoMqttClient: status_topic set without status_dispatch_callback")
        if status_dispatch_callback is not None and status_topic is None:
            raise ValueError("PahoMqttClient: status_dispatch_callback set without status_topic")

        self._dispatch = dispatch_callback
        self._status_dispatch = status_dispatch_callback
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None

        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._topic = topic
        self._status_topic = status_topic

    def start(self) -> None:
        """Connect to the broker and run the subscriber loop in a daemon thread."""
        topic = self._topic
        status_topic = self._status_topic
        logger.info(
            "PahoMqttClient will subscribe to topic=%r status_topic=%r username=%r",
            topic,
            status_topic,
            self._username,
        )

        def on_connect(_client, _userdata, _flags, rc):
            if rc == 0:
                # Subscribe to the envelope topic first (existing
                # behavior). When status_topic is set, also subscribe
                # to it — the same broker connection, the same
                # SUBSCRIBE flow.
                subscriptions = [(topic, 1)]
                if status_topic is not None:
                    subscriptions.append((status_topic, 1))
                results = []
                # `subscribe()` accepts a list; paho fires SUBACK
                # asynchronously via `on_subscribe` for each.
                for t, qos in subscriptions:
                    r, mid = _client.subscribe(t, qos)
                    logger.info(
                        "PahoMqttClient subscribe attempt: topic=%s " "subscribe_result=%s mid=%s",
                        t,
                        r,
                        mid,
                    )
                    results.append((t, r, mid))
            else:
                logger.warning("PahoMqttClient connection failed: rc=%s", rc)

        def on_subscribe(_client, _userdata, _mid, _granted_qos):
            # Fired when the broker sends SUBACK. Confirms the subscription
            # is now active — without this, a successful on_connect can
            # still leave us receiving nothing (broker accepted the TCP
            # connection but rejected the topic). With both on_connect +
            # on_subscribe logged, "no messages" gaps in the journalctl
            # mean the broker isn't delivering, not that we're not subscribed.
            logger.info(
                "PahoMqttClient subscribe ACKed: mid=%s granted_qos=%s",
                _mid,
                _granted_qos,
            )

        def on_message(_client, _userdata, msg):
            """Dispatch by msg.topic to the right callback.

            Two callbacks' exception paths MUST be isolated — a raise in
            `status_dispatch_callback` does not affect `dispatch_callback`
            and vice versa (Decision 8 in openspec/changes/
            add-sign-status-reports/design.md). We achieve isolation by
            wrapping each callback invocation in its own try/except so
            an exception in one does not propagate to the other.

            Round 7b (live-bug triage, "are the logs upstream?"):
            every inbound MQTT message fires two INFO records at
            the broker→app boundary BEFORE any envelope parsing
            or Python dispatch logic runs:
              - `[MQTT_INCOMING]` — single keyword grep target
                carrying topic + payload byte count + first 200
                bytes. Operator can verify "did the broker
                deliver this message?" without parsing the
                paho internals.
              - `PahoMqttClient received:` — the long-standing
                record carrying `topic=` and `payload=%r` for
                backward-compatible log scrapers.
            Both lines are deterministic per inbound message —
            same byte count, same first-200 preview — so a
            network replay or duplicate-deliver detection is
            possible by inspecting consecutive lines.
            """
            payload_bytes = len(msg.payload)
            preview = msg.payload[:200]
            logger.info(
                "[MQTT_INCOMING] topic=%s bytes=%d preview=%r",
                msg.topic,
                payload_bytes,
                preview,
            )
            logger.info(
                "PahoMqttClient received: topic=%s payload=%r",
                msg.topic,
                msg.payload,
            )
            payload = msg.payload.decode(errors="replace")
            if msg.topic == topic:
                try:
                    self._dispatch(payload)
                    logger.debug("PahoMqttClient dispatch callback returned cleanly")
                except Exception as e:
                    logger.warning(
                        "PahoMqttClient dispatch callback raised: %s",
                        e,
                        exc_info=True,
                    )
                return
            if msg.topic == status_topic and self._status_dispatch is not None:
                try:
                    self._status_dispatch(payload)
                    logger.debug("PahoMqttClient status dispatch returned cleanly")
                except Exception as e:
                    logger.warning(
                        "PahoMqttClient status dispatch raised: %s",
                        e,
                        exc_info=True,
                    )
                return
            # Some other topic — log at DEBUG and drop. This shouldn't
            # happen if the broker is configured correctly.
            logger.debug(
                "PahoMqttClient ignoring message on unhandled topic=%s",
                msg.topic,
            )

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
                    client.on_subscribe = on_subscribe  # type: ignore[reportAttributeAccessIssue]
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
