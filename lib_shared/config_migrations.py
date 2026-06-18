"""Config migrations: bring older SignConfig payloads up to the current version.

Each entry in MIGRATIONS is a `(dict) -> dict` callable that takes a payload at
version N and returns a payload at version N+1. Migrations run in order from
the payload's version up to the current version. Adding a new migration is a
3-step change: write the function, register it in MIGRATIONS, and bump
SignConfig.CURRENT_VERSION.

The pattern is small and well-established (same shape Rails' data_migrations,
Django's migrations/, and Pydantic v2's `model_validator(mode="before")` use,
but minimal — for this project a full ORM migration framework would be
overkill).
"""

import logging
from typing import Callable, Dict, Optional

from lib_shared.models import EffectsSettings, TextSettings

log = logging.getLogger("heart")


def _v1_to_v2(d: dict) -> dict:
    """v1 → v2: drop tz_offset_mins + rendering, add effects_settings + text_settings, bump version.

    Preserves filters, senders, sign, timezone. The on-disk message list
    (messages.json in S3) is not part of the config and is not touched.
    """
    out = dict(d)
    out.pop("tz_offset_mins", None)
    # The old RenderingSettings block is being removed; the new text_settings
    # block replaces it. There is no v1 → v2 mapping for individual rendering
    # fields — the new block starts from defaults.
    out.pop("rendering", None)
    if "effects_settings" not in out:
        out["effects_settings"] = EffectsSettings().to_dict()
    if "text_settings" not in out:
        out["text_settings"] = TextSettings().to_dict()
    out["version"] = 2
    return out


MIGRATIONS: Dict[int, Callable[[dict], dict]] = {
    1: _v1_to_v2,
}


def migrate(d: dict, current_version: int) -> dict:
    """Run all migrations needed to bring `d` up to `current_version`.

    If `d` has no version key, it's treated as v1. Each migration receives
    the output of the previous one (chained). Stops at `current_version`.
    Raises KeyError with a clear message if a migration is missing for a
    required step.

    Idempotency: `migrate(v2_payload, current_version=2)` returns the
    input unchanged (the for-loop's `range(2, 2)` is empty).
    """
    d = d or {}
    version = int(d.get("version", 1))
    out = d
    for v in range(version, current_version):
        if v not in MIGRATIONS:
            raise KeyError(f"No migration registered for v{v} → v{v + 1}")
        out = MIGRATIONS[v](out)
    return out


def migrate_on_startup(
    s3_getter,
    sqlite_writer,
    mqtt_publisher,
    s3_writer,
    log_func: Optional[Callable[[str], None]] = None,
):
    """Run the startup migration: read S3, migrate, write back if changed.

    Called from `heart-message-manager/main.py` (or wherever the existing
    "rebuild-from-S3 on startup" step lives) after the S3 read. Reads the
    latest config via `s3_getter()`, runs `migrate(...)` on the result, and
    if the version changed (i.e. a migration ran), calls `sqlite_writer`,
    `mqtt_publisher`, and `s3_writer` with the migrated config. If the
    stored version is already at `CURRENT_VERSION` (or no S3 config exists),
    the function is a no-op — no S3 write, no SQLite write, no MQTT publish.

    Args:
        s3_getter: callable returning the latest config dict from S3 (or None).
        sqlite_writer: callable(SignConfig-compatible dict) -> None. Writes
            the migrated config to the local SQLite cache.
        mqtt_publisher: callable(dict) -> None. Publishes the migrated
            config as a `type="config"` envelope to MQTT.
        s3_writer: callable(dict) -> None. Writes a new S3 entry at the
            current version, replacing the old one.
        log_func: optional callable(str) -> None for the migration log line.

    Returns:
        The migrated config dict (SignConfig-shaped), or None if the
        S3 read returned no config (the server may still want to
        initialize the first config via the writers).
    """
    from lib_shared.models import SignConfig

    if log_func is None:
        log_func = log.info

    cfg_data = s3_getter()
    if cfg_data is None:
        # Fresh install — initialize the defaults and write them everywhere.
        defaults = SignConfig().to_dict()
        sqlite_writer(SignConfig.from_dict(defaults))
        mqtt_publisher(defaults)
        s3_writer(defaults)
        log_func("Initialized default SignConfig (no prior S3 config found)")
        return defaults

    # migrate() treats a missing version key as v1.
    original_version = int(cfg_data.get("version", 1))
    migrated = migrate(cfg_data, current_version=SignConfig.CURRENT_VERSION)
    new_version = int(migrated.get("version", SignConfig.CURRENT_VERSION))
    if original_version >= new_version:
        # No migration ran (idempotent re-run or already-current config).
        return migrated

    # A migration ran. Write the migrated config everywhere.
    sqlite_writer(SignConfig.from_dict(migrated))
    mqtt_publisher(migrated)
    s3_writer(migrated)
    log_func(
        f"Migrated SignConfig from v{original_version} to v{new_version} "
        f"(preserved {len(migrated.get('filters', []))} filters, "
        f"{len(migrated.get('senders', []))} senders)"
    )
    return migrated
