## Why

PR #41 introduced a universal `on_change` event spine, but it dropped two things at once:

1. **`EffectsCoordinator` lost its message-selection logic.** Before the refactor, the coordinator picked a message to display — including picking uniformly from the most recent N messages during idle so the sign always had *something* to show between real SMS arrivals. The new "JS pushes `pending_text`" model never re-pushes during idle, so the sign now shows only background between messages.
2. **`InMemoryMessages.get_messages()` computes derived state on every read.** `_enrich_messages()` runs the filter regex pass and the timezone formatter on every call, even though the inputs (`messages` + `filters` + `timezone`) only change on events. With a 100-message buffer and any future per-frame consumer, this wasted work compounds.

These look like separate bugs but share a root cause: the refactor put read-time work where event-time work belongs, and dropped the coordinator's responsibility to pick a message because the new event model didn't carry that responsibility forward. Fixing both at once restores a user-visible feature (idle rotation) and removes wasted per-read computation.

## What Changes

- **Add `MessageManager.message_manager: MessageManager` (optional) to `EffectsCoordinator` constructor.** When set, the coordinator's tick path picks a display message from the manager — fresh messages take priority; in idle it picks uniformly from the most recent `recent_count` messages (the pre-refactor `recent_provider` semantics, minus the bug that prevented anything firing during idle). When `None`, the coordinator keeps its current "caller pushes `pending_text`" behavior, preserving the existing `apply_config` JS-callable path for config envelopes (the heavy-rebind side stays JS-driven because it's infrequent but expensive).
- **Throttle the manager pull to ~4 Hz on tick, not 30+ FPS.** Reading on every frame is the cost we're avoiding; reading at 4 Hz is plenty for human-visible text and keeps the read path trivial. The store-once-consume-store model means the tick consumer just reads a property.
- **Move `_enrich_messages()` from read-time to event-time in `MessageManager`.** `_handle_message()` triggers a per-entry re-enrich on add (cheap — one entry). `_handle_config()` triggers a full re-enrich when filter rules or timezone change. `get_messages()` becomes a thin read that returns the already-enriched `MessageView` list. No per-call filter regex, no per-call timezone formatting.
- **Wire the pre-existing `recent_count` config field to actually mean something.** It was retained for compat in the prior refactor but had no consumer. After this change it's the source of truth for idle-rotation size.
- **Remove `reRender` registration on `App.registerOnChange` in `preview.js`.** The coordinator pulls on tick (throttled), so JS no longer needs to push on every change for the message-pick side. `apply_config` stays as a JS-callable — config envelopes are infrequent events, the rebind is heavy, and the JS shim is the right place for it.

## Capabilities

### New Capabilities

- `derived-state-materialization`: the discipline that pre-computable per-message enrichment (`suppressed`, filter rules, `sender_name`, `display_time`) lives in `MessageManager` as a stored field on `MessageView`, recomputed only on the event that changes it, and read by consumers as a property access. The `EffectsCoordinator`'s idle-rotation pull from `MessageManager` (throttled, recent-N, fresh-message-priority) is part of this capability because the same store-once-read-many principle governs it.

### Modified Capabilities

_None — no prior spec-level requirements exist in `openspec/specs/`, so there are no requirement deltas to record._

## Impact

- **New tests**: `tests/lib_shared/message_manager_enrichment_test.py` (enrichment runs on event, not on read); `tests/lib_shared/effects_coordinator_idle_rotation_test.py` (coordinator pulls from `MessageManager` during idle, fresh-message priority, recent-N sampling).
- **Modified code**: `lib_shared/effects_coordinator.py` (constructor accepts `message_manager`, throttled `_get_display_text` on tick, idle-rotation semantics), `lib_shared/message_manager.py` (`_enrich_messages` becomes a precompute-on-event step; `get_messages` returns already-enriched views), `lib_shared/messages.py` (`MessageView` carries precomputed enrichment fields; `InMemoryMessages.get_messages` becomes a thin read), `heart-message-manager/preview.js` (drop `reRender` registration on `App.registerOnChange`).
- **Config semantics**: the pre-existing `recent_count` field on `SignConfig` is now load-bearing for idle rotation (was retained-for-compat, now consumed).
- **No public-API break.** `MessageManager.get_messages()` returns the same shape it does today (a list of `MessageView`s); the only difference is *when* the views are enriched.
- **No new dependencies.** Pure refactor of existing event/read boundaries.
- **Out of scope**: Pydantic validation on the wire boundaries (#43); Pi-vs-browser divergence (the same `MessageManager` class works in both — `is_browser` already gates the right behavior); anything outside `lib_shared/effects_coordinator.py`, `lib_shared/message_manager.py`, `lib_shared/messages.py`, `heart-message-manager/preview.js`, and their tests. PR #41's `d5ff585` cache work and `c769270` cleanup are independent and not coupled to this change.
