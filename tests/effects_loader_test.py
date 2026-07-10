"""Tests for lib_shared.effects_loader (override-aware loader + folded factory).

Covers:
- Precedence: env var > repo-root override > canonical
- Override-active signal
- Schema-version policy (fail-loud on future version, best-effort on old)
- Empty `effects` list warning
- `reset_effects_settings()` roundtrip
- The folded-in factory: `make_effect_class` resolves canonical names,
  returns None for unknown names, raises AttributeError for wrong
  class names.

The previous `tests/effects_factory_test.py` is gone — its cases
moved here (the factory now lives in `effects_loader`).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import lib_shared.effects_loader as effects_loader  # noqa: E402
from lib_shared.effects_loader import (  # noqa: E402
    is_effects_settings_override_active,
    load_effects_settings,
    make_effect_class,
    reset_effects_settings,
)


@pytest.fixture(autouse=True)
def _reset_loader_cache():
    """Each test sees a fresh loader cache.

    The loader caches the parsed dict for the process lifetime
    (design D9). Tests that swap the active config (env var,
    repo-root override, fake module-level cache) need a clean
    slate per test.
    """
    reset_effects_settings()
    yield
    reset_effects_settings()


@pytest.fixture
def fake_loader_cache():
    """Replace the loader's cache with a caller-supplied dict.

    Mirrors the test fixture pattern used by tests that previously
    monkey-patched `_DEFAULT_EFFECTS_LIST_FULL` (now removed):
    set a fake config dict, assign to the loader's cache, and the
    next `load_effects_settings()` returns it without touching disk.
    """

    def _set(data: dict):
        effects_loader._cache = data
        return data

    return _set


# ---------------------------------------------------------------------------
# Precedence — env var > repo-root override > canonical
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_env_var_takes_precedence_over_repo_root_override(self, monkeypatch, tmp_path):
        """Env var wins over `config_overrides/effects_settings.json`."""
        env_file = tmp_path / "env_override.json"
        env_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "Fireworks",
                            "module": "lib_shared.patterns.fireworks",
                            "class_name": "Fireworks",
                            "enabled": True,
                        }
                    ],
                    "fade_seconds": 9.9,
                    "hold_seconds": 9.9,
                    "intro_seconds": 9.9,
                    "idle_seconds": 9.9,
                    "recent_count": 9,
                }
            )
        )
        # Repo-root override (different content) — should be IGNORED
        # because env var wins.
        repo_override = _PROJECT_ROOT / "config_overrides" / "effects_settings.json"
        repo_override.parent.mkdir(exist_ok=True)
        repo_override.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "NightSky",
                            "module": "lib_shared.patterns.nightsky",
                            "class_name": "NightSky",
                            "enabled": True,
                        }
                    ],
                    "fade_seconds": 1.0,
                    "hold_seconds": 1.0,
                    "intro_seconds": 1.0,
                    "idle_seconds": 1.0,
                    "recent_count": 1,
                }
            )
        )
        try:
            monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(env_file))
            reset_effects_settings()
            assert is_effects_settings_override_active() is True
            cfg = load_effects_settings()
            assert cfg["recent_count"] == 9
            assert [e["name"] for e in cfg["effects"]] == ["Fireworks"]
        finally:
            repo_override.unlink(missing_ok=True)

    def test_repo_root_override_used_when_no_env_var(self, monkeypatch):
        """With no env var, the repo-root override is used."""
        # Make sure no env var is set from prior tests.
        monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)
        repo_override = _PROJECT_ROOT / "config_overrides" / "effects_settings.json"
        repo_override.parent.mkdir(exist_ok=True)
        repo_override.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "Flame",
                            "module": "lib_shared.patterns.flame",
                            "class_name": "Flame",
                            "enabled": True,
                        }
                    ],
                    "fade_seconds": 7.0,
                    "hold_seconds": 7.0,
                    "intro_seconds": 7.0,
                    "idle_seconds": 7.0,
                    "recent_count": 7,
                }
            )
        )
        try:
            reset_effects_settings()
            assert is_effects_settings_override_active() is True
            cfg = load_effects_settings()
            assert cfg["recent_count"] == 7
        finally:
            repo_override.unlink(missing_ok=True)

    def test_canonical_used_when_no_override(self, monkeypatch):
        """No env var, no repo-root override → canonical."""
        monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)
        repo_override = _PROJECT_ROOT / "config_overrides" / "effects_settings.json"
        repo_override.unlink(missing_ok=True)
        reset_effects_settings()
        assert is_effects_settings_override_active() is False
        cfg = load_effects_settings()
        # Canonical: 9 effects (PngDisplay/VideoDisplay/Flame removed), recent_count=5.
        assert len(cfg["effects"]) == 9
        assert cfg["recent_count"] == 5

    def test_env_var_pointing_to_missing_file_falls_back(self, monkeypatch, tmp_path):
        """Env var set to a non-existent path → warning + fallback."""
        missing = tmp_path / "nope.json"
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(missing))
        reset_effects_settings()
        # Should not raise; loader logs a warning and uses canonical.
        cfg = load_effects_settings()
        assert len(cfg["effects"]) == 9
        assert cfg["recent_count"] == 5


# ---------------------------------------------------------------------------
# is_effects_settings_override_active
# ---------------------------------------------------------------------------


class TestOverrideActive:
    def test_false_when_no_override(self, monkeypatch):
        monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)
        repo_override = _PROJECT_ROOT / "config_overrides" / "effects_settings.json"
        repo_override.unlink(missing_ok=True)
        assert is_effects_settings_override_active() is False

    def test_true_when_env_var_points_to_existing_file(self, monkeypatch, tmp_path):
        f = tmp_path / "ov.json"
        f.write_text("{}")
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(f))
        assert is_effects_settings_override_active() is True

    def test_false_when_env_var_points_to_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(tmp_path / "nope.json"))
        repo_override = _PROJECT_ROOT / "config_overrides" / "effects_settings.json"
        repo_override.unlink(missing_ok=True)
        assert is_effects_settings_override_active() is False


# ---------------------------------------------------------------------------
# Schema + content validation
# ---------------------------------------------------------------------------


class TestSchema:
    def test_canonical_has_schema_version_and_nine_effects(self):
        """The canonical JSON declares schema_version=1 + 9 effects
        (PngDisplay/VideoDisplay removed in #38 — those are now inner
        renderers consumed by MediaCycler, not registry entries; Flame
        removed as well)."""
        cfg = load_effects_settings()
        assert cfg["schema_version"] == 1
        assert len(cfg["effects"]) == 9

    def test_every_effect_entry_has_required_keys(self):
        """Each effects entry has name, enabled, module, class_name."""
        cfg = load_effects_settings()
        for entry in cfg["effects"]:
            assert isinstance(entry["name"], str)
            assert isinstance(entry["enabled"], bool)
            assert isinstance(entry["module"], str)
            assert isinstance(entry["class_name"], str)

    def test_top_level_keys_subset_of_EffectsSettings_plus_schema_version(self):
        """Top-level keys = EffectsSettings dataclass fields ∪ {schema_version}."""
        cfg = load_effects_settings()
        from lib_shared.models import EffectsSettings

        import inspect

        sig = inspect.signature(EffectsSettings.__init__)
        ds_params = {p for p in sig.parameters if p != "self"}
        expected = ds_params | {"schema_version"}
        assert set(cfg.keys()) == expected

    def test_canonical_modules_are_importable(self):
        """Every `module` value resolves via importlib.import_module."""
        cfg = load_effects_settings()
        import importlib

        for entry in cfg["effects"]:
            mod = importlib.import_module(entry["module"])
            assert hasattr(mod, entry["class_name"])


# ---------------------------------------------------------------------------
# Empty effects list → WARNING (design D11)
# ---------------------------------------------------------------------------


class TestEmptyEffects:
    def test_empty_effects_list_returns_empty_with_warning(self, caplog, monkeypatch, tmp_path):
        """An empty `effects` list yields `[]` and logs a WARNING.

        Uses a tmp_path file (env var) so the loader runs its full
        disk-load path, including the empty-effects warning. The
        cache-only path skips that warning (no need to spam the log
        every call); this test exercises the on-disk path.
        """
        f = tmp_path / "empty.json"
        f.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [],
                    "fade_seconds": 1.0,
                    "hold_seconds": 1.0,
                    "intro_seconds": 1.0,
                    "idle_seconds": 1.0,
                    "recent_count": 1,
                }
            )
        )
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(f))
        reset_effects_settings()
        with caplog.at_level(logging.WARNING, logger="heart"):
            cfg = load_effects_settings()
        assert cfg["effects"] == []
        # At least one WARNING mentions the empty effects list.
        assert any("empty" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# reset_effects_settings()
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_cache_and_re_reads(self, monkeypatch, tmp_path):
        """After reset, the next load re-reads the file (env var honored)."""
        f1 = tmp_path / "one.json"
        f1.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [],
                    "fade_seconds": 1.0,
                    "hold_seconds": 1.0,
                    "intro_seconds": 1.0,
                    "idle_seconds": 1.0,
                    "recent_count": 1,
                }
            )
        )
        f2 = tmp_path / "two.json"
        f2.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [],
                    "fade_seconds": 2.0,
                    "hold_seconds": 2.0,
                    "intro_seconds": 2.0,
                    "idle_seconds": 2.0,
                    "recent_count": 2,
                }
            )
        )

        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(f1))
        reset_effects_settings()
        assert load_effects_settings()["recent_count"] == 1

        # Switch the env var; without reset, cache holds the old dict.
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(f2))
        assert load_effects_settings()["recent_count"] == 1  # cached

        # With reset, the new file is read.
        reset_effects_settings()
        assert load_effects_settings()["recent_count"] == 2


# ---------------------------------------------------------------------------
# Folded factory — make_effect_class (was effects_factory.py)
# ---------------------------------------------------------------------------


class TestFactory:
    def test_resolves_canonical_name(self):
        """`make_effect_class("Fireworks")` returns the Fireworks class."""
        cls = make_effect_class("Fireworks")
        assert cls is not None
        assert cls.__name__ == "Fireworks"
        # The class lives in lib_shared.patterns.fireworks.
        assert cls.__module__ == "lib_shared.patterns.fireworks"

    def test_resolves_all_browser_safe_effects(self):
        """Fireworks, Hyperspace, NightSky have no heavy top-level
        deps and resolve cleanly without numpy / cv2 / PIL installed.

        Flame was removed (refactor(patterns): remove the Flame effect);
        the new patterns (WindFire, CoronalMassEjection, Eyeball) all
        import `numpy` at module top-level, so they don't qualify here.
        """
        for name in ("Fireworks", "Hyperspace", "NightSky"):
            cls = make_effect_class(name)
            assert cls is not None, f"{name!r} did not resolve"
            assert cls.__name__ == name
            assert callable(cls)

    def test_returns_none_for_unknown_name(self, caplog):
        """Unknown names return None (logged as a warning)."""
        with caplog.at_level(logging.WARNING, logger="heart"):
            cls = make_effect_class("NotARealEffect")
        assert cls is None
        assert any("NotARealEffect" in rec.message for rec in caplog.records)

    def test_repeated_calls_are_idempotent(self):
        """Calling the factory twice returns the same class object."""
        cls1 = make_effect_class("Fireworks")
        cls2 = make_effect_class("Fireworks")
        assert cls1 is cls2

    def test_wrong_class_name_raises_attribute_error(self, monkeypatch, tmp_path):
        """When the module imports but `class_name` is missing, raise."""
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "NotInModule",
                            "module": "lib_shared.patterns.fireworks",
                            "class_name": "NotInModule",
                            "enabled": True,
                        }
                    ],
                    "fade_seconds": 1.0,
                    "hold_seconds": 1.0,
                    "intro_seconds": 1.0,
                    "idle_seconds": 1.0,
                    "recent_count": 1,
                }
            )
        )
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(bad))
        reset_effects_settings()
        with pytest.raises(AttributeError):
            make_effect_class("NotInModule")


# ---------------------------------------------------------------------------
# Override lists fewer / different effects than canonical (D2: REPLACE)
# ---------------------------------------------------------------------------


class TestReplaceSemantics:
    def test_override_with_three_effects_returns_only_three(self, monkeypatch, tmp_path):
        """REPLACE: override with 3 entries → loader returns 3 entries,
        not a merge of canonical + override."""
        f = tmp_path / "three.json"
        f.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "Fireworks",
                            "module": "lib_shared.patterns.fireworks",
                            "class_name": "Fireworks",
                            "enabled": True,
                        },
                        {
                            "name": "NightSky",
                            "module": "lib_shared.patterns.nightsky",
                            "class_name": "NightSky",
                            "enabled": True,
                        },
                        {
                            "name": "Flame",
                            "module": "lib_shared.patterns.flame",
                            "class_name": "Flame",
                            "enabled": False,
                        },
                    ],
                    "fade_seconds": 1.0,
                    "hold_seconds": 8.0,
                    "intro_seconds": 3.0,
                    "idle_seconds": 60.0,
                    "recent_count": 3,
                }
            )
        )
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(f))
        reset_effects_settings()
        cfg = load_effects_settings()
        assert [e["name"] for e in cfg["effects"]] == [
            "Fireworks",
            "NightSky",
            "Flame",
        ]
        assert cfg["recent_count"] == 3
        # Canonical-only names are NOT in the override (no merge).
        assert make_effect_class("Hyperspace") is None

    def test_override_can_introduce_names_not_in_canonical(self, monkeypatch, tmp_path):
        """An override can name an effect that's not in the canonical —
        no canonical-validation gate. Resolution still goes through the
        dynamic-import path."""
        f = tmp_path / "new.json"
        f.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "Fireworks",
                            "module": "lib_shared.patterns.fireworks",
                            "class_name": "Fireworks",
                            "enabled": True,
                        },
                        {
                            "name": "NewPattern",
                            "module": "lib_shared.patterns.fireworks",
                            "class_name": "Fireworks",
                            "enabled": True,
                        },
                    ],
                    "fade_seconds": 1.0,
                    "hold_seconds": 1.0,
                    "intro_seconds": 1.0,
                    "idle_seconds": 1.0,
                    "recent_count": 1,
                }
            )
        )
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(f))
        reset_effects_settings()
        assert make_effect_class("NewPattern") is not None
