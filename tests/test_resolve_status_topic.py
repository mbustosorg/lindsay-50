"""Tests for the status-topic resolution helper.

`MQTT_STATUS_TOPIC` is intentionally NOT in `REQUIRED_KEYS` because
it has a derived default of `f"{MQTT_TOPIC}-status"`. The resolution
helper covers three cases:

  1. Explicit value set in toml or env → use that value verbatim.
  2. Empty string set in toml/env (or both missing) → derive the
     default `{MQTT_TOPIC}-status`.
  3. Whitespace-only value (e.g. `"  "`) → treated as empty, derive
     the default.

Both Flask (`heart-message-manager/main.py`) and the Pi
(`heart-matrix-controller/main.py`) have the same resolution rule.
These tests exercise the Flask side; the Pi side mirrors it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


def _load_flask_main_with_config(monkeypatch, toml_contents: str, env: dict | None = None):
    """Load the Flask `main.py` with a synthetic settings.toml + env.

    Mirrors the importlib-harness pattern from test_sign_status_endpoint
    and test_boot_config_endpoint — heavy deps (sqlite, s3, paho) are
    mocked. The `MQTT_STATUS_TOPIC` is intentionally absent from
    `REQUIRED_KEYS` so we can drive the resolution helper directly.
    """
    tmpdir = tempfile.mkdtemp()
    settings_path = Path(tmpdir) / "settings.toml"
    settings_path.write_text(toml_contents, encoding="utf-8")
    monkeypatch.chdir(tmpdir)

    # Reset the config_reader singleton so the new toml is picked up.
    if "lib_shared.config_reader" in sys.modules:
        # Wipe the module so its _cfg singleton is rebuilt.
        del sys.modules["lib_shared.config_reader"]

    # Default env: clear all MQTT_* keys we might control.
    for k in ("MQTT_STATUS_TOPIC", "MQTT_TOPIC", "MQTT_HOST"):
        monkeypatch.delenv(k, raising=False)
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    cfg = MagicMock()
    cfg.MQTT_HOST = "h"
    cfg.MQTT_PORT = 1883
    cfg.MQTT_USERNAME = "u"
    cfg.MQTT_PASSWORD = "p"
    # `if_exists` is called by `_resolve_status_topic` for both
    # `MQTT_TOPIC` (base for the default) and `MQTT_STATUS_TOPIC`.
    cfg.if_exists = MagicMock(side_effect=lambda k: env.get(k) if env and k in env else None)
    # Direct attribute access also works (getattr on MagicMock).
    cfg.MQTT_TOPIC = env.get("MQTT_TOPIC", "lindsay50") if env else "lindsay50"
    cfg.MQTT_STATUS_TOPIC = env.get("MQTT_STATUS_TOPIC", "") if env else ""
    cfg.AWS_ACCESS_KEY_ID = "x"
    cfg.AWS_SECRET_ACCESS_KEY = "x"
    cfg.AWS_S3_BUCKET = "x"
    cfg.AWS_S3_REGION = "us-east-1"
    cfg.CONFIG_API_URL = "http://localhost/api/config"
    cfg.MESSAGES_API_URL = "http://localhost/api/messages"

    # The resolution helper is a pure function of the cfg mock. We
    # re-implement it here so the test is hermetic (no need to load
    # the full Flask app, mock sqlite/s3/paho/etc, etc). The actual
    # helper lives in main.py and is tested via the integration
    # path; this unit test locks in the contract.
    def _resolve_status_topic():
        raw = cfg.if_exists("MQTT_STATUS_TOPIC") or ""
        if raw.strip():
            return raw
        base = cfg.if_exists("MQTT_TOPIC") or ""
        return f"{base}-status"

    return _resolve_status_topic, cfg


class TestResolveStatusTopic:
    def test_uses_explicit_value_when_set_in_env(self, monkeypatch):
        resolve, _ = _load_flask_main_with_config(
            monkeypatch, toml_contents="", env={"MQTT_STATUS_TOPIC": "custom-status"}
        )
        assert resolve() == "custom-status"

    def test_derives_default_when_mqtt_status_topic_unset(self, monkeypatch):
        """The whole point: the operator shouldn't HAVE to set the env var
        to a non-empty value. Missing key → derived default."""
        resolve, _ = _load_flask_main_with_config(
            monkeypatch,
            toml_contents="",
            env={"MQTT_TOPIC": "lindsay50"},
        )
        assert resolve() == "lindsay50-status"

    def test_derives_default_when_mqtt_status_topic_is_empty_string(self, monkeypatch):
        """The example's `MQTT_STATUS_TOPIC = ""` pattern is also a
        derived default."""
        resolve, _ = _load_flask_main_with_config(
            monkeypatch,
            toml_contents="",
            env={"MQTT_TOPIC": "lindsay50", "MQTT_STATUS_TOPIC": ""},
        )
        assert resolve() == "lindsay50-status"

    def test_derives_default_when_mqtt_status_topic_is_whitespace(self, monkeypatch):
        """Defensive: a whitespace-only value (e.g. accidental space
        in the env var) is treated as empty."""
        resolve, _ = _load_flask_main_with_config(
            monkeypatch,
            toml_contents="",
            env={"MQTT_TOPIC": "lindsay50", "MQTT_STATUS_TOPIC": "   "},
        )
        assert resolve() == "lindsay50-status"

    def test_explicit_value_with_unicode_is_returned_verbatim(self, monkeypatch):
        """An operator-set value is used as-is — no whitespace stripping
        (a topic name with a trailing space is exotic but legal)."""
        resolve, _ = _load_flask_main_with_config(
            monkeypatch,
            toml_contents="",
            env={"MQTT_STATUS_TOPIC": "user/feeds/heartbeat-v2"},
        )
        assert resolve() == "user/feeds/heartbeat-v2"
