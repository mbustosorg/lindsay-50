"""Regression tests for config-envelope delivery without broker-side retain.

Round 9 (issue #71 follow-up): the dashboard's "Current config"
modal and messages list depend on `_handle_config` running on the
in-browser MessageManager. When the WS envelope is missed (broker
fan-out drop per `feedback_clean_session_aio_fan_out.md`, WS
reconnect race), the in-memory state stays stale until the next
page reload — which forces `seed()` to repopulate from REST.

Round 8 attempt (the v191 fix) passed `retain=True` to the broker,
which is correct MQTT 3.1.1 semantics but is silently ignored by
Adafruit IO. Per io.adafruit.com/api/docs/mqtt.html: "we don't
actually store data in the broker but at a lower level and can't
support PUBLISH retain directly". The advertised workaround is the
`<feed-topic>/get` publish-and-replay pattern: subscriber publishes
an empty payload to `<feed>/get` and AIO republishes the last
value of that feed back to the subscriber on the original feed
topic.

Round 9 implements that pattern in the BROWSER (mqtt_ws_client.js
SUBACK handler), so the WS-only contract is preserved: the browser
still subscribes via WS, the publish still goes via MQTT, the
change fan-out still runs through `_emit_change`. We're fixing
delivery, not the receiver.

Round 10 (operator confirmation): the SUBACK /get fetch fires on
EVERY SUBACK, including the first. The round-9 gate
(`lastConnectedAt !== null`) was dead code — `lastConnectedAt` is
set in `socket.onopen` before SUBACK arrives, so the "skip first
SUBACK" branch never skipped anything. The seed() REST hydrate is
the primary path; the /get fetch is a self-healing overlay that
catches anything the seed missed.

These tests pin that contract via static-source assertions — we
inspect the source files directly so we don't need a live broker.
The end-to-end publish flow is exercised by the integration tests
in `tests/settings_post_handler_test.py` etc., which only assert
that publish_envelope is called; this file is the SPECIFIC contract
that the /get-fetch-on-reconnect works correctly and that the
server-side retain claim was removed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# --- Signature acceptance ---------------------------------------------------


def test_publish_envelope_accepts_retain_kwarg():
    """`PahoMqttClient.publish_envelope` MUST accept a `retain` kwarg.

    Even though AIO doesn't honor it, the flag is correct MQTT 3.1.1
    semantics and is forwarded to paho for standards-compliant
    brokers (Heroku Mosquitto, local dev, any non-AIO deployment).
    Default value is False — events are not state, only the wire
    envelope is; the round 9 /get-fetch pattern is what recovers
    state on the browser side.
    """
    src = (_PROJECT_ROOT / "lib_shared" / "paho_mqtt_client.py").read_text()
    assert "def publish_envelope(self, envelope, retain: bool = False)" in src, (
        "PahoMqttClient.publish_envelope signature must accept "
        "retain: bool = False — the kwarg is correct MQTT semantics "
        "for standards-compliant brokers even though AIO ignores it."
    )
    assert "client.publish(topic, payload.encode(), qos=1, retain=retain)" in src, (
        "publish_envelope must forward the retain kwarg to "
        "client.publish() — otherwise the broker never sees the flag."
    )


# --- Server does NOT pass retain on config publishes -----------------------
#
# AIO silently ignores retain=True, so passing it gives a false sense of
# broker-side state recovery. Round 9 strips it from the server-side
# publish; the real fix is on the browser (mqtt_ws_client.js /get fetch).


def test_mqtt_client_publish_config_does_not_pass_retain_true():
    """`_mqtt_client_publish_config` MUST NOT pass `retain=True`.

    Round 9 correction: the previous test (round 8) asserted the
    OPPOSITE — that retain=True was passed — because we believed
    AIO honored the flag. It does not (per AIO docs). The server
    side now passes no retain flag; recovery from a missed config
    envelope happens on the BROWSER side via the /get fetch on WS
    reconnect.

    We scan the function BODY (not the docstring) so narrative
    text mentioning retain=True doesn't false-positive the
    assertion. Body starts at the first non-docstring line.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text()
    fn_idx = src.find("def _mqtt_client_publish_config")
    assert fn_idx != -1, "_mqtt_client_publish_config must be defined"
    fn_section = src[fn_idx : fn_idx + 2000]
    # Skip the docstring (find the next non-docstring line).
    body_start = fn_section.find('"""', fn_section.find("def "))
    if body_start != -1:
        body_start = fn_section.find('"""', body_start + 3) + 3
    body = fn_section[body_start:] if body_start != -1 else fn_section
    assert "publish_envelope(" in body
    assert "retain=True" not in body, (
        "_mqtt_client_publish_config body must NOT pass retain=True — "
        "AIO ignores the flag, so passing it suggests recovery that "
        "doesn't happen. Recovery is via the browser-side /get fetch."
    )


