# Tasks: materialize-derived-state

## 1. Materialize enrichment on event in `lib_shared/`

- [x] 1.1 In `lib_shared/messages.py`, make `InMemoryMessages.add()` precompute enrichment on the new `MessageView` it constructs — call `self._enrich_messages([view])` before appending to `self._msgs`. The four derived fields (`suppressed`, `rules`, `sender_name`, `display_time`) are populated at write-time, not read-time.
- [x] 1.2 In `lib_shared/messages.py`, change `InMemoryMessages.get_messages(limit, suppress)` to a thin read — return the already-enriched `MessageView` list, sort by `received_at` desc, slice to `limit`. Do not call `self._enrich_messages(entries)` in the read path. The `suppress=True` filter still excludes entries whose precomputed `suppressed` is True.
- [x] 1.3 In `lib_shared/messages.py`, add a `re_enrich_all()` method on `InMemoryMessages` that runs `self._enrich_messages(list(self._msgs))` against the current `self._config`. This is the event-time full re-enrich called by `MessageManager._handle_config()`.
- [x] 1.4 In `lib_shared/message_manager.py`, hook `_handle_message()` to ensure the new entry is enriched (if 1.1 doesn't do it inside `InMemoryMessages.add`, do it here — surface is the same).
- [x] 1.5 In `lib_shared/message_manager.py`, hook `_handle_config()` to call `self._messages.re_enrich_all()` after `self._config.update_from_dict(payload)` so that filter-rule or timezone changes reclassify all buffered messages.
- [x] 1.6 Add `tests/lib_shared/message_manager_enrichment_test.py` with the 4 scenarios from the spec:
  - New message arrival enriches only the new entry (existing entries' derived fields unchanged).
  - Config change re-enriches all entries (mutation of an existing `MessageView`'s `suppressed` after a filter rule is added).
  - `get_messages()` does not invoke `_apply_filter` or `_matches` (spy on them, assert no calls during read).
  - `get_messages()` does not invoke `_format_display_time` (spy, assert no calls during read).

## 2. Coordinator pulls from `MessageManager` via `get_display_message()` in `lib_shared/effects_coordinator.py`

- [x] 2.1 Make `message_manager: MessageManager` a required keyword argument on `EffectsCoordinator.__init__`. Drop the `None` default. Store it as `self.message_manager`. The constructor raises `TypeError` if a caller omits it (this is automatic once the default is removed).
- [x] 2.2 Add `get_display_message() -> str | None` to `EffectsCoordinator`:
  - Read `self.message_manager.messages.get_messages(limit=self.recent_count, suppress=True)`.
  - If the list is empty, return `None`.
  - If the head entry's `id` differs from `self._last_shown_message_id`, return its `body` and update `self._last_shown_message_id`.
  - Otherwise, pick uniformly at random from the list (use `random.choice`) and return that entry's `body`, updating `self._last_shown_message_id` to the picked id.
  - Initialize `self._last_shown_message_id = None` in `__init__`.
- [x] 2.3 Add a `_PULL_INTERVAL = 0.25` class-level constant and a `_last_message_pull: float = 0.0` instance attribute. In `tick()`, when `now - self._last_message_pull >= self._PULL_INTERVAL`, call `self.get_display_message()` and store the result on `self._last_display_message`; otherwise consume the cached value. Update `self._last_message_pull = now` after each pull.
- [x] 2.4 Wire the pull result into the existing fade-out / swap / fade-in state machine in `tick()`. The result replaces the source of the "next text" — wherever the existing code reads `self.pending_text` and queues it for the scroller, read `self._last_display_message` instead. The state machine itself does not change shape; only the input source.
- [x] 2.5 Extend `apply_settings(self, effects_settings, text_settings=None)` on `EffectsCoordinator` to handle the full config-application work that `main.py::_on_config_update` does today:
  - Pacing fields (already in scope): `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count` — write them every call (idempotent).
  - Effects rebuild: compute a hash of the declared rotation (`effects_settings.effects`); if it differs from `self._last_effects_hash`, call `build_effects(effects_settings, display=self.display)`, assign to `self.effects`, reset `self.idx = -1`, and update `self._last_effects_hash`.
  - Scroller text settings: if `text_settings is not None`, compute a hash of `(text_settings.color, text_settings.speed)`; if it differs from `self._last_text_settings_hash`, call `self.scroller.set_color(text_settings.color)` and `self.scroller.set_speed(text_settings.speed)`, and update `self._last_text_settings_hash`.
  - Initialize `self._last_effects_hash` and `self._last_text_settings_hash` to `None` in `__init__`.
  - `apply_settings` is called from the `MessageManager`'s `on_change` callback (see 4.1) — it is no longer called by JS.
- [x] 2.6 Add `tests/lib_shared/effects_coordinator_get_display_message_test.py` with the 6 scenarios from the spec:
  - Required `message_manager` raises `TypeError` when omitted.
  - `get_display_message()` returns the head entry's body and updates `_last_shown_message_id` when the head is fresh.
  - `get_display_message()` samples uniformly from the most recent `recent_count` entries when the head has already been shown (seed `random` and assert the pick).
  - `get_display_message()` returns `None` on an empty buffer.
  - `get_display_message()` respects `recent_count` (a 3-message buffer with `recent_count=3` is read fully; with `recent_count=2` only the head 2 are read).
  - `tick()` calls `get_display_message()` at most every 250 ms even when called in a tight loop (spy on the method, count invocations across 1 second of ticks).

## 3. Remove the push path from `EffectsCoordinator` and the JS surface

- [x] 3.1 In `lib_shared/effects_coordinator.py`, delete the `set_text(self, text)` method. Remove the `self.pending_text = None` line from `__init__`. Remove any read of `self.pending_text` in `tick()` (replaced by 2.4).
- [x] 3.2 In `lib_shared/effects_coordinator.py`, change `start(self, startup_text)` to `start(self)`. Remove the `if startup_text: self.pending_text = startup_text` block. The coordinator's first pull produces the most recent message in the manager's buffer; no separate "show this body after the heart" hook is needed.
- [x] 3.3 In `heart-message-manager/preview_main.py`, delete the `window.request_message` and `window.apply_config` exports (and their backing Python functions `request_message(...)` and `apply_config(...)`). Update the module docstring to drop the lines that describe them.
- [x] 3.4 In `heart-message-manager/static/preview/preview.js`, delete the `reRender` function. Delete the `if (window.App && typeof window.App.registerOnChange === "function")` registration block. Delete the explicit `reRender()` call on init (the coordinator drives the first frame via its pull). Keep the file's canvas-construction logic.
- [x] 3.5 Update the comment block at the top of `static/preview/preview.js` to reflect the new contract: the coordinator pulls from the in-browser `MessageManager`; the preview page no longer pushes via `request_message` or `apply_config`.
- [x] 3.6 Add `tests/lib_shared/effects_coordinator_no_push_path_test.py` with the 5 scenarios from the spec:
  - `coordinator.set_text("hello")` raises `AttributeError`.
  - Reading `coordinator.pending_text` raises `AttributeError`.
  - `coordinator.start("seed")` raises `TypeError` (or equivalent) for the extra positional argument.
  - A test that loads `preview_main.py` and asserts `window.request_message` and `window.apply_config` are `undefined` (use a stub `js.window` and import the module under a fake `js` namespace, or rely on a static check of the source file).
  - A static check of `heart-message-manager/static/preview/preview.js` that asserts the file does not contain `reRender`, `registerOnChange`, `request_message`, or `apply_config` as a function or call site.

## 4. Wire-up: `on_change` callback applies config to the coordinator and fans out to JS

- [x] 4.1 In `heart-matrix-controller/main.py`, replace the existing `_on_change()` no-op with a closure over the coordinator:
  ```python
  def _on_change():
      coord.apply_settings(manager.config.effects_settings, manager.config.text_settings)
  manager = MessageManager(on_change=_on_change, ...)
  ```
  Then pass `message_manager=manager` to the `EffectsCoordinator` constructor. The manager does NOT need a `coordinator=` reference; the closure captures it.
- [x] 4.2 In `heart-message-controller/main.py`, also clean up the existing dispatch-wrapping pattern: delete `_on_config_update(cfg_dict)`, delete `_dispatch_with_config(raw)`, and revert `_message_mgr.dispatch = _dispatch_with_config` (the manager's own `dispatch` is the unpatched method, and the MQTT client's `dispatch_callback=_message_mgr.dispatch` works as-is). Delete the `_recent = _message_mgr.get_messages(limit=1)` / `coordinator.start(_startup_text)` block — the coordinator's first pull produces the most recent message in the manager's buffer.
- [x] 4.3 In `heart-message-manager/preview_main.py`, construct the per-page `MessageManager` (or reuse the app-scoped one) with the same closure pattern. The browser callback additionally calls `create_proxy(_on_change_js)()` to fan out to JS subscribers:
  ```python
  def _on_change():
      coord.apply_settings(manager.config.effects_settings, manager.config.text_settings)
      create_proxy(_on_change_js)()
  manager = MessageManager(on_change=_on_change, ...)
  ```
  Then pass `message_manager=manager` to the per-page `EffectsCoordinator`.
- [x] 4.4 Add `tests/lib_shared/effects_coordinator_apply_settings_test.py` with the 5 scenarios from the spec:
  - `apply_settings(effects_settings, text_settings)` writes the pacing fields (`fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`).
  - `apply_settings` rebuilds `coordinator.effects` and resets `coordinator.idx` when the declared rotation changes (compare `effects_settings.effects` before and after; assert new `coordinator.effects` list).
  - `apply_settings` calls `coordinator.scroller.set_color(...)` and `coordinator.scroller.set_speed(...)` when `text_settings.color` or `text_settings.speed` changes (spy on the scroller).
  - `apply_settings` is idempotent on message-only emits: no effects rebuild, no scroller mutation (spy on `build_effects` and `scroller.set_color`/`set_speed`, assert they are NOT called when the relevant fields match the prior state).
  - The on_change closure in `heart-matrix-controller/main.py` and `heart-message-manager/preview_main.py` is a single function that calls `coord.apply_settings(manager.config.effects_settings, manager.config.text_settings)` (static check of the source files).
- [x] 4.5 Add `tests/lib_shared/message_manager_on_change_test.py` with the 3 scenarios from the spec:
  - `on_change` callback is invoked exactly once per `MessageManager._emit_change()` call from either `_handle_message()` or `_handle_config()`.
  - In the browser runtime, `on_change` additionally calls `create_proxy(_on_change_js)()` to fan out to JS subscribers (spy on the proxy).
  - `MessageManager(coordinator=coord)` raises `TypeError: __init__() got an unexpected keyword argument 'coordinator'` (the manager does not accept a coordinator reference).
- [x] 4.6 Extend `tests/lib_shared/effects_coordinator_no_push_path_test.py` with one additional scenario: a static check of `heart-matrix-controller/main.py` that asserts the file does not define `_on_config_update` or `_dispatch_with_config`, does not assign `_message_mgr.dispatch = _dispatch_with_config`, and does not call `coordinator.start(_startup_text)`.
- [x] 4.7 Run `PYTHONPATH=. pytest tests/ -v` from the repo root. Assert 304 pass + 1 skip (the same skip as the pre-change baseline). All 24 new test scenarios (4 enrichment + 6 get_display_message + 6 no_push_path + 5 apply_settings + 3 on_change) pass.
- [ ] 4.8 Manual sign test: with the Pi running and a few seeded messages in the buffer, observe the sign for 60 seconds with no fresh SMS. Verify that the display rotates through the recent messages (idle-rotation working). Open the browser preview; verify the JS console is clean of `reRender apply_config` / `reRender request_message` warnings and that the preview rotates through the same recent messages.
