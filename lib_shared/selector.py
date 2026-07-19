"""Message selection algorithms (issue #26).

Defines a pluggable `MessageSelector` ABC plus two concrete
implementations:

  - `RandomSelector` — the historical rotation: uniform random pick
    from the candidate pool, after filtering out the coordinator's
    `exclude_id` (the just-consumed message — anti-repeat hint).
    Kept as the explicit operator opt-out: pass
    `RandomSelector()` to the `EffectsCoordinator(selector=...)`
    constructor to restore the historical rotation. The default
    since 2026-07-18 is `WeightedSelector` (see
    `USE_WEIGHTED_SELECTOR`), which solves the
    same-message-back-to-back symptom via `display_recency` rather
    than via the coordinator's anti-repeat hint.

  - `WeightedSelector` — additive weighted scoring on three components:
        score(message, now, event_log, favorites) =
            W_DISPLAY  * display_recency(message, now, event_log,
                                         current_event_type)
          + W_SEND     * send_recency(message, eligible_set, now)
          + W_FAVORITE * (1.0 if message.id in favorites else 0.0)

    Ships behind `USE_WEIGHTED_SELECTOR=False` so the historical
    rotation runs unchanged. Flip the constant (or pass an explicit
    `WeightedSelector()` instance to `EffectsCoordinator(...)`) to
    enable.

The `EffectsCoordinator` accepts any `MessageSelector` subclass as a
constructor kwarg — the coordinator itself is selector-agnostic. New
algorithms (e.g. round-robin, weighted-random, per-sender priority)
subclass `MessageSelector` and can be dropped in without coordinator
changes. The same Python class runs in both runtimes (PyScript on the
browser, CPython on the Pi); only the event-log source differs.

Only two operational values flow through `heart-matrix-controller/settings.toml`:

    EVENT_LOG_PATH          default "data/events.jsonl"
    EVENT_LOG_MAX_ENTRIES   default 100

These describe *where the artifact lives on disk*, not *how the
algorithm scores* — the weights, decay window, and eligibility window
are behavioral knobs and live as module-level constants in this file.
Operators who want to tune them edit this file and redeploy. This
mirrors the `TextSettings.MIN_SPEED/MAX_SPEED/DEFAULT_SPEED` pattern
in `lib_shared/models.py`.

Algorithm design notes (the `WeightedSelector`):

  - Eligibility filter: a message is eligible iff
    `now - received_at_epoch <= OFFSET_SECONDS`. The offset is checked
    against `received_at` (the message record), not the event log,
    because dormant older messages should stay dormant.

  - Display-recency (per event_type): for the most recent event in
    the log matching `(message.id, current_event_type)`, `display_recency`
    is `1.0` if none, else `min(1.0, (now - last_event.timestamp) /
    SATURATION_SECONDS)` — i.e. a just-shown message (age ≈ 0) sits
    out with `display_recency ≈ 0.0`, and `display_recency` grows
    linearly toward `1.0` as the message ages toward
    `SATURATION_SECONDS`. The semantic is "freshness FOR picking":
    long-ago or never-shown messages surface, recently-shown messages
    wait their turn. A `text_display` event does NOT reduce the
    `image_display` selector's display-recency for the same message.

  - Send-recency: normalized over the eligible set. Newest eligible
    message gets `1.0`, oldest gets `0.0`.

  - Pre-emption (Decision 7): the selector is NOT consulted for new
    envelopes; the renderer pushes new messages directly via the MQTT
    subscribe callback. A new SMS does NOT write a `text_display` event
    for itself — it pre-empts by virtue of being new, not by winning
    the weighted competition.

  - Tie-breaker: when two messages have identical scores, the selector
    picks deterministically by sorting on `(-score, received_at_epoch,
    message.id)`. Lower score first, then older message first, then
    lower message-id first.

  - Determinism: `pick()` is a pure function of `(messages, now,
    event_log)`. Same inputs always produce the same output.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Iterable, Optional

from lib_shared.models import Message

# ---------------------------------------------------------------------------
# Behavioral knobs for the WeightedSelector (code constants — see module
# docstring). Tunable by editing this file and redeploying. NOT in
# settings.toml, NOT on the Flask Settings page. The values below are the
# documented defaults and the tests in `tests/test_selector.py` import them
# by name and assert the literal defaults.
# ---------------------------------------------------------------------------

# Additive weights for the three score components. Defaults reflect the
# design's "never-shown recent wins, recently-shown sits out, favorites
# surface more often" intent. FAVORITE is additive (not a multiplier on
# SEND_RECENCY) so we can tune "how much do favorites dominate"
# independently from "how recent is recent".
W_DISPLAY: float = 0.6
W_SEND: float = 0.3
W_FAVORITE: float = 0.4

# Display-recency decay window. A message shown this many seconds ago
# gets display_recency ≈ 0.0 (sits out). Default 24 hours — long enough
# to avoid repeating the same message within a typical rotation cycle,
# short enough that a recently-shown message can resurface within a day.
SATURATION_SECONDS: float = 86_400.0

# Eligibility window. Messages with `received_at_epoch < now -
# OFFSET_SECONDS` are dormant and never appear in the eligible set.
# Default 14 days — long enough to keep a couple of weeks of recent
# activity eligible, short enough to suppress ancient messages.
OFFSET_SECONDS: float = 14 * 86_400.0  # 1,209,600 seconds = 14 days

# Default-selector rollout flag. When False, the call sites
# (`heart-matrix-controller/main.py`, `heart-message-manager/app_main.py`)
# instantiate `RandomSelector()` (the historical rotation). Flipping to
# True swaps in `WeightedSelector()` — same coordinator wiring, same
# event log, different pick algorithm. The flag is for the caller's
# default-selector pick; the coordinator itself is selector-agnostic
# and accepts any `MessageSelector` instance as a constructor kwarg.
#
# Flipped to True on 2026-07-18 in response to the "same message
# selected back-to-back" symptom observed in the browser preview:
# `WeightedSelector.display_recency` penalizes a just-shown message
# (`~0.0` immediately after `text_display` is logged, growing back
# toward `1.0` over `SATURATION_SECONDS`), so the rotation naturally
# avoids back-to-back repeats. `RandomSelector` had no such
# mechanism — the coordinator's anti-repeat hint (`exclude_id`) is
# the defense-in-depth shim, but the weighted algorithm is the
# designed fix. `RandomSelector` stays in the file (Default-to-X
# rule: don't delete alternatives when switching the default) — pass
# `RandomSelector()` to `EffectsCoordinator(selector=...)` to
# restore the historical rotation.
USE_WEIGHTED_SELECTOR: bool = True


class MessageSelector(ABC):
    """Abstract base for message selection algorithms.

    A `MessageSelector` picks the next `Message` to display from a pool
    of candidates. Subclasses encapsulate the algorithm — random pick,
    weighted score, round-robin, etc. The `EffectsCoordinator` accepts
    any subclass via its `selector` kwarg; swapping algorithms is a
    one-line change at the call site (or flip `USE_WEIGHTED_SELECTOR`
    in this module to switch defaults).

    Contract:
      - `pick()` MUST be pure with respect to its arguments for any
        subclass that advertises determinism (e.g. `WeightedSelector`).
        Subclasses that are inherently non-deterministic (e.g.
        `RandomSelector`) MUST document the non-determinism.
      - `pick()` MUST return `None` when the candidate pool is empty
        (or when eligibility filtering yields no candidates). The
        coordinator treats `None` as "rotation pauses; pre-emption is
        unaffected".
      - `pick()` MUST NOT mutate its inputs. The coordinator and the
        call sites depend on the candidate list being preserved across
        calls so the head-of-list fresh-id check still works.
      - `pick()` MUST honor `exclude_id` when supplied: the message
        whose id matches `exclude_id` MUST NOT be returned unless
        every candidate matches (the pool would otherwise be empty
        and the sign would go dark).
    """

    @abstractmethod
    def pick(
        self,
        messages: Iterable[Message],
        now: float,
        event_log: object | None = None,
        favorites: Optional[Iterable[str]] = None,
        event_type: Optional[str] = None,
        exclude_id: Optional[str] = None,
    ) -> Optional[Message]:
        """Pick the next message to display.

        Args:
            messages: Iterable of `Message` candidates (typically
                `message_manager.get_messages(...)` results, already
                filtered by the renderer's `recent_count`). Order does
                NOT matter — selectors sort internally as needed.
            now: Epoch seconds (float) representing the current time.
                Passed in rather than read from `time.time()` so tests
                can pin a clock.
            event_log: Optional event log for selectors that need
                display-recency (e.g. `WeightedSelector`). Selectors
                that don't use it (e.g. `RandomSelector`) ignore the
                argument. Matches both `EventLog` (Pi) and
                `IndexedDBEventLog` (browser) — both expose
                `last_for(message_id, event_type)` and
                `query(event_type, message_id, since)`.
            favorites: Optional iterable of message IDs that are
                favorites. Selectors that don't surface a favorite
                boost (e.g. `RandomSelector`) ignore this.
            event_type: Optional event_type discriminator (e.g.
                `"text_display"`) for selectors that need
                per-event-type display-recency. Selectors that don't
                (e.g. `RandomSelector`) ignore this.
            exclude_id: Optional message id to drop from the
                candidate pool before picking. The coordinator passes
                the message it just consumed at the previous out→in
                transition so the next pick avoids back-to-back
                selection of the same message. When the exclusion
                would leave an empty pool, the unfiltered pool is
                used — the sign must not go dark just because the
                currently-rendered message is the only one available.

        Returns:
            The next `Message` to display, or `None` when the eligible
            set is empty.
        """


class RandomSelector(MessageSelector):
    """Uniformly-random pick from the candidates.

    Mirrors the historical rotation: the previous
    `random.choice(entries)` call in `EffectsCoordinator.get_display_message()`.
    Use this when no behavior change is desired — it's the default
    `USE_WEIGHTED_SELECTOR=False` selector.

    Non-deterministic — same inputs may return different outputs across
    calls. Tests that need determinism should seed `random.seed(...)`
    before driving the selector, or use `WeightedSelector` instead.

    Honors `exclude_id` by dropping that id from the candidate pool
    before the random pick. The exclusion is the coordinator's
    anti-repeat hint — the next pick should avoid the message that
    was just consumed. When exclusion would empty the pool, the
    unfiltered pool is used so the sign keeps rotating instead of
    pausing on the only-available message.
    """

    def pick(
        self,
        messages: Iterable[Message],
        now: float,
        event_log: object | None = None,
        favorites: Optional[Iterable[str]] = None,
        event_type: Optional[str] = None,
        exclude_id: Optional[str] = None,
    ) -> Optional[Message]:
        """Pick uniformly at random. Ignores `now`, `event_log`,
        `favorites`, and `event_type` — pure random.choice over the
        candidate pool, after `exclude_id` is filtered out.

        Falls back to the unfiltered pool when `exclude_id` would
        leave an empty pool — the sign must not go dark just because
        the currently-rendered message is the only candidate.
        """
        del now, event_log, favorites, event_type
        candidates = list(messages)
        if not candidates:
            return None
        if exclude_id is not None:
            filtered = [m for m in candidates if m.id != exclude_id]
            candidates = filtered or candidates
        return random.choice(candidates)


class WeightedSelector(MessageSelector):
    """Deterministic weighted selector for the next message to display (issue #26).

    Algorithm (see module docstring for the full rationale):

      1. Filter to the eligible set: `now - received_at_epoch <=
         OFFSET_SECONDS`.
      2. For each candidate, compute `display_recency` from the event
         log (per the current pattern's `event_type`), `send_recency`
         normalized over the eligible set, and the favorite boost.
      3. Score = `W_DISPLAY * display_recency + W_SEND * send_recency
                 + W_FAVORITE * (1.0 if message.id in favorites else 0.0)`.
      4. Pick the highest score, breaking ties on `(-score,
         received_at_epoch, id)`.

    Args:
        event_type: The default `current_event_type` discriminator
            (e.g. `"text_display"`). The selector filters events by
            this so a `text_display` event does NOT reduce the
            `image_display` selector's display-recency for the same
            message (Decision 2). The per-call `event_type` kwarg
            overrides this for tests and runtime pattern switches.
        favorites: Optional iterable of message IDs that are favorites.
            Read at pick time — the selector does not cache this set,
            so callers can update the favorites list between picks
            without reconstructing the selector. Defaults to an empty
            iterable (no favorites).
    """

    def __init__(
        self,
        event_type: str = "text_display",
        favorites: Optional[Iterable[str]] = None,
    ) -> None:
        """Initialize the selector.

        Args:
            event_type: Default event_type discriminator (see class
                docstring). Per-call `event_type` overrides this.
            favorites: Iterable of message IDs that are favorites. Read
                at pick time, so callers can update the list between
                picks. Defaults to no favorites.
        """
        self._event_type = event_type
        self._favorites = frozenset(favorites or ())

    def pick(
        self,
        messages: Iterable[Message],
        now: float,
        event_log: object | None = None,
        favorites: Optional[Iterable[str]] = None,
        event_type: Optional[str] = None,
        exclude_id: Optional[str] = None,
    ) -> Optional[Message]:
        """Pick the next message to display by weighted score.

        Args:
            messages: Iterable of `Message` candidates.
            now: Epoch seconds (float).
            event_log: An object exposing `last_for(message_id,
                event_type) -> dict | None` and (optionally)
                `query(event_type, message_id, since)`. Missing events
                count as never-shown (`display_recency = 1.0`).
            event_type: Override the constructor's `event_type` for
                this call only.
            favorites: Override the constructor's favorites list for
                this call only.
            exclude_id: Optional message id to drop from the
                candidate pool before scoring — the coordinator's
                anti-repeat hint. Defensive double-check on top of
                the `display_recency` component (which already
                penalizes just-shown messages). When exclusion would
                leave an empty pool, the unfiltered eligible set is
                used so the sign keeps rotating.

        Returns:
            The next `Message` to display, or `None` when the eligible
            set is empty.
        """
        current_event_type = event_type if event_type is not None else self._event_type

        candidates = [m for m in messages if self._is_eligible(m, now)]
        if not candidates:
            return None
        if exclude_id is not None:
            filtered = [m for m in candidates if m.id != exclude_id]
            candidates = filtered or candidates

        if len(candidates) == 1:
            return candidates[0]

        fav_set = frozenset(favorites) if favorites is not None else self._favorites

        # Send-recency is normalized over the eligible set. The oldest
        # eligible message gets 0.0; the newest gets 1.0.
        oldest_received = min(candidates, key=lambda m: m.received_at_epoch()).received_at_epoch()
        newest_received = max(candidates, key=lambda m: m.received_at_epoch()).received_at_epoch()
        span = newest_received - oldest_received

        scored: list[tuple[float, float, str, Message]] = []
        for m in candidates:
            disp = self._display_recency(m, now, event_log, current_event_type)
            send = self._send_recency(m, oldest_received, span)
            fav = 1.0 if m.id in fav_set else 0.0
            score = W_DISPLAY * disp + W_SEND * send + W_FAVORITE * fav
            scored.append((score, m.received_at_epoch(), m.id, m))

        # Tie-breaker: with key `(-score, received_at_epoch, id)`,
        # the natural ascending tuple order places the lowest -score
        # (= highest actual score) first, then the oldest received
        # message, then the lowest `message.id`. We use `min` so the
        # returned element is the first one in that ascending order —
        # the documented tie-breaker.
        best = min(scored, key=lambda t: (-t[0], t[1], t[2]))
        return best[3]

    # --- internals ---

    @staticmethod
    def _is_eligible(message: Message, now: float) -> bool:
        """True iff the message is within the eligibility window.

        The window is checked against `received_at_epoch`, NOT against
        the event log. A message older than `now - OFFSET_SECONDS` is
        dormant — it does not appear in the eligible set even if never
        shown.
        """
        try:
            received_at = message.received_at_epoch()
        except Exception:
            # If the message lacks a parseable timestamp, treat it as
            # ineligible rather than crashing the selector. Messages
            # in this state are an upstream bug; the rotation pauses
            # until they're cleared.
            return False
        return (now - received_at) <= OFFSET_SECONDS

    @staticmethod
    def _display_recency(
        message: Message,
        now: float,
        event_log,
        current_event_type: str,
    ) -> float:
        """Compute the display-recency component (0..1, 1 = never shown or long ago).

        Reads the most recent event in the log matching `(message.id,
        current_event_type)`. A never-shown message gets 1.0. A message
        shown recently gets a low value that grows toward 1.0 as the
        message ages toward `SATURATION_SECONDS` — the semantic is
        "freshness FOR picking" (never-shown or long-ago surfaces;
        just-shown sits out).

        A `text_display` event does NOT reduce the `image_display`
        selector's display-recency for the same message — we filter
        strictly by `current_event_type`.

        Defensive against log backends missing `last_for`: fall back to
        a linear scan via `query(...)`.
        """
        if event_log is None:
            return 1.0
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
            return 0.0
        if age >= SATURATION_SECONDS:
            return 1.0
        return min(1.0, age / SATURATION_SECONDS)

    @staticmethod
    def _send_recency(message: Message, oldest_received: float, span: float) -> float:
        """Compute the send-recency component (0..1, 1 = newest eligible).

        Normalized over the eligible set. The oldest eligible message
        gets 0.0; the newest gets 1.0. When all eligible messages
        share the same `received_at_epoch` (span == 0), every message
        gets 1.0 — there's no recency signal to honor.
        """
        if span <= 0.0:
            return 1.0
        return (message.received_at_epoch() - oldest_received) / span
