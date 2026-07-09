# Spec: message-selection

## ADDED Requirements

### Requirement: Next-message selection is deterministic and weighted

The system SHALL provide a selector function that, given the current set of messages, the current time, and the event log, returns exactly one message to display next. The selector MUST be deterministic — the same input (messages, now, event_log) MUST always produce the same output.

#### Scenario: Deterministic pick with fixed inputs

- **WHEN** the selector is invoked twice with the same message set, the same `now()`, and the same event log
- **THEN** it MUST return the same message on both invocations

#### Scenario: Empty eligible set returns nothing

- **WHEN** the selector is invoked and no message satisfies the eligibility window
- **THEN** it MUST return None (the rotation pauses; pre-emption is unaffected)

### Requirement: Recency-of-display weight from the event log

The selector SHALL compute a `display_recency` component for each message by reading the most recent event in the event log matching `(message.id, current pattern's event_type)`. The value SHALL be 1.0 when no matching event exists, and SHALL approach 0.0 as time since the most recent matching event grows. The decay rate SHALL be controlled by `saturation_seconds`.

#### Scenario: Never-shown message gets full display weight

- **WHEN** a message has no matching event in the log and other messages have matching events
- **THEN** the never-shown message's `display_recency` SHALL equal 1.0

#### Scenario: Recently-shown message gets reduced display weight

- **WHEN** a message was shown 1 hour ago and `saturation_seconds` is 24 hours
- **THEN** its `display_recency` SHALL be approximately 1 − (3600/86400) ≈ 0.958 (the system MAY tune the precise formula; the requirement is that the value is reduced below 1.0)

#### Scenario: Display-recency is per-event-type

- **WHEN** a message has a recent `text_display` event but no `image_display` event
- **THEN** the selector for a text-rendering pattern MUST see the recent text event (reduced display weight), and the selector for an image-rendering pattern MUST see no matching event (full display weight)

### Requirement: Recency-of-send weight

The selector SHALL compute a `send_recency` component for each eligible message, normalized across the eligible set so the newest eligible message gets 1.0 and the oldest eligible message gets 0.0.

#### Scenario: Newest eligible message gets full send weight

- **WHEN** message A is the most recently sent among the eligible set
- **THEN** message A's `send_recency` SHALL equal 1.0

#### Scenario: Oldest eligible message gets zero send weight

- **WHEN** message B is the least recently sent among the eligible set
- **THEN** message B's `send_recency` SHALL equal 0.0

### Requirement: Two-week eligibility window

The selector SHALL exclude messages whose `sent_at` is older than `now − offset_seconds` from the rotation pool. The default `offset_seconds` SHALL be 1,209,600 (14 days). The offset SHALL be configurable via settings.

#### Scenario: Message older than the offset is excluded

- **WHEN** a message was sent 30 days ago and `offset_seconds` is the 14-day default
- **THEN** that message MUST NOT appear in the eligible set passed to the selector

#### Scenario: Message within the offset is eligible

- **WHEN** a message was sent 7 days ago and `offset_seconds` is the 14-day default
- **THEN** that message MUST appear in the eligible set

### Requirement: Favorite boost

The selector SHALL treat messages marked `favorite = true` as having a higher priority than equivalent non-favorite messages. The implementation MAY achieve this by adding a positive favorite weight to the score or by clamping the favorite message's effective `sent_at` to a recent timestamp; either approach satisfies this requirement as long as a favorite with the same recency-of-display as a non-favorite scores higher.

#### Scenario: Favorite beats non-favorite at equal recency

- **WHEN** two messages have identical event-log recency and identical `sent_at`, but only one is marked `favorite`
- **THEN** the favorite message MUST be selected over the non-favorite

#### Scenario: Non-favorite message can still be selected

- **WHEN** a non-favorite message has a much higher recency-of-display weight than a favorite
- **THEN** the selector MAY still pick the non-favorite (the boost is a tilt, not a guarantee)

### Requirement: Pi-local event log

The system SHALL maintain an append-only event log on the Pi's local disk at the configured `EVENT_LOG_PATH` (default `data/events.jsonl`). The renderer SHALL append one event per message advance, immediately after the message begins rendering. The log SHALL survive process restarts. The selector SHALL read this log to compute `display_recency`.

#### Scenario: Event written after each advance

- **WHEN** the renderer advances from message A to message B
- **THEN** the event log SHALL contain a new line describing the rendering of message B with the renderer's current pattern's `event_type`

#### Scenario: Selector reads the log on each pick

- **WHEN** the selector picks the next message
- **THEN** it SHALL read the event log (via its in-memory cache) to determine each message's most recent display timestamp

