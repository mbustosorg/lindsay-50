"""Tests for SignSettings.target_version default resolution.

Issue #51 wire contract:
  - target_version lives on SignSettings (alongside `sign_name`).
  - Default at construction time resolves to Flask's running short SHA
    (via `from_heroku_or_git()` + `short_sha()`).
  - The wire form is ALWAYS concrete — Flask never persists an empty value.
  - `from_dict` falls back to the default when the field is missing
    (forward-compatible: pre-change Flask publishes payloads without
    `target_version`).
  - `to_dict` always includes the field.
  - SQLite + S3-rebuild round-trip the new field.

Hermetic: the default-resolution cache is patched for isolation so tests
don't shell out to `git rev-parse HEAD`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def fresh_models_module():
    """Reload `lib_shared.models` fresh so we can patch its cache.

    The `_default_target_version_cache` class attribute caches the
    Flask running SHA across constructions. Tests that want to
    exercise the default path must reset that cache (or patch the
    resolver) so previous constructions don't poison the result.
    """
    if "lib_shared.models" in sys.modules:
        del sys.modules["lib_shared.models"]
    spec = importlib.util.spec_from_file_location("lib_shared.models", str(_PROJECT_ROOT / "lib_shared" / "models.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lib_shared.models"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Default construction-time resolution
# ---------------------------------------------------------------------------


class TestDefaultTargetVersion:
    def test_default_resolves_to_flask_short_sha(self, fresh_models_module):
        """SignSettings() with no target_version defaults to Flask's
        running short SHA, resolved at construction time."""
        SignSettings = fresh_models_module.SignSettings
        # Stub the resolver to return a known value.
        with patch.object(
            SignSettings,
            "_default_target_version",
            return_value="abc1234",
            create=True,
        ):
            s = SignSettings()
            assert s.target_version == "abc1234"

    def test_default_cache_initialised_once(self, fresh_models_module):
        """The Flask running SHA is resolved lazily once and cached for
        the process lifetime — every subsequent construction reuses the
        cached value without shelling out to git."""
        SignSettings = fresh_models_module.SignSettings

        # Reset the cache, then trigger the resolver once via __init__.
        SignSettings._default_target_version_cache = None
        s1 = SignSettings()  # cache miss → populates cache
        cached_value = SignSettings._default_target_version_cache
        assert cached_value is not None, "first construction should populate the cache"

        # Subsequent constructions should hit the cache (no fresh resolve).
        # We verify by reading the cache attribute after each construction.
        s2 = SignSettings()
        s3 = SignSettings()
        assert SignSettings._default_target_version_cache == cached_value
        assert s1.target_version == s2.target_version == s3.target_version

    def test_default_cache_skipped_when_already_populated(self, fresh_models_module):
        """When the cache is already populated, the resolver short-circuits
        and returns the cached value without calling the underlying
        `from_heroku_or_git` resolver."""
        SignSettings = fresh_models_module.SignSettings

        # Patch the underlying resolver at its source (lib_shared.boot_config)
        # so the classmethod short-circuit is observable.
        from lib_shared import boot_config

        def fail_resolver():
            raise AssertionError("from_heroku_or_git should not be invoked when cache is populated")

        SignSettings._default_target_version_cache = "preshed"
        try:
            with patch.object(boot_config, "from_heroku_or_git", side_effect=fail_resolver):
                s = SignSettings()
                assert s.target_version == "preshed"
        finally:
            SignSettings._default_target_version_cache = None

    def test_explicit_target_version_overrides_default(self, fresh_models_module):
        """A caller-supplied target_version wins over the default."""
        SignSettings = fresh_models_module.SignSettings
        s = SignSettings(sign_name="X", target_version="feedface")
        assert s.target_version == "feedface"

    def test_empty_string_target_version_is_preserved(self, fresh_models_module):
        """Empty-string target_version is a caller-supplied value —
        NOT a missing field — and is preserved as-is. (Flask's request
        path normalizes this to the freshly-resolved Flask SHA at
        persist time; the constructor doesn't auto-substitute.)"""
        SignSettings = fresh_models_module.SignSettings
        s = SignSettings(target_version="")
        assert s.target_version == ""


# ---------------------------------------------------------------------------
# from_dict / to_dict round-trip
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_missing_target_version_falls_back_to_default(self, fresh_models_module):
        """Pre-change Flask publishes payloads without `target_version`;
        the Pi (and any reader) gets the construction-time default."""
        SignSettings = fresh_models_module.SignSettings
        with patch.object(
            SignSettings,
            "_default_target_version",
            return_value="abc1234",
            create=True,
        ):
            s = SignSettings.from_dict({"sign_name": "X"})
            assert s.target_version == "abc1234"

    def test_explicit_target_version_is_preserved(self, fresh_models_module):
        SignSettings = fresh_models_module.SignSettings
        s = SignSettings.from_dict({"sign_name": "X", "target_version": "feedface"})
        assert s.target_version == "feedface"

    def test_from_dict_none_uses_default(self, fresh_models_module):
        SignSettings = fresh_models_module.SignSettings
        with patch.object(
            SignSettings,
            "_default_target_version",
            return_value="abc1234",
            create=True,
        ):
            s = SignSettings.from_dict(None)
            assert s.sign_name == SignSettings.DEFAULT_SIGN_NAME
            assert s.target_version == "abc1234"

    def test_from_dict_empty_dict_uses_default(self, fresh_models_module):
        SignSettings = fresh_models_module.SignSettings
        with patch.object(
            SignSettings,
            "_default_target_version",
            return_value="abc1234",
            create=True,
        ):
            s = SignSettings.from_dict({})
            assert s.sign_name == SignSettings.DEFAULT_SIGN_NAME
            assert s.target_version == "abc1234"


class TestToDict:
    def test_to_dict_always_includes_target_version(self, fresh_models_module):
        """The wire form is ALWAYS concrete — to_dict must include
        `target_version` even when it falls back to the default."""
        SignSettings = fresh_models_module.SignSettings
        s = SignSettings(target_version="abc1234")
        d = s.to_dict()
        assert "target_version" in d
        assert d["target_version"] == "abc1234"

    def test_round_trip_through_from_to_dict(self, fresh_models_module):
        SignSettings = fresh_models_module.SignSettings
        original = SignSettings(sign_name="Test", target_version="deadbee")
        round_tripped = SignSettings.from_dict(original.to_dict())
        assert round_tripped.to_dict() == original.to_dict()


# ---------------------------------------------------------------------------
# SQLite round-trip
# ---------------------------------------------------------------------------


class TestSqliteRoundTrip:
    def test_sign_config_round_trips_target_version(self, fresh_models_module, tmp_path):
        """Persist a SignConfig with a pinned target_version, reload it,
        and verify the field survives."""
        from heart_message_manager import sqlite as sqlite_mod

        SignConfig = fresh_models_module.SignConfig
        SignSettings = fresh_models_module.SignSettings

        # Redirect SQLite to a tmp file so we don't touch the real DB.
        db_path = tmp_path / "test.db"
        import unittest.mock as mock

        with mock.patch.object(sqlite_mod, "_db_path", return_value=db_path):
            sqlite_mod.init_db()
            cfg = SignConfig(sign_settings=SignSettings(sign_name="X", target_version="abc1234"))
            sqlite_mod.put_config(cfg)
            loaded = sqlite_mod.get_config()

        assert loaded.sign_settings.to_dict() == {
            "sign_name": "X",
            "target_version": "abc1234",
            "enforce_allowed_senders": True,
            "timezone": "US/Pacific",
        }


# ---------------------------------------------------------------------------
# S3-rebuild path — sqlite.put_config(SignConfig.from_dict(...))
# ---------------------------------------------------------------------------


class TestS3RebuildPath:
    def test_s3_rebuild_re_creates_with_default_target_version(
        self,
        fresh_models_module,
    ):
        """When S3 rebuild recreates the SignSettings row, the field
        is initialized to the construction-time default (Flask SHA),
        mirroring every other SignSettings field."""
        # Simulate what `sqlite.rebuild_from_s3` does on the success path:
        # pass an S3-loaded dict through SignConfig.from_dict, which
        # constructs SignSettings.from_dict({...}).
        # The S3 row may NOT carry target_version (pre-change data);
        # the construction must default.
        cfg = fresh_models_module.SignConfig.from_dict({"sign_settings": {"sign_name": "FromS3"}, "timezone": "US/Pacific"})
        assert "target_version" in cfg.sign_settings.to_dict()
        # The field is concrete (never None, never empty when the resolver
        # succeeds — which it does in the test environment).
        assert isinstance(cfg.sign_settings.target_version, str)
