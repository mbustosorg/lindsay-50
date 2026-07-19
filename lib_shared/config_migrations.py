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
from lib_shared.phone_utils import normalize_phone

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


def _v2_to_v3(d: dict) -> dict:
    """v2 → v3: senders/FilterRule taxonomy change, bump version.

    Three transformations (see the ``senders-status`` and
    ``filter-rule-status`` capabilities):

    1. **senders entries** — rename the v2 ``status`` field
       (``"allowed"``/``"blocked"``) to the v3 ``action`` field
       (``"allow"``/``"suppress"``) and add a new ``status`` lifecycle field
       defaulting to ``"enabled"``. The legacy v1 dict shape
       (``{phone: name}``) is normalized to the list shape.
    2. **type=sender filter rules** — REMOVED from the wire. Each is converted
       to a senders list entry (``action="suppress"``, ``status="enabled"``,
       ``name``/``phone`` both = the rule's pattern) and dropped from
       ``filters``. Deduplicated by normalized phone — a pre-existing senders
       entry wins.
    3. **remaining filter rules** — rename the ``enabled`` bool to a ``status``
       enum (``True`` → ``"enabled"``, ``False`` → ``"disabled"``; missing →
       ``"enabled"``).

    Returns a shallow copy — never mutates the caller's dict (matches
    ``_v1_to_v2``). Preserves ``sign``, ``timezone``, ``effects_settings``,
    ``text_settings`` unchanged.
    """
    out = dict(d)

    # --- senders: rename status → action, add lifecycle status ---
    raw_senders = out.get("senders", [])
    senders_list: list[dict] = []
    if isinstance(raw_senders, dict):
        # Legacy v1 dict shape {phone: name} — every entry was implicitly
        # allow + enabled.
        for phone, name in raw_senders.items():
            senders_list.append({"phone": phone, "name": name, "action": "allow", "status": "enabled"})
    elif isinstance(raw_senders, list):
        for entry in raw_senders:
            new_entry = dict(entry)
            old_status = new_entry.pop("status", None)
            if "action" not in new_entry:
                if old_status == "blocked":
                    new_entry["action"] = "suppress"
                elif old_status == "allowed":
                    new_entry["action"] = "allow"
                else:
                    # Missing or already-v3 value — default to allow.
                    new_entry["action"] = "allow"
            # New lifecycle field: every pre-existing sender was "on".
            new_entry["status"] = "enabled"
            senders_list.append(new_entry)

    # --- filters: convert type=sender to senders entries, rename enabled ---
    raw_filters = out.get("filters", [])
    new_filters: list[dict] = []
    existing_norm = {normalize_phone(s["phone"]) for s in senders_list}
    for rule in raw_filters:
        if rule.get("type") == "sender":
            phone = rule.get("pattern", "")
            norm = normalize_phone(phone)
            if norm not in existing_norm:
                senders_list.append({"phone": phone, "name": phone, "action": "suppress", "status": "enabled"})
                existing_norm.add(norm)
            # Drop the rule — sender matching is the senders list's job now.
            continue
        new_rule = dict(rule)
        if "status" not in new_rule:
            enabled = new_rule.pop("enabled", True)
            new_rule["status"] = "enabled" if enabled else "disabled"
        new_filters.append(new_rule)

    out["senders"] = senders_list
    out["filters"] = new_filters
    out["version"] = 3
    return out


MIGRATIONS: Dict[int, Callable[[dict], dict]] = {
    1: _v1_to_v2,
    2: _v2_to_v3,
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
