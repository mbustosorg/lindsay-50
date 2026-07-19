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

from lib_shared.models import EffectsSettings, SignSettings, TextSettings

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
    """v2 → v3: nest `sign`/`timezone`/`enforcement_enabled`/`name_display_format`,
    convert `type=sender` filters into senders list entries, rename `enabled: bool`
    → `status: "enabled"|"disabled"` on remaining rules, and map legacy
    `status="allowed"|"blocked"` on senders entries → `allowed: bool`.

    Issue #6 / implement-senders-filtering. The migration is a SHALLOW COPY
    of the input — does not mutate the caller's dict.

    Steps (in order, all additive; later steps see earlier results):

    1. Structural moves:
        - top-level `sign` block → `sign_settings` (renamed `name` → `sign_name`)
        - top-level `timezone` → `sign_settings.timezone`
        - top-level `enforcement_enabled` → `text_settings.enforcement_enabled`
        - top-level `name_display_format` → `effects_settings.name_display_format`
       Existing `sign_settings` / `text_settings` / `effects_settings` blocks
       are preserved (the merge is additive — pre-existing values win; the
       new field is only added if absent).
    2. Senders entry migration (list shape):
        - legacy `status="allowed"` → `allowed=True`; `status="blocked"` →
          `allowed=False`; missing → `allowed=True` (back-compat).
        - The migrated entry SHALL NOT contain a `status` field.
    3. Filter migration:
        - `type=sender` rules are converted to senders list entries with
          `allowed=False` (the v2 sender rule was always a suppression rule;
          under the v3 allowlist-only model, that maps to `allowed=False`).
          The new entry uses `pattern` as both `name` and `phone`. The rule
          is dropped from `filters`.
        - Other rules: rename `enabled: bool` → `status: "enabled"|"disabled"`.
          Missing `enabled` → `status="enabled"` (back-compat default).

    After all steps, `version` is set to `3`.
    """
    out = dict(d)

    # --- Step 1a: build sign_settings ---
    existing_sign = out.get("sign_settings")
    if isinstance(existing_sign, dict):
        # Existing block wins; only fill in missing fields.
        sign_block = dict(existing_sign)
    else:
        sign_block = {}
    legacy_sign = out.pop("sign", None)
    if isinstance(legacy_sign, dict):
        # `name` → `sign_name` rename
        if "name" in legacy_sign and "sign_name" not in sign_block:
            sign_block["sign_name"] = legacy_sign["name"]
        # Carry any other keys as-is (forward-compat for new SignSettings fields)
        for k, v in legacy_sign.items():
            if k != "name" and k not in sign_block:
                sign_block[k] = v
    if "sign_name" not in sign_block:
        sign_block["sign_name"] = SignSettings.DEFAULT_SIGN_NAME
    if "timezone" in out and "timezone" not in sign_block:
        sign_block["timezone"] = out.pop("timezone")
    elif "timezone" in out:
        out.pop("timezone")  # drop top-level; sign_settings already has one
    if "timezone" not in sign_block:
        sign_block["timezone"] = SignSettings.DEFAULT_TIMEZONE
    out["sign_settings"] = sign_block

    # --- Step 1b: build text_settings with enforcement_enabled ---
    existing_text = out.get("text_settings")
    if isinstance(existing_text, dict):
        text_block = dict(existing_text)
        # Only merge new fields when the existing block is a real dict.
        if "enforcement_enabled" in out and "enforcement_enabled" not in text_block:
            text_block["enforcement_enabled"] = out.pop("enforcement_enabled")
        else:
            out.pop("enforcement_enabled", None)
        if "enforcement_enabled" not in text_block:
            text_block["enforcement_enabled"] = TextSettings.DEFAULT_ENFORCEMENT_ENABLED
        out["text_settings"] = text_block
    else:
        # Non-dict existing value (None, str, list, etc.) — preserve it
        # verbatim so the upstream validator can return 400. We still
        # drop the top-level `enforcement_enabled` (the new field) so
        # it doesn't leak as a stray top-level key.
        out.pop("enforcement_enabled", None)

    # --- Step 1c: build effects_settings with name_display_format ---
    existing_effects = out.get("effects_settings")
    if isinstance(existing_effects, dict):
        effects_block = dict(existing_effects)
        if "name_display_format" in out and "name_display_format" not in effects_block:
            effects_block["name_display_format"] = out.pop("name_display_format")
        else:
            out.pop("name_display_format", None)
        if "name_display_format" not in effects_block:
            effects_block["name_display_format"] = EffectsSettings.DEFAULT_NAME_DISPLAY_FORMAT
        out["effects_settings"] = effects_block
    else:
        # Non-dict existing value — preserve verbatim so the upstream
        # validator can return 400. We still drop the top-level
        # `name_display_format` (the new field).
        out.pop("name_display_format", None)

    # --- Step 2: senders entry migration ---
    senders = out.get("senders", [])
    # Legacy v1 dict shape: {phone: name}; convert to list shape first.
    if isinstance(senders, dict):
        senders = [{"phone": p, "name": n, "allowed": True} for p, n in senders.items()]
    new_senders = []
    for entry in senders:
        if not isinstance(entry, dict):
            continue
        if "phone" not in entry:
            continue
        migrated = {
            "phone": entry["phone"],
            "name": entry.get("name", ""),
        }
        if "status" in entry:
            legacy_status = entry["status"]
            if legacy_status == "allowed":
                migrated["allowed"] = True
            elif legacy_status == "blocked":
                migrated["allowed"] = False
            else:
                migrated["allowed"] = True
        else:
            migrated["allowed"] = entry.get("allowed", True)
        # Sanitize the normalized key for dedup at filter-migration step below
        from lib_shared.phone_utils import normalize_phone

        migrated["_normalized"] = normalize_phone(migrated["phone"])
        new_senders.append(migrated)
    senders = new_senders

    # --- Step 3: filter migration ---
    filters = out.get("filters", [])
    if not isinstance(filters, list):
        filters = []
    new_filters = []
    existing_normalized_keys = {s["_normalized"] for s in senders}
    for rule in filters:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") == "sender":
            # Convert to senders list entry; allowed=False (the v2 rule was
            # always a suppression rule — under v3 allowlist-only, that maps
            # to NOT on the allowlist). The pattern becomes both name + phone.
            pattern = rule.get("pattern", "")
            if not pattern:
                continue
            from lib_shared.phone_utils import normalize_phone

            normalized = normalize_phone(pattern)
            if normalized in existing_normalized_keys:
                # Pre-existing senders entry wins (no overwrite — operator
                # already curated that entry).
                continue
            existing_normalized_keys.add(normalized)
            senders.append(
                {
                    "phone": pattern,
                    "name": pattern,
                    "allowed": False,
                    "_normalized": normalized,
                }
            )
            # DROP the rule — sender matching is the senders list's job now.
            continue
        # Otherwise: rename enabled: bool → status: "enabled"|"disabled"
        new_rule = dict(rule)
        if "status" not in new_rule:
            if "enabled" in new_rule:
                enabled_val = new_rule.pop("enabled")
                new_rule["status"] = "enabled" if enabled_val else "disabled"
            else:
                new_rule["status"] = "enabled"
        new_filters.append(new_rule)
    out["filters"] = new_filters

    # Strip the temporary `_normalized` keys before returning.
    out["senders"] = [{k: v for k, v in s.items() if k != "_normalized"} for s in senders]

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
