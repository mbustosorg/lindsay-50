## MODIFIED Requirements

### Requirement: EffectsCoordinator no longer exposes request_message

`EffectsCoordinator` SHALL NOT expose a public `request_message(text)` method. New messages arrive via the `MessageManager` (which is now the single source of recent messages), and the coordinator reads from the manager on its next `tick()`. The internal `_recent` deque SHALL be removed; the coordinator SHALL NOT maintain its own recent-message buffer.

The Pi's `heart-matrix-controller/main.py` SHALL NOT pass an `on_message=lambda msg: coordinator.request_message(msg.body)` callback to `MessageManager`. The `MessageManager` is constructed with no `on_message` argument.

The browser preview's `heart-message-manager/preview_main.py` SHALL NOT call `coordinator.request_message(body)`; the polling loop's body-handoff collapses to a single mechanism shared with the Pi (the new path is `message_manager.dispatch(...)` or an equivalent that the coordinator observes on its next tick).

#### Scenario: EffectsCoordinator has no request_message method
- **WHEN** a client inspects `dir(EffectsCoordinator)`
- **THEN** `"request_message"` SHALL NOT be a member of the public API (private helpers prefixed with `_` are not part of the public surface)

#### Scenario: A new message on the Pi reaches the coordinator via the MessageManager
- **WHEN** a `type="message"` envelope arrives on the Pi and `MessageManager.dispatch` adds the body to its ring buffer
- **THEN** the next `coordinator.tick()` SHALL observe the new body in its random-recent pick or its `pending_text` check, and the coordinator SHALL queue the message for display at the next mode transition

#### Scenario: A new message in the browser reaches the coordinator via the MessageManager
- **WHEN** the browser's `setInterval(pollLatestMessage, 3000)` loop calls `coordinator.request_message` (or its replacement)
- **THEN** the call SHALL be routed through the browser's `MessageManager` (not the coordinator's removed method), and the coordinator SHALL observe the new body on its next `tick()`

### Requirement: EffectsCoordinator reads recent messages from a MessageManager

`EffectsCoordinator.__init__` SHALL accept a `message_manager` argument (a `MessageManager` or any object with a `get_messages(limit, suppress=True)` method returning objects with a `.message.body` attribute). The `recent_provider` callable argument SHALL be removed. The coordinator SHALL call `message_manager.get_messages(limit=self.recent_count, suppress=True)` on every `_random_recent()` invocation.

The Pi passes its own `_message_mgr` (a `MessageManager` constructed with no `on_message` callback). The browser preview passes its own `MessageManager` (the same instance the polling loop feeds). One path, one argument.

#### Scenario: The Pi constructs the coordinator with a MessageManager
- **WHEN** `EffectsCoordinator(display, scroller, effects, heart=heartbeat, message_manager=_message_mgr, effects_settings=cfg.effects_settings)` is called
- **THEN** the coordinator SHALL hold a reference to `_message_mgr` and SHALL call `_message_mgr.get_messages(limit=self.recent_count, suppress=True)` on every random-recent pick

#### Scenario: The browser constructs the coordinator with a MessageManager
- **WHEN** the browser preview's coordinator is constructed with a `message_manager` argument
- **THEN** the coordinator SHALL use the manager's ring buffer as the single source of recent messages, and the coordinator's `_recent` deque SHALL NOT be populated separately

#### Scenario: The coordinator survives a manager that returns an empty list
- **WHEN** `message_manager.get_messages(limit=5, suppress=True)` returns an empty list
- **THEN** `_random_recent()` SHALL return `None` and the coordinator SHALL keep its current state (no `pending_text` update, no mode transition)

### Requirement: EffectsCoordinator takes an EffectsSettings argument, not a full SignConfig

`EffectsCoordinator.__init__` SHALL accept an `effects_settings: EffectsSettings` argument (not a `SignConfig`). The coordinator SHALL NOT receive a reference to the full `SignConfig`; it SHALL NOT know about filters, senders, sign name, or timezone. The pacing-related keyword arguments (`fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`) SHALL be read from `effects_settings` when not provided explicitly. The per-field kwargs SHALL remain as overrides for tests, but the default behavior is to read from the block.

The coordinator SHALL NOT subscribe to live config updates mid-run. The Pi re-constructs the coordinator on every config message; the new coordinator picks up the new values. The fade-in-progress (if any) completes with the old values; the next mode transition uses the new ones.

