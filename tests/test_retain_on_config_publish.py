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


def test_browser_get_fetch_removed_on_suback():
    """Round 14 (operator call): the AIO `<topic>/get` last-value
    workaround is REMOVED from mqtt_ws_client.js entirely. The
    SUBACK branch must NOT publish to `<topic>/get` anymore.

    The /get fetch was originally added in a5d6102 (round 9) to
    recover from broker fan-out drops. The actual root cause was
    the AIO 1KB payload limit with feed history on (per
    feedback_aio_feed_history_payload_limit.md) — the /get
    workaround didn't help there. It also surfaces AIO's broker-
    cached last value as if it were live, which (a) hides real
    drift behind stale data and (b) tricks the operator into
    thinking the Pi is actively publishing when it's not.

    Both the status-topic WS (sign_status.js) and the config-topic
    WS (dashboard_runtime.py) now rely on real-time broker delivery
    only. Config gets seeded from /api/config on page load; future
    updates come via MQTT publishes.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "mqtt_ws_client.js").read_text()
    suback_idx = src.find('[mqtt-ws] SUBACK')
    assert suback_idx != -1, "SUBACK handler must be present"
    window = src[suback_idx : suback_idx + 4000]
    # Strip comments before checking — the round-14 comment block in
    # mqtt_ws_client.js intentionally references "/get" as historical
    # context. The check is for CODE that calls buildPublish with a
    # `<topic>/get` destination, not for the comment word.
    code_only = re.sub(r"//[^\n]*", "", window)
    code_only = re.sub(r"/\*[\s\S]*?\*/", "", code_only)
    assert "/get" not in code_only, (
        "round 14: SUBACK branch must NOT publish to <topic>/get — "
        "the AIO last-value workaround was removed (operator call). "
        "Surfacing broker-cached stale data as live misleads the "
        "operator and doesn't fix the actual root cause (AIO 1KB "
        "payload limit with feed history on)."
    )
    # buildPublish is still present in the file (used elsewhere for
    # outbound QoS-1 PUBLISH if a future caller needs it), but the
    # SUBACK branch must not call it for a /get fetch.
    assert "buildPublish(getTopic" not in code_only, (
        "round 14: SUBACK branch must NOT call buildPublish with a "
        "<topic>/get destination — the /get fetch is gone"
    )


def test_browser_get_fetch_no_first_suback_skip_gate():
    """Round 14 supersedes round 10's gate-removal assertion.

    In round 10 the SUBACK branch had a `lastConnectedAt !== null`
    gate that was supposed to skip the /get fetch on first SUBACK
    (turned out to be dead code). Round 10 asserted the gate was
    removed.

    In round 14 the /get fetch itself is gone — so the
    `lastConnectedAt !== null` reference must be gone too. This
    test is the round-14 equivalent: the SUBACK branch has neither
    the fetch NOR the gate that used to guard it.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "static" / "mqtt_ws_client.js").read_text()
    suback_idx = src.find('[mqtt-ws] SUBACK')
    window = src[suback_idx : suback_idx + 4000]
    assert (
        "lastConnectedAt !== null" not in window
    ), (
        "round 14: SUBACK branch must NOT reference `lastConnectedAt` "
        "as a gate — round 10 removed the gate and round 14 made the "
        "/get fetch itself gone, so the gate is doubly dead"
    )
    assert (
        "first SUBACK — skipping" not in window
    ), (
        "round 14: SUBACK branch must NOT have the round-10 skip "
        "comment — the /get fetch is gone entirely"
    )
