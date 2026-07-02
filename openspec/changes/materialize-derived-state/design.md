## Context

**Where the bug lives.** `lib_shared/messages.py::InMemoryMessages.get_messages()` calls `self._enrich_messages(entries)` on every read, which runs the filter regex (`_apply_filter` + `_matches`) and the timezone formatter (`_format_display_time` via `zoneinfo.ZoneInfo`) over every entry in the ring buffer. Inputs to the enrichment (`messages` + `filters` + `timezone`) only change on events: a new message arriving, a config envelope changing the rules or timezone. The read cost therefore grows with buffer size × filter count × caller frequency, while the answer only changes on events.

**Where the dropped feature lives.** `lib_shared/effects_coordinator.py::EffectsCoordinator` was the consumer that picked a message to display — fresh-message priority, then uniform random pick from the most recent N during idle. PR #41's "universal `on_change`" refactor replaced the coordinator's pull with a `pending_text` slot that callers (e.g. the JS `reRender` aggregator in `static/preview/preview.js`) push into. The `recent_count` config field was retained on the dataclass for compat, but no consumer reads it; the JS side never re-pushes during idle, so the sign shows only background between real SMS arrivals.

**Where the dead push path lives.** PR #41's refactor introduced a "JS pushes" model: `static/preview/preview.js::reRender` calls `window.request_message(body)` and `window.apply_config(cfg)`, which `preview_main.py` wires to `coordinator.set_text(body)` and `coordinator.apply_settings(cfg)` respectively. The browser's `MessageManager` is *already* kept up-to-date by the same envelope subscription that the Pi uses (per the comment in `static/preview/preview.js:11`). The push path duplicates the manager's state into the coordinator on every change — work the manager has already done, work the coordinator can just read. With the manager as the single source of truth, the push path is dead.

**Stakeholders.** The Pi device (`heart-matrix-controller/main.py`) is the primary user-visible consumer; the browser preview (`heart-message-manager/preview_main.py` + `static/preview/preview.js`) is the secondary consumer that runs the same `EffectsCoordinator` and `MessageManager` classes (gated by the `is_browser` flag). The Flask server (`heart-message-manager/main.py`) is unaffected — it uses `FilteredMessages` subclasses directly and only reads `get_messages()` for the admin UI and live ring buffer.

**Constraints.**
- The Pi and browser share the same `lib_shared/` code; no fork.
- The `MessageView` public shape must not break — admin UI and the WS bridge iterate over `get_messages()` results and read `message` / `suppressed` / `rules` / `sender_name` / `display_time`.
- The manager must remain the single source of truth on both runtimes. The coordinator pulls; nothing pushes into the coordinator.
- `pytest tests/` must stay at 304 pass + 1 skip (the existing skip is unrelated).

## Goals / Non-Goals

**Goals:**
- `_enrich_messages()` runs only on the event that changes the inputs (a new message, a config change), not on every `get_messages()` call.
- `EffectsCoordinator` has a single `get_display_message()` method that encapsulates the selection algorithm (fresh-message priority, then uniform recent-N sampling).
- The coordinator's pull is throttled to ~4 Hz so per-tick reads stay trivial.
- The push path is removed entirely: no `set_text`, no `pending_text`, no `startup_text`, no `window.request_message`, no `window.apply_config`, no `reRender` aggregator.
- Config updates flow through `MessageManager._handle_config()`, which forwards the embedded `EffectsSettings` to the coordinator — JS no longer drives the rebind.
- `recent_count` becomes load-bearing again on both the Pi and the browser preview.

