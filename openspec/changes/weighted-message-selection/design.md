## Context

The display device currently iterates through messages in arrival order — whatever is oldest in the ring buffer gets shown next. There is no record of when each message was last shown, no notion of recency-of-send relative to other messages, and no notion of favorite. The effect is a flat rotation that surfaces the same messages repeatedly while older ones go unseen. The SMS-to-display pipeline (Twilio webhook → Flask → SQLite → S3 → MQTT → Pi subscriber → display loop) is otherwise stable.

Critically, the only process that knows when a message is actually being rendered is the Pi — the Twilio webhook fires on receipt, the Flask server stores the message, and the Pi is downstream of all of that. So the source of truth for "when was this last shown" must live on the Pi, not on the server. We model this as an append-only event log on the Pi's disk, written by the renderer immediately after each message advances. The selector reads the log on each pick. The log uses a generic schema (`event_type` discriminator) so future pattern types plug in without redesign and the log can later be published to MQTT for remote debugging.

The browser preview (`/playful`) shares the same Python `MessageSelector` class via PyScript; it maintains its own IndexedDB-backed event log so the preview's picks are self-consistent (but not necessarily identical to the Pi's — preview is illustrative, not authoritative).

## Goals / Non-Goals

**Goals:**

- Define a deterministic priority function that picks the next message given (messages, `now()`, event_log).
- Make the score's contributions explicit and tunable: a recency-of-display component (sourced from the event log), a recency-of-send component (sourced from the message's `sent_at`), and a favorite boost (sourced from the message's `favorite` flag).
- Apply a configurable eligibility window (default two weeks) so ancient messages do not compete.
- Persist render events on the Pi's disk so a restart does not reset rotation history.
- Preserve the existing pre-emption invariant: a newly arrived message always shows immediately, bypassing the selector.
- Use a generic event schema that supports future pattern types (`image_display`, `video_display`, …) and is forward-compatible with MQTT publication for remote debugging.
- Keep the change additive — new files in `lib_shared/` and `heart-matrix-controller/`, no breaking changes to MQTT envelope, Twilio webhook, the server's `Message` model, or the server's storage schemas.

**Non-Goals:**

- Choosing where `favorite` lives in storage (e.g., a separate `favorites` table vs a boolean column on `messages`). Implementation choice deferred to the implementer; spec only requires the flag to be readable by the selector.
- Designing the dashboard UI for "favorite" or showing the selector's reasoning to operators.
- ML-based selection (collaborative filtering, embeddings). Out of scope for v1.
- Publishing events to MQTT for remote debugging. Out of scope for v1; the schema is forward-compatible (the event has the fields an MQTT publisher would need) but no MQTT code ships in this change.
- Changing the broker, transport, or envelope shape.
- Cross-process coordination of selector state (e.g., distributed locks). The selector is local to whatever process runs it.

## Decisions

### Decision 1 — Weight is the sum of two normalized components (plus favorite boost)

The issue text says "These factors should be combined (added?) to establish a weight". We pick **additive** because the two components live on independent 0–1 scales and we want each to be explainable in isolation ("high score because never-shown = 1.0 and recent = 0.9"). Multiplicative would lose interpretability.

```
score(message, now, event_log) =
    w_display   * display_recency(message, now, event_log)   # 0..1, 1 = never shown
  + w_send      * send_recency(message, eligible_set, now)   # 0..1, 1 = most recent
  + w_favorite  * (1.0 if message.favorite else 0.0)

# Defaults (overridable via settings.toml):
#   w_display   = 0.6
#   w_send      = 0.3
#   w_favorite  = 0.4   # additive; a favorite never-shown message beats a non-favorite never-shown message
```

The favorite weight is additive rather than a multiplier on `send_recency` (which the issue suggested as one option) because additive lets us tune "how much do favorites dominate" independently from "how recent is recent". The implementer may also implement the issue's alternative — clamping a favorite's effective `sent_at` to `now − 1 hour` — and that is acceptable as long as the result is functionally identical to a favorites-tilted score.

**Alternatives considered:**
- Multiplicative (`score = display * send`): elegant but a never-shown ancient message would score ~0, which violates the "never-shown recent wins" intent.
- Sort by event-log timestamp then by `sent_at` (lexicographic): simpler but loses the continuous tuning surface; users could not express "favor recent messages more" without reordering keys.
- Pure ML ranking: deferred (non-goal).
- Embedding `last_shown_at` on the `Message` model and replicating through server/Pi/browser: rejected (see Decision 5).

### Decision 2 — `display_recency` is derived from the event log, not a Message field

```
display_recency(message, now, event_log) =
    1.0 if no matching event for (message.id, current event_type)
    else max(0.0, 1.0 - (now - last_event.timestamp) / saturation_seconds)
```

Where "matching event" means the most recent event in the log whose `event_type` equals the renderer's current pattern type AND whose `message_id` equals `message.id`. A text-render pattern only looks at `text_display` events; an image-render pattern only looks at `image_display` events. This keeps the recency computation per-pattern, so a message shown as an image and then as text does not unfairly suppress its text score.

A never-shown message gets the maximum value — "fresh slate, show me." A message shown recently gets a low value — "sit out for a while." The `saturation_seconds` knob controls how aggressively recently-shown messages are excluded.

### Decision 3 — `send_recency` is normalized over the eligible set, not over all time

```
send_recency(message, eligible_set, now) =
    (message.sent_at - min(eligible_set, key=sent_at)) /
    (now - min(eligible_set, key=sent_at))
    if denominator > 0
    else 1.0
```

Normalizing across the eligible set (rather than against an absolute reference like "epoch") keeps the score meaningful regardless of how old the database is. The oldest eligible message gets 0.0; the newest eligible message gets 1.0.

### Decision 4 — Eligibility window uses `sent_at`, not the event log

A message is eligible iff `now − sent_at ≤ offset_seconds` (default 14 days). The offset is checked against `sent_at` because we want dormant older messages to stay dormant — a message from two years ago should not pop up just because it has never been shown. The implementation exposes `OFFSET_SECONDS` in `settings.toml`.

### Decision 5 — Display-recency lives in a Pi-local append-only event log; server's `Message` model is unchanged

**Rationale (corrected from prior draft):** the original proposal embedded `last_shown_at` on the `Message` dataclass and replicated it through server, Pi, and browser. That's wrong — only the renderer knows when a message was shown. Replicating "displayed at" through the server implies the server has that signal, which it does not. Worse, replicating it would require every write to be atomic across three storage backends, and the server's write would need to come from the Pi (over MQTT), turning a one-way data path into a bidirectional one for no benefit.

The corrected model:

- The Pi owns a single file: `data/events.jsonl` (configurable via `EVENT_LOG_PATH`). One JSON object per line, appended by the renderer immediately after each message advances. The renderer reads the log into an in-memory cache on boot and refreshes it on every append (write-through). The selector reads from the cache; reads are O(matching-events-for-this-pattern-and-id), not O(file-size).
- The server's `Message` model is unchanged. The server's SQLite schema is unchanged. No new columns. No migration. The Twilio webhook path is unchanged.
- The browser preview maintains its own event log in IndexedDB (`js.indexedDB`-backed Python class). It does NOT replicate the Pi's log — preview is illustrative, not authoritative. Documented as such.

**Event schema:**

```json
{
  "event_type": "text_display",
  "message_id": "abc123",
  "timestamp": 1752080123.45,
  "sent_at": 1752000000.0,
  "favorite": false
}
```

Fields are denormalized (`sent_at`, `favorite` repeated from the message) so a debug consumer can filter and sort without joining. `event_type` is the discriminator; supported values in v1 are `text_display`, with `image_display` and `video_display` reserved for future pattern types.

### Decision 6 — Determinism via a stable tie-breaker

Two messages with identical scores must pick deterministically. The selector sorts by `(−score, sent_at, message.id)` so the tie-breaker is: lower score first, then older message first, then lower message-id first. This guarantees the same input set always yields the same output — important for testing and for the dashboard preview agreeing with itself.

### Decision 7 — Pre-emption is a separate code path

The selector is only invoked during the regular rotation loop. The MQTT subscribe callback (new envelope) pushes the new message directly to the renderer without consulting the selector. This keeps the selector simple and the pre-emption invariant trivially correct. The renderer MAY write a `preempted` event to the log for debug visibility (out of scope to require).

### Decision 8 — Log rotation for bounded disk usage

The event log rotates when the active file exceeds 10 MB or 30 days, whichever first. The rotation moves the active file to `events.jsonl.<UTC-date>.gz` (gzip-compressed) and starts a fresh active file. Archives are retained for 90 days, then deleted. The rotation runs at process startup and on a periodic check (e.g., every hour). The selector's in-memory cache is rebuilt on rotation so old events stop counting toward `display_recency` once archived.

## Risks / Trade-offs

- [Risk] **Event log corruption or partial writes.** A crash mid-write could leave a truncated JSON line at the end of the file. → Mitigation: the reader skips any line that fails JSON parsing and logs a warning. The selector treats missing recent events as a "conservative bias toward variety" (assumes shown recently — does not repeat). One bad line loses at most one event.

- [Risk] **Log file grows unbounded without rotation.** → Mitigation: rotation at 10 MB or 30 days, 90-day archive retention (Decision 8).

- [Risk] **Selector reads the file on every pick — could be slow at scale.** → Mitigation: write-through in-memory cache loaded at boot and on every append. The selector reads from cache, not the file. Pick latency is O(matching-events-for-this-pattern-and-id), typically O(1) per candidate.

- [Risk] **Browser preview shows a different message than the device because IndexedDB is per-browser and not synced.** → Mitigation: documented as preview-is-illustrative. The selector function is the same Python class on both sides, so given the same `messages`, `now()`, and `event_log`, the pick agrees. Divergence comes from state being per-instance, which is acceptable for a preview.

- [Risk] **Two-week eligibility window means a new message arriving during a quiet stretch may be the only eligible candidate for a long time.** → Mitigation: when only one message is eligible, the selector returns it (the score is trivially the highest). This is the correct behavior; documented explicitly.

- [Risk] **Favorite boost additive vs clamp ambiguity could cause confusion.** → Mitigation: the spec is "favorites have a higher score than equivalent non-favorites." Either additive or clamp-style implementations satisfy the spec. The implementer picks one and documents it in code.

- [Risk] **Forward-compat assumption: future MQTT publication needs the events in real-time.** → Mitigation: the renderer writes events synchronously; a future publisher can subscribe to the `EventLog.append` hook and publish immediately. No retroactive change needed.

## Migration Plan

- No database migration. The server's `Message` model and SQLite schema are unchanged. The event log is a brand-new file on the Pi.
- New settings keys: `EVENT_LOG_PATH` (default `data/events.jsonl`), `SELECTOR_*` weights, `USE_WEIGHTED_SELECTOR` (default false), `SELECTOR_OFFSET_SECONDS` (default 14 days), `SELECTOR_SATURATION_SECONDS` (default 24 hours).
- Deploy Pi first with `USE_WEIGHTED_SELECTOR=false` (the new path is dark-shipped). Old first-in/first-out rotation continues to run. Flip the flag and observe for at least one full `OFFSET_SECONDS` window before rolling to other Pis.
- Rollback: flip `USE_WEIGHTED_SELECTOR=false` and restart. The previous code path is preserved behind the flag.

## Open Questions

- Should `favorite` survive message deletion? (E.g., if a sender favorites a message and then the admin deletes it, do we keep the favorite flag in case the message is re-sent?) — Deferred. Implementer picks a behavior; spec does not constrain.
- Should there be a "max times shown" cap to prevent a popular favorite from dominating? — Deferred to v2; not in this change.
- Should the selector record WHY it picked each message (which component dominated) for operator debugging? — Deferred; would require either a sidecar log or a UI affordance. Spec does not require it.
- When the future MQTT publication lands, should it use a dedicated topic (`sign/events`) or reuse the existing envelope topic? — Deferred to the future change.