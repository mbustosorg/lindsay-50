"""Long-lived MQTT publisher for the status flow.

The Pi publishes `StatusSnapshot` JSON to MQTT_STATUS_TOPIC every 5s. A
fresh-client-per-publish approach would burn 17,280 TCP+TLS handshakes per
day per sign at 5s cadence — long-lived is the right pattern for a regular
heartbeat (Decision 9 in openspec/changes/add-sign-status-reports/
design.md).

`StatusPublisher` wraps a single paho `mqtt.Client`:

  - Constructor calls `connect_async(...)` and `loop_start()`. The
    `loop_start()` background thread handles the network; the render
    loop's tick thread calls `publish()` directly without blocking.
  - `publish(payload_dict)` JSON-encodes the payload and calls
    `client.publish(topic, payload.encode(), qos=0)`. paho's
    `client.publish()` is thread-safe and non-blocking — it enqueues
    into the outgoing buffer; the loop thread reads from it. QoS 0
    means fire-and-forget (Decision 3); a slow broker cannot stall
    the render loop.
  - On `publish()` returning a non-success rc (broker disconnect or
    buffer full), a `threading.Timer` schedules a reconnect attempt
    5 seconds later.
  - `close()` stops the loop and disconnects cleanly so the broker
    doesn't accumulate stale sessions.

This class is the device-side MQTT path for the status flow. It sits
alongside the Flask-side `PahoMqttClient.publish_envelope` (irregular
SMS-driven publishes, fresh-client-per-call — see Decision 9's
"why long-lived for status but fresh-client-per-call for envelope").
The two paths do not share a long-lived publisher; a status-publish
failure does not affect the envelope-publish path or the `.status.json`
file write (Decision 13).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


# Reconnect interval when a publish returns a non-success rc. The Pi's
# status flow operates on a 5s publish cadence, so a 5s reconnect timer
# matches the network-throttled recovery window. paho's loop thread
# also retries CONNACK on its own; the timer is a second line of
# defense if a publish reveals the disconnect before a read does.
DEFAULT_RECONNECT_INTERVAL_S = 5.0


class StatusPublisher:
    """Long-lived paho publisher for the status flow.

    Holds a single `mqtt.Client` for the lifetime of the Pi process.
    `publish()` is thread-safe (paho enqueues into the outgoing
    buffer; loop_start()'s background thread handles the network).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int | str,
        username: str,
        password: str,
        topic: str,
        reconnect_interval_s: float = DEFAULT_RECONNECT_INTERVAL_S,
        client_factory: Any = None,
    ) -> None:
        """Initialize the publisher; connect_async + loop_start at construct time.

        Args:
            host: MQTT broker host (e.g. "io.adafruit.com").
            port: MQTT broker port — int or numeric string. 8883
                triggers TLS via `tls_set_context`; other ports connect
                in the clear (matching PahoMqttClient's behavior).
            username: MQTT broker username.
            password: MQTT broker password.
            topic: Wire-format topic to publish on (e.g.
                "user/feeds/lindsay50-status").
            reconnect_interval_s: Seconds to wait before retrying a
                non-success publish. Default 5s.
            client_factory: Test override for the paho `mqtt.Client`
                constructor. Production passes None and gets the
                default; tests inject a Mock to assert on lifecycle.
        """
        self._host = host
        self._port = int(port)
        self._username = username
        self._password = password
        self._topic = topic
        self._reconnect_interval_s = reconnect_interval_s
        self._factory = client_factory

        # Construction-time client setup. Tests pass a MagicMock for
        # `_factory` to inspect calls without networking.
        self._client: Any = self._build_client()
        self._client.username_pw_set(self._username, self._password)
        if self._port == 8883:
            # TLS required for Adafruit IO and any other 8883 broker.
            self._client.tls_set_context()
        # `connect_async` returns immediately; `loop_start` runs a
        # background thread that handles the network without blocking
        # the caller. `publish()` is thread-safe against this
        # background thread.
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

        # Reconnect bookkeeping. A threading.Timer schedules a single
        # reconnect attempt; the timer is owned by `publish()` and
        # recreated per failure so a stuck broker doesn't pile up
        # timers.
        self._reconnect_timer: Optional[threading.Timer] = None

        # Idempotency guard for `close()`. A second close() is a no-op
        # — does not re-stop the loop, does not re-disconnect. The
        # render loop's `finally` block calls close() once at shutdown;
        # callers don't have to reason about double-close.
        self._closed: bool = False

    def _build_client(self) -> Any:
        """Construct the paho client. Tests override via `client_factory`."""
        if self._factory is not None:
            return self._factory(clean_session=True)  # type: ignore[reportCallIssue]
        return mqtt.Client(clean_session=True)  # type: ignore[reportPrivateImportUsage]

    def publish(self, payload_dict: dict[str, Any]) -> bool:
        """Publish a JSON-encoded payload at QoS 0 to the configured topic.

        Returns True on success, False on any error or non-success rc.
        On failure, schedules a `threading.Timer` to reconnect after
        `reconnect_interval_s` seconds (cancels any pending reconnect
        first so multiple failures don't pile up timers).
        """
        try:
            payload = json.dumps(payload_dict, separators=(",", ":"))
            result = self._client.publish(
                self._topic,
                payload.encode("utf-8"),
                qos=0,
            )
            rc = getattr(result, "rc", None)
            # `MQTT_ERR_NO_CONN` is a transient state — paho's loop thread
            # already handles CONNACK retries on its own, so the defensive
            # reconnect timer would just pile up redundant reconnects on
            # every brief disconnect. Only schedule a reconnect for rc
            # values paho won't auto-recover from (queue full, bad topic,
            # etc.). The QoS-0 fire-and-forget semantics mean a brief
            # disconnect drops the current publish; paho will catch up on
            # the next 5s tick.
            if rc is not None and rc != mqtt.MQTT_ERR_SUCCESS and rc != mqtt.MQTT_ERR_NO_CONN:
                logger.warning(
                    "StatusPublisher.publish: rc=%s (topic=%s); scheduling reconnect",
                    rc,
                    self._topic,
                )
                self._schedule_reconnect()
                return False
            return True
        except Exception as exc:
            logger.warning("StatusPublisher.publish raised: %s", exc)
            self._schedule_reconnect()
            return False

    def _schedule_reconnect(self) -> None:
        """Cancel any pending reconnect timer and schedule a fresh one.

        A single timer in flight is enough — paho's background loop
        also retries CONNACK on its own; the timer just bounds the
        gap if a publish reveals the disconnect before a read does.
        """
        if self._reconnect_timer is not None:
            try:
                self._reconnect_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(self._reconnect_interval_s, self._do_reconnect)
        timer.daemon = True
        self._reconnect_timer = timer
        timer.start()

    def _do_reconnect(self) -> None:
        """Synchronously retry the connection. The loop thread resumes."""
        self._reconnect_timer = None
        try:
            logger.info("StatusPublisher: reconnecting to %s:%d", self._host, self._port)
            # `connect_async` is idempotent — if already connecting, the
            # loop will continue; if disconnected, it will reconnect.
            self._client.connect_async(self._host, self._port, keepalive=60)
        except Exception as exc:
            logger.warning("StatusPublisher reconnect raised: %s", exc)
            # Schedule another attempt; otherwise a stuck broker leaves
            # the publisher silent until the next publish() fails.
            self._schedule_reconnect()

    def close(self) -> None:
        """Cleanly stop the loop, cancel reconnect timer, and disconnect.

        Idempotent — calling close() on an already-closed publisher
        is a no-op. Called from `main.py`'s shutdown `finally` block.
        """
        if self._closed:
            return
        self._closed = True
        if self._reconnect_timer is not None:
            try:
                self._reconnect_timer.cancel()
            except Exception:
                pass
            self._reconnect_timer = None
        try:
            self._client.loop_stop()
        except Exception as exc:
            logger.warning("StatusPublisher.loop_stop raised: %s", exc)
        try:
            self._client.disconnect()
        except Exception as exc:
            logger.warning("StatusPublisher.disconnect raised: %s", exc)
