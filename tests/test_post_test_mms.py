"""Tests for `scripts/post_test_mms.py` — admin-credential resolution.

The script reads defaults from `heart-message-manager/settings.toml`
([auth] ADMIN_USERNAME / ADMIN_PASSWORD + top-level PORT) and the
ADMIN_USERNAME / ADMIN_PASSWORD env vars. Precedence (highest first):

  --flag > $ADMIN_USERNAME > settings.toml top-level > settings.toml [auth] > built-in

This mirrors the server's own `ConfigReader.get_raw` precedence
(`lib_shared/config_reader.py`), which reads top-level first and
falls back to nested sections. The script must agree so the
operator sees the same credentials the server accepts.

These tests pin the precedence via `_settings_defaults` and the
loader (`_load_settings`) — no Flask, no network, no S3.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "post_test_mms.py"


@pytest.fixture
def script_mod():
    """Import the script as a module without executing `main`.

    The script's `if __name__ == "__main__":` guard keeps `main()`
    from running at import time, so a plain importlib load is safe.
    """
    spec = importlib.util.spec_from_file_location("post_test_mms", str(_SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["post_test_mms"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_settings(tmp_path: Path, *, username: str = "", password: str = "", port: str = "") -> Path:
    """Write a minimal settings.toml with only the keys we test."""
    lines: list[str] = []
    if port:
        lines.append(f'PORT = "{port}"')
    if username or password:
        lines.append("[auth]")
    if username:
        lines.append(f'ADMIN_USERNAME = "{username}"')
    if password:
        lines.append(f'ADMIN_PASSWORD = "{password}"')
    p = tmp_path / "settings.toml"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# _load_settings: best-effort parse
# ---------------------------------------------------------------------------


def test_load_settings_returns_empty_when_file_missing(script_mod, tmp_path):
    """A missing settings.toml is NOT an error — caller treats
    empty as 'no defaults available'."""
    missing = tmp_path / "does_not_exist.toml"
    assert script_mod._load_settings(missing) == {}


def test_load_settings_parses_auth_table(script_mod, tmp_path):
    """The script reads the [auth] table — write a TOML with
    nested keys and confirm the dict round-trips."""
    settings = tmp_path / "settings.toml"
    settings.write_text('[auth]\nADMIN_USERNAME = "alice"\nADMIN_PASSWORD = "hunter2"\n')
    cfg = script_mod._load_settings(settings)
    assert cfg["auth"]["ADMIN_USERNAME"] == "alice"
    assert cfg["auth"]["ADMIN_PASSWORD"] == "hunter2"


def test_settings_defaults_reads_top_level_admin_keys(script_mod, monkeypatch, tmp_path):
    """Deployed `settings.toml` files often flatten
    ADMIN_USERNAME / ADMIN_PASSWORD to the top level (not under
    [auth]). The script must read both shapes — `settings.toml.example`
    uses `[auth]`, but real configs (incl. this repo's dev config)
    put them at the top level."""
    settings = _write_settings(tmp_path, username="alice", password="hunter2", port="4242")
    # Top-level keys (no [auth] section).
    settings.write_text('PORT = "4242"\nADMIN_USERNAME = "alice"\nADMIN_PASSWORD = "hunter2"\n')
    monkeypatch.setattr(script_mod, "_DEFAULT_SETTINGS", settings)
    user, pw, port = script_mod._settings_defaults()
    assert user == "alice"
    assert pw == "hunter2"
    assert port == "4242"


def test_settings_defaults_top_level_wins_over_auth_table(script_mod, monkeypatch, tmp_path):
    """When both shapes are present (top-level + [auth] table),
    top-level wins — this mirrors the server's own ConfigReader
    precedence (`lib_shared/config_reader.py:ConfigReader.get_raw`),
    which reads top-level first and falls back to nested sections.
    The script must behave the same way so the operator sees the
    same credentials the server accepts."""
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'PORT = "4242"\n'
        'ADMIN_USERNAME = "top-level-alice"\n'
        'ADMIN_PASSWORD = "top-level-secret"\n'
        "[auth]\n"
        'ADMIN_USERNAME = "table-alice"\n'
        'ADMIN_PASSWORD = "table-secret"\n'
    )
    monkeypatch.setattr(script_mod, "_DEFAULT_SETTINGS", settings)
    user, pw, _ = script_mod._settings_defaults()
    assert user == "top-level-alice"
    assert pw == "top-level-secret"


def test_load_settings_handles_malformed_toml_gracefully(script_mod, tmp_path, capsys):
    """A malformed settings.toml logs a warning and returns {}
    instead of crashing — the operator who can't parse their config
    should still be able to use the script with --username/--password."""
    bad = tmp_path / "broken.toml"
    bad.write_text("[auth\nthis is not valid toml")
    cfg = script_mod._load_settings(bad)
    assert cfg == {}
    # Warning surfaced on stderr so the operator knows why their
    # settings were ignored.
    captured = capsys.readouterr()
    assert "could not parse" in captured.err


# ---------------------------------------------------------------------------
# _settings_defaults: convenience wrapper at the default path
# ---------------------------------------------------------------------------


def test_settings_defaults_falls_back_to_builtins_when_file_missing(script_mod, monkeypatch, tmp_path):
    """When the default settings.toml is missing (operator hasn't
    created one), the built-in defaults `('admin', 'secret123',
    '')` kick in. `_settings_defaults` returns three strings."""
    monkeypatch.setattr(script_mod, "_DEFAULT_SETTINGS", tmp_path / "missing.toml")
    user, pw, port = script_mod._settings_defaults()
    assert user == ""
    assert pw == ""
    assert port == ""


def test_settings_defaults_reads_auth_and_port(script_mod, monkeypatch, tmp_path):
    """When the default settings.toml exists, the function
    returns its `[auth]` creds + top-level PORT verbatim."""
    settings = _write_settings(tmp_path, username="alice", password="hunter2", port="4242")
    monkeypatch.setattr(script_mod, "_DEFAULT_SETTINGS", settings)
    user, pw, port = script_mod._settings_defaults()
    assert user == "alice"
    assert pw == "hunter2"
    assert port == "4242"


# ---------------------------------------------------------------------------
# _load_settings_from: same shape, explicit path
# ---------------------------------------------------------------------------


def test_load_settings_from_returns_empty_tuple_when_missing(script_mod, tmp_path):
    """`_load_settings_from` returns three empty strings on a
    missing file — matching `_settings_defaults`'s shape so the
    caller can treat both identically."""
    out = script_mod._load_settings_from(tmp_path / "missing.toml")
    assert out == ("", "", "")


def test_load_settings_from_reads_explicit_path(script_mod, tmp_path):
    """The function is the per-path cousin of `_settings_defaults` —
    point it at any settings.toml and it returns the same shape."""
    settings = _write_settings(tmp_path, username="bob", password="s3cret", port="8080")
    out = script_mod._load_settings_from(settings)
    assert out == ("bob", "s3cret", "8080")
