"""Shared config for heart-message-manager.

Loads from settings.toml if present (local dev), falls back to environment
variables (Heroku / production). Environment variables always take precedence.

This module is safe to import on CircuitPython (no settings.toml support there;
config is handled separately in heart-matrix-controller/settings.toml).
"""

import os
from pathlib import Path


class Config:
    """Singleton config object. Env vars always override settings.toml.

    All values are returned as strings. Callers are responsible for casting
    to int, float, bool, etc. as needed.
    """

    def __init__(self):
        self._toml = self._load_toml()

    @staticmethod
    def _load_toml() -> dict:
        """Load settings.toml from current working directory if it exists."""
        settings_path = Path(os.getcwd()) / "settings.toml"
        if not settings_path.exists():
            return {}
        try:
            import tomllib
            with open(settings_path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            return {}

    def get(self, key: str, default: str | None = None) -> str | None:
        """Get a config value as a string.

        Priority: env var > settings.toml > default.
        Env vars are always strings. TOML values are stringified before returning.
        """
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val
        toml_val = self._toml.get(key)
        if toml_val is not None:
            return str(toml_val)
        return default

    def __getattr__(self, name: str):
        """Allow attribute-style access: cfg.AIO_USERNAME → cfg.get("AIO_USERNAME")."""
        if name.startswith("_"):
            raise AttributeError(name)
        val = self.get(name)
        if val is None:
            raise AttributeError(name)
        return val


# Module-level singleton — instantiated once when this module is first imported
cfg = Config()
