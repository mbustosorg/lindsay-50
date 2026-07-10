# Round 4 — Drop the interrupt; tee up new arrivals in a FIFO queue

## Context

The `EffectsCoordinator` currently has **two interrupt triggers** that fire
_immediately_ when a fresh-id SMS arrives:

1. `fresh_id_interrupt` — when the head of `current_messages` has an id
   that isn't in `_consumed_message_ids` and we're in `hold` mode (line 1121).
2. `new_id` — same check, but in `background` mode (line 1225).

Both fire a fade-out _right now_, mid-cycle. The user has watched enough live
journal to know this is a bug, not a feature:

> "the interruption / receipt of a new message isn't working properly
> either. can we just tee it up to be the next message selected, rather
> than trying to interrupt the current cycle? we could just keep a
> 'new messages' list — if there are non-zero entries, just pick the
> oldest from that list (and remove it). a short-circuit on the next
> selection. this feels less complicated."

The symptoms from the live Pi:
- A media cycler (10s window) gets cut off 2s in by a fresh text SMS.
- Multiple arrivals during a 10s hold cause the sign to flap.
- The "suppress while media cycler active" exemption was a workaround
  for the interrupt, not a fix for it — and it only applied to the
  cycler, leaving the same issue for text-only SMS.

**Root cause.** The state machine treats new arrivals as "interrupt
events" rather than "queued work". The fix: invert the relationship.
The state machine continues at its own pace; new arrivals accumulate
in a FIFO; the next natural pick site drains one entry off the queue.

## Approach

### 1. Add the FIFO to `MessageManager` — `lib_shared/message_manager.py`

`MessageManager` already owns the in-memory buffer and the dispatcher
(used by both the Pi's paho client and the browser preview's WebSocket
shim). Centralize the queue there so the contract is the same across
all clients — Flask server doesn't need it (no coordinator runs there),
but the Pi and the browser preview both go through `dispatch →
_handle_message`.

**The queue MUST be a `collections.deque`, not a `list`.** The Pi
runs two threads: the paho daemon thread `append`s to the queue via
`_handle_message`, while the main thread `pop`s from it via
`_pick_next`. `list.pop(0)` is O(n) and not atomic under CPython — a
concurrent `append` can land mid-`memmove` and corrupt indices.
`deque.popleft()` is O(1) and atomic under the GIL, matching the
existing `InMemoryMessages._msgs: deque(maxlen=100)` pattern in
`lib_shared/messages.py:147`. The existing code relies on the same
GIL + atomic-deque contract for the buffer; the new queue extends
the same model — no new lock needed.

```python
from collections import deque

# lib_shared/message_manager.py — MessageManager.__init__
self._new_messages_queue: deque[MessageView] = deque(maxlen=200)

# New FIFO API
def take_next_new_message(self) -> MessageView | None:
    """Pop and return the OLDEST queued fresh arrival, FIFO.
    Returns None when the queue is empty. The coordinator's
    `_pick_next` short-circuits on this BEFORE the random-pool
    path — when something is queued, the random pool is ignored."""
    try:
        return self._new_messages_queue.popleft()
    except IndexError:
        return None
```

**`maxlen=200`** caps memory: at 2× the buffer's 100-entry ring,
it covers the worst case (flood during a 30s hold) while bounding
growth. Drops are oldest-first, consistent with how `_msgs` already
handles overflow. Dropped entries remain in the buffer (subject to
its own maxlen) and re-enter the random pool once the queue drains.

**Why on `MessageManager`, not `EffectsCoordinator`.** The MQTT
dispatch thread (paho / WebSocket) writes to the manager; the
coordinator's `tick()` thread reads. Putting the queue on the
manager keeps the lock surface small (one owning object) and means
the browser preview gets it for free. Flask doesn't run an
`EffectsCoordinator` — it only publishes envelopes — so Flask
doesn't see the queue at all (no coordinator to consume from).

**Note on the existing buffer's thread-safety.** The buffer
(`InMemoryMessages._msgs: deque(maxlen=100)` +
`_seen_ids: set[str]`) is currently read lock-free by the main
thread's coordinator `tick()` and written lock-free by the paho
daemon thread. That's a pre-existing race that the new queue does
NOT introduce — `deque.append`, `deque.popleft`, and `set.add` are
all atomic under CPython's GIL, matching the existing pattern. The
new queue follows the same contract; an explicit `threading.Lock`
is intentionally NOT added (consistent with the rest of the file).
The browser preview is single-threaded (JS event loop), so this is
moot there.

