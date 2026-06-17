# Tasks: materialize-derived-state

## 1. Materialize enrichment on event in `lib_shared/`

- [ ] 1.1 In `lib_shared/messages.py`, make `InMemoryMessages.add()` precompute enrichment on the new `MessageView` it constructs — call `self._enrich_messages([view])` before appending to `self._msgs`. The four derived fields (`suppressed`, `rules`, `sender_name`, `display_time`) are populated at write-time, not read-time.
- [ ] 1.2 In `lib_shared/messages.py`, change `InMemoryMessages.get_messages(limit, suppress)` to a thin read — return the already-enriched `MessageView` list, sort by `received_at` desc, slice to `limit`. Do not call `self._enrich_messages(entries)` in the read path. The `suppress=True` filter still excludes entries whose precomputed `suppressed` is True.
- [ ] 1.3 In `lib_shared/messages.py`, add a `re_enrich_all()` method on `InMemoryMessages` that runs `self._enrich_messages(list(self._msgs))` against the current `self._config`. This is the event-time full re-enrich called by `MessageManager._handle_config()`.
- [ ] 1.4 In `lib_shared/message_manager.py`, hook `_handle_message()` to call `self._messages._enrich_messages([new_view])` after `self._messages.add(...)` (or refactor `InMemoryMessages.add` to do this internally — see 1.1; the surface is the same).
- [ ] 1.5 In `lib_shared/message_manager.py`, hook `_handle_config()` to call `self._messages.re_enrich_all()` after `self._config.update_from_dict(payload)` so that filter-rule or timezone changes reclassify all buffered messages.
- [ ] 1.6 Add `tests/lib_shared/message_manager_enrichment_test.py` with the 4 scenarios from the spec:
  - New message arrival enriches only the new entry (existing entries' derived fields unchanged).
  - Config change re-enriches all entries (mutation of an existing `MessageView`'s `suppressed` after a filter rule is added).
  - `get_messages()` does not invoke `_apply_filter` or `_matches` (spy on them, assert no calls during read).
  - `get_messages()` does not invoke `_format_display_time` (spy, assert no calls during read).

## 2. Coordinator pulls from `MessageManager` during idle in `lib_shared/effects_coordinator.py`

- [ ] 2.1 Add an optional `message_manager: MessageManager | None = None` keyword argument to `EffectsCoordinator.__init__`. Store it as `self.message_manager`. When `None`, the coordinator's tick path is unchanged from today (consumes `self.pending_text`).
- [ ] 2.2 Add a `_last_message_pull: float = 0.0` attribute and a `_PULL_INTERVAL = 0.25` class-level constant (4 Hz). On each tick, when `self.message_manager is not None`, call `self._get_display_text()` only when `now - self._last_message_pull >= self._PULL_INTERVAL`; otherwise consume the cached value.
- [ ] 2.3 Implement `_get_display_text()`:
  - Read `self.message_manager.messages.get_messages(limit=self.recent_count, suppress=True)`.
  - If the list is empty, return `None`.
  - Track `self._last_shown_message_id`; if a fresh message is in the list whose `id` differs from `self._last_shown_message_id`, return its `body` and update `_last_shown_message_id`.
  - Otherwise, pick uniformly at random from the list (use `random.choice`) and return that message's `body`.
- [ ] 2.4 In `tick()`, when `self.message_manager is not None`, the `pending_text` push path is replaced by the throttled pull: if `_get_display_text()` returns a value, set `self.pending_text = value` (the existing tick logic that consumes `pending_text` continues to work as-is).
- [ ] 2.5 In `apply_settings(...)`, the existing line `self.recent_count = effect_settings.recent_count` is retained — `recent_count` is now load-bearing, the same line is consumed by the new pull path.
- [ ] 2.6 Add `tests/lib_shared/effects_coordinator_idle_rotation_test.py` with the 5 scenarios from the spec:
  - Coordinator with `message_manager` set reads from the manager on tick.
  - The pull runs at most every 250 ms even when tick is called every 16 ms (spy on `get_messages`).
  - Fresh message (id not yet shown) takes priority over random recent-N.
  - Idle-rotation (no fresh message) picks uniformly from the most recent `recent_count` non-suppressed messages (seeded random, assert the pick).
  - Coordinator with `message_manager=None` continues to consume `pending_text` (existing test path remains green).

## 3. Drop `reRender` registration in `heart-message-manager/static/preview/preview.js`

- [ ] 3.1 In the init block of `heart-message-manager/static/preview/preview.js`, remove the `window.App.registerOnChange(reRender)` call and the surrounding `if (window.App && typeof window.App.registerOnChange === "function")` guard. The `reRender` function definition stays, and the explicit `reRender()` call on init stays.
- [ ] 3.2 Inside `reRender`, the `apply_config` rebind call (via `pyConfig.apply_config` or whatever the existing call shape is — preserve verbatim) is retained. Only the `request_message` push path inside `reRender` is no longer the source of truth for the message-pick side; the coordinator pulls.
- [ ] 3.3 Verify the preview page still loads in the browser (Interceptor screenshot of `/preview`); no console errors from the missing registration. The 3 s polling cadence and the WS-on-config-envelope path are unchanged.

## 4. Wire-up and verification

- [ ] 4.1 In `heart-matrix-controller/main.py`, pass `message_manager=manager` to the `EffectsCoordinator` constructor (the Pi already constructs a `MessageManager`; the only change is the new keyword arg).
- [ ] 4.2 In `heart-message-manager/preview_main.py`, pass `message_manager=app_state["message_manager"]` to the per-page `EffectsCoordinator` (the browser preview already constructs a `MessageManager` for the seed-fetch path; the only change is the new keyword arg).
- [ ] 4.3 Run `PYTHONPATH=. pytest tests/ -v` from the repo root. Assert 304 pass + 1 skip (the same skip as the pre-change baseline). All 9 new test scenarios (4 enrichment + 5 coordinator) pass.
- [ ] 4.4 Manual sign test: with the Pi running and a few seeded messages in the buffer, observe the sign for 60 seconds with no fresh SMS. Verify that the display rotates through the recent messages (idle-rotation working) and that the test passes `pytest tests/lib_shared/effects_coordinator_idle_rotation_test.py` on the device (or in a CI-equivalent environment).
