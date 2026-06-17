## Context

**Where the bug lives.** `lib_shared/messages.py::InMemoryMessages.get_messages()` calls `self._enrich_messages(entries)` on every read, which runs the filter regex (`_apply_filter` + `_matches`) and the timezone formatter (`_format_display_time` via `zoneinfo.ZoneInfo`) over every entry in the ring buffer. Inputs to the enrichment (`messages` + `filters` + `timezone`) only change on events: a new message arriving, a config envelope changing the rules or timezone. The read cost therefore grows with buffer size × filter count × caller frequency, while the answer only changes on events.

**Where the dropped feature lives.** `lib_shared/effects_coordinator.py::EffectsCoordinator` was the consumer that picked a message to display — fresh-message priority, then uniform random pick from the most recent N during idle. PR #41's "universal `on_change`" refactor replaced the coordinator's pull with a `pending_text` slot that callers (e.g. the JS `reRender` aggregator in `static/preview/preview.js`) push into. The `recent_count` config field was retained on the dataclass for compat, but no consumer reads it; the JS side never re-pushes during idle, so the sign shows only background between real SMS arrivals.

**Stakeholders.** The Pi device (`heart-matrix-controller/main.py`) is the primary user-visible consumer; the browser preview (`heart-message-manager/preview_main.py` + `static/preview/preview.js`) is the secondary consumer that runs the same `EffectsCoordinator` and `MessageManager` classes (gated by the `is_browser` flag). The Flask server (`heart-message-manager/main.py`) is unaffected — it uses `FilteredMessages` subclasses directly and only reads `get_messages()` for the admin UI and live ring buffer.

**Constraints.**
- The Pi and browser share the same `lib_shared/` code; no fork.
- The `pending_text` push path must keep working — the JS shim still uses `apply_config` to push config envelopes and `request_message` for fresh-message push, and the coordinator must continue to honor pushes when `message_manager is None`.
- `MessageView`'s public shape must not break — admin UI and the WS bridge iterate over `get_messages()` results and read `message` / `suppressed` / `rules` / `sender_name` / `display_time`.
- `pytest tests/` must stay at 304 pass + 1 skip (the existing skip is unrelated).

## Goals / Non-Goals

**Goals:**
- `_enrich_messages()` runs only on the event that changes the inputs (a new message, a config change), not on every `get_messages()` call.
- `EffectsCoordinator` regains idle-mode message selection: fresh-message priority, then uniform recent-N sampling from `MessageManager` during idle.
- The coordinator's pull from `MessageManager` is throttled (~4 Hz) so the per-tick read stays trivial regardless of buffer size.
- `recent_count` becomes load-bearing again on both the Pi and the browser preview.
- The `pending_text` push path still works (for the JS-driven config-rebind and any caller that wants push semantics).

