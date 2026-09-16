"""Regression test for the retain flag on config publishes.

Round 8, issue #71 follow-up: the dashboard's "Current config" modal
and messages list depend on `_handle_config` running on the in-browser
MessageManager. When the WS envelope is missed (broker fan-out drop
per `feedback_clean_session_aio_fan_out.md`, WS reconnect race), the
in-memory state stays stale until the next page reload — which
forces `seed()` to repopulate from REST.

Fix: `_mqtt_client_publish_config` now passes `retain=True` to
`publish_envelope`, so the broker stores the latest config as the
"last retained message" on the envelope topic. New and reconnecting
subscribers receive the retained config immediately on SUBSCRIBE —
covers the WS-reconnect race without introducing an API fallback.
Message and command envelopes MUST stay non-retained (they're events,
not state).

These tests pin that contract via static-source assertions — we
inspect the source files directly so we don't need a live broker.
The end-to-end publish flow is exercised by the integration tests
in `tests/settings_post_handler_test.py` etc., which only assert
that publish_envelope is called; this file is the SPECIFIC contract
that config retains and messages don't.
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

    Default value should be False (events are not state — only
    config is).
    """
    src = (_PROJECT_ROOT / "lib_shared" / "paho_mqtt_client.py").read_text()
    assert "def publish_envelope(self, envelope, retain: bool = False)" in src, (
        "PahoMqttClient.publish_envelope signature must accept "
        "retain: bool = False — config callers pass True, all "
        "other callers leave it at the default."
    )
    # And the parameter must actually flow through to paho's publish
    # call (otherwise the broker never sees retain=True).
    assert "client.publish(topic, payload.encode(), qos=1, retain=retain)" in src, (
        "publish_envelope must forward the retain kwarg to "
        "client.publish() — otherwise the broker never sees the flag."
    )


# --- Config publishes are retained ------------------------------------------


def test_mqtt_client_publish_config_passes_retain_true():
    """`_mqtt_client_publish_config` MUST pass `retain=True` so the
    broker keeps the latest config for reconnecting subscribers.

    Static-source check: read the source file directly and confirm
    the call site carries the retain=True kwarg.
    """
    src = (_PROJECT_ROOT / "heart-message-manager" / "main.py").read_text()
    fn_idx = src.find("def _mqtt_client_publish_config")
    assert fn_idx != -1, "_mqtt_client_publish_config must be defined"
    fn_section = src[fn_idx : fn_idx + 2000]
    assert "publish_envelope(" in fn_section
    assert "retain=True" in fn_section, (
        "_mqtt_client_publish_config must pass retain=True to publish_envelope"
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
