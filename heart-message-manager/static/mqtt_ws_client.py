"""Browser-side MQTT-over-WebSocket client (PyScript wrapper).

Calls the native JS shim in `mqtt_ws_client.js` via Pyodide's
`create_proxy`. The Python side holds the user-supplied `on_envelope`
callable; the shim's `onStatus` callback drives the connection status
UI block in `templates/preview.html`.

Used by the base template's `app.js` to receive inbound
`MessageEnvelope` JSON on the configured topic and dispatch them into
the in-browser `MessageManager`.
"""

from js import createMqttWsClient, create_proxy  # type: ignore[import-not-found]  # noqa: F401


class MqttWsClient:
    """Thin Python wrapper around the native JS MQTT-WS shim.

    Args:
        ws_url: The broker's MQTT-over-WebSocket endpoint
                (e.g. `ws://localhost:9002/mqtt` or `wss://io.adafruit.com/mqtt`).
        username: MQTT broker username.
        password: MQTT broker password.
        topic: MQTT topic to subscribe to.
        on_envelope: Python callable invoked with each inbound PUBLISH
                     payload decoded as a UTF-8 string (raw JSON).
        long_disconnect_ms: Threshold in milliseconds for the
                     elapsed-time -> paused transition. Defaults to 300000
                     (5 minutes); a lower value is useful for manual QA.
    """

    def __init__(
        self,
        ws_url: str,
        username: str,
        password: str,
        topic: str,
        on_envelope,
        long_disconnect_ms: int = 300000,
    ) -> None:
        """Create the wrapper. The underlying JS client is not started
        until `start()` is called."""
        self._on_envelope = on_envelope
        self._client = createMqttWsClient(
            {
                "url": ws_url,
                "username": username,
                "password": password,
                "topic": topic,
                "longDisconnectMs": long_disconnect_ms,
                "onEnvelope": create_proxy(self._on_envelope_js),
                "onStatus": create_proxy(self._on_status_js),
            }
        )

    def _on_envelope_js(self, raw: str) -> None:
        """JS shim -> Python: forward the envelope string to the user callback."""
        if self._on_envelope is not None:
            self._on_envelope(raw)

    def _on_status_js(self, state: str, detail) -> None:
        """JS shim -> Python: status event. Surface to a page-level
        element so the operator sees Live / Reconnecting / Paused / Error.

        Default behavior: log to console. The base template's `app.js`
        registers a richer status handler when present.
        """
        try:
            # detail is a JsProxy; coerce to a plain dict for logging
            d = dict(detail) if detail is not None else {}
        except Exception:
            d = {}
        print(f"[mqtt_ws] status={state} detail={d}")

    def start(self) -> None:
        """Open the WebSocket and begin the CONNECT / SUBSCRIBE handshake."""
        self._client.start()

    def close(self) -> None:
        """Close the WebSocket and stop reconnect attempts."""
        self._client.close()