#### Scenario: The coordinator initializes pacing from the effects_settings block
- **WHEN** `EffectsCoordinator(..., effects_settings=EffectsSettings(fade_seconds=3.0, hold_seconds=20.0, intro_seconds=4.0, idle_seconds=120.0, recent_count=10))` is called
- **THEN** the coordinator's `self.fade_seconds` SHALL be 3.0, `self.hold_seconds` SHALL be 20.0, `self.intro_seconds` SHALL be 4.0, `self.idle_seconds` SHALL be 120.0, and `self.recent_count` SHALL be 10

#### Scenario: Explicit kwargs override the effects_settings block
- **WHEN** `EffectsCoordinator(..., effects_settings=cfg.effects_settings, fade_seconds=5.0)` is called
- **THEN** the coordinator's `self.fade_seconds` SHALL be 5.0 (the explicit override), and the other fields SHALL come from `cfg.effects_settings`

#### Scenario: A config message updates pacing on the next coordinator construction
- **WHEN** a config message with new `effects_settings.pacing` arrives and the device rebuilds the coordinator with the new block
- **THEN** the new coordinator's pacing fields SHALL equal the new values, and the next `tick()` SHALL use the new fade duration for any active fade

#### Scenario: The coordinator never receives a SignConfig
- **WHEN** a code reviewer greps the codebase for `EffectsCoordinator(..., config=` (passing a SignConfig)
- **THEN** there SHALL be no call sites; the only accepted argument is `effects_settings: EffectsSettings`. The coordinator's `import` of `lib_shared.models.SignConfig` SHALL NOT be required (a forward declaration that the coordinator does not depend on the full config model).

### Requirement: SignConfig no longer carries tz_offset_mins

`SignConfig` SHALL NOT have a `tz_offset_mins` field. The constructor, `from_dict`, `to_dict`, `update`, and `update_from_dict` SHALL NOT reference `tz_offset_mins`. The wire shape emitted by `to_dict()` SHALL NOT include a `tz_offset_mins` key.

The timezone offset SHALL be computed at read-time by `lib_shared/messages._format_display_time` (and any other reader) via `zoneinfo.ZoneInfo` from the IANA `timezone` string on the config. The `tz_offset_mins` parameter on `_format_display_time` SHALL be removed.

`heart-message-manager/sqlite.py` SHALL NOT recompute the offset on every config write. `heart-message-manager/server_time.tz_offset_mins` SHALL be removed (its only caller is the deleted SQLite recompute).

The v1 → v2 migration in `lib_shared/config_migrations.py` SHALL drop `tz_offset_mins` from any v1 payload it processes.

#### Scenario: SignConfig.to_dict() does not include tz_offset_mins
- **WHEN** a `SignConfig` is serialized via `to_dict()`
- **THEN** the resulting dict SHALL NOT contain a `tz_offset_mins` key

#### Scenario: SignConfig.from_dict() ignores tz_offset_mins
- **WHEN** `SignConfig.from_dict({"tz_offset_mins": -420, "timezone": "America/Los_Angeles", "version": 1})` is called (a v1 payload)
- **THEN** the migration runs, the resulting `SignConfig` SHALL have `timezone="America/Los_Angeles"`, `version=2`, and SHALL NOT have any stored offset; the offset SHALL be computed at read-time via `ZoneInfo`

#### Scenario: The message display time uses the computed offset
- **WHEN** `_format_display_time("2026-06-15T18:00:00Z", timezone="America/Los_Angeles")` is called
- **THEN** the returned string SHALL reflect Pacific time (UTC-7 in June, due to DST) — e.g. "2026-06-15 11:00 am pdt"

#### Scenario: An invalid timezone falls back to US/Pacific
- **WHEN** `_format_display_time("2026-06-15T18:00:00Z", timezone="Not/A/Real/Zone")` is called
- **THEN** the returned string SHALL reflect US/Pacific time and SHALL NOT raise

#### Scenario: The Flask dashboard reads the offset from the IANA timezone
- **WHEN** the operator's browser loads the dashboard with `window.APP_CONFIG.timezone = "America/New_York"`
- **THEN** the displayed message times SHALL be in Eastern time, computed in the browser via `Intl.DateTimeFormat` (which uses the IANA name directly)