# --- Non-config publishes are NOT retained ----------------------------------


def _envelope_calls_without_retain(src: str, envelope_type: str) -> list[tuple[str, bool]]:
    """Return every `publish_envelope` call where the matched envelope
    is the named type AND a flag indicating whether the call passed
    retain=True.

    The pattern we recognize is the same one Flask uses:

        envelope = MessageEnvelope(<type>, ...)
        ... optionally some intervening lines ...
        ok = _mqtt_client.publish_envelope(envelope[, retain=...])

    We scan the source for the variable binding, then walk forward
    up to ~20 lines and look for the matching `publish_envelope(envelope)`
    call. If `retain=True` appears in that call, it's a violation.
    """
    binding_re = re.compile(
        rf'envelope\s*=\s*MessageEnvelope\(\s*"{re.escape(envelope_type)}"',
    )
    # Single-line pattern (used by message publishes).
    inline_re = re.compile(
        rf'publish_envelope\(\s*MessageEnvelope\(\s*"{re.escape(envelope_type)}".*?\)(?:\s*,\s*retain=(?:True|False))?\s*\)',
        re.DOTALL,
    )
    out: list[tuple[str, bool]] = []
    # Inline matches (message publishes — they're one-liners).
    for m in inline_re.finditer(src):
        call = m.group(0)
        retained = "retain=True" in call
        out.append((call, retained))
    # Multi-line matches — find every `envelope = MessageEnvelope(<type>, ...)`
    # and look at the next 20 lines for the publish call.
    for m in binding_re.finditer(src):
        end_of_binding = src.find("\n", m.end())
        if end_of_binding == -1:
            continue
        window = src[end_of_binding : end_of_binding + 2000]
        call_match = re.search(
            r"publish_envelope\(\s*envelope(\s*,\s*retain=(?:True|False))?\s*\)",
            window,
        )
        if not call_match:
            continue
        call = call_match.group(0)
        retained = "retain=True" in call
        out.append((call, retained))
    return out


def test_message_publish_does_not_retain():
    """A message published via the Flask MQTT path must NOT set retain.

    Messages are events, not state — retaining them would mean a
    reconnecting subscriber would re-deliver the same SMS to the
    rotation buffer on every reconnect.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text()
    matches = _envelope_calls_without_retain(src, "message")
    assert matches, "expected at least one message-publish call site"
    for call, retained in matches:
        assert not retained, (
            f"message publish must not pass retain=True: {call!r} "
            "(messages are events, not state — only config retains)"
        )


def test_command_publish_does_not_retain():
    """A command envelope (force-upgrade / restart / shutdown /
    check-for-update) must NOT set retain.

    These are one-shot operator actions. Retaining them would mean
    a reconnecting subscriber would re-execute the last command on
    every reconnect — that's a foot-gun.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text()
    matches = _envelope_calls_without_retain(src, "command")
    assert matches, "expected at least one command-publish call site"
    for call, retained in matches:
        assert not retained, (
            f"command publish must not pass retain=True: {call!r}"
        )


# --- Browser-side /get fetch on WS reconnect -------------------------------


