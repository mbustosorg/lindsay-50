## ADDED Requirements

### Requirement: Per-message enrichment is precomputed on the event that changes it

The system SHALL precompute per-message derived fields (`suppressed`, `rules`, `sender_name`, `display_time`) on the event that mutates their inputs (a new message arriving via `InMemoryMessages.add()`, or a config change via `InMemoryMessages.re_enrich_all()` triggered by `MessageManager._handle_config()`) and SHALL store the result on the `MessageView` instance held in the ring buffer. Subsequent reads via `InMemoryMessages.get_messages()` SHALL return the precomputed values without re-running the filter regex pass or the timezone formatter.

#### Scenario: New message arrival enriches only the new entry
- **WHEN** a new `Message` is added to `InMemoryMessages`
- **THEN** the new `MessageView` is created with `suppressed`, `rules`, `sender_name`, and `display_time` populated by `_enrich_messages`
- **AND** existing `MessageView` entries in the buffer are not re-enriched

#### Scenario: Config change re-enriches all entries
- **WHEN** `MessageManager._handle_config()` applies a new `SignConfig` whose `filters` or `timezone` differ from the prior values
- **THEN** `InMemoryMessages.re_enrich_all()` is called
- **AND** all `MessageView` entries in the buffer are re-enriched against the new filters and timezone
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

### Requirement: EffectsCoordinator requires a MessageManager and pulls via get_display_message

The system SHALL construct `EffectsCoordinator` with a required `message_manager: MessageManager` keyword argument (no default, no `None` fallback). The coordinator SHALL expose a `get_display_message() -> str | None` method that encapsulates the selection algorithm: it reads the most recent `recent_count` non-suppressed messages from the manager, returns the body of the head entry when its id differs from `self._last_shown_message_id` (fresh-message priority), and otherwise picks uniformly at random from the list and returns that entry's body. The coordinator's tick path SHALL call `get_display_message()` throttled to approximately 4 Hz and SHALL use the cached result on non-pull ticks.

#### Scenario: EffectsCoordinator requires message_manager
- **WHEN** `EffectsCoordinator(...)` is constructed without a `message_manager` argument
- **THEN** the constructor raises `TypeError` with a message that names the missing parameter

#### Scenario: get_display_message returns the freshest unshown message
- **WHEN** `get_display_message()` is called and the most recent non-suppressed message has an id different from `self._last_shown_message_id`
- **THEN** the method returns that message's `body` as a string
- **AND** updates `self._last_shown_message_id` to that message's id
- **AND** does not perform a random sample

#### Scenario: get_display_message samples uniformly from the recent-N when no fresh message
- **WHEN** `get_display_message()` is called and the most recent non-suppressed message has an id equal to `self._last_shown_message_id` (i.e. the head was already shown)
- **THEN** the method picks one entry uniformly at random from the most recent `recent_count` non-suppressed messages
- **AND** returns the picked entry's `body` as a string
- **AND** updates `self._last_shown_message_id` to the picked entry's id

#### Scenario: get_display_message returns None on an empty buffer
- **WHEN** `get_display_message()` is called and the manager's most recent `recent_count` non-suppressed messages list is empty
- **THEN** the method returns `None`

#### Scenario: get_display_message respects recent_count
- **WHEN** `EffectsSettings.recent_count` is 3 and the manager has 10 non-suppressed messages
- **THEN** `get_display_message()` reads at most the 3 most recent entries from the manager
- **AND** samples from those 3 entries (not from the full 10)

#### Scenario: tick pulls at most every 250 ms
- **WHEN** `tick()` is called repeatedly in a tight loop (e.g. every 16 ms)
- **THEN** `get_display_message()` is invoked at most every 250 ms
- **AND** on non-pull ticks, the coordinator consumes the cached result from the previous pull

### Requirement: The push path is removed from EffectsCoordinator and the JS surface