**Non-Goals:**
- Changing the `MessageView` dataclass shape (no field additions or removals; only the *when* of when fields are filled in changes).
- Pydantic validation on the wire (separate change, #43).
- Replacing `pending_text` push semantics — it's preserved as a no-op-when-`message_manager`-is-`None` fallback.
- Any change to `heart-message-manager/main.py` (Flask server) or to the SQLite/S3 storage layer.
- Replacing 3s polling with WebSocket (separate, also out of scope).
- Adding new patterns or pattern-level changes.

## Decisions

### D1. Pre-compute enrichment on event, store on `MessageView`, read is a property

**Choice.** `_enrich_messages` becomes a precompute step, called from `MessageManager._handle_message()` (per-entry, on add) and `MessageManager._handle_config()` (full, on config change). `InMemoryMessages.get_messages()` becomes a thin read: returns the already-enriched list, no per-call filter or formatter work. The four derived fields (`suppressed`, `rules`, `sender_name`, `display_time`) live as pre-populated attributes on the `MessageView` instances stored in the deque.

**Why over the alternative (compute on read, cache key on inputs):**
- The "cache key on inputs" alternative (hash the buffer snapshot + filters + tz, memoize the result) is more code, hides a real invariant behind a hash, and is invalidated the moment anything in the buffer mutates anyway. Pre-computing on event makes the invariant explicit: *the view is enriched as of the last event that affected it*.
- `MessageView` is already a per-entry object; carrying per-entry derived state is a natural fit. The deque already stores `MessageView` instances — we're just deciding when their derived fields get filled in.
- A future per-frame consumer (animation that wants `sender_name` for the current text) reads `entry.sender_name` and that's it. The hot read path is one attribute access.

**Alternative considered: lazy enrichment via `__getattr__` on `MessageView`.** Rejected: it doesn't fix the filter regex work (every read still does the filter pass), it spreads enrichment cost across reads in a way that's harder to reason about, and the `_matches` regex is the most expensive piece, not the attribute lookup.

### D2. Optional `message_manager` parameter on `EffectsCoordinator`; `None` keeps legacy push semantics

**Choice.** The constructor adds an optional `message_manager: MessageManager | None = None` keyword argument. When `None`, the coordinator's tick path is unchanged from today: it consumes `self.pending_text` (set by `set_text(...)` and `start(...)`) and shows whatever's queued. When set, the coordinator's tick path pulls from `MessageManager` instead, throttled to ~4 Hz.

**Why over the alternative (always require `message_manager`, drop the push path):**
- The JS shim still pushes `pending_text` for the boot-splash startup case and for the `apply_config` rebind path. Dropping the push path forces the JS shim to construct a `MessageManager` it doesn't otherwise need.
- The push path is a clean, well-understood contract. The pull path is a new contract. Optional `message_manager` lets us land the pull path without ripping out the push path — they coexist.
- Tests can pass a stub manager, and the existing tests that exercise `set_text(...)` / `pending_text` keep working without modification.

**Alternative considered: `message_manager` always required, replace `set_text` with a `MessageManager`-driven queue.** Rejected: bigger blast radius, larger diff, no test surface benefit.

### D3. ~4 Hz throttle on the coordinator's pull, not per-frame

**Choice.** The coordinator's tick path calls `_get_display_text()` only when `now - self._last_message_pull >= 0.25` (4 Hz). Otherwise the tick path consumes the cached `self.pending_text` from the previous pull.

**Why 4 Hz:**
- The sign displays human-visible text; 4 Hz refresh is 4× faster than any human can perceive text change. 1 Hz is too slow (a fresh SMS might wait up to a second to appear). 30+ FPS is the cost we're avoiding.
- The 4 Hz pull is bounded: 4 × `get_messages` per second. The thin read is O(buffer size) for the sort, with no filter or formatter work. At 100 messages × 4 Hz = 400 sort iterations/sec — trivial.

**Why not a smarter trigger (e.g. on `_emit_change`):** the JS shim already drives `_emit_change` for config envelopes and fresh-message push. Hooking the throttle into the change event is the alternative — but it conflates the throttling policy (how often to re-read) with the event surface (what changed). The simpler design is: pull on a fixed cadence, store the result, tick consumes the store. The change event remains the trigger for *what's valid* (cache invalidation), not *when to read*.

**Alternative considered: pull on every `tick` (no throttle).** Rejected: that's the regression we're fixing — 30 FPS × 100 messages × filter regex is the wasted work.

**Alternative considered: pull only on `_emit_change` from `MessageManager`.** Rejected: requires the coordinator to subscribe to the manager's change event, which couples the two classes for a policy that 4 Hz expresses more simply. Plus, idle rotation needs to *continue* rotating even with no events — a tick-driven throttle is the right shape for that.

### D4. Keep `apply_config` as a JS-callable; drop `reRender`'s `App.registerOnChange` registration

**Choice.** In `heart-message-manager/static/preview/preview.js`, the `reRender` function no longer subscribes to `window.App.registerOnChange`. The function still exists and is still called once on init (and from any future explicit trigger). The `apply_config` rebind call inside `reRender` stays because config envelopes are infrequent events and the rebind is heavy — JS-driven is the right place for it.

**Why split them:**
- `apply_config` is event-driven (config envelope arrives, JS does a heavy rebind). The coordinator's `apply_settings(...)` already exists for this and the JS side calls it. Pulling on a 4 Hz tick for the message-pick side doesn't help the rebind — those are independent concerns.
- `reRender`'s `request_message` push was the only side that benefited from event-driven re-render, and now the coordinator pulls instead. So that registration is dead.

**Alternative considered: keep `reRender` registered, but make it a no-op for the message-pick path.** Rejected: leaves dead code; the registration has no remaining consumer.

### D5. `recent_count` becomes load-bearing; default 5 unchanged

**Choice.** The pre-existing `recent_count: int = 5` on `EffectsSettings` is now consumed: the coordinator samples uniformly from the most recent `recent_count` messages during idle, after the fresh-message-priority check. The default is unchanged so existing configs work without modification.

**Why 5:** the historic default; the issue explicitly says it was retained for compat and starts "meaning something again." No reason to retune the default in the same change.

## Risks / Trade-offs

- **Stale enrichment between events.** After a new message arrives, the deque has the new entry pre-enriched (cheap, on add). A subsequent config change re-enriches all entries (filter rules or timezone might re-classify). But a *partial* config change that only touches some rules (e.g. user edits one filter in the admin UI) triggers a full re-enrich, which is O(buffer × filters). → Acceptable: full re-enrich is O(100) at typical buffer size, runs only on the config-envelope event, and the alternative (per-rule re-enrich) is more code for marginal benefit.
- **Memory: `MessageView` carries precomputed fields even when no consumer reads them.** → Acceptable: the four derived fields are small (a bool, a list of dicts, a name string, a formatted timestamp). At 100-message buffer, this is single-digit KB.
- **Throttle lag of up to 250 ms between a fresh SMS arriving and it appearing on the sign.** → Acceptable: humans don't perceive 250 ms latency for a sign display, and the alternative (per-frame pull) is the regression we're avoiding. If a user ever finds 250 ms too slow, lowering the throttle is a one-line change.
- **Two ways to set pending text: `set_text(...)` push and the manager pull.** → Mitigated: the manager pull takes priority in the tick path when `message_manager` is set; the `set_text` push remains the fallback. A consumer that uses both will see the manager's pick on next tick. Documented in the coordinator's docstring; not a behavioral surprise for the existing JS shim, which only uses the push path when `message_manager is None`.
- **Backwards compatibility of the `get_messages` return shape.** The four derived fields are populated on the `MessageView` either way; consumers see the same shape. No risk.
- **No new tests for `apply_config` JS path.** → Mitigated: the `apply_config` JS path is unchanged in this change; only the `reRender` registration is removed. Existing tests in `effects_coordinator_test.py` cover `apply_settings(...)` semantics.

## Migration Plan

This change is a pure refactor inside `lib_shared/` + the removal of one line of JS. No data migration, no config migration, no deploy sequencing.

**Rollout:**
1. Land the change behind the existing test suite. The new tests (`tests/lib_shared/message_manager_enrichment_test.py`, `tests/lib_shared/effects_coordinator_idle_rotation_test.py`) are part of the change and gate it.
2. Deploy to the Pi (`git pull` on the device, restart `lindsay_50.service`).
3. Reload the preview page in the browser; idle rotation resumes.

**Rollback:**
- Revert the commit. No state to roll back. The `pending_text` push path is preserved when `message_manager is None`, so reverting to the pre-change `EffectsCoordinator` constructor signature is a no-op for callers — the only visible difference after revert is that idle-mode rotation stops working again.

## Open Questions

_None at write time._ The change is bounded, the contract is clear, and the acceptance shape in the proposal maps directly to the four new test files plus the existing `tests/lib_shared/messages_test.py` / `tests/lib_shared/effects_coordinator_test.py` tests that should still pass.
