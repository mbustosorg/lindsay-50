## 1. EffectsSettings, TextSettings, and _DEFAULT_EFFECTS_LIST_FULL in lib_shared/models.py

- [ ] 1.1 In `lib_shared/models.py`, add the `EffectsSettings` class — `__init__(effects=None, fade_seconds=2.0, hold_seconds=15.0, intro_seconds=5.0, idle_seconds=300.0, recent_count=5)`. The `effects` argument SHALL default to the module-level constant `_DEFAULT_EFFECTS_LIST_FULL = [{"name": "Hyperspace", "enabled": True}, {"name": "VideoDisplay", "enabled": False}, {"name": "PngDisplay", "enabled": False}, {"name": "Honeycomb", "enabled": True}, {"name": "Flame", "enabled": True}, {"name": "Fireworks", "enabled": True}, {"name": "NightSky", "enabled": True}]` — the full 7-effect list in canonical rotation order, with the 5 historically-defaulted effects enabled and the 2 asset-dependent effects (VideoDisplay, PngDisplay) disabled. Each entry SHALL be a `{"name": str, "enabled": bool}` dict; rotation order is preserved by list position. The class SHALL provide `from_dict`, `to_dict`, and `validate()`. `validate()` SHALL raise `ValueError` with a per-field message on negative pacing durations or `recent_count < 1`; it SHALL also raise if any entry in `effects` is not a `dict` with string `name` and bool `enabled` keys
- [ ] 1.2 In the same file, add the `TextSettings` class (renamed from the previous `ScrollerSettings` — the block is the umbrella for all text effects, with the scroller being the only v1 implementation) — `__init__(frame_delay=0.04, offset_seconds=1.0, color=0xFF0000, text_effect="scroll")`, `from_dict`, `to_dict`, and `validate()`. `text_effect` SHALL be a `Literal["scroll"]` in v1; `from_dict` SHALL raise `ValueError` on any other value. `TextSettings.TEXT_EFFECTS = ("scroll",)` is a class-level constant
- [ ] 1.3 Add a unit test in `tests/effects_settings_test.py` asserting:
  - The defaults match the historical `EffectsCoordinator` constructor values and the 7-entry `_DEFAULT_EFFECTS_LIST_FULL` (5 enabled + 2 disabled)
  - `from_dict` / `to_dict` round-trip losslessly with the new `{"name", "enabled"}` shape
  - `validate()` raises on each out-of-range pacing field
  - `validate()` raises on `recent_count < 1`
  - `validate()` raises on a malformed entry (e.g. `{"name": "Flame"}` with no `enabled`, or `{"enabled": true}` with no `name`)
  - `from_dict({"effects": [{"name": "NotAnEffect", "enabled": True}]})` is accepted at the `from_dict` level (the canonical-set check happens at the Flask validation layer, not inside `EffectsSettings` — the class is just a value object)
- [ ] 1.4 Add a unit test in `tests/text_settings_test.py` asserting the defaults, the round-trip, the `text_effect` validation, and that `validate()` raises on negative `frame_delay` / `offset_seconds` and on colors outside 0–0xFFFFFF

## 2. SignConfig gains effect_settings and text_settings; loses tz_offset_mins AND rendering; gains CURRENT_VERSION and migrations