The system SHALL NOT provide a `set_text(...)` method on `EffectsCoordinator`, SHALL NOT carry a `pending_text` attribute, and SHALL NOT accept a `startup_text` argument on `start()`. The system SHALL NOT expose `window.request_message` or `window.apply_config` from `heart-message-manager/preview_main.py`, and SHALL NOT define or call a `reRender` aggregator that pushes to the coordinator in `heart-message-manager/static/preview/preview.js`. The browser's `MessageManager` (already kept up-to-date by the existing envelope subscription) is the single source of truth; the coordinator pulls on tick.

#### Scenario: EffectsCoordinator has no set_text method
- **WHEN** a caller invokes `coordinator.set_text("hello")`
- **THEN** `AttributeError: 'EffectsCoordinator' object has no attribute 'set_text'` is raised

#### Scenario: EffectsCoordinator has no pending_text attribute
- **WHEN** a caller reads `coordinator.pending_text`
- **THEN** `AttributeError: 'EffectsCoordinator' object has no attribute 'pending_text'` is raised

#### Scenario: EffectsCoordinator.start takes no startup_text argument
- **WHEN** `coordinator.start("seed message")` is called
- **THEN** `TypeError: start() takes 1 positional argument but 2 were given` (or equivalent) is raised

#### Scenario: preview_main.py does not expose window.request_message
- **WHEN** the preview page's PyScript runtime finishes bootstrapping
- **THEN** `window.request_message` is `undefined`
- **AND** `window.apply_config` is `undefined`

#### Scenario: preview.js does not register reRender
- **WHEN** the preview page loads
- **THEN** `heart-message-manager/static/preview/preview.js` does not contain a `reRender` function definition
- **AND** does not call `window.App.registerOnChange(reRender)`
- **AND** does not call `window.request_message(...)` or `window.apply_config(...)` anywhere in the file

### Requirement: The on_change callback applies config changes to the coordinator and fans out to JS in a single handler

The system SHALL construct `MessageManager` with a single `on_change: Callable[[], None] | None` callback. The callback SHALL be the only Python-side fan-out point: it SHALL apply the new config to the coordinator by calling `coordinator.apply_settings(effect_settings, text_settings)` with the current values from the manager's config, and (in the browser) it SHALL additionally call `create_proxy(_on_change_js)()` to notify JS subscribers. The `MessageManager` itself SHALL NOT hold a reference to the coordinator; the callback is a closure constructed by the entrypoint (the Pi's `main.py` or the browser's `preview_main.py`) with the coordinator in scope. The `coordinator.apply_settings(effect_settings, text_settings)` method SHALL handle pacing-field updates, effects-rotation rebuild (when the effects list changes), and scroller text-settings updates (when color or speed change) — and SHALL be idempotent across message-only emits so the callback is cheap on every tick.

#### Scenario: on_change applies the new config to the coordinator on every emit
- **WHEN** `MessageManager._emit_change()` is invoked from either `_handle_message()` or `_handle_config()`
- **THEN** the `on_change` callback is called exactly once
- **AND** the callback calls `coordinator.apply_settings(manager.config.effect_settings, manager.config.text_settings)` to apply the current config to the coordinator

#### Scenario: apply_settings updates pacing fields
- **WHEN** `coordinator.apply_settings(effect_settings, text_settings)` is called
- **THEN** `coordinator.fade_seconds`, `coordinator.hold_seconds`, `coordinator.intro_seconds`, `coordinator.idle_seconds`, and `coordinator.recent_count` reflect the values from `effect_settings`

#### Scenario: apply_settings rebuilds effects when the rotation changes
- **WHEN** `coordinator.apply_settings(effect_settings, text_settings)` is called and `effect_settings.effects` differs from the prior rotation
- **THEN** `coordinator.effects` is reassigned to a fresh `build_effects(effect_settings, display=coordinator.display)` result
- **AND** `coordinator.idx` is reset to `-1`

#### Scenario: apply_settings updates the scroller when text settings change
- **WHEN** `coordinator.apply_settings(effect_settings, text_settings)` is called and `text_settings.color` or `text_settings.speed` differs from the prior values
- **THEN** `coordinator.scroller.set_color(text_settings.color)` is called
- **AND** `coordinator.scroller.set_speed(text_settings.speed)` is called