#### Scenario: Missing event equals never-shown

- **WHEN** a message has no matching event in the log
- **THEN** the selector SHALL treat it as never-shown (`display_recency = 1.0`)

#### Scenario: Log survives a restart

- **WHEN** the Pi process restarts and the selector is invoked
- **THEN** it MUST read the on-disk log and the previously recorded display timestamps MUST persist (no reset to never-shown)

### Requirement: Generic event format with event_type filter

The event schema SHALL be generic. Each event MUST carry an `event_type` discriminator (e.g., `text_display`, `image_display`, `video_display`) plus `message_id`, `timestamp`, `sent_at`, and `favorite` fields. The selector and any debug consumer MUST be able to filter events by `event_type` and by `message_id`.

#### Scenario: Filter by event_type works

- **WHEN** a consumer reads the event log filtering by `event_type = "text_display"`
- **THEN** it MUST see only events from text rendering (no image or video events)

#### Scenario: Filter by message_id works

- **WHEN** a consumer reads the event log for a specific `message_id`
- **THEN** it MUST see only events for that message

#### Scenario: Forward-compatible event_type values

- **WHEN** a future pattern type (e.g., `image_display`) renders a message and writes an event to the log
- **THEN** the existing `text_display` selector MUST NOT see the new event_type in its matching set (the selector MUST filter by `event_type`)

### Requirement: New-message pre-emption

A newly arrived message (received via the MQTT subscribe callback or the Twilio webhook) MUST bypass the selector and be displayed immediately, interrupting whatever the selector would have picked next. This is an absolute invariant.

#### Scenario: New SMS pre-empts current display

- **WHEN** a new SMS arrives while message X is being rendered
- **THEN** the renderer MUST switch to displaying the new message immediately, without waiting for the selector's next pick

#### Scenario: New message does not pollute selector state

- **WHEN** a new SMS pre-empts the display
- **THEN** the selector's eligible set MUST remain unchanged. The pre-emption MUST NOT write a `text_display` event for the pre-empting message (it pre-empts by virtue of being new, not by winning the weighted competition). The renderer MAY write an event with a different `event_type` (e.g., `preempted`) for debug visibility, but MUST NOT mark it as `text_display` for selector purposes.

### Requirement: Stable tie-breaker

When two or more messages have identical scores, the selector MUST pick deterministically using a stable tie-breaker (lower message-id first, with older `sent_at` as a secondary tie-breaker).

#### Scenario: Identical scores resolve by message-id

- **WHEN** two messages have the same weighted score
- **THEN** the selector MUST pick the message with the lower message-id (or, equivalently, a documented stable ordering) so the result is reproducible

### Requirement: Tunable weights via settings

The three selector weights (display, send, favorite) plus `saturation_seconds`, `offset_seconds`, and `event_log_path` MUST be configurable via `settings.toml` so operators can tune the rotation feel and the log location without code changes. Defaults MUST match the values listed in the design document.

#### Scenario: Settings drive the selector

- **WHEN** an operator changes `SELECTOR_W_DISPLAY` in `settings.toml` and restarts the relevant process
- **THEN** the selector MUST use the new weight on the next pick (no caching, no restart of the whole pipeline required)

### Requirement: Event log rotation

The event log SHALL rotate to keep disk usage bounded. When the active file exceeds 10 MB or 30 days of age, whichever comes first, the active file SHALL be archived as `events.jsonl.<UTC-date>.gz` and a fresh active file SHALL be started. Archives SHALL be retained for 90 days, then deleted.

#### Scenario: Large log triggers rotation

- **WHEN** the active event log exceeds 10 MB
- **THEN** the active file SHALL be archived (gzip-compressed) and a new active file SHALL be started

#### Scenario: Old archive is purged

- **WHEN** an archive's date is more than 90 days old
- **THEN** the archive SHALL be deleted

### Requirement: Browser preview uses the same selector with its own event log

The browser preview at `/playful` SHALL use the same `MessageSelector` Python class as the Pi. The browser SHALL maintain its own event log in IndexedDB (NOT a copy of the Pi's log) so the preview's picks are self-consistent. The preview is illustrative; it MAY diverge from the Pi's on-wall state, and this MUST be documented.

#### Scenario: Browser selector uses IndexedDB-backed log

- **WHEN** the browser preview page calls the selector
- **THEN** it SHALL pass an event log backed by IndexedDB, and the selector SHALL read events from there

#### Scenario: Browser and Pi use the same selector class

- **WHEN** the same message set and clock are passed to the selector in both the browser preview and on the Pi
- **THEN** both SHALL execute the same Python class — only the event-log source differs (IndexedDB vs Pi-local JSONL)