**Non-Goals:**
- Changing the `MessageView` dataclass shape (no field additions or removals; only the *when* of when fields are filled in changes).
- Pydantic validation on the wire (separate change, #43).
- Replacing MQTT-over-WebSocket with a different transport (the existing transport works; this change is about how the coordinator consumes the state, not how the state arrives).
- Adding new patterns or pattern-level changes.
- Changing the seed-fetch path in `app_main.py` (the manager's initial REST fetch still populates the buffer; the coordinator's first pull reads from the seeded buffer).
- Any change to `heart-message-manager/main.py` (Flask server) or to the SQLite/S3 storage layer.

## Decisions

### D1. Pre-compute enrichment on event, store on `MessageView`, read is a property

**Choice.** `_enrich_messages` becomes a precompute step, called from `InMemoryMessages.add()` (per-entry, on add) and from a new `InMemoryMessages.re_enrich_all()` method called by `MessageManager._handle_config()` (full, on config change). `InMemoryMessages.get_messages()` becomes a thin read: returns the already-enriched list, no per-call filter or formatter work. The four derived fields (`suppressed`, `rules`, `sender_name`, `display_time`) live as pre-populated attributes on the `MessageView` instances stored in the deque.

**Why over the alternative (compute on read, cache key on inputs):**
- The "cache key on inputs" alternative (hash the buffer snapshot + filters + tz, memoize the result) is more code, hides a real invariant behind a hash, and is invalidated the moment anything in the buffer mutates anyway. Pre-computing on event makes the invariant explicit: *the view is enriched as of the last event that affected it*.
- `MessageView` is already a per-entry object; carrying per-entry derived state is a natural fit. The deque already stores `MessageView` instances — we're just deciding when their derived fields get filled in.
- A future per-frame consumer (animation that wants `sender_name` for the current text) reads `entry.sender_name` and that's it. The hot read path is one attribute access.

**Alternative considered: lazy enrichment via `__getattr__` on `MessageView`.** Rejected: it doesn't fix the filter regex work (every read still does the filter pass), it spreads enrichment cost across reads in a way that's harder to reason about, and the `_matches` regex is the most expensive piece, not the attribute lookup.

### D2. `message_manager` is a required constructor argument; the push path is gone

**Choice.** `EffectsCoordinator.__init__` takes `message_manager: MessageManager` as a required keyword argument with no default. There is no `None` fallback, no `set_text(...)` method, no `pending_text` slot, no `start(startup_text)` push surface. The coordinator pulls on tick; that's the only path.

**Why required, not optional:**
- Optional preserves the vestigial push path and forces every consumer (tests, the Pi entrypoint, the browser shim) to think about which mode they're in. The push path is dead; the code should not leave a door open to it.
- Tests that want to exercise the coordinator in isolation can pass a stub `MessageManager` (the existing `InMemoryMessages` is trivially stubbable — its constructor only needs a `SignConfig`).
- The Pi's `main.py` and the browser's `preview_main.py` already construct a `MessageManager`; the only change is "pass it to the coordinator."

**Alternative considered: `message_manager` optional, with a `None` fallback to the existing push path.** Rejected: the push path is dead weight, and optionality forces tests and consumers to think about a contract that has no production caller. Removing it cleanly is the right call.

**Alternative considered: `set_text` and `startup_text` retained as no-ops with a deprecation warning.** Rejected: no deprecation horizon is meaningful when nothing is deployed; the cleanest contract is "the method does not exist."

### D3. ~4 Hz throttle on the coordinator's pull, not per-frame

**Choice.** The coordinator's tick path calls `get_display_message()` only when `now - self._last_message_pull >= 0.25` (4 Hz). Otherwise the tick path consumes the cached `self._last_display_message` from the previous pull.

**Why 4 Hz:**
- The sign displays human-visible text; 4 Hz refresh is 4× faster than any human can perceive text change. 1 Hz is too slow (a fresh SMS might wait up to a second to appear). 30+ FPS is the cost we're avoiding.
- The 4 Hz pull is bounded: 4 × `get_messages` per second. The thin read is O(buffer size) for the sort, with no filter or formatter work. At 100 messages × 4 Hz = 400 sort iterations/sec — trivial.

**Why not a smarter trigger (e.g. on `MessageManager._emit_change`):** the manager already drives `_emit_change` for config envelopes and fresh-message events. Hooking the throttle into the change event conflates the throttling policy (how often to re-read) with the event surface (what changed). The simpler design is: pull on a fixed cadence, store the result, tick consumes the store. The change event remains the trigger for *what's valid* (cache invalidation), not *when to read*.

**Alternative considered: pull on every `tick` (no throttle).** Rejected: that's the regression we're fixing — 30 FPS × 100 messages × filter regex is the wasted work.

**Alternative considered: pull only on `MessageManager._emit_change`.** Rejected: idle rotation needs to *continue* rotating even with no events; a tick-driven throttle is the right shape for that. Plus, decoupling the pull cadence from the event surface lets us tune either independently.

### D4. Remove the JS push surface entirely; the manager is the only state owner

**Choice.** `heart-message-manager/preview_main.py` no longer exposes `window.request_message` or `window.apply_config`. The `reRender` function in `heart-message-manager/static/preview/preview.js` is removed; the `App.registerOnChange(reRender)` registration is removed. JS needs nothing related to display state — the manager's `on_change` listener (registered in `app_main.py`) is the only event hook the preview page needs, and even that is for non-display concerns (e.g. the admin UI's live ring buffer).