- [ ] 2.1 In `lib_shared/models.py`, add a class constant `SignConfig.CURRENT_VERSION = 2`. Add `effect_settings: EffectsSettings` and `text_settings: TextSettings` to `SignConfig.__init__`. Default `effect_settings` to `EffectsSettings()` and `text_settings` to `TextSettings()`. Bump the `version` argument default from 1 to `cls.CURRENT_VERSION`
- [ ] 2.2 Remove `tz_offset_mins` from `SignConfig.__init__`, `from_dict`, `to_dict`, `update`, and `update_from_dict`. The wire shape SHALL NOT include `tz_offset_mins` after this change
- [ ] 2.3 Remove the `rendering: RenderingSettings` field from `SignConfig` entirely. Delete the `RenderingSettings` class from `lib_shared/models.py` (it is dead code once `SignConfig.rendering` is gone). The wire shape SHALL NOT include `rendering` after this change. Any code that read `cfg.rendering.*` is updated in the relevant tasks below
- [ ] 2.4 Add `effect_settings` and `text_settings` to `to_dict` and `from_dict` (using the wire names `effect_settings` and `text_settings` — not `scroller_settings` and not `scroller`). The new fields SHALL be guarded by the same `threading.RLock` as the existing fields (`update_from_dict` and `to_dict` SHALL wrap the read/mutate in `_with_lock`)
- [ ] 2.5 At the top of `from_dict` and `update_from_dict`, call `data = migrate(data, current_version=cls.CURRENT_VERSION)` from `lib_shared.config_migrations`. The migration SHALL run BEFORE the field-by-field update; the `version` field in the input is treated as the source version. This is defense-in-depth: the primary migration path is the startup hook in task 3.2
- [ ] 2.6 Add a unit test in `tests/sign_runtime_config_test.py` asserting:
  - `to_dict()` on a default `SignConfig` includes `effect_settings` (7 entries — 5 enabled + 2 disabled — in canonical rotation order, default pacing, recent_count 5) and `text_settings` (defaults) with `version: 2`, and does NOT include `rendering` or `tz_offset_mins`
  - `from_dict(to_dict(cfg))` round-trips losslessly
  - An empty `from_dict({})` produces a config at v2 with the defaults
  - `from_dict({"version": 1, "tz_offset_mins": -420, "rendering": {...}, "filters": [...], "senders": [...]})` produces a v2 config with `tz_offset_mins` and `rendering` not stored, the new blocks at their defaults, and the original `filters` and `senders` preserved
  - `hasattr(cfg, "rendering")` is `False` and `RenderingSettings` is not importable from `lib_shared.models`
  - A concurrent read of `cfg.effect_settings.fade_seconds` while another thread calls `update_from_dict` with a new `effect_settings` returns either the old or the new value, never a half-mutated value

## 3. Config migrations module in lib_shared; server-side startup migration

- [ ] 3.1 Create `lib_shared/config_migrations.py` with the `MIGRATIONS` registry dict and a `migrate(d, current_version)` function. The module SHALL export:
  - `MIGRATIONS: dict[int, Callable[[dict], dict]]` — currently `{1: _v1_to_v2}`
  - `migrate(d: dict, current_version: int) -> dict` — chains the registered migrations from the input's version up to `current_version`; treats a missing `version` key as v1; raises `KeyError` with a clear message if a migration is missing for a required step
  - `migrate_on_startup(s3_getter, sqlite_writer, mqtt_publisher, *, log=logger.info)` — calls `s3_getter()` to read the latest config, runs `migrate(d, current_version=SignConfig.CURRENT_VERSION)`, and if the version changed (i.e. a migration ran), calls `sqlite_writer(migrated)` and `mqtt_publisher(migrated)` and writes the migrated config back to S3 via a new `s3_writer` argument. If the stored version is already at `CURRENT_VERSION`, the function is a no-op
