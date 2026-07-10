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

The selector SHALL exclude messages whose `received_at` is older than `now − OFFSET_SECONDS` from the rotation pool. The default `OFFSET_SECONDS` SHALL be 1,209,600 (14 days). `OFFSET_SECONDS` SHALL be defined as a module-level constant in `lib_shared/selector.py`; it SHALL NOT be a key in `settings.toml` in this change. The constant SHALL be seconds-denominated so unit tests can use small windows (e.g., 60 seconds) without depending on real-world durations. Future operator-facing presentation of the eligibility window on the admin UI is a separate change; the present change MUST NOT add UI controls for `OFFSET_SECONDS`.

#### Scenario: Message older than the offset is excluded

- **WHEN** a message was sent 30 days ago and `OFFSET_SECONDS` is the 14-day default
- **THEN** that message MUST NOT appear in the eligible set passed to the selector

#### Scenario: Message within the offset is eligible

- **WHEN** a message was sent 7 days ago and `OFFSET_SECONDS` is the 14-day default
- **THEN** that message MUST appear in the eligible set

#### Scenario: Unit test with a small window

- **WHEN** a unit test sets `OFFSET_SECONDS` to a small value (e.g., 60 seconds) and constructs the selector with messages of varying `received_at`
- **THEN** only messages whose `received_at` falls within the small window MUST appear in the eligible set

### Requirement: Favorite boost

The selector SHALL treat messages marked `favorite = true` as having a higher priority than equivalent non-favorite messages. The implementation MAY achieve this by adding a positive favorite weight to the score or by clamping the favorite message's effective `received_at` to a recent timestamp; either approach satisfies this requirement as long as a favorite with the same recency-of-display as a non-favorite scores higher.

#### Scenario: Favorite beats non-favorite at equal recency

- **WHEN** two messages have identical event-log recency and identical `received_at`, but only one is marked `favorite`
- **THEN** the favorite message MUST be selected over the non-favorite

#### Scenario: Non-favorite message can still be selected

- **WHEN** a non-favorite message has a much higher recency-of-display weight than a favorite
- **THEN** the selector MAY still pick the non-favorite (the boost is a tilt, not a guarantee)

### Requirement: Pi-local event log

The system SHALL maintain an append-only event log on the Pi's local disk at the configured `EVENT_LOG_PATH` (default `data/events.jsonl`). The renderer SHALL append one event per message advance, immediately after the message begins rendering. The log SHALL survive process restarts. The selector SHALL read this log to compute `display_recency`.

#### Scenario: Event written after each advance

- **WHEN** the renderer advances from message A to message B
- **THEN** the event log SHALL contain a new line describing the rendering of message B with the renderer's current pattern's `event_type`

#### Scenario: Event schema carries only immutable facts

- **WHEN** an event is written to the log
- **THEN** it MUST contain `{event_type, message_id, timestamp, received_at}` and MUST NOT contain mutable current-state fields such as `favorite` (favorite is read from the message record at pick time, not from the log)

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

The event schema SHALL be generic. Each event MUST carry an `event_type` discriminator (e.g., `text_display`, `image_display`, `video_display`) plus `message_id`, `timestamp`, and `received_at` fields. The schema MUST NOT include mutable current-state fields (such as `favorite`). The selector and any debug consumer MUST be able to filter events by `event_type` and by `message_id`.

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

When two or more messages have identical scores, the selector MUST pick deterministically using a stable tie-breaker (lower message-id first, with older `received_at` as a secondary tie-breaker).

#### Scenario: Identical scores resolve by message-id

- **WHEN** two messages have the same weighted score
- **THEN** the selector MUST pick the message with the lower message-id (or, equivalently, a documented stable ordering) so the result is reproducible

### Requirement: Selector weights and tunables are code constants

The selector's three weights (`W_DISPLAY`, `W_SEND`, `W_FAVORITE`), the display-recency decay window (`SATURATION_SECONDS`), the eligibility window (`OFFSET_SECONDS`), and the rollout flag (`USE_WEIGHTED_SELECTOR`) SHALL be defined as module-level constants in `lib_shared/selector.py`. They SHALL NOT be keys in `settings.toml` in this change. Operators tune them by editing the source and redeploying; no Settings page UI controls them in this change. The constants MUST be importable from `lib_shared.selector` (e.g., `from lib_shared.selector import W_DISPLAY, W_SEND, W_FAVORITE, SATURATION_SECONDS, OFFSET_SECONDS, USE_WEIGHTED_SELECTOR`) so tests can verify the documented defaults.