### 2. Wire the queue at the only "new arrival" insertion site

`MessageManager._handle_message` (line 520, with the buffer add at
line 554) is the single funnel for new MQTT envelopes that come off
the broker and represent fresh SMS/MMS events. The two other `add()`
paths are NOT new arrivals:

- `hydrate_from_cache` (lines 382–408, browser sessionStorage
  rehydrate) — `source` comes from the cached entry's `source` field,
  which can be `"rest"` or `"mqtt"`. Either way, replay of a previous
  session is NOT a fresh arrival in the current session.
- `seed()` (lines 684–699, REST seed-from-API on Pi boot) — pre-
  existing messages, not fresh arrivals. Source is forced to
  `"rest"`.

The exact insertion:

```python
# lib_shared/message_manager.py — _handle_message, after the buffer add
view = self._messages.add(msg, source="mqtt")
if view is not None:
    self._messages._enrich_messages([view])
    self._new_messages_queue.append(view)  # ← NEW — live MQTT only
```

The `source="mqtt"` discriminator is already encoded by the call
site — no extra `if source == "mqtt":` guard needed. The seed and
cache paths use different code branches and never reach this line.

### 3. Drain the queue first in `_pick_next`

`lib_shared/effects_coordinator.py:_pick_next` (line 475) currently
calls `get_display_message()` (random.choice over the recent pool)
and re-rolls for same-id avoidance. Insert the FIFO drain at the top:

```python
def _pick_next(self) -> str | None:
    """Round 4 (queue): drain the FIFO of fresh arrivals before
    falling back to the recent-pool random pick. Round 3's id-based
    re-roll still applies on the random-pool path."""
    queue_msg = self.message_manager.take_next_new_message()
    if queue_msg is not None:
        # Round 4 queue drain: write the standard pick-state triple
        # (entry, id, body) so downstream consumers (selected-log,
        # out→in's cycler rebuild) all read consistent state.
        self._last_picked_entry = queue_msg
        self._last_shown_message_id = queue_msg.message.id
        body = queue_msg.message.body
        self._last_display_message = body
        return body
    # Fall through to the recent-pool random pick (unchanged).
    body = self.get_display_message()
    if body is None:
        return None
    ...
```

`Message.body` is typed `str` (never `None`), so the `or ""` from
the sketch is a no-op and misleading. For MMS-only messages with
`body=""` and `media=[...]`, this returns `""` — the out→in branch's
existing `if text:` check clears the scroller while
`_maybe_build_media_cycler` constructs the `MediaCycler` (or
`BrowserMediaOverlay`) from `_last_picked_entry.message.media`. That
path is unchanged and already exercised by the round-1 media tests.

The selected-log still fires at the **3** `_begin_out` sites
(`cycler_complete`, `intro_done`, `background idle`) and still reads
`_last_picked_entry` — no per-site refactor needed. The queue just
controls WHICH entry is in there.

### 4. Drop the interrupt machinery

These become dead code once the queue drains at natural pick sites:

- **`hold` branch (line 1121–1156):** drop `fresh_id_landed`, drop the
  cycler exemption log ("Coordinator hold: suppressing fresh-id
  interrupt while media cycler active"), drop the `_begin_out_trigger =
  "fresh_id_interrupt"` block, drop the now-empty `if fresh_id_landed`
  arm. The remaining `elif now - self.phase_start >= effects_settings.hold_seconds:`
  arm is the only `hold`→`text_out` trigger.
- **`background` branch (line 1225–1278):** drop `fresh_id_landed`
  detection (and the cycler-exempt log + `_last_suppressed_message_id`
  field), drop the `trigger = "new_id"` arm. The remaining
  `trigger = "idle"` arm is the only fade-out trigger from background.
- **`_consumed_message_ids` set (line 254, added at out→in line
  1048/1051, read in fresh_id check):** drop the field and all
  read/write sites. Round 3's `_last_shown_message_id` re-roll in
  `_pick_next` already prevents immediate re-pick; the queue provides
  FIFO ordering for new arrivals; the random pool handles the
  fill-in-between case.
- **Two `_current_is_active_media_cycler` cycler-exempt callers in
  the `hold` and `background` branches:** drop both. The exemption
  only existed to suppress fresh-id interrupts during cycler playback
  — without interrupts, no exemption needed.
- **`_last_suppressed_message_id` field:** drop. It only fed the
  cycler-exempt log.

### 5. Five `_begin_out_trigger` values collapse to three

After the interrupt drop, only three triggers fire `_begin_out`:

- `intro_done` (heart fade at boot, line 955)
- `cycler_complete` (media cycler exhausts, line 734)
- `idle` (background→out after `idle_seconds`, line 1273)

`fresh_id_interrupt` and `new_id` are gone. The "Coordinator:
starting fade out from mode=X effect=Y trigger=Z" log now lists
one of three values.

### 6. Live verification on the Pi

After deploy, send a few test SMS messages during a hold:

1. **Sign does NOT interrupt mid-hold.** A 2nd SMS arriving 2s into
   a 10s media hold plays out the full cycler before fading out.
2. **Queue is FIFO.** Send 3 SMS in rapid succession (`Hey 1`, `Hey 2`,
   `Hey 3`) during a hold. The sign shows them in arrival order
   once the current cycle drains: `Hey 1 → Hey 2 → Hey 3`.
3. **Recent-pool fallback still works.** After the queue empties,
   random pick from the recent pool continues.
4. **No `fresh_id_interrupt` / `new_id` strings in the journal.**
   Grep `journalctl -u lindsay_50 | grep -E "fresh_id_interrupt|new_id"`
   returns 0 lines.

## Critical files

- `lib_shared/message_manager.py` — `MessageManager.__init__` adds
  `_new_messages_queue: deque[MessageView]` (maxlen=200); new
  `take_next_new_message()` method; `_handle_message` (line 554,
  immediately after the buffer `add`) appends to the queue.
- `lib_shared/effects_coordinator.py` — `_pick_next` (line 475)
  drains the queue first; the `hold` (line 1115–1156) and `background`
  (line 1220–1278) branches drop the interrupt arms; `_consumed_message_ids`
  field and reads/writes gone; `_last_suppressed_message_id` field
  gone; `EffectsCoordinator.__init__` fields pruned.
- `heart-matrix-controller/main.py` — no change expected. The Pi's
  existing wiring `dispatch_callback=manager.dispatch`
  (heart-matrix-controller/main.py:103) already routes new MQTT
  envelopes through the manager's `_handle_message`, which is
  where the queue lives.
- `heart-message-manager/app_main.py` — no change expected. The
  browser's `_on_envelope_js` (line 107) calls
  `_message_manager.dispatch(...)` on the same `_handle_message`
  funnel as the Pi. The browser's coordinator (constructed at
  line 286) reads from the queue via the same `_pick_next` path.

## Out of scope

- **The Flask server running a coordinator.** Flask only publishes
  envelopes; it doesn't render the sign. No queue needed there.
- **The seed-from-API REST add (`seed()` / `add_many`, line 699)**
  and the browser's `hydrate_from_cache` (line 408). Those messages
  are pre-existing, not fresh arrivals — they're already in the
  buffer before the Pi boots (or browser session restarts).
  Queueing them would cause the Pi to show every seeded message
  immediately, which is the wrong behavior. Queue append lives
  ONLY on the `source="mqtt"` path in `_handle_message`.
- **A new pre-caching or warm-up flow.** The change is purely about
  pick-site sequencing; lazy media fetch on cycle advance is
  unchanged.

## Verification

### Unit tests (host-side)

Add to `tests/effects_coordinator_test.py`:

- `test_queue_drains_on_next_pick` — manager queues a message;
  `coord.tick()` triggers a fade-out; the picked message is the
  queued one.
- `test_queue_is_fifo` — manager queues three messages in arrival
  order; three sequential picks return them in the same order, with
  the queue empty after the third.
- `test_queue_takes_priority_over_recent_pool` — buffer has recent
  message `m1` AND queue has `m2`; pick returns `m2`, queue empties,
  next pick returns `m1` from the recent pool.
- `test_no_interruption_during_hold` — SMS arrives mid-hold;
  `fresh_id_interrupt` log does NOT fire; the hold completes naturally
  through `hold_seconds`; the next pick consumes the queued message.
- `test_no_interruption_in_background` — SMS arrives mid-background;
  `new_id` log does NOT fire; idle_seconds elapses; the next pick
  consumes the queued message.
- `test_drained_queue_falls_through_to_random_pool` — queue empty,
  recent pool has multiple messages; pick returns a body from the
  random pool via the existing `_pick_next` re-roll path.

Add to `tests/effects_coordinator_logging_test.py`:

- `test_no_fresh_id_interrupt_log` — drive a hold with a mid-hold
  arrival; caplog has zero `fresh_id_interrupt` / `Coordinator hold
  interrupt (new id)` records.
- `test_no_new_id_trigger_log` — drive a background with a mid-
  background arrival; caplog has zero `trigger=new_id` fade-out
  records.

Drop or invert these existing tests (they assert the interrupt
_behavior_, which is now removed):

- `test_fresh_id_interrupt_picks_before_fade_out` → invert to assert
  no fade-out during the hold (only after `hold_seconds`).
- `test_background_re_rolls_on_fresh_id` → invert to assert no fade-
  out during idle (only after `idle_seconds`).
- `test_hold_mode_interrupted_by_new_message` (effects_coordinator_test.py:351)
  — **directly contradicts the new design**. This test asserts
  `coord.mode == "out"` immediately after a new message arrives mid-
  hold. The new design is "no interruption ever." **Invert to assert
  the hold survives** (`mode == "hold"` immediately after arrival;
  `mode == "text_out"` only after `hold_seconds`).
- `test_hold_does_not_interrupt_on_random_picks_from_shown_set` →
  already asserts no interrupt on random re-picks; round 4 extends
  the contract to "no interrupt on ANY arrival during hold".

Update test infrastructure:

- **`_StubMessageManager` (tests/effects_coordinator_test.py:109)** —
  the stub's `add_message(view)` currently bypasses the queue (only
  writes to `_entries`). Production code writes to both buffer AND
  queue on each new message. Mirror the production flow so the
  tests exercise the new contract:

  ```python
  from collections import deque

  class _StubMessageManager:
      def __init__(self, messages=None, ...):
          ...
          self._entries = list(messages or [])
          self._new_messages_queue = deque()

      def add_message(self, view):
          self._entries.append(view)
          self._new_messages_queue.append(view)  # mirror production

      def take_next_new_message(self):
          try:
              return self._new_messages_queue.popleft()
          except IndexError:
              return None
  ```

  This is imported in tests/effects_coordinator_logging_test.py:45
  too — one definition covers both.

- **Empty-body queue-pop regression test** (new): when the queue
  drains an MMS-only message (`body=""` but `media=[…]`),
  `_pick_next` returns `""` (not `None`). The out→in branch's
  `if text:` check clears the scroller while
  `_maybe_build_media_cycler` constructs the cycler from
  `_last_picked_entry.message.media`. Verify this end-to-end with a
  queue-fed MMS-only entry.

Add to `tests/message_manager_test.py` (or a new file):

- `test_handle_message_appends_to_queue` — `dispatch(envelope)` →
  manager's queue has 1 entry.
- `test_take_next_new_message_returns_oldest` — three envelopes
  dispatched; three takes return them in order.
- `test_take_next_new_message_returns_none_on_empty` — queue empty
  after drains.
- `test_handle_config_does_not_queue` — `_handle_config` (line 467)
  hydrates the buffer but does NOT append to the queue; verify by
  dispatching a config envelope followed by a `take_next_new_message`
  that returns None.
- `test_seed_from_rest_does_not_queue` — `seed_from_messages` (REST
  hydration) does NOT append to the queue.

### Run the full suite

```
pytest tests/ -v
```

All existing tests pass with the inverted assertions, all new tests
pass.

### Live verification on the Pi

Deploy + restart + observe:

1. `sudo systemctl restart lindsay_50` after `git pull` on the Pi.
2. `journalctl -u lindsay_50 -f` on the laptop.
3. Send `Hey 1` to the Twilio number. Within 30s, `Hey 2`, `Hey 3`.
4. Watch the journal — `Hey 1` shows fully (1 cycle each), then `Hey 2`,
   then `Hey 3`, in order. No flap.
5. Send a long-media MMS, wait 2s, send a short-text SMS. The cycler
   plays out its full window before the text SMS lands on the sign.

## What this is NOT

This round is **not** a fix to the log noise. Round 4 also includes
separate, smaller log-shape cleanups (round-3 follow-ons) that landed
out-of-band before the queue work — selected-log now carries the
effect name in one line, `fade in done` and `hold→text_out` logs are
gone, `Scroller.set_text` is DEBUG not INFO. Those are already
committed as part of the round-4 small-cleanups commit and don't
touch the coordinator state machine.