def test_browser_get_fetch_builder_exists():
    """mqtt_ws_client.js MUST expose a buildPublish frame builder.

    The /get-fetch-on-reconnect pattern (round 9) sends a QoS-1
    PUBLISH to `<feed-topic>/get` so AIO replays the last value
    of the feed. That's a client PUBLISH from the WS shim, which
    didn't exist before — only the inbound-PUBLISH parser did.
    The builder is a thin wrapper over the existing `packet()`
    helper but must be present and use QoS-1 flags (0x02) so the
    broker hands us a PUBACK and doesn't drop the publish.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "mqtt_ws_client.js").read_text()
    assert "function buildPublish" in src, (
        "mqtt_ws_client.js must define buildPublish — the /get-fetch "
        "pattern (round 9) needs a client→broker PUBLISH builder"
    )
    # The QoS-1 flags byte is 0x02 (DUP=0, QoS=01, RETAIN=0).
    # AIO requires QoS 1 on the SUBSCRIBE per the existing comment,
    # and the /get fetch must also be QoS 1 so we get a PUBACK.
    assert "0x02" in src, (
        "buildPublish must use the QoS-1 flags byte (0x02) — AIO "
        "drops QoS-0 PUBLISH frames silently on this broker."
    )


def test_browser_get_fetch_fires_on_reconnect_suback():
    """On every SUBACK (including reconnects), the browser MUST
    publish to `<topic>/get` to fetch AIO's last value.

    Round 10: the /get fetch fires on EVERY SUBACK. The
    `lastConnectedAt` gate was dead code (it's set in onopen
    before SUBACK) and was removed. The fetch is a self-healing
    overlay on top of the REST seed — it catches any save that
    landed during the page-load race window.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "mqtt_ws_client.js").read_text()
    # Find the SUBACK branch by scanning for the SUBACK log line.
    suback_idx = src.find('[mqtt-ws] SUBACK')
    assert suback_idx != -1, "SUBACK handler must be present"
    # Look for the /get fetch within a reasonable window after SUBACK.
    # Window is generous so we don't miss the surrounding branch.
    window = src[suback_idx : suback_idx + 4000]
    assert "/get" in window, (
        "SUBACK branch must publish to <topic>/get so AIO replays "
        "the last config to the browser (round 9 /get-fetch pattern)"
    )
    assert "buildPublish" in window, (
        "SUBACK branch must call buildPublish to send the /get fetch"
    )


def test_browser_get_fetch_fires_on_every_suback():
    """The /get fetch MUST fire on EVERY SUBACK, including the first.

    Round 10 (operator confirmation): the in-browser seed() races
    AIO's MQTT queue — if a config save lands BEFORE the page
    finishes loading, the in-memory state at seed-time is stale
    even on first connect. The /get fetch on every SUBACK is the
    self-healing overlay that catches this.

    The previous round asserted the fetch was gated by
    `lastConnectedAt !== null` (skipping the first SUBACK). That
    gate doesn't actually work — `lastConnectedAt` is set in
    `socket.onopen`, which always fires before SUBACK — so the
    "first SUBACK skip" never skipped anything. Round 10 removes
    the gate entirely and lets the fetch fire on every SUBACK.
    The seed() REST hydrate is still the primary path; the /get
    fetch is the backstop.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "mqtt_ws_client.js").read_text()
    suback_idx = src.find('[mqtt-ws] SUBACK')
    window = src[suback_idx : suback_idx + 4000]
    # The skip path is gone — there should be NO conditional gating
    # the /get fetch. We assert that the "skip on first connect"
    # branch was removed (round 9 → round 10 correction).
    assert (
        "first SUBACK — skipping" not in window
    ), (
        "round 10: SUBACK branch must NOT skip the /get fetch on "
        "first SUBACK — the gate never fired anyway (lastConnectedAt "
        "is set in onopen before SUBACK) and removing it makes the "
        "fetch self-healing across race conditions"
    )
    assert (
        "lastConnectedAt !== null" not in window
    ), (
        "round 10: SUBACK branch must NOT gate the /get fetch on "
        "lastConnectedAt — the gate is dead code now"
    )
    # And the fetch itself must still be present in the branch.
    assert "/get" in window, "SUBACK branch must publish to <topic>/get"
    assert "buildPublish" in window, "SUBACK branch must call buildPublish"
