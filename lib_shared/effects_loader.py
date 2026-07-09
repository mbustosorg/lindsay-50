"""Override-aware loader for the canonical effects-settings JSON.

This module is the single source of truth for the effects registry: the
canonical `lib_shared/config/effects_settings.json` ships in git; an
operator can override it via `config_overrides/effects_settings.json`
(gitignored) or via the `EFFECTS_SETTINGS_OVERRIDE` env var (test
fixtures, advanced operators).

The factory `make_effect_class(name)` lives here too — folded in from
the now-deleted `lib_shared/effects_factory.py`. The factory resolves a
canonical name to its Effect class via dynamic import (lazy, on-demand
so numpy/cv2/PIL are only paid for if the effect is actually used).

Precedence (first match wins):
  1. `EFFECTS_SETTINGS_OVERRIDE` env var (path to a JSON file)
  2. `config_overrides/effects_settings.json` at the repo root
  3. `lib_shared/config/effects_settings.json` (canonical, in git)

Override semantics are REPLACE — see design.md D2. The override file
must contain every field the canonical carries; we do not merge.

Schema-version policy (design D10):
  - File `schema_version` > loader-known max → raise (operator has a
    future-version file). This is the only fail-loud case.
  - File `schema_version` < loader-known min → log warning, attempt
    to load (best-effort).
  - File fails to parse → log error, fall back to canonical.

Empty `effects` list (design D11): log WARNING, return as-is. The
coordinator's `build_effects()` fallback keeps the sign producing a
frame even when the rotation is empty.

Cache (design D9): process-lifetime. Tests use `reset_effects_settings()`
to swap configs.
"""

import importlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("heart")

# Schema-version policy constants. Loader is currently version 1.
# `MIN` is the oldest version we still load (best-effort). `MAX` is the
# newest version we accept without raising — anything above is a
# future-version file the operator built against a newer loader.
SCHEMA_VERSION_MIN = 1
SCHEMA_VERSION_MAX = 1

# Default file locations. The canonical lives at the in-git path under
# `lib_shared/config/`. The repo-root override folder holds operator
# local-dev edits (gitignored — see `.gitignore`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_PATH = _REPO_ROOT / "lib_shared" / "config" / "effects_settings.json"
_REPO_ROOT_OVERRIDE_PATH = _REPO_ROOT / "config_overrides" / "effects_settings.json"
_ENV_VAR_NAME = "EFFECTS_SETTINGS_OVERRIDE"


_cache: dict | None = None


def _resolve_path() -> Path:
    """Resolve which file the loader should read (highest precedence wins).

    Order:
      1. `EFFECTS_SETTINGS_OVERRIDE` env var, if set and the file exists.
      2. `config_overrides/effects_settings.json` at the repo root.
      3. `lib_shared/config/effects_settings.json` (canonical).

    Returns the path to the file the loader will actually open.
    """
    env_path = os.environ.get(_ENV_VAR_NAME)
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        log.warning(
            "effects_loader: %s=%r set but file does not exist; falling back",
            _ENV_VAR_NAME,
            env_path,
        )
    if _REPO_ROOT_OVERRIDE_PATH.is_file():
        return _REPO_ROOT_OVERRIDE_PATH
    return _CANONICAL_PATH


def _load_from_disk(path: Path) -> dict:
    """Read a JSON file and apply the schema-version policy.

    Args:
        path: Path to the JSON file.

    Returns:
        Parsed dict with `effects`, `fade_seconds`, `hold_seconds`,
        `intro_seconds`, `idle_seconds`, `recent_count`, and
        `schema_version`.

    Raises:
        RuntimeError: If the file's `schema_version` exceeds
            `SCHEMA_VERSION_MAX` (operator has a future-version file).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("effects_loader: failed to parse %s: %s", path, exc)
        # Try the canonical as a fallback (only when the failing file
        # is not itself the canonical — otherwise we'd recurse forever).
        if path == _CANONICAL_PATH:
            raise
        log.warning("effects_loader: falling back to canonical %s", _CANONICAL_PATH)
        return _load_from_disk(_CANONICAL_PATH)

    schema_version = data.get("schema_version", 0)
    if schema_version > SCHEMA_VERSION_MAX:
        raise RuntimeError(
            f"effects_loader: {path} has schema_version={schema_version}, "
            f"loader max is {SCHEMA_VERSION_MAX} (future-version file)"
        )
    if schema_version < SCHEMA_VERSION_MIN:
        log.warning(
            "effects_loader: %s schema_version=%d is older than min=%d; " "attempting best-effort load",
            path,
            schema_version,
            SCHEMA_VERSION_MIN,
        )
    return data


def is_effects_settings_override_active() -> bool:
    """Return True if an operator override file is in effect.

    True if either the env var points to an existing file or the
    repo-root `config_overrides/effects_settings.json` exists.
    """
    env_path = os.environ.get(_ENV_VAR_NAME)
    if env_path and Path(env_path).is_file():
        return True
    return _REPO_ROOT_OVERRIDE_PATH.is_file()


def load_effects_settings() -> dict:
    """Return the loaded effects-settings dict (cached for process lifetime).

    On the first call: resolve the active path, parse the JSON, log
    the source + count, and cache the dict. Subsequent calls return
    the cached value. Tests call `reset_effects_settings()` to swap
    the cache between cases.
    """
    global _cache
    if _cache is not None:
        return _cache

    path = _resolve_path()
    data = _load_from_disk(path)
    log.info(
        "effects_loader: loaded %d effects from %s (schema_version=%d)",
        len(data.get("effects", [])),
        path,
        data.get("schema_version", 0),
    )
    if not data.get("effects"):
        log.warning(
            "effects_loader: %s has an empty `effects` list; "
            "build_effects() will fall back to the first canonical effect",
            path,
        )
    _cache = data
    return data


def reset_effects_settings() -> None:
    """Clear the loader cache. Test-only helper.

    Production code should not call this — the process-lifetime cache
    is the contract (design D9). Tests use this to swap between fake
    and real configs without polluting the cache across cases.
    """
    global _cache
    _cache = None


def make_effect_class(name: str) -> type | None:
    """Resolve a canonical effect name to its Effect class via the loader.

    Looks up `name` in `load_effects_settings()["effects"]`, then
    `importlib.import_module(entry["module"])` + `getattr(module,
    entry["class_name"])` to fetch the class. The per-name import
    scope is the whole point — `numpy`/`cv2`/`PIL` are only imported
    when an effect that needs them is requested.

    Args:
        name: Canonical effect name (e.g. "Fireworks").

    Returns:
        The Effect class, or None when the name is not registered
        (logged as a warning so the operator can see the drift).
        Raises AttributeError when the module imports cleanly but the
        class name is wrong — that's a config bug worth surfacing.
    """
    entries = load_effects_settings().get("effects", [])
    entry = next((e for e in entries if e.get("name") == name), None)
    if entry is None:
        log.warning("make_effect_class: unknown effect name %r (skipped)", name)
        return None
    module = importlib.import_module(entry["module"])
    return getattr(module, entry["class_name"])