#### Scenario: Constants live in code, not settings

- **WHEN** an operator inspects `heart-matrix-controller/settings.toml` and `heart-message-manager/settings.toml`
- **THEN** neither file SHALL contain `SELECTOR_*` keys or `USE_WEIGHTED_SELECTOR`. The selector knobs SHALL be present in `lib_shared/selector.py` only.

#### Scenario: Constants are importable with documented defaults

- **WHEN** a test imports the constants from `lib_shared.selector`
- **THEN** `W_DISPLAY` SHALL equal `0.6`, `W_SEND` SHALL equal `0.3`, `W_FAVORITE` SHALL equal `0.4`, `SATURATION_SECONDS` SHALL equal `86400`, `OFFSET_SECONDS` SHALL equal `1209600`, and `USE_WEIGHTED_SELECTOR` SHALL equal `False`

### Requirement: Event log is a bounded ring

The event log SHALL be bounded to the most recent N entries (default 100, configurable via `EVENT_LOG_MAX_ENTRIES`). When the log is at capacity, appending a new event SHALL drop the oldest entry. The on-disk file SHALL always hold exactly the most recent N entries.

#### Scenario: At-capacity append drops the oldest entry

- **WHEN** the log has N entries and a new event is appended
- **THEN** the oldest entry SHALL be dropped and the on-disk file SHALL be rewritten to hold the N most recent entries (in append order)

#### Scenario: Bounded disk usage

- **WHEN** the operator inspects `EVENT_LOG_PATH` on a running Pi
- **THEN** the file SHALL contain at most `EVENT_LOG_MAX_ENTRIES` lines (default 100)

#### Scenario: Max entries is configurable

- **WHEN** the operator changes `EVENT_LOG_MAX_ENTRIES` in settings and restarts the process
- **THEN** the in-memory cache and the on-disk file SHALL both cap at the new value

### Requirement: Browser preview uses the same selector with its own event log

The browser preview at `/playful` SHALL use the same `MessageSelector` Python class as the Pi. The browser SHALL maintain its own event log in IndexedDB (NOT a copy of the Pi's log) so the preview's picks are self-consistent. The preview is illustrative; it MAY diverge from the Pi's on-wall state, and this MUST be documented.

#### Scenario: Browser selector uses IndexedDB-backed log

- **WHEN** the browser preview page calls the selector
- **THEN** it SHALL pass an event log backed by IndexedDB, and the selector SHALL read events from there

#### Scenario: Browser and Pi use the same selector class

- **WHEN** the same message set and clock are passed to the selector in both the browser preview and on the Pi
- **THEN** both SHALL execute the same Python class — only the event-log source differs (IndexedDB vs Pi-local JSONL)

### Requirement: Eligibility window is a code constant (future-UI candidate)

The message-display eligibility window (`OFFSET_SECONDS` in `lib_shared/selector.py`) SHALL NOT be exposed as a control on the Flask Settings page in this change. The Flask Settings page, the Flask route handlers, and the templates SHALL NOT be modified to add eligibility-window or selector-weight controls. A future change MAY add an operator-facing UI for `OFFSET_SECONDS` (and possibly the three weights and `SATURATION_SECONDS`); that future change is responsible for translating between the seconds-denominated code constant and the days/hours input the operator sees.

#### Scenario: Settings page does not expose eligibility window in this change

- **WHEN** an operator opens the Settings page in this change
- **THEN** there SHALL be no labeled control for the message-display eligibility window (in days or otherwise), and no controls for the selector weights or saturation

#### Scenario: Templates and routes are not modified

- **WHEN** the change is complete
- **THEN** the Settings template(s), the playful Settings template(s), and the settings-update route handler SHALL NOT have been modified to handle `SELECTOR_OFFSET_SECONDS`, `SELECTOR_W_DISPLAY`, `SELECTOR_W_SEND`, `SELECTOR_W_FAVORITE`, `SELECTOR_SATURATION_SECONDS`, or `USE_WEIGHTED_SELECTOR`