"""Tests for `lib_shared/config_reader.py`.

The reader's `_validate` step rejects startup if any required
key is missing from both env and TOML. Empty strings ARE valid
"set" values — they're deliberate "use the default" sentinels
in settings.toml (e.g. `MQTT_STATUS_TOPIC = ""` is the example's
default and the app resolves it via `MQTT_STATUS_TOPIC or
f"{MQTT_TOPIC}-status"`).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest


def _fresh_config_reader(monkeypatch, toml_contents: str, env: dict | None = None):
    """Build a fresh `config_reader` module with a custom settings.toml.

    Reloads the module so the module-level singleton `_cfg` is
    reset, then points cwd at a tmp dir with the requested toml
    file. Returns the freshly imported `config_reader` module.
    """
    import tempfile

    tmpdir = tempfile.mkdtemp()
    settings_path = Path(tmpdir) / "settings.toml"
    settings_path.write_text(toml_contents, encoding="utf-8")

    # Clear any existing singleton so get_config() re-runs the
    # validator.
    if "lib_shared.config_reader" in sys.modules:
        del sys.modules["lib_shared.config_reader"]

    # cd to the tmp dir so _load_toml picks up our settings.toml.
    monkeypatch.chdir(tmpdir)

    # Wipe any env vars we want to control.
    if env is not None:
        for k in ("MQTT_STATUS_TOPIC", "MQTT_TOPIC", "MQTT_HOST"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    else:
        for k in ("MQTT_STATUS_TOPIC", "MQTT_TOPIC", "MQTT_HOST"):
            monkeypatch.delenv(k, raising=False)

    spec = importlib.util.spec_from_file_location(
        "lib_shared.config_reader",
        str(Path(__file__).parent.parent / "lib_shared" / "config_reader.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lib_shared.config_reader"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestRequiredKeysValidation:
    def test_required_key_with_empty_string_value_is_accepted(self, monkeypatch):
        """Regression: `MQTT_STATUS_TOPIC = ""` is a valid value, not a
        missing key. The validator must treat empty strings as
        "set" (deliberate sentinel for "use the default")."""
        toml = """
MQTT_HOST = "h"
MQTT_TOPIC = "t"
MQTT_STATUS_TOPIC = ""
"""
        mod = _fresh_config_reader(monkeypatch, toml)
        # Should not raise.
        cfg = mod.get_config({"MQTT_HOST", "MQTT_TOPIC", "MQTT_STATUS_TOPIC"})
        assert cfg.MQTT_STATUS_TOPIC == ""
        assert cfg.MQTT_TOPIC == "t"
        assert cfg.MQTT_HOST == "h"

    def test_required_key_missing_from_toml_and_env_raises(self, monkeypatch):
        toml = """
MQTT_HOST = "h"
"""
        mod = _fresh_config_reader(monkeypatch, toml)
        with pytest.raises(KeyError) as excinfo:
            mod.get_config({"MQTT_HOST", "MQTT_TOPIC", "MQTT_STATUS_TOPIC"})
        # The error names the missing keys.
        msg = str(excinfo.value)
        assert "MQTT_TOPIC" in msg
        assert "MQTT_STATUS_TOPIC" in msg

    def test_env_var_overrides_empty_toml_value(self, monkeypatch):
        """An env var takes precedence even when the toml has an empty string."""
        toml = """
MQTT_HOST = "h"
MQTT_TOPIC = "t"
MQTT_STATUS_TOPIC = ""
"""
        mod = _fresh_config_reader(monkeypatch, toml, env={"MQTT_STATUS_TOPIC": "env-status"})
        cfg = mod.get_config({"MQTT_HOST", "MQTT_TOPIC", "MQTT_STATUS_TOPIC"})
        assert cfg.MQTT_STATUS_TOPIC == "env-status"

    def test_no_required_keys_passes(self, monkeypatch):
        toml = ""
        mod = _fresh_config_reader(monkeypatch, toml)
        cfg = mod.get_config(set())
        assert cfg is not None
