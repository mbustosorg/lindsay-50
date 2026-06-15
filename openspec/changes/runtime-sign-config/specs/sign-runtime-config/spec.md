## ADDED Requirements

### Requirement: SignConfig carries effect_settings and text_settings blocks (and no rendering field)

`SignConfig` SHALL include two new top-level fields in its serialized form: `effect_settings` (an object containing the `effects` list of `{name, enabled}` entries, `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, and `recent_count`) and `text_settings` (an object with `frame_delay`, `offset_seconds`, `color`, `text_effect`).

`SignConfig` SHALL NOT include a `rendering` field. The previous `RenderingSettings` block (which held the historical "Rendering Defaults" UI fields: `speed`, `font`, `font_size`, `color`, `letter_spacing`, `line_spacing`, `bg_color`, `align`) is being superseded by `text_settings` + `effect_settings` in this change, and is removed entirely. The `RenderingSettings` class SHALL be deleted from `lib_shared/models.py` and any code that read `cfg.rendering.*` SHALL be updated to read the new blocks.

The `effect_settings.effects` field SHALL be a list of `{"name": str, "enabled": bool}` objects — the FULL canonical set of effect classes (Hyperspace, VideoDisplay, PngDisplay, Honeycomb, Flame, Fireworks, NightSky — 7 entries), each with an `enabled: bool` flag the operator toggles. The list order IS the rotation order; the device builds its rotation by iterating the list and including only entries with `enabled: true`. The list shape is intentional: the "what effects exist" and "which are on" answers live in one place, and re-enabling a disabled effect preserves its position in the rotation.

The `effect_settings` block SHALL have these validation rules:
- `effects` SHALL be a list of objects, each with a string `name` (from the device's known set) and a boolean `enabled`. An entry with an unknown `name` SHALL be rejected at the Flask validation layer; `from_dict` at the value-object layer SHALL reject a non-list or an entry missing `name` / `enabled`.
- `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds` SHALL each be non-negative numbers.
- `recent_count` SHALL be a positive integer.

The `text_settings` block SHALL have these validation rules:
- `frame_delay` SHALL be a non-negative number.
- `offset_seconds` SHALL be a non-negative number.
- `color` SHALL be an integer in the range 0–0xFFFFFF.
- `text_effect` SHALL be one of the allowed enum values (currently `("scroll",)`). The block is named `text_settings` (not `scroller_settings`) because future text effects (swirl, bounce) will share this block.

Both new blocks SHALL have `from_dict` / `to_dict` round-trip support and SHALL be guarded by the same `threading.RLock` that protects the existing fields. The constructor SHALL accept the new fields as keyword arguments and SHALL default each to a known-good value when not provided. Both block classes (`EffectsSettings` and `TextSettings`) and the `_DEFAULT_EFFECTS_LIST_FULL` constant SHALL live in `lib_shared/models.py` alongside `SignConfig` — no separate `effects_settings.py` or `text_settings.py` files.

The default `effect_settings.effects` SHALL be the full 7-entry list in canonical rotation order, with the 5 historically-defaulted effects (Hyperspace, Honeycomb, Flame, Fireworks, NightSky) set to `enabled: true` and the 2 asset-dependent effects (VideoDisplay, PngDisplay) set to `enabled: false` — matches the historical 5-effect rotation visual while keeping the full set visible to the operator (who can toggle the asset-dependent ones on once they have asset files in place). The default `effect_settings` timing values SHALL match the historical `EffectsCoordinator` constructor defaults: `fade_seconds: 2.0`, `hold_seconds: 15.0`, `intro_seconds: 5.0`, `idle_seconds: 300.0`. The default `effect_settings.recent_count` SHALL be `5`. The default `text_settings` values SHALL match the historical `Scroller` constructor defaults: `frame_delay: 0.04`, `offset_seconds: 1.0`, `color: 0xFF0000`, `text_effect: "scroll"`.

#### Scenario: A config with the new fields round-trips through `to_dict` and `from_dict`
- **WHEN** a `SignConfig` is constructed with `effect_settings=EffectsSettings(effects=[{"name": "Flame", "enabled": True}, {"name": "Fireworks", "enabled": True}], fade_seconds=3.0, recent_count=10)` and `text_settings=TextSettings(frame_delay=0.02, color=0x00FF00)`
- **THEN** `to_dict()` SHALL return a dict whose `effect_settings` and `text_settings` keys match the input, and `from_dict(to_dict())` SHALL produce a `SignConfig` whose blocks equal the original

#### Scenario: A config with no new fields uses the defaults
- **WHEN** a `SignConfig` is constructed with no `effect_settings` or `text_settings` arguments
- **THEN** `to_dict()` SHALL return a dict with `effect_settings.effects` equal to `_DEFAULT_EFFECTS_LIST_FULL` (the full 7-entry list, 5 enabled + 2 disabled) and `text_settings` equal to the historical `TextSettings` defaults

#### Scenario: An empty payload parses to the defaults
- **WHEN** `SignConfig.from_dict({})` is called
- **THEN** the resulting `SignConfig` SHALL have the default `effect_settings` (7 effects, 5 enabled + 2 disabled, timing defaults, recent_count 5) and the default `text_settings`

#### Scenario: SignConfig.to_dict() does not include rendering
- **WHEN** a `SignConfig` is serialized via `to_dict()`
- **THEN** the resulting dict SHALL NOT contain a `rendering` key

#### Scenario: SignConfig has no rendering attribute and RenderingSettings is gone
- **WHEN** a `SignConfig` is constructed
- **THEN** `hasattr(cfg, "rendering")` SHALL be `False`; the `RenderingSettings` class SHALL NOT be importable from `lib_shared.models`

#### Scenario: Concurrent reads of the new fields are safe
- **WHEN** one thread reads `cfg.effect_settings.fade_seconds` while another thread calls `cfg.update_from_dict({...})` with a new `effect_settings`
- **THEN** the read SHALL NOT raise, SHALL return either the old or the new value (not a half-mutated value), and SHALL be deterministic for any single read

#### Scenario: Out-of-range effect_settings values are rejected
- **WHEN** a `SignConfig` is constructed with `effect_settings=EffectsSettings(fade_seconds=-1)` or `effect_settings=EffectsSettings(recent_count=0)`
- **THEN** construction (or the `validate()` call) SHALL raise `ValueError` with a per-field message

#### Scenario: Out-of-range text_settings values are rejected
- **WHEN** a `SignConfig` is constructed with `text_settings=TextSettings(frame_delay=-0.01)` or `text_settings=TextSettings(color=0x1000000)`
- **THEN** construction (or the `validate()` call) SHALL raise `ValueError`

#### Scenario: An invalid text_effect is rejected
- **WHEN** a `SignConfig` is constructed with `text_settings=TextSettings(text_effect="swirl")`
- **THEN** `TextSettings.from_dict({"text_effect": "swirl"})` SHALL raise `ValueError` (since "swirl" is not in the v1 enum)

### Requirement: SignConfig carries a CURRENT_VERSION and the v1 payload migrates to v2

`SignConfig` SHALL expose a `CURRENT_VERSION = 2` class constant. The constructor's `version` argument SHALL default to `CURRENT_VERSION`. The `from_dict` and `update_from_dict` methods SHALL call a `migrate(d, current_version)` function at the top of the method body, where `migrate` is a small registry in `lib_shared/config_migrations.py` that brings older payloads up to `current_version` by chaining registered migration functions.

The registry SHALL contain at least one entry: `MIGRATIONS = {1: _v1_to_v2}`. The `_v1_to_v2` function SHALL:
- Return a shallow copy of the input dict.
- Remove `tz_offset_mins` if present.
- Remove `rendering` if present (the old `RenderingSettings` block is being superseded by `text_settings` + `effect_settings` in this change — there is no v1 → v2 mapping for individual rendering fields; the new blocks start from defaults).
- Set `effect_settings` to the default `EffectsSettings().to_dict()` if absent.
- Set `text_settings` to the default `TextSettings().to_dict()` if absent.
- Set `version` to `2`.
- Preserve `filters`, `senders`, `sign`, and `timezone` unchanged.

A payload with no `version` key SHALL be treated as `version: 1` (the historical default).

#### Scenario: A v1 payload migrates to v2 on read
- **WHEN** `SignConfig.from_dict({"filters": [...], "senders": [...], "sign": {...}, "timezone": "America/Los_Angeles", "tz_offset_mins": -420, "rendering": {"speed": 2}, "version": 1})` is called
- **THEN** the resulting `SignConfig` SHALL have `version == 2`, `effect_settings` equal to the default `EffectsSettings()`, `text_settings` equal to the default `TextSettings()`, NO `tz_offset_mins` attribute, NO `rendering` key, and the original `filters`, `senders`, `sign`, and `timezone` preserved

#### Scenario: A v2 payload is idempotent under migration
- **WHEN** `migrate({"version": 2, ...}, current_version=2)` is called
- **THEN** the result SHALL equal the input unchanged (the for-loop's `range(2, 2)` is empty)

#### Scenario: update_from_dict migrates the incoming payload
- **WHEN** an in-memory `SignConfig` (at v2) receives `cfg.update_from_dict({"version": 1, "tz_offset_mins": -420})`
- **THEN** the in-memory config SHALL end up at v2 with `tz_offset_mins` not stored and the new blocks at their defaults

#### Scenario: A missing migration raises a clear error
- **WHEN** `migrate({"version": 1}, current_version=4)` is called and the registry has migrations for 1→2 and 2→3 but not 3→4
- **THEN** the call SHALL raise `KeyError` with a message naming the missing step

#### Scenario: A v1 payload arriving over MQTT is migrated on the device
- **WHEN** a `type="config"` envelope with a v1 payload arrives on the device and `MessageManager._handle_config` calls `update_from_dict`
- **THEN** the in-memory `SignConfig` on the device SHALL be at v2 with the v1 fields preserved (verified by reading `config.version`, `config.timezone`, `config.filters` after the dispatch returns)

#### Scenario: Messages and suppression rules survive a v1 → v2 migration
- **WHEN** a v1 config with `filters=[{type: "keyword", pattern: "spam", action: "suppress"}]` and `senders=[{phone: "+15551234567", name: "Lindsay"}]` is migrated to v2
- **THEN** the migrated `SignConfig` SHALL have the same `filters` and `senders` (verified by `cfg.filters == [...]` and `cfg.senders == {...}`); the on-disk message list in S3 (`messages.json`) is not touched by the migration

### Requirement: The server runs the config migration on startup (proactive, not lazy)

On server startup, after the existing "rebuild-from-S3 on startup" step in `heart-message-manager/sqlite.py` (or wherever that step lives in `heart-message-manager/`), the server SHALL run `migrate_on_startup(s3_getter, sqlite_writer, mqtt_publisher)` from `lib_shared/config_migrations.py`. This function SHALL:
- Read the latest config from S3.
- Run `migrate(d, current_version=SignConfig.CURRENT_VERSION)` on the result.
- If a migration ran (i.e. the stored version is older than `CURRENT_VERSION`):
  - Write a new S3 entry at the current version, replacing the old one.
  - Call the SQLite writer to update the local cache.
  - Call the MQTT publisher to push the migrated config as a `type="config"` envelope.
- If the stored version is already at `CURRENT_VERSION`, the function SHALL be a no-op — no S3 write, no SQLite write, no MQTT publish.

The function SHALL be idempotent. Re-running it on a config that's already at `CURRENT_VERSION` SHALL be a no-op.

The purpose of this requirement is to ensure the running code only ever sees `CURRENT_VERSION` — it doesn't have to be backward-compatible with old shapes for months waiting for an operator to click "Save" on the settings page. Without the startup migration, the stored S3 config could stay at v1 indefinitely, forcing the running code to handle both v1 and v2 reads.

The defense-in-depth migration in `SignConfig.from_dict` and `SignConfig.update_from_dict` (from the previous requirement) is kept for safety: a v1 payload arriving over MQTT (e.g. from a stale message cached before the server's startup migration) is upgraded to v2 in the device's in-memory config.

#### Scenario: Server startup with a v1 S3 config migrates it to v2 everywhere
- **WHEN** the server starts and the latest S3 config is `{"version": 1, "tz_offset_mins": -420, "rendering": {...}, "filters": [...], "senders": [...], "timezone": "America/Los_Angeles"}` (no `effect_settings`, no `text_settings`)
- **THEN** the startup migration SHALL:
  - Run `_v1_to_v2` on the payload, producing `{"version": 2, "filters": [...], "senders": [...], "timezone": "America/Los_Angeles", "effect_settings": <defaults>, "text_settings": <defaults>}` (no `tz_offset_mins`, no `rendering`).
  - Write the migrated payload as a new S3 entry, replacing the v1 entry.
  - Update the local SQLite cache to the migrated config (verified by reading the SQLite row).
  - Publish a `type="config"` envelope to MQTT with the migrated payload (verified by capturing the published message).
- **AND** the running code, after startup, SHALL be able to read the config at v2 with `config.effect_settings.effects == _DEFAULT_EFFECTS_LIST_FULL` (the 7-entry default, 5 enabled + 2 disabled), `config.version == 2`, and `config.filters == [...]` (the original v1 filters preserved)

#### Scenario: Server startup with a v2 S3 config is a no-op
- **WHEN** the server starts and the latest S3 config is `{"version": 2, "effect_settings": {...}, "text_settings": {...}}` (already at `CURRENT_VERSION`)
- **THEN** the startup migration SHALL be a no-op — no S3 write, no SQLite write, no MQTT publish (verified by checking that the S3 write function was NOT called, the SQLite write function was NOT called, and the MQTT publish function was NOT called)

#### Scenario: Server startup with no S3 config (fresh install) initializes the defaults
- **WHEN** the server starts and the S3 read returns `None` (no config has ever been written) or an empty dict
- **THEN** the startup migration SHALL treat the empty payload as v1, run `_v1_to_v2`, and the resulting config SHALL be at v2 with all defaults. The startup migration SHALL write this default config to S3, SQLite, and MQTT (this is also the first config the device sees, so the operator can immediately see the defaults in the admin UI)

#### Scenario: The startup migration logs the upgrade
- **WHEN** a migration runs on startup
- **THEN** the server SHALL log a single INFO line in the format `"Migrated SignConfig from v1 to v2 (preserved N filters, M senders)"` so an operator tailing the logs can see the upgrade happened

#### Scenario: The startup migration preserves the v1 fields and drops tz_offset_mins + rendering
- **WHEN** the startup migration runs on a v1 config with `sign.name="Lindsay's Heart"`, `rendering.speed=2`, `tz_offset_mins=-420`, `timezone="America/Los_Angeles"`, `filters=[...]`, and `senders={"+15551234567": "Lindsay"}`
- **THEN** the migrated config SHALL preserve `sign`, `filters`, `senders`, and `timezone` byte-for-byte, SHALL drop `tz_offset_mins` and `rendering` entirely, and SHALL add the new `effect_settings` and `text_settings` blocks at their defaults. Verified by reading the post-migration S3 entry

#### Scenario: The startup migration handles a missing S3 gracefully
- **WHEN** the server starts and the S3 read raises (e.g. credentials rotated, network error)
- **THEN** the startup migration SHALL propagate the exception (so the existing startup error handling kicks in and the server fails fast, as before). The migration SHALL NOT silently swallow S3 read errors

### Requirement: SignConfig serializes and deserializes via the existing wire path

The new blocks SHALL round-trip through the same `MessageEnvelope(type="config", payload=SignConfig.to_dict())` wire path that already exists. No new envelope type is introduced. The `MessageManager._handle_config` path SHALL continue to call `SignConfig.update_from_dict` on the payload; `update_from_dict` SHALL run the migration at the top of the method body, then do the field-by-field update.

#### Scenario: A type="config" envelope with the new fields updates the in-memory config
- **WHEN** a `type="config"` envelope arrives over MQTT whose `payload` is a dict containing `effect_settings` and `text_settings`
- **THEN** `MessageManager._handle_config` SHALL call `SignConfig.update_from_dict(payload)`, and the in-memory `SignConfig` SHALL reflect the new values (verified by reading `config.effect_settings.fade_seconds`, `config.text_settings.frame_delay` after the dispatch returns)

#### Scenario: A type="config" envelope without the new blocks leaves them at their defaults
- **WHEN** a `type="config"` envelope arrives whose `payload` is a dict that does NOT contain `effect_settings` or `text_settings`
- **THEN** `SignConfig.update_from_dict` SHALL keep the existing values for the missing blocks (not overwrite them with defaults)

#### Scenario: The Flask PUT endpoint normalizes the incoming payload to v2
- **WHEN** a client PUTs a v1 payload (no `version` key) to `/api/config`
- **THEN** the handler SHALL run `migrate(...)` on the incoming JSON, the saved SQLite row SHALL have `version: 2`, and the published `type="config"` envelope SHALL be at v2

### Requirement: The device reads runtime config from code defaults and from MQTT

The device's `heart-matrix-controller/main.py` SHALL construct the initial `SignConfig` from the `EffectsSettings()` and `TextSettings()` constructor defaults (i.e. from the `_DEFAULT_EFFECTS_LIST_FULL` constant in `lib_shared/models.py`, plus the `EffectsSettings` / `TextSettings` defaults). `settings.toml` is NOT a source of truth for these values. When a `type="config"` envelope arrives later, the in-memory `SignConfig` SHALL be replaced via `update_from_dict` and the coordinator + scroller + effect list SHALL be re-built from the new blocks.

#### Scenario: The device boots with the code defaults
- **WHEN** the device starts with no incoming config message
- **THEN** the boot-time `SignConfig` SHALL have `effect_settings.effects == _DEFAULT_EFFECTS_LIST_FULL` (the full 7-entry list, 5 enabled + 2 disabled), the `EffectsSettings` timing defaults, `recent_count: 5`, and the `TextSettings` defaults; the coordinator's timing params and the scroller's frame_delay / offset_seconds / color SHALL be initialized from those defaults; the boot-time rotation SHALL be the 5 enabled effects (Hyperspace, Honeycomb, Flame, Fireworks, NightSky) in that order

#### Scenario: A config message after boot updates the in-memory config
- **WHEN** the device is running and a `type="config"` envelope arrives with new `effect_settings` or `text_settings` values
- **THEN** the in-memory `SignConfig` SHALL be updated in place, the effect list SHALL be re-built from the new `effect_settings.effects` (filtering by `enabled: true`), and the next coordinator construction SHALL use the new timing. The fade-in-progress (if any) SHALL complete with the old timing values; the next mode transition SHALL use the new ones.

### Requirement: The device builds the effect rotation from the configured order (filtered by `enabled`)

The device SHALL maintain a constant map of effect class names to classes (e.g. `{"Hyperspace": Hyperspace, "VideoDisplay": VideoDisplay, ...}`). On boot and on every config message, the device SHALL iterate the configured `effect_settings.effects` list in order, include only entries with `enabled: true`, and instantiate the named class for each with the display. Entries that are not in the map SHALL be logged and skipped. Entries whose constructor raises SHALL be logged and skipped. The resulting list SHALL be passed to the `EffectsCoordinator` as its `effects` argument. `Heartbeat` SHALL be constructed separately (it is the boot-splash effect, not part of the rotation) and SHALL be passed as `coordinator.heart`.

#### Scenario: All enabled effects in the config initialize successfully
- **WHEN** the device boots with `effect_settings.effects = [{"name": "Hyperspace", "enabled": True}, {"name": "Flame", "enabled": True}, {"name": "Fireworks", "enabled": True}, {"name": "NightSky", "enabled": True}]`
- **THEN** the device SHALL construct one instance of each, in that order, and pass the list to the coordinator

#### Scenario: An unknown effect name is logged and skipped
- **WHEN** the device boots with `effect_settings.effects = [{"name": "Hyperspace", "enabled": True}, {"name": "DoesNotExist", "enabled": True}, {"name": "Flame", "enabled": True}]`
- **THEN** the device SHALL construct `Hyperspace` and `Flame` only; the "DoesNotExist" entry SHALL be logged at WARNING level and the rotation SHALL contain two effects

#### Scenario: An effect that fails to construct is logged and skipped
- **WHEN** the device boots with `effect_settings.effects = [{"name": "VideoDisplay", "enabled": True}, {"name": "Flame", "enabled": True}]` and `VideoDisplay(display)` raises (e.g. the video file is missing)
- **THEN** `VideoDisplay` SHALL be logged at WARNING level with the exception, `Flame` SHALL be constructed normally, and the rotation SHALL contain one effect

#### Scenario: Disabled effects are not in the rotation
- **WHEN** the device boots with `effect_settings.effects = [{"name": "Hyperspace", "enabled": True}, {"name": "VideoDisplay", "enabled": False}, {"name": "Flame", "enabled": True}]`
- **THEN** the device SHALL construct `Hyperspace` and `Flame` only; `VideoDisplay` SHALL be skipped (not even attempted) because `enabled: false`, and the rotation SHALL contain two effects in the order `[Hyperspace, Flame]`

#### Scenario: Heartbeat is never in the rotation
- **WHEN** the device boots
- **THEN** the `Heartbeat` effect SHALL be constructed separately and passed as `coordinator.heart`, and SHALL NOT appear in the rotation list regardless of the `effect_settings.effects` config

#### Scenario: A config message can disable an effect
- **WHEN** the device is running with the rotation `[Hyperspace, Honeycomb, Flame, Fireworks, NightSky]` and a config message arrives with `effect_settings.effects = [{"name": "Hyperspace", "enabled": True}, {"name": "Honeycomb", "enabled": False}, {"name": "Flame", "enabled": False}, {"name": "Fireworks", "enabled": True}, {"name": "NightSky", "enabled": True}]`
- **THEN** the rotation SHALL be re-built to `[Hyperspace, Fireworks, NightSky]`, the `Honeycomb` and `Flame` instances SHALL be dropped, and the next cycle advance SHALL move to `Fireworks`

#### Scenario: A config message can re-enable an asset-dependent effect
- **WHEN** the device is running with the default 7-entry config (5 enabled, VideoDisplay and PngDisplay disabled) and a config message arrives with `effect_settings.effects = [..., {"name": "VideoDisplay", "enabled": True}, ...]` (VideoDisplay's `enabled` flag flipped to `true`)
- **THEN** the rotation SHALL be re-built to include `VideoDisplay` in its original list position, and `VideoDisplay(display)` SHALL be constructed (or skipped-and-logged if the asset file is missing)

### Requirement: Scroller reads frame_delay, offset_seconds, color, and text_effect from config

The device's `Scroller` SHALL be constructed with a `TextSettings` object. `frame_delay`, `offset_seconds`, `_color`, and `text_effect` SHALL be initialized from the settings. Existing keyword-argument overrides SHALL remain for tests. The `text_effect` value SHALL be stored on the scroller but SHALL NOT change scroller behavior in v1 (only `"scroll"` is supported; future text effects will branch on this field).

#### Scenario: The scroller is constructed from TextSettings
- **WHEN** `MatrixScroller(display, text_settings=TextSettings(frame_delay=0.02, offset_seconds=0.5, color=0x00FF00, text_effect="scroll"))` is called
- **THEN** the scroller's `frame_delay` SHALL be 0.02, `offset_seconds` SHALL be 0.5, the rendered text color SHALL be `(0, 255 * brightness, 0)`, and `text_effect` SHALL be `"scroll"`

#### Scenario: A config message with new text values updates the scroller on the next construction
- **WHEN** a config message arrives with `text_settings.frame_delay: 0.06` (slower scroll)
- **THEN** the next `MatrixScroller` construction SHALL pick up the new value, and the next `set_text` call SHALL use 60 ms per pixel

### Requirement: No separate `GET /api/effects` endpoint (data lives in `GET /api/config`)

The Flask process SHALL NOT expose a `GET /api/effects` endpoint. The canonical effect set is reachable from `GET /api/config`'s `effect_settings.effects` field (a list of `{"name", "enabled"}` dicts). The admin UI SHALL render its Effects List sub-section from that field directly. The device's `_DEFAULT_EFFECTS_LIST` constant remains the source of truth for the canonical effect class map, used by both the device (to instantiate classes) and the Flask process (to validate incoming `effect_settings.effects` names against the canonical set in `PUT /api/config`).

#### Scenario: GET /api/effects returns 404
- **WHEN** any client calls `GET /api/effects`
- **THEN** the response SHALL be HTTP 404 (the route is not registered)

#### Scenario: The admin UI does not call /api/effects
- **WHEN** the `/settings` page loads
- **THEN** the browser SHALL issue exactly one config fetch (`GET /api/config`); no second request to `/api/effects` SHALL be made

#### Scenario: PUT /api/config validates effect names against the canonical set
- **WHEN** a `PUT /api/config` body contains an entry `{"name": "DoesNotExist", "enabled": true}`
- **THEN** the server SHALL respond HTTP 400 with a body indicating the unknown name; the existing config SHALL be left untouched (atomic update)