- [ ] 3.2 Implement `_v1_to_v2(d)` as the v1 → v2 migration:
  - Returns a shallow copy of `d` (does not mutate the caller's dict)
  - Pops `tz_offset_mins` if present
  - Pops `rendering` if present (the old `RenderingSettings` block is being superseded by `text_settings` + `effect_settings`; no v1 → v2 mapping for individual rendering fields — the new blocks start from defaults)
  - Sets `effect_settings` to the default `EffectsSettings().to_dict()` if absent
  - Sets `text_settings` to the default `TextSettings().to_dict()` if absent
  - Sets `version` to `2`
  - Preserves `filters`, `senders`, `sign`, and `timezone` unchanged
- [ ] 3.3 Add a unit test in `tests/config_migrations_test.py` asserting:
  - `migrate({"version": 1, "tz_offset_mins": -420, "rendering": {"speed": 2}, "filters": [{"type": "keyword", "pattern": "spam", "action": "suppress"}]}, current_version=2)` returns a dict at v2 with `tz_offset_mins` and `rendering` dropped, `effect_settings` and `text_settings` set to defaults, `filters` preserved, and `version: 2`
  - `migrate({"version": 2, ...}, current_version=2)` returns the input unchanged (idempotency)
  - `migrate({"tz_offset_mins": -420}, current_version=2)` (no version key) is treated as v1 and returns a v2 dict
  - `migrate({"version": 1}, current_version=4)` with only v1 → v2 registered raises `KeyError` with a message naming the missing step
  - A v1 payload with `senders=[{"phone": "+15551234567", "name": "Lindsay"}]` preserves the `senders` through the migration
  - The migration does NOT mutate the input dict (the caller's original dict retains its `version: 1`, `tz_offset_mins`, and `rendering`)
- [ ] 3.4 Add a unit test in `tests/config_migrations_test.py` (or a new `tests/migrate_on_startup_test.py`) asserting:
  - `migrate_on_startup(s3_getter=lambda: v1_dict, sqlite_writer=mock, mqtt_publisher=mock, s3_writer=mock, log=mock_log)` — the v1 dict is migrated; the migrated config is passed to `sqlite_writer`, `mqtt_publisher`, and `s3_writer`; `mock_log` is called once with an INFO line including "Migrated SignConfig from v1 to v2"
  - `migrate_on_startup(s3_getter=lambda: v2_dict, ...)` — none of the writers are called; no log line is emitted
  - `migrate_on_startup(s3_getter=lambda: None, ...)` (no S3 config, fresh install) — a default v2 config is constructed and passed to all writers (the first config the device sees, so the operator can see the defaults in the admin UI)
  - `migrate_on_startup(s3_getter=raises, ...)` — the exception propagates (so the existing startup error handling kicks in; the migration does NOT silently swallow S3 read errors)

## 4. heart-message-manager/main.py: call migrate_on_startup from the S3-rebuild path

- [ ] 4.1 In `heart-message-manager/main.py` (or `heart-message-manager/sqlite.py` — wherever the existing "rebuild-from-S3 on startup" step lives), call `migrate_on_startup(...)` from `lib_shared.config_migrations` after the S3 read. The s3_getter is the existing S3 read function; the sqlite_writer is the existing SQLite write function; the mqtt_publisher is the existing MQTT publish function. The s3_writer is the existing S3 write function. The order SHALL be: SQLite (local, fast) → MQTT (devices see the new config) → S3 (eventual consistency), so a failure between MQTT and S3 leaves the device seeing v2, the server's SQLite at v2, and S3 at v1 (the next startup will re-migrate, idempotently)
- [ ] 4.2 Add a test in `tests/test_message_manager.py` (or a new `tests/startup_migration_test.py`) asserting:
  - On server startup with a v1 S3 config, the SQLite row is updated to v2 (verified by reading the SQLite row), a `type="config"` MQTT envelope is published with the v2 payload (verified by capturing the published message), and the S3 entry is rewritten at v2 (verified by reading the S3 entry back)
  - On server startup with a v2 S3 config, no SQLite update, no MQTT publish, and no S3 write happen (verified by checking that none of the writer functions are called)
  - On server startup with no S3 config (fresh install), a default v2 config is written to SQLite, MQTT, and S3 (the first config the device sees)
  - The startup migration logs an INFO line "Migrated SignConfig from v1 to v2 (preserved N filters, M senders)" when a migration runs (the log call is captured in the test)

## 5. MessageManager._handle_config and update_from_dict accept the new shape

- [ ] 5.1 Verify (no code change) that `MessageManager._handle_config` already calls `SignConfig.update_from_dict(envelope.payload)` — confirmed in the existing code at `lib_shared/message_manager.py:132-135`. Because `update_from_dict` now calls `migrate(...)` at the top, a v1 payload arriving over MQTT is transparently upgraded to v2 in the device's in-memory config (defense in depth — the primary migration path is the startup hook in task 4)
- [ ] 5.2 Add a test in `tests/test_message_manager.py` asserting:
  - A `type="config"` envelope with a v2 payload (new `effect_settings` + `text_settings` keys) updates the in-memory `SignConfig` (read after dispatch returns the new values)
  - A `type="config"` envelope with a v1 payload (no `version` key, has `tz_offset_mins` and `rendering`) is migrated to v2 on the device: the in-memory `SignConfig` ends up at v2 with `tz_offset_mins` and `rendering` not stored and the new blocks at their defaults
  - A `type="config"` envelope WITHOUT the new blocks leaves the existing values alone (no overwrite with defaults)

## 6. lib_shared/messages.py: timezone offset computed at read-time

- [ ] 6.1 In `lib_shared/messages.py`, change `_format_display_time(received_at, tz_offset_mins)` to `_format_display_time(received_at, timezone)`. The function SHALL compute the offset via `zoneinfo.ZoneInfo(timezone)` and convert the UTC `received_at` to local time. On `ZoneInfoNotFoundError` or `ValueError`, fall back to `ZoneInfo("US/Pacific")` and continue
- [ ] 6.2 Update the caller in `lib_shared/messages.py:_enrich_messages` (or wherever the offset is currently passed) to pass the IANA `timezone` from the config instead of `tz_offset_mins`
- [ ] 6.3 Remove `tz_offset_mins` from the `InMemoryMessages` constructor signature and any related plumbing (the offset is now computed at format-time, not stored on the messages container)
- [ ] 6.4 Add a test in `tests/messages_test.py` asserting:
  - `_format_display_time("2026-06-15T18:00:00Z", timezone="America/Los_Angeles")` returns a string reflecting Pacific time (UTC-7 in June)
  - `_format_display_time("2026-06-15T18:00:00Z", timezone="America/New_York")` returns a string reflecting Eastern time (UTC-4 in June)
  - `_format_display_time("2026-06-15T18:00:00Z", timezone="Not/A/Zone")` returns a US/Pacific string and does NOT raise

## 7. heart-message-manager/sqlite.py and server_time.py: drop tz_offset_mins

- [ ] 7.1 In `heart-message-manager/sqlite.py`, remove the `cfg.tz_offset_mins = tz_offset_mins(cfg.timezone)` recompute on every config write (around line 137)
- [ ] 7.2 In `heart-message-manager/server_time.py`, remove the `tz_offset_mins(tz_name)` function (the only caller was the deleted SQLite recompute)
- [ ] 7.3 Add a test asserting that `sqlite.put_config(cfg)` does NOT mutate `cfg.tz_offset_mins` (the attribute no longer exists; the test asserts the config is written unchanged)

## 8. Flask: extended PUT /api/config validation (NO GET /api/effects)

- [ ] 8.1 In `heart-message-manager/main.py`, do NOT add a `GET /api/effects` endpoint. The canonical effect set is reachable from `GET /api/config`; the UI reads it from `cfg.effect_settings.effects` directly
- [ ] 8.2 Add `_build_sign_config_from_request(data: dict) -> tuple[SignConfig | None, Response | None]` that:
  - Calls `migrate(data, current_version=SignConfig.CURRENT_VERSION)` at the top so v1 inputs are normalized to v2 before validation
  - Validates `effect_settings.effects` as a list of `{"name": str, "enabled": bool}` dicts (NOT a list of strings) and rejects any entry whose `name` is not in the device's canonical effect set (the same set used by the device's `_DEFAULT_EFFECTS_LIST` for instantiating classes) with HTTP 400 and a per-field error
  - Returns `(None, jsonify({"error": "..."}), 400)` when an effect entry is malformed, an effect name is unknown, a behavior field is out of range, `recent_count` is not a positive integer, the color is out of range, or `text_effect` is not in the v1 enum
  - Returns `(SignConfig.from_dict(data), None)` when validation passes
  - Returns per-field error messages in the format `{"error": "effect_settings.effects: unknown effect 'X'"}` (or similar)
- [ ] 8.3 Update `api_put_config` to call `_build_sign_config_from_request` and return the 400 response on validation failure. The saved SQLite row SHALL be at `version: 2` regardless of the input version (the migration runs before `from_dict` constructs the `SignConfig`, and `from_dict` preserves the migrated `version`)
- [ ] 8.4 Add tests in `tests/test_auth.py` (or a new `tests/api_config_validation_test.py`) asserting:
  - `GET /api/effects` returns HTTP 404 (the route is intentionally absent)
  - `PUT /api/config` with `effect_settings.effects=[{"name": "DoesNotExist", "enabled": True}]` returns HTTP 400
  - `PUT /api/config` with `effect_settings.effects=[{"name": "Flame"}]` (missing `enabled`) returns HTTP 400
  - `PUT /api/config` with `effect_settings.fade_seconds=-1` returns HTTP 400
  - `PUT /api/config` with `effect_settings.recent_count=0` returns HTTP 400
  - `PUT /api/config` with `text_settings.text_effect="swirl"` returns HTTP 400
  - `PUT /api/config` with a v1 payload (no `version` key, has `tz_offset_mins` and `rendering`) returns HTTP 200 and the saved SQLite row is at v2 with `tz_offset_mins` and `rendering` dropped
  - `PUT /api/config` with a well-formed v2 payload (effect entries as `{"name", "enabled"}` dicts) returns HTTP 200 and the new values are persisted to SQLite

## 9. heart-message-manager/templates/settings.html: one Effects section (Effects List + Settings sub-sections) + a Text section

- [ ] 9.1 Add an "Effects" section that contains two sub-sections. Inside it, an "Effects List" sub-section (named for what the operator sees — a list of which effects are enabled — not "Rotation", which implies cycling) renders one checkbox per entry in `cfg.effect_settings.effects` (the full 7-entry list of `{"name", "enabled"}` dicts), in the order it appears in the config. Each checkbox is named `effect_<classname>` and is checked if the entry's `enabled` flag is `true`. The page does NOT call `GET /api/effects` (that endpoint was dropped — the data lives in `GET /api/config`). Inside the same Effects section, a "Settings" sub-section (named generically because future fields added to the block may not be pacing-related) renders five labeled slider+number pairs:
  - "Fade speed" (0.1–10.0, step 0.1)
  - "Hold time" (1–120, step 1)
  - "Intro time" (0–30, step 0.5)
  - "Idle time" (30–3600, step 30)
  - "Recent messages" (1–20, step 1) — newly surfaced per operator feedback
  Each pair is linked by JS so the slider sets the number input and vice versa
- [ ] 9.2 Add a separate "Text" section (NOT a sub-section of Effects — the text renderer is a different subsystem, and the section is named "Text" rather than "Scrolling" because the scroller is one of several planned text effects that will share the `text_settings` block) with:
  - A "Scroll speed" slider (0–100) that maps to `text_settings.frame_delay` in the range 0.1–0.01 (inverse)
  - A "Text color" `<input type="color">` plus a hex text input
  - A "Text effect" `<select>` with one option "scroll", rendered as disabled with a tooltip "More text effects coming soon"
- [ ] 9.3 REMOVE the previous "Rendering Defaults" section entirely. The "Speed" field's value maps to `effect_settings.fade_seconds` for the v2 default; existing v1 `rendering.speed` values are NOT carried over by the UI (the v1 → v2 migration drops the `rendering` block). The other old "Rendering Defaults" fields (font, font_size, letter_spacing, line_spacing, bg_color, align) are gone with no replacement in this change
- [ ] 9.4 Add a small inline `<script>` block (or extend `static/app.js`) that wires the slider+number pairs and the scroll-speed inverse mapping, and serializes the new fields into the existing form submit (no fetch rewrite; the form is still a single POST to `/settings`). The Effects List sub-section's checkboxes serialize as `effect_settings.effects` entries of the form `{"name": "<classname>", "enabled": <bool>}` — the full 7-entry list is submitted on every save, with each entry's `enabled` flag reflecting the checkbox state at submit time
- [ ] 9.5 Add a test in `tests/sign_settings_ui_test.py` asserting that `GET /settings` returns HTML containing:
  - An "Effects" section that contains both an "Effects List" sub-section AND a "Settings" sub-section
  - The Effects List sub-section has seven checkboxes named after the effect classes (Hyperspace, VideoDisplay, PngDisplay, Honeycomb, Flame, Fireworks, NightSky — but NOT Heartbeat) in canonical rotation order, with the 5 historically-defaulted effects rendered as checked and the 2 asset-dependent effects (VideoDisplay, PngDisplay) rendered as unchecked
  - The Settings sub-section has the four historical labels AND the new "Recent messages" label and the wire name `recent_count` in a `name=` attribute
  - A separate "Text" section (NOT a sub-section of Effects) with the scroll-speed slider, the text color input, and the disabled text-effect select
  - The page does NOT contain a top-level "Pacing" section header, does NOT contain a "Scrolling" section header, and does NOT contain a "Rendering Defaults" section header
  - The page does NOT issue a fetch to `/api/effects` (verified by `grep` on the rendered HTML for the absence of any such URL)
- [ ] 9.6 Add a test asserting that `POST /settings` with the new fields (including `recent_count=10`) updates the in-memory config (via a follow-up `GET /api/config` call) and publishes a `type="config"` envelope

## 10. EffectsCoordinator: read from EffectsSettings, drop request_message, drop recent_provider

- [ ] 10.1 In `lib_shared/effects_coordinator.py`, change the constructor signature to accept `effect_settings: EffectsSettings` (NOT `config: SignConfig`) and `message_manager` (replacing `recent_provider`). The behavior-related kwargs (`fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`) SHALL be read from `effect_settings` when not provided explicitly
- [ ] 10.2 Remove the public `request_message(text)` method. Remove the `self._recent = deque(...)` initialization. Update `_random_recent` to read from `message_manager.get_messages(limit=self.recent_count, suppress=True)` instead of the lambda path
- [ ] 10.3 Update the docstring at the top of the file (the one explaining the Pi vs. browser paths) to reflect the single-message-manager design and to note that the coordinator does NOT import or depend on `SignConfig`
- [ ] 10.4 Add a test in `tests/effects_coordinator_cleanup_test.py` asserting:
  - `EffectsCoordinator` no longer has a public `request_message` method
  - `EffectsCoordinator(..., message_manager=mock_manager, effect_settings=es)` reads recent bodies from `mock_manager.get_messages(limit=5, suppress=True)`
  - `_random_recent` returns `None` when `message_manager.get_messages` returns an empty list
  - Behavior fields are read from `effect_settings` when not overridden (including `recent_count`)
  - Explicit `fade_seconds=5.0` kwarg overrides the effect_settings value
  - The coordinator's `import` block does NOT include `lib_shared.models.SignConfig` (the coordinator is decoupled from the full config model)

## 11. heart-matrix-controller/main.py: build effects from effect_settings, wire manager to coordinator

- [ ] 11.1 Add a constant `_EFFECT_CLASSES` mapping effect class names to classes (e.g. `{"Hyperspace": Hyperspace, "VideoDisplay": VideoDisplay, "PngDisplay": PngDisplay, "Honeycomb": Honeycomb, "Flame": Flame, "Fireworks": Fireworks, "NightSky": NightSky, "Heartbeat": Heartbeat}`)
- [ ] 11.2 Add `_build_effects(effect_settings, display) -> list[Effect]` that iterates `effect_settings.effects` in order, includes only entries with `enabled: true`, instantiates the matching class with `display` for each included entry, and skips (with a WARNING log) any name not in `_EFFECT_CLASSES` or whose constructor raises
- [ ] 11.3 Replace the hard-coded effect list literal in `main.py` with `_build_effects(cfg.effect_settings, display)`. Construct `Heartbeat(display)` separately and pass it as `coordinator.heart`. The default effect list (when `cfg.effect_settings.effects` is the 7-entry default) produces a 5-effect rotation: the 5 enabled entries are instantiated, the 2 disabled ones (VideoDisplay, PngDisplay) are filtered out before the class lookup
- [ ] 11.4 Construct the `MessageManager` with NO `on_message` argument. Pass `_message_mgr` and `cfg.effect_settings` to `EffectsCoordinator` (do NOT pass `cfg` — the coordinator takes only the block)
- [ ] 11.5 Add a test in `tests/main_effects_build_test.py` (or extend an existing test) asserting:
  - `_build_effects(EffectsSettings(effects=[{"name": "Hyperspace", "enabled": True}, {"name": "Flame", "enabled": True}, {"name": "Fireworks", "enabled": True}, {"name": "NightSky", "enabled": True}]), stub_display)` returns four instances in the configured order
  - `_build_effects(EffectsSettings(effects=[{"name": "Hyperspace", "enabled": True}, {"name": "DoesNotExist", "enabled": True}, {"name": "Flame", "enabled": True}]), stub_display)` returns the known effects only and logs a WARNING
  - `_build_effects(EffectsSettings(effects=[{"name": "VideoDisplay", "enabled": True}, {"name": "Flame", "enabled": True}]), stub_display)` where `VideoDisplay(stub_display)` raises returns `[Flame_instance]` and logs a WARNING with the exception
  - `_build_effects(EffectsSettings(effects=[{"name": "Hyperspace", "enabled": True}, {"name": "VideoDisplay", "enabled": False}, {"name": "Flame", "enabled": True}]), stub_display)` returns `[Hyperspace_instance, Flame_instance]` in that order — `VideoDisplay` is skipped without even attempting the constructor because `enabled: false`
  - The default `EffectsSettings()` constructor produces the 7-entry default (5 enabled + 2 disabled), and `_build_effects(default_settings, stub_display)` returns 5 instances in the default rotation order (the 2 disabled entries are filtered out)

## 12. heart-matrix-controller/scroller.py: read from TextSettings

- [ ] 12.1 Add a `text_settings: TextSettings | None` keyword argument to `MatrixScroller.__init__`. When provided, initialize `frame_delay`, `offset_seconds`, `_color`, and `text_effect` from the settings. When not provided, fall back to the existing keyword arguments (kept for tests with explicit overrides)
- [ ] 12.2 Add a test in `tests/scroller_matrix_test.py` asserting:
  - `MatrixScroller(display, text_settings=TextSettings(frame_delay=0.02, color=0x00FF00))` initializes `self.frame_delay = 0.02` and `self._color = 0x00FF00`
  - Explicit kwargs override the settings (e.g. `MatrixScroller(display, text_settings=cfg, frame_delay=0.01)` ends up with `frame_delay=0.01`)
  - `text_effect` is stored on the scroller but does NOT change scroller behavior in v1 (no branching on it in `tick` or `render`)

## 13. heart-message-manager/preview_main.py and static/app.js

- [ ] 13.1 In `heart-message-manager/preview_main.py`, remove the `js.window.request_message = request_message` assignment and the `def request_message(body)` function. Replace the `setInterval(pollLatestMessage, 3000)` body-handoff with a call to the new mechanism (e.g. `message_manager.dispatch(MessageEnvelope("message", {"body": body, ...}).to_json())` or a new `message_manager.feed_to_coordinator(coordinator)` method). The polling loop SHALL still poll `/api/live-messages?limit=1&suppress=true` every 3 s; only the call to the coordinator changes
- [ ] 13.2 In `heart-message-manager/static/app.js`, add a "Sign settings" card to the dashboard that reads `window.APP_CONFIG.effect_settings`, `effect_settings.effects`, and `text_settings` (NOT `scroller_settings` and NOT `scroller`) and renders the current values with human-readable labels (effect rotation, fade / hold / intro / idle / recent_count, scroll speed in pixels per second, text effect). The card is informational only; the settings are still edited on `/settings`
- [ ] 13.3 Add a test asserting that `static/app.js` reads the new fields from `window.APP_CONFIG` (e.g. `grep "APP_CONFIG.effect_settings" heart-message-manager/static/app.js` matches and `grep "APP_CONFIG.text_settings" heart-message-manager/static/app.js` matches)
- [ ] 13.4 Add a test asserting that `heart-message-manager/preview_main.py` does NOT contain a `def request_message(` definition (the call site has been replaced)

## 14. Test fixture cleanup for tz_offset_mins removal

- [ ] 14.1 In `tests/test_message_manager.py`, remove `tz_offset_mins` from the test fixtures (around line 100 and line 416). Replace any assertions that read `cfg.tz_offset_mins` with assertions that read the offset via `zoneinfo.ZoneInfo(cfg.timezone).utcoffset(...)` at a fixed instant
- [ ] 14.2 In `tests/test_auth.py` (or any other test that reads `tz_offset_mins`), apply the same fixture update
- [ ] 14.3 In `heart-message-manager/static/app.js`, replace the `(config && config.tz_offset_mins) || 0` fallback (around line 226) with `(config && config.timezone) ? new Intl.DateTimeFormat(...)` — the offset is computed in the browser from the IANA name

## 15. Verification

- [ ] 15.1 Run the full test suite (`PYTHONPATH=. pytest tests/ -v`) and confirm all new + existing tests pass. Fix any failures introduced by the `tz_offset_mins` removal, the `rendering` field removal, the `EffectsCoordinator` signature change, the `scroller` → `text_settings` rename, the `ScrollerSettings` → `TextSettings` class rename, or the `RenderingSettings` class deletion
- [ ] 15.2 Start the Flask app locally, visit `/settings`, and confirm the one-big-Effects-section structure renders with the current config's values (Effects List sub-section with effect checkboxes, Settings sub-section with the five sliders including recent_count, separate Text section). Confirm there is no "Pacing", "Scrolling", or "Rendering Defaults" section header. Change a settings field, save, and confirm the page reflects the new value on reload
- [ ] 15.3 Start the Flask app locally and confirm `GET /api/effects` returns HTTP 404 (the route is intentionally absent in this change). The same data is available via `GET /api/config`'s `effect_settings.effects` field
- [ ] 15.4 Trigger an SMS via the local curl test in the project README. Confirm the new message arrives on the device within one MQTT round trip. Confirm the device's `EffectsCoordinator` queue + display the new message correctly (the `request_message` removal is transparent if the wiring is right)
- [ ] 15.5 Edit `cfg.effect_settings.effects` to disable an effect (e.g. flip "Flame"'s `enabled` flag to `false` in the settings UI; the entry stays in the list, just with `enabled: false`), save, and confirm the device's rotation skips the disabled effect on the next cycle advance. Confirm the WARNING log for any unconstructable effect
- [ ] 15.6 Confirm `grep -r "tz_offset_mins" lib_shared/ heart-message-manager/ heart-matrix-controller/ tests/` returns no matches (the field is fully removed)
- [ ] 15.7 Confirm `grep -nE "def request_message|on_message=lambda.*coordinator" heart-matrix-controller/main.py heart-message-manager/preview_main.py` returns no matches (the old wiring is gone)
- [ ] 15.8 Confirm `grep -nE "EffectsCoordinator\(.*config=" heart-matrix-controller/ heart-message-manager/` returns no matches (the coordinator takes `effect_settings`, not the full `SignConfig`)
- [ ] 15.9 Confirm `grep -nE 'self\.scroller_settings\b|self\.scroller\b.*=' lib_shared/models.py` returns no matches — `SignConfig` has `effect_settings` and `text_settings` only (the field was renamed; `ScrollerSettings` no longer exists). The class `Scroller` (the device's scroller instance) and the class `TextSettings` (the settings blob) are distinct types
- [ ] 15.10 Confirm `grep -nE 'RenderingSettings|cfg\.rendering|\.rendering\.' lib_shared/ heart-message-manager/ heart-matrix-controller/ tests/` returns no matches — the `RenderingSettings` class is deleted and `SignConfig.rendering` is gone
- [ ] 15.11 Confirm `git diff` on `lib_shared/message_manager.py` shows no change to the `_handle_config` body (it still calls `update_from_dict`; only the shape of the payload changes, and the migration runs inside `update_from_dict`)
- [ ] 15.12 Confirm the dashboard's "Sign settings" card renders the current values when the page loads with a non-default config
- [ ] 15.13 Run a v1 → v2 migration test: PUT a v1 payload (no `version` key, has `tz_offset_mins` and `rendering`) to `/api/config`, then `GET /api/config` and assert the response is at v2 with `tz_offset_mins` and `rendering` not present and the new blocks at their defaults. Confirm the on-disk SQLite row is at v2 (verify via direct SQLite query in the test)
- [ ] 15.14 Confirm a v1 config arriving over MQTT is migrated on the device: simulate by calling `MessageManager.dispatch(v1_envelope_json)` (where the envelope payload includes `tz_offset_mins` and `rendering`) and assert the in-memory `SignConfig.version == 2` and the new blocks are present
- [ ] 15.15 Confirm the **startup migration** works end-to-end: with a v1 S3 config in place, restart the server and confirm (a) the SQLite row is updated to v2, (b) a `type="config"` MQTT envelope is published with the v2 payload, and (c) the S3 entry is rewritten at v2. Then restart again and confirm the startup is a no-op (no SQLite write, no MQTT publish, no S3 write)