#### Scenario: apply_settings is idempotent on message-only emits
- **WHEN** `coordinator.apply_settings(effect_settings, text_settings)` is called with values that match the prior state
- **THEN** the function returns without rebuilding the effects rotation (the effects hash matches)
- **AND** the function returns without calling `scroller.set_color` or `scroller.set_speed` (the text-settings hash matches)
- **AND** pacing-field writes still occur but write the same values that were already in effect

#### Scenario: on_change additionally fans out to JS in the browser
- **WHEN** `MessageManager._emit_change()` is invoked in the browser runtime
- **THEN** the `on_change` callback additionally calls `create_proxy(_on_change_js)()` to notify JS subscribers
- **AND** JS subscribers receive the change notification via the existing `App.registerOnChange` surface

#### Scenario: MessageManager has no coordinator= parameter
- **WHEN** a caller constructs `MessageManager(...)` and inspects its signature
- **THEN** the constructor does not accept a `coordinator` argument
- **AND** `MessageManager(coordinator=coord)` raises `TypeError: __init__() got an unexpected keyword argument 'coordinator'`

#### Scenario: heart-matrix-controller/main.py no longer wraps dispatch
- **WHEN** `heart-matrix-controller/main.py` is read
- **THEN** the file does not define `_on_config_update`
- **AND** the file does not define `_dispatch_with_config`
- **AND** the file does not assign `_message_mgr.dispatch = _dispatch_with_config` (the manager's own `dispatch` is the unpatched method)
- **AND** the file does not call `coordinator.start(_startup_text)` (the coordinator's first pull produces the most recent message in the manager's buffer)
- **AND** the `_on_change` function defined in main.py is a closure over the coordinator that calls `coord.apply_settings(manager.config.effect_settings, manager.config.text_settings)`

### Requirement: pytest tests/ stays at 304 pass + 1 skip

The system SHALL keep `pytest tests/` at 304 passing tests and 1 skipped test after this change is applied, with the same skip as the current baseline (the skip is unrelated to this change).

#### Scenario: Full test suite passes
- **WHEN** `PYTHONPATH=. pytest tests/ -v` is run from the repo root
- **THEN** 304 tests pass and 1 test is skipped
- **AND** the skipped test is the same one that is skipped on `main` before this change is applied

#### Scenario: New tests for enrichment, get_display_message, the removed push path, the on_change callback, and the apply_settings extension are present
- **WHEN** the test suite is run
- **THEN** `tests/lib_shared/message_manager_enrichment_test.py` exists with at least 4 scenarios (new-message enriches only new entry, config change re-enriches all, get_messages does not run filter or formatter, get_messages does not allocate MessageView on hot read)
- **AND** `tests/lib_shared/effects_coordinator_get_display_message_test.py` exists with at least 6 scenarios (fresh-message priority, recent-N sampling, returns None on empty buffer, respects recent_count, tick pulls at most every 250 ms, required message_manager raises TypeError)
- **AND** `tests/lib_shared/effects_coordinator_no_push_path_test.py` exists with at least 6 scenarios (set_text raises AttributeError, pending_text raises AttributeError, start with startup_text raises TypeError, preview_main.py does not expose window.request_message or window.apply_config, preview.js has no reRender / registerOnChange(reRender) / request_message / apply_config references, heart-matrix-controller/main.py does not define _on_config_update or _dispatch_with_config)
- **AND** `tests/lib_shared/effects_coordinator_apply_settings_test.py` exists with at least 5 scenarios (apply_settings updates pacing fields, apply_settings rebuilds effects when rotation changes, apply_settings updates scroller when text settings change, apply_settings is idempotent on message-only emits, apply_settings is callable from the on_change closure in main.py / preview_main.py)
- **AND** `tests/lib_shared/message_manager_on_change_test.py` exists with at least 3 scenarios (on_change applies the new config to the coordinator on every emit, on_change additionally fans out to JS in the browser runtime, MessageManager has no coordinator= parameter)
