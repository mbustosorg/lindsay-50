"""Deterministic weighted message selector (issue #26).

Picks the next message to display by combining three additive components:

    score(message, now, event_log, favorites) =
        W_DISPLAY  * display_recency(message, now, event_log, current_event_type)
      + W_SEND     * send_recency(message, eligible_set, now)
      + W_FAVORITE * (1.0 if message.id in favorites else 0.0)

The selector is consumed by both the Pi's renderer and the browser preview;
the same Python class runs in both runtimes (PyScript on the browser side,
CPython on the Pi). Only the event-log source differs — `EventLog` on the
Pi (JSONL), `IndexedDBEventLog` in the browser — both expose the same
`query(event_type, message_id, since)` / `last_for(message_id, event_type)`
surface so this module is backend-agnostic.

Behavioral knobs (`W_DISPLAY`, `W_SEND`, `W_FAVORITE`, `SATURATION_SECONDS`,
`OFFSET_SECONDS`, `USE_WEIGHTED_SELECTOR`) live as module-level constants
right below. They are NOT in `settings.toml` and NOT on the Flask Settings
page — operators who want to tune them edit this file and redeploy. This
mirrors the `TextSettings.MIN_SPEED/MAX_SPEED/DEFAULT_SPEED` pattern in
`lib_shared/models.py`: behavioral knobs are code constants, settings.toml
holds per-deployment operational values (paths, capacities, broker hosts).

Only two operational values for the event log flow through
`heart-matrix-controller/settings.toml`:

    EVENT_LOG_PATH          default "data/events.jsonl"
    EVENT_LOG_MAX_ENTRIES   default 100

These describe *where the artifact lives on disk*, not *how the algorithm
scores* — that's the per-Pi / per-deployment variance that belongs in
settings.toml.

Eligibility filter (Decision 4): a message is eligible iff
`now - sent_at <= OFFSET_SECONDS`. The offset is checked against `sent_at`
(not the event log) because dormant older messages should stay dormant.

Display-recency (Decision 2): for the most recent event in the log matching
`(message.id, current_event_type)`, `display_recency` is `1.0` if no
matching event exists, else `max(0.0, 1.0 - (now - last_event.timestamp) /
SATURATION_SECONDS)`. Per-event-type — a `text_display` event does not
reduce the `image_display` selector's display-recency for the same message.

Send-recency (Decision 3): normalized over the eligible set. The newest
eligible message gets `1.0`, the oldest gets `0.0`.

Pre-emption (Decision 7): the selector is NOT consulted for new envelopes;
the renderer pushes new messages directly via the MQTT subscribe callback.
A new SMS does NOT write a `text_display` event for itself — it pre-empts
by virtue of being new, not by winning the weighted competition.

Tie-breaker (Decision 6): when two messages have identical scores, the
selector picks deterministically by sorting on `(-score, sent_at, message.id)`.
Lower score first, then older message first, then lower message-id first.
This guarantees the same input set always yields the same output.

Determinism: `pick()` is a pure function of `(messages, now, event_log,
current_event_type, favorites)`. Same inputs always produce the same output
— important for unit tests and for the dashboard preview agreeing with itself.
"""

from __future__ import annotations

from typing import Iterable, Optional

from lib_shared.models import Message

# ---------------------------------------------------------------------------
# Behavioral knobs (code constants — see module docstring).
#
# Tunable by editing this file and redeploying. NOT in settings.toml, NOT
# on the Flask Settings page. The values below are the documented defaults
# and the tests in `tests/test_selector.py` import them by name and assert
# the literal defaults — see spec scenario "Constants are importable with
# documented defaults".
# ---------------------------------------------------------------------------

# Additive weights for the three score components. Defaults reflect the
# design's "never-shown recent wins, recently-shown sits out, favorites
# surface more often" intent. FAVORITE is additive (not a multiplier on
# SEND_RECENCY) so we can tune "how much do favorites dominate"
# independently from "how recent is recent" — see Decision 1.
W_DISPLAY: float = 0.6
W_SEND: float = 0.3
W_FAVORITE: float = 0.4