**Why remove `reRender` and not just stop calling it:** the function's body is the push path. Keeping the function around with an empty body is dead code; removing the file's content is the cleanest landing.

**Why remove `App.registerOnChange(reRender)`:**
- The push path was the only consumer of the change event for the display layer. With the pull path, the coordinator doesn't need a JS push on every change.
- The `on_change` event surface remains (other listeners — admin UI, live ring buffer — can still subscribe), but the preview page no longer participates.

**Alternative considered: keep `reRender` as a no-op stub for back-compat with any future JS that might call it.** Rejected: nothing is deployed, no JS calls it, and a stub invites someone to wire it up again. Clean removal.

### D5. `recent_count` becomes load-bearing; default 5 unchanged

**Choice.** The pre-existing `recent_count: int = 5` on `EffectsSettings` is now consumed: `get_display_message()` reads the most recent `recent_count` non-suppressed messages, and on idle (no fresh-message hit) picks uniformly at random from that list. The default is unchanged so existing configs work without modification.

**Why 5:** the historic default; the issue explicitly says it was retained for compat and starts "meaning something again." No reason to retune the default in the same change.

### D6. The single `on_change` callback handles both Python-side state updates and JS fan-out

**Choice.** `MessageManager` already exposes an `on_change: Callable[[], None] | None` callback, invoked from `_emit_change()` on both `_handle_message()` and `_handle_config()`. Today the callback is constructed (in the browser path) as a `create_proxy(_on_change_js)` shim that fans out to JS subscribers, and in the Pi's main.py it's a no-op. This change uses the same callback for both jobs: (1) apply the new config to the coordinator via `coordinator.apply_settings(...)` — pacing, effects rebuild, scroller text settings — and (2) fan out to JS subscribers via the existing `create_proxy(_on_change_js)` mechanism. The `MessageManager` itself stays unaware of the coordinator; the callback is a closure constructed by the entrypoint (`heart-matrix-controller/main.py` on the Pi, `heart-message-manager/preview_main.py` in the browser) with the coordinator in scope.

The `on_change` callback shape on the Pi and in the browser:

```python
# Pi (heart-matrix-controller/main.py)
def _on_change():
    coord.apply_settings(manager.config.effects_settings, manager.config.text_settings)
manager = MessageManager(on_change=_on_change, ...)
_mqtt_client = PahoMqttClient(dispatch_callback=_message_mgr.dispatch, ...)

# Browser (heart-message-manager/preview_main.py)
def _on_change():
    coord.apply_settings(manager.config.effects_settings, manager.config.text_settings)
    create_proxy(_on_change_js)()  # fan out to JS subscribers
manager = MessageManager(on_change=_on_change, ...)
```

