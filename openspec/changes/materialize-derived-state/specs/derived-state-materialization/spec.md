## ADDED Requirements

### Requirement: Per-message enrichment is precomputed on the event that changes it

The system SHALL precompute per-message derived fields (`suppressed`, `rules`, `sender_name`, `display_time`) on the event that mutates their inputs (a new message arriving, or a config change that affects filter rules or timezone) and SHALL store the result on the `MessageView` instance held in the ring buffer. Subsequent reads via `InMemoryMessages.get_messages()` SHALL return the precomputed values without re-running the filter regex pass or the timezone formatter.

#### Scenario: New message arrival enriches only the new entry
- **WHEN** a new `Message` is added to `InMemoryMessages`
- **THEN** the new `MessageView` is created with `suppressed`, `rules`, `sender_name`, and `display_time` populated by `_enrich_messages`
- **AND** existing `MessageView` entries in the buffer are not re-enriched

#### Scenario: Config change re-enriches all entries
- **WHEN** `MessageManager._handle_config()` applies a new `SignConfig` whose `filters` or `timezone` differ from the prior values
- **THEN** all `MessageView` entries in the buffer are re-enriched against the new filters and timezone
- **AND** the re-enrichment runs at most once per config-envelope event

#### Scenario: get_messages returns precomputed values without filter or formatter work
- **WHEN** `InMemoryMessages.get_messages()` is called
- **THEN** it returns the stored `MessageView` list with the four derived fields already populated
- **AND** it does not invoke `_apply_filter` or `_matches` for any entry
- **AND** it does not invoke `_format_display_time` for any entry

#### Scenario: get_messages does not allocate a new MessageView on the hot read path
- **WHEN** `InMemoryMessages.get_messages(limit, suppress=True)` is called
- **THEN** the returned list contains the same `MessageView` instances that are stored in the deque
- **AND** no per-call `MessageView(...)` construction occurs for entries that were already in the buffer

### Requirement: EffectsCoordinator selects messages from MessageManager during idle

The system SHALL provide an `EffectsCoordinator` constructor option that, when set, makes the coordinator pull its display text from a `MessageManager` instance. The pull SHALL run on the tick path throttled to approximately 4 Hz, with the result stored on the coordinator and consumed by the tick handler. When the pull produces a fresh message that is not yet displayed, the coordinator SHALL advance to show it; when the pull produces no fresh message, the coordinator SHALL pick uniformly at random from the most recent `recent_count` non-suppressed messages and show the selected one.

#### Scenario: Coordinator with message_manager set pulls from MessageManager
- **WHEN** an `EffectsCoordinator` is constructed with `message_manager=manager`
- **THEN** the coordinator's tick path reads from `manager.messages` instead of consuming `self.pending_text`
- **AND** the read runs at most every 250 ms (4 Hz), regardless of tick frequency

#### Scenario: Fresh message takes priority over the recent-N sample
- **WHEN** the throttled pull returns a message that the coordinator has not yet shown
- **THEN** the coordinator displays that message and clears the "unshown" flag
- **AND** does not enter idle-rotation sampling on this tick

#### Scenario: Idle-rotation samples uniformly from the most recent N messages
- **WHEN** the throttled pull returns no unshown fresh message
- **THEN** the coordinator picks one message uniformly at random from the most recent `recent_count` non-suppressed messages
- **AND** displays the selected message via the scroller

#### Scenario: recent_count is load-bearing
- **WHEN** the `EffectsSettings.recent_count` value is changed via `apply_settings(...)`
- **THEN** the coordinator uses the new value on the next idle-rotation pick
- **AND** the prior value is not retained for compat

#### Scenario: Coordinator without message_manager preserves push semantics
- **WHEN** an `EffectsCoordinator` is constructed with `message_manager=None`
- **THEN** the tick path consumes `self.pending_text` (set via `set_text(...)` or `start(...)`)
- **AND** the existing JS-driven push path via `apply_settings(...)` continues to work

### Requirement: preview.js does not register reRender on App.registerOnChange

The system SHALL NOT subscribe the `reRender` aggregator in `heart-message-manager/static/preview/preview.js` to `window.App.registerOnChange` for the message-pick path, because the coordinator now pulls on tick. The `reRender` function SHALL still be callable (e.g. on init) and SHALL still call `apply_config` for the config-envelope rebind path; only the `App.registerOnChange(reRender)` registration is removed.

#### Scenario: reRender is no longer registered on App.registerOnChange
- **WHEN** the preview page loads
- **THEN** `heart-message-manager/static/preview/preview.js` does not call `window.App.registerOnChange(reRender)`
- **AND** the `reRender` function is still defined and still callable for explicit triggers

#### Scenario: apply_config rebind path is preserved in reRender
- **WHEN** a config envelope arrives and the JS shim invokes `reRender`'s apply_config branch
- **THEN** the rebind still runs (config envelopes are infrequent events, the rebind is heavy, and the JS shim is the right place for it)
- **AND** the coordinator's `apply_settings(...)` is still called

### Requirement: pytest tests/ stays at 304 pass + 1 skip

The system SHALL keep `pytest tests/` at 304 passing tests and 1 skipped test after this change is applied, with the same skip as the current baseline (the skip is unrelated to this change).

#### Scenario: Full test suite passes
- **WHEN** `PYTHONPATH=. pytest tests/ -v` is run from the repo root
- **THEN** 304 tests pass and 1 test is skipped
- **AND** the skipped test is the same one that is skipped on `main` before this change is applied

#### Scenario: New tests for enrichment and idle rotation are present
- **WHEN** the test suite is run
- **THEN** `tests/lib_shared/message_manager_enrichment_test.py` exists with at least 4 scenarios (new-message enriches only new entry, config change re-enriches all, get_messages does not run filter or formatter, get_messages does not allocate MessageView on hot read)
- **AND** `tests/lib_shared/effects_coordinator_idle_rotation_test.py` exists with at least 5 scenarios (coordinator with manager pulls, fresh-message priority, idle-rotation samples from recent-N, recent_count is load-bearing, push semantics preserved when manager is None)
