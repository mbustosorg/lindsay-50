# Event Log

The Pi-side display controller maintains a small append-only JSONL log of
each rendered event. The log is the selector's source of truth for
**display-recency** — "how long ago did this message last appear on the
sign?" — and it feeds the weighted pick algorithm in
`lib_shared/selector.py`.

The browser preview runs the same selector against its own IndexedDB-backed
log (per-browser, not synced), so a developer's preview agrees with the Pi
about which message "should come next" given the same inputs.

## Where it lives

- **Pi**: `heart-matrix-controller/data/events.jsonl` (configurable via
  `EVENT_LOG_PATH` in `heart-matrix-controller/settings.toml`).
- **Browser**: per-origin IndexedDB store, managed by
  `heart-message-manager/event_log.py`.

The file is created on the first append; until then it does not exist.
The directory is `EVENT_LOG_PATH`'s parent and is auto-created.

## Schema (every line)

```json
{"event_type": "text_display", "message_id": "<id>", "timestamp": 1700000000.0, "sent_at": 1699999900.0}
```

| Field         | Type         | Description                                                |
|---------------|--------------|------------------------------------------------------------|
| `event_type`  | string       | Discriminator — `"text_display"`, `"image_display"`, `"video_display"`, etc. The selector filters by this for per-event-type display-recency. |
| `message_id`  | string       | The ID of the displayed message. The selector reads it for `display_recency` lookup. |
| `timestamp`   | float (secs) | When the message advanced onto the display. Used to compute `display_recency`. |
| `sent_at`     | float (secs) | When the SMS was received — denormalized for debugging. **Not** used by the selector (eligibility is checked against the message record, not the event log). |

The schema is **exactly** these four keys — `favorite`, `body`, and any other
mutable/derived fields are explicitly absent. See
`tests/test_event_log.py::test_event_schema_has_exactly_required_keys`.

## Bounded ring

Default capacity is `EVENT_LOG_MAX_ENTRIES = 100`. When the file holds N
entries and a new event is appended, the oldest entry is dropped and the
file is rewritten atomically (via `tempfile.mkstemp` + `os.replace`) with
the N most recent entries. No archive, no compression — the log is a
debugging surface, not a history.

Raise or lower the cap via `EVENT_LOG_MAX_ENTRIES` in
`heart-matrix-controller/settings.toml`. The default 100 is enough for
~1.5 days of steady SMS traffic at the typical rotation pace; raise it
if you need to debug a longer window.

## Corrupt-line tolerance

If the loader encounters a line that fails JSON parsing (or is missing
required keys), it skips that line and logs a warning. One bad line MUST
NOT lose other events. Verified by
`tests/test_event_log.py::test_corrupt_line_skipped_other_events_survive`.

## Selector integration

`lib_shared.selector.py:MessageSelector.pick()` reads `display_recency`
from the log: for each candidate message, it grabs the most recent event
matching `(message_id, current_event_type)` and applies a 24-hour decay
window (`SATURATION_SECONDS = 86_400`). Messages never shown get
`display_recency = 1.0`; messages shown recently sit out.

The selector's other two score components (`send_recency`, `favorite`)
are computed independently of the log.

## Reading the log

```bash
# All text-display events (default `rg` from the project CLAUDE.md):
rg '"event_type": "text_display"' heart-matrix-controller/data/events.jsonl

# Most-recent first by timestamp — quick eyeball of "what's been on the sign":
rg '"event_type": "text_display"' heart-matrix-controller/data/events.jsonl \
  | python3 -c "import sys,json; [print(e['message_id'], e['timestamp']) for e in sorted((json.loads(l) for l in sys.stdin), key=lambda x: -x['timestamp'])]"

# Per-message history (replace m1 with a real id):
rg 'm1' heart-matrix-controller/data/events.jsonl
```

## Operational notes

- **Rollout**: the new selector ships with `USE_WEIGHTED_SELECTOR = False`
  in `lib_shared/selector.py`. Existing first-in/first-out rotation
  remains the active code path. Flip the constant to `True` and redeploy
  to enable. Recommended rollout: enable on a test Pi first, observe for
  at least one full `OFFSET_SECONDS` window (14 days by default) before
  enabling globally.

- **Rollback**: set `USE_WEIGHTED_SELECTOR = False` in
  `lib_shared/selector.py` and redeploy. The previous rotation code path
  remains intact and the event log is unused. **Note**: existing entries
  in `data/events.jsonl` are not cleared on rollback — re-enabling
  inherits any history that accumulated during the disabled period.

- **No `favorite` in events**: `favorite` is read from the Message
  record at pick time, NOT stamped into the event payload. The event
  schema is locked to four keys (test 6.17). Storing `favorite` in the
  event log would corrupt the append-only invariant — see memory entry
  "Pi-local event log, not Message mutation, for display-recency".

## Future work (deferred)

Future work MAY publish events to an MQTT topic (e.g. `sign/events`) for
remote debugging. The schema is forward-compatible — each event already
has `event_type`, `message_id`, `timestamp`, `sent_at` — but **no MQTT
code ships in this change**. See `openspec/changes/weighted-message-selection/tasks.md`
§9 for the deferred list.