**`coordinator.apply_settings` becomes the single config-application method on the coordinator.** It currently updates only the pacing fields (`fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`). This change extends it to also handle the heavier work that `main.py::_on_config_update` does today:
- **Effects rebuild** — when `effects_settings.effects` (the declared rotation) changes, call `build_effects(effects_settings, display=self.display)`, assign the result to `self.effects`, and reset `self.idx = -1` so the next fade picks the head of the new list. Guarded by a hash of the effects list so message-only emits don't rebuild on every tick.
- **Scroller text settings** — when `text_settings.color` or `text_settings.speed` changes, call `self.scroller.set_color(...)` and `self.scroller.set_speed(...)`. Guarded by a hash of the relevant fields.

The function is idempotent across message-only emits: same values are written, no observable change. The guards ensure the heavier work (effects rebuild, scroller mutation) only runs on actual config changes.

**This change also cleans up the dispatch-wrapping pattern that exists in `heart-matrix-controller/main.py` today.** The current main.py has:
- `_on_change()` — a no-op (this is what the current spec is implicitly relying on)
- `_on_config_update(cfg_dict)` — a separate function that does the actual config-application work (effects rebuild, scroller text settings, `coordinator.apply_settings`)
- `_dispatch_with_config(raw)` — a monkey-patch of `_message_mgr.dispatch` that intercepts config envelopes to call `_on_config_update` in addition to the normal `dispatch`

