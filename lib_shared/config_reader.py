"""Shared config for heart-message-manager.

Loads from settings.toml if present (local dev), falls back to environment
variables (Heroku / production). Environment variables always take precedence.

This module is safe to import on CircuitPython (no settings.toml support there;
config is handled separately in heart-matrix-controller/settings.toml).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _masked(key: str) -> bool:
    """Return True if the value for this key should be masked in logs."""
    return any(k in key.upper() for k in ("PASSWORD", "KEY", "SECRET", "TOKEN"))


class ConfigReader:
    """Singleton config object. Env vars always override settings.toml.

    Validates required keys at startup. get() raises KeyError on missing keys.
    if_exists() returns None for missing keys without throwing.
    All values are returned as strings. Callers are responsible for casting
    to int, float, bool, etc. as needed.
    """

    def __init__(self, required_keys: set[str] | None = None):
        self._toml = self._load_toml()
        self._required = required_keys or []
        self._validate()

    def _load_toml(self) -> dict:
        """Load settings.toml from current working directory if it exists."""
        settings_path = Path(os.getcwd()) / "settings.toml"
        if not settings_path.exists():
            logger.info("No settings.toml found in %s", os.getcwd())
            return {}
        try:
            import tomllib
            with open(settings_path, "rb") as f:
                data = tomllib.load(f)
            logger.info("Loaded settings.toml from %s", settings_path)
            return data
        except Exception:
            logger.warning("Could not parse settings.toml in %s", os.getcwd())
            return {}

    def _validate(self) -> None:
        """Fail fast if any required key is missing from both env and toml."""
        missing = [k for k in self._required if not self.get_raw(k)]
        if missing:
            raise KeyError(f"Missing required config keys: {', '.join(missing)}")
        self._debug_log()

    def _debug_log(self) -> None:
        """Print all required keys and their values (passwords masked)."""
        logger.info("=== CONFIG ===")
        for key in sorted(self._required):
            val = self.get_raw(key)
            if val is None:
                logger.warning("%s: (not set)", key)
            elif _masked(key):
                logger.info("%s: ***", key)
            else:
                logger.info("%s: %s", key, val)
        logger.info("=== END CONFIG ===")

    def get(self, key: str) -> str:
        """Get a config value as a string. Raises KeyError if not found."""
        val = self.get_raw(key)
        if val is None:
            raise KeyError(key)
        return val

    def get_raw(self, key: str) -> str | None:
        """Get value from env or toml without defaults."""
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val
        toml_val = self._toml.get(key)
        if toml_val is not None:
            return str(toml_val)
        return None

    def if_exists(self, key: str) -> str | None:
        """Get a config value as a string, or None if not found."""
        return self.get_raw(key)

    def __getattr__(self, name: str):
        """Allow attribute-style access: cfg.MQTT_USERNAME → cfg.get("MQTT_USERNAME")."""
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get(name)


_cfg: ConfigReader | None = None

def get_config(required_keys: set[str] | None = None) -> ConfigReader:
    """Create (or return existing) ConfigReader singleton.

    First call creates the singleton with required_keys.
    Subsequent calls return the existing instance (required_keys ignored).
    """
    global _cfg
    if _cfg is None:
        if required_keys is None:
            raise ValueError("Initial call requires required_keys")
        _cfg = ConfigReader(required_keys)
    return _cfg