# Display-recency decay window. A message shown this many seconds ago
# gets display_recency ≈ 0.0 (sits out). A message shown half this long
# ago gets ≈ 0.5. Default 24 hours — long enough to avoid repeating the
# same message within a typical rotation cycle, short enough that a
# recently-shown message can resurface within a day.
SATURATION_SECONDS: float = 86_400.0

# Eligibility window. Messages with `sent_at < now - OFFSET_SECONDS` are
# dormant and never appear in the eligible set. Default 14 days — long
# enough to keep a couple of weeks of recent activity eligible, short
# enough to suppress ancient messages that would otherwise compete.
OFFSET_SECONDS: float = 14 * 86_400.0  # 1,209,600 seconds = 14 days

# Rollout flag. When False, the selector is bypassed entirely at the call
# site (see `heart-matrix-controller/main.py`) — the existing first-in /
# first-out rotation runs unchanged. Flipping this to True and redeploying
# enables the new selector. The selector's own `pick()` does NOT check this
# flag (so tests can exercise the algorithm in isolation); the flag is a
# code-level switch in the caller.
USE_WEIGHTED_SELECTOR: bool = False


class MessageSelector:
    """Deterministic weighted selector for the next message to display.

    Algorithm (see module docstring for the full rationale):

      1. Filter to the eligible set: `now - sent_at <= OFFSET_SECONDS`.
      2. For each candidate, compute `display_recency` from the event log
         (per the current pattern's `event_type`), `send_recency` normalized
         over the eligible set, and the favorite boost.
      3. Score = `W_DISPLAY * display_recency + W_SEND * send_recency
                 + W_FAVORITE * (1.0 if message.id in favorites else 0.0)`.
      4. Pick the highest score, breaking ties on `(-score, sent_at, id)`.

    Args:
        favorites: Optional iterable of message IDs that are favorites.
            Read at pick time — the selector does not cache this set, so
            callers can update the favorites list between picks without
            needing to reconstruct the selector. Defaults to an empty
            iterable (no favorites).
    """

    def __init__(self, favorites: Optional[Iterable[str]] = None) -> None:
        """Initialize the selector.

        Args:
            favorites: Iterable of message IDs that are favorites. Read
                at pick time, so callers can update the list between
                picks. Defaults to no favorites.
        """
        self._favorites = frozenset(favorites or ())

    def pick(
        self,
        messages: Iterable[Message],
        now: float,
        event_log,
        current_event_type: str = "text_display",
        favorites: Optional[Iterable[str]] = None,
    ) -> Optional[Message]:
        """Pick the next message to display, or None if no candidate is eligible.

        Args:
            messages: Iterable of `Message` candidates (typically
                `message_manager.get_messages(...)` results, already
                filtered by the renderer's `recent_count`). Order does
                NOT matter — the selector sorts internally.
            now: Epoch seconds (float) representing the current time.
                Passed in rather than read from `time.time()` so tests
                can pin a clock.
            event_log: An object exposing `last_for(message_id,
                event_type) -> dict | None` and (optionally)
                `query(event_type, message_id, since)` — matches both
                the Pi-side `EventLog` and the browser-side
                `IndexedDBEventLog`. Missing events count as never-shown
                (`display_recency = 1.0`).
            current_event_type: The event_type discriminator for the
                renderer's current pattern (e.g. `"text_display"`). The
                selector filters events by this so an `image_display`
                event does NOT reduce the `text_display` selector's
                display-recency for the same message (Decision 2).
            favorites: Optional iterable of message IDs that are
                favorites. Overrides the constructor's favorites list
                for this call only (lets tests inject per-call favorites
                without reconstructing the selector).

        Returns:
            The next `Message` to display, or None when the eligible
            set is empty. The rotation pauses on None; pre-emption is
            unaffected (see module docstring).
        """
        candidates = [m for m in messages if self._is_eligible(m, now)]
        if not candidates:
            return None

        if len(candidates) == 1:
            return candidates[0]

        fav_set = frozenset(favorites) if favorites is not None else self._favorites

        # Send-recency is normalized over the eligible set. The oldest
        # eligible message gets 0.0; the newest gets 1.0.
        oldest_sent = min(candidates, key=lambda m: m.sent_at_epoch()).sent_at_epoch()
        newest_sent = max(candidates, key=lambda m: m.sent_at_epoch()).sent_at_epoch()
        span = newest_sent - oldest_sent

        scored: list[tuple[float, float, str, Message]] = []
        for m in candidates:
            disp = self._display_recency(m, now, event_log, current_event_type)
            send = self._send_recency(m, oldest_sent, span)
            fav = 1.0 if m.id in fav_set else 0.0
            score = W_DISPLAY * disp + W_SEND * send + W_FAVORITE * fav
            # Sort key: -score (ascending), then sent_at ascending
            # (older first), then message.id ascending. Returns the
            # natural Python tuple order so `max(..., key=...)` picks
            # the highest-scoring candidate with the documented tie
            # breakers.
            scored.append((score, m.sent_at_epoch(), m.id, m))

        # The "highest score" pick: with key `(-score, sent_at, id)`,
        # the natural ascending tuple order places the lowest -score
        # (= highest actual score) first, then the oldest `sent_at`,
        # then the lowest `message.id`. We use `min` so the returned
        # element is the first one in that ascending order — the
        # documented tie-breaker.
        best = min(scored, key=lambda t: (-t[0], t[1], t[2]))
        return best[3]

    # --- internals ---

    @staticmethod
    def _is_eligible(message: Message, now: float) -> bool:
        """True iff the message is within the eligibility window.

        The window is checked against `sent_at`, NOT against the event
        log (Decision 4). A message older than `now - OFFSET_SECONDS`
        is dormant — it does not appear in the eligible set even if
        never shown.
        """
        try:
            sent_at = message.sent_at_epoch()
        except Exception:
            # If the message lacks a parseable timestamp, treat it as
            # ineligible rather than crashing the selector. Messages
            # in this state are an upstream bug; the rotation pauses
            # until they're cleared.
            return False
        return (now - sent_at) <= OFFSET_SECONDS

    @staticmethod
    def _display_recency(
        message: Message,
        now: float,
        event_log,
        current_event_type: str,
    ) -> float:
        """Compute the display-recency component (0..1, 1 = never shown).

        Reads the most recent event in the log matching `(message.id,
        current_event_type)`. A never-shown message gets 1.0. A message
        shown recently gets a low value that decays toward 0.0 over
        `SATURATION_SECONDS`.

        Per Decision 2: a `text_display` event does NOT reduce the
        display-recency seen by an `image_display` selector — we filter
        strictly by `current_event_type`.

        Defensive against log backends missing `last_for`: fall back to
        a linear scan via `query(...)`.
        """
        last = None
        try:
            if hasattr(event_log, "last_for"):
                last = event_log.last_for(message.id, current_event_type)
            elif hasattr(event_log, "query"):
                matching = list(event_log.query(event_type=current_event_type, message_id=message.id))
                last = matching[-1] if matching else None
        except Exception:
            # A buggy or missing event log should not crash the
            # selector; treat as never-shown.
            last = None
        if last is None:
            return 1.0
        try:
            last_ts = float(last.get("timestamp", 0.0))
        except (TypeError, ValueError):
            return 1.0
        if last_ts <= 0.0:
            return 1.0
        age = now - last_ts
        if age <= 0.0:
            return 1.0
        if age >= SATURATION_SECONDS:
            return 0.0
        return max(0.0, 1.0 - (age / SATURATION_SECONDS))

    @staticmethod
    def _send_recency(message: Message, oldest_sent: float, span: float) -> float:
        """Compute the send-recency component (0..1, 1 = newest eligible).

        Normalized over the eligible set (Decision 3). The oldest
        eligible message gets 0.0; the newest gets 1.0. When all
        eligible messages share the same `sent_at` (span == 0), every
        message gets 1.0 — there's no recency signal to honor.
        """
        if span <= 0.0:
            return 1.0
        return (message.sent_at_epoch() - oldest_sent) / span