After this change:
- `_on_change()` becomes the closure above (no longer a no-op)
- `_on_config_update()` is deleted
- `_dispatch_with_config()` is deleted; `_message_mgr.dispatch = _dispatch_with_config` is reverted
- The MQTT client's `dispatch_callback=_message_mgr.dispatch` works as-is (the manager's own `dispatch` handles both message and config envelopes)
- The `coordinator.start(_startup_text)` call (which depends on the push path that's being removed) is also dropped — the coordinator's first pull produces the most recent message in the manager's buffer

The cleanup is consistent with the on_change pattern: one callback, one method, no monkey-patching.

**Why the single callback, not a `coordinator=` reference on the manager:**
- The existing `on_change` mechanism is the documented fan-out point for "something in `MessageManager` changed." Both the coordinator (Python-side consumer) and JS subscribers (browser-side consumers) are *consumers* of that change — they sit on the same side of the abstraction. A single callback that updates both is simpler to read than two coupled handlers.
- Adding `coordinator=coord` to `MessageManager.__init__` couples the manager to a specific consumer type. The manager should not know what a coordinator is; it should know that *something* wants to be notified when its state changes.
- The closure pattern lets the entrypoint keep the coordinator local — no need to plumb a coordinator reference through `MessageManager`'s type signature, no need to add a new "Python-side consumer" parameter alongside the existing JS-side `on_change`. Future Python-side consumers (e.g. a config-change logger) just add a line to the closure.
- The `on_change` callback is called on every emit (message *and* config). For a message-only change, the coordinator's pacing is unchanged, the effects list is unchanged, and the scroller text settings are unchanged — `apply_settings` is effectively a no-op write of the same values (or skipped entirely if the relevant fields are guarded). Cheap and correct. We don't need a separate config-only emit path.
- The cleanup also makes the existing main.py pattern obsolete: the dispatch wrapping was the workaround for "the manager doesn't expose a config hook." With the on_change callback as the fan-out, no wrapping is needed.

**Alternative considered: the coordinator subscribes to `MessageManager.on_change` and calls `apply_settings` itself.** Rejected: the coordinator doesn't need to know about `MessageManager`'s emit surface — it just needs the right state. The entrypoint-constructed closure is the cleanest place for the wiring, because the entrypoint is the only place that has both the coordinator and the manager in scope.

**Alternative considered: keep the dispatch wrapping, just rename `_on_config_update` to call `apply_settings` internally.** Rejected: the dispatch wrapping exists only because there's no clean fan-out point. Now there is one. Keeping the wrapping alongside the new pattern is two mechanisms for the same job, which is exactly the complexity we're trying to remove.

## Risks / Trade-offs

- **Stale enrichment between events.** After a new message arrives, the deque has the new entry pre-enriched (cheap, on add). A subsequent config change re-enriches all entries (filter rules or timezone might re-classify). But a *partial* config change that only touches some rules (e.g. user edits one filter in the admin UI) triggers a full re-enrich, which is O(buffer × filters). → Acceptable: full re-enrich is O(100) at typical buffer size, runs only on the config-envelope event, and the alternative (per-rule re-enrich) is more code for marginal benefit.
- **Memory: `MessageView` carries precomputed fields even when no consumer reads them.** → Acceptable: the four derived fields are small (a bool, a list of dicts, a name string, a formatted timestamp). At 100-message buffer, this is single-digit KB.
- **Throttle lag of up to 250 ms between a fresh SMS arriving and it appearing on the sign.** → Acceptable: humans don't perceive 250 ms latency for a sign display, and the alternative (per-frame pull) is the regression we're avoiding. If a user ever finds 250 ms too slow, lowering the throttle is a one-line change.
- **Required `message_manager` breaks any test that constructs the coordinator standalone.** → Mitigated: the new test file `tests/lib_shared/effects_coordinator_no_push_path_test.py` documents the contract (a test that constructs the coordinator without a manager fails with a clear `TypeError` from the missing positional argument), and the existing tests are updated in tasks 2.5 to pass a stub `MessageManager`.
- **Removing `set_text` and `startup_text` is a hard break for any in-flight branch that uses them.** → Mitigated: per the issue, the work in PR #41's `d5ff585` cache and `c769270` cleanup is independent and not coupled; nothing else in the repo calls these methods. (Verified by `grep -rn 'set_text\|startup_text' lib_shared/ heart-message-manager/ heart-matrix-controller/` — the only callers are `preview_main.py::request_message` and `preview_main.py::start(startup_text)`, both of which are removed in this change.)
- **No JS rebind path means the browser preview cannot drive a config change ad-hoc.** → Mitigated: the manager's envelope subscription is the production config path; the WS/MQTT broker pushes config envelopes to all subscribers. JS ad-hoc rebind is not a production use case.
- **No new tests for the removed `apply_config` JS path.** → Not applicable: the path is removed; tests for it would be tests for absent code. The new `tests/lib_shared/effects_coordinator_no_push_path_test.py` asserts the surface is gone.

## Migration Plan

This change is a pure refactor inside `lib_shared/` + the removal of one JS file's body. No data migration, no config migration, no deploy sequencing.

**Rollout:**
1. Land the change behind the existing test suite. The new tests (`tests/lib_shared/message_manager_enrichment_test.py`, `tests/lib_shared/effects_coordinator_get_display_message_test.py`, `tests/lib_shared/effects_coordinator_no_push_path_test.py`) are part of the change and gate it.
2. Deploy to the Pi (`git pull` on the device, restart `lindsay_50.service`). The Pi's `main.py` is updated to pass `message_manager` and to wire the manager's `coordinator` reference.
3. Reload the preview page in the browser; idle rotation resumes; the JS console is clean of `reRender apply_config` / `reRender request_message` warnings (those lines are gone).

**Rollback:**
- Revert the commit. No state to roll back. The only visible difference after revert is that idle-mode rotation stops working again, and the push-path code returns.

## Open Questions

_None at write time._ The change is bounded, the contract is clear, and the acceptance shape in the proposal maps directly to the four new test files plus the existing `tests/lib_shared/messages_test.py` / `tests/lib_shared/effects_coordinator_test.py` tests that should still pass after the push-path removal.
