"""Message selection algorithms (issue #26).

Defines a pluggable `MessageSelector` ABC plus two concrete
implementations:

  - `RandomSelector` — the historical rotation: uniform random pick
    from the candidate pool, after filtering out the coordinator's
    `exclude_id` (the just-consumed message — anti-repeat hint).
    Operators pick it via the `selector_algorithm` config field
    (admin UI: "Selection algorithm"). Kept in the file as the
    explicit operator opt-out (Default-to-X-keeps-Y rule).

  - `WeightedSelector` — additive weighted scoring on three components:
        score(message, now, event_log, favorites) =
            W_DISPLAY  * display_recency(message, now, event_log,
                                         current_event_type)
          + W_SEND     * send_recency(message, eligible_set, now)
          + W_FAVORITE * (1.0 if message.id in favorites else 0.0)

    The default selection algorithm — `selector_algorithm="weighted"`.
    Solves the same-message-back-to-back symptom via `display_recency`
    rather than via the coordinator's anti-repeat hint.

The `EffectsCoordinator` accepts any `MessageSelector` subclass as a
constructor kwarg (`selector=`) and calls `make_selector(algorithm)` to
resolve the live-config default. New algorithms (round-robin,
weighted-random, per-sender priority) subclass `MessageSelector`,
register an entry in `VALID_SELECTOR_ALGORITHMS`, and can be selected
via the admin UI without coordinator changes. The same Python class
runs in both runtimes (PyScript on the browser, CPython on the Pi);
only the event-log source differs.

Configuration fields (operator-tunable; NOT code constants):

    effects_settings.selector_algorithm   "weighted" (default) | "random"
    effects_settings.lookback_days        14 (default; 1..365)

`selector_algorithm` picks the algorithm. `lookback_days` is the
eligibility window: messages older than `lookback_days` are filtered
out of the candidate pool by `build_eligible_messages`. Both fields
are surfaced on the admin /settings page; both apply uniformly to
all selection algorithms (per the user's "single shared candidate
pool" design — the coordinator builds the eligible set once and
hands it to whichever selector `make_selector` returns).

Behavioral knobs (scoring weights, display-recency decay window)
remain module-level constants alongside the algorithm. Per the
project's behavioral-knobs-in-code rule — operators tune these by
editing this file and redeploying, not via settings.toml. Only the
two values above (`selector_algorithm`, `lookback_days`) cross the
admin-UI boundary, because they describe WHAT pool to pick from and
WHICH algorithm to run, not HOW the algorithm scores.

Algorithm design notes (the `WeightedSelector`):

  - Eligibility filter: a message is eligible iff
    `now - received_at_epoch <= lookback_days * 86_400.0`. The
    eligibility check happens in `build_eligible_messages` BEFORE
    `pick()` is called — selectors receive a pre-filtered pool and
    can assume every entry is within `lookback_seconds` of `now`.
    The filter is checked against `received_at` (the message
    record), not the event log, because dormant older messages
    should stay dormant.

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
# Selector registry (issue #26). The admin UI surfaces this list as a
# dropdown; `make_selector(algorithm)` resolves a string to a concrete
# `MessageSelector` instance. Adding a new algorithm = add an entry here
# + register its class in `make_selector`. Coordinator is selector-agnostic.
# ---------------------------------------------------------------------------

VALID_SELECTOR_ALGORITHMS: tuple[str, ...] = ("weighted", "random")
DEFAULT_SELECTOR_ALGORITHM: str = "weighted"


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


def build_eligible_messages(
    message_manager,
    *,
    now: float,
    lookback_seconds: float,
) -> list[Message]:
    """Return every unsuppressed message in the manager whose
    `received_at` is within `lookback_seconds` of `now`.

    Single source of truth for the eligibility filter — used by
    `EffectsCoordinator._pick_message_via_selector` before delegating
    to the configured selector. Both `WeightedSelector` and
    `RandomSelector` receive this pre-filtered pool; neither runs its
    own eligibility check. Centralizing the filter here means:

      1. No selector does wasted work on messages that get thrown
         out — the "select over and over just to throw it out"
         anti-pattern.
      2. Adding a new selector subclass doesn't require duplicating
         the eligibility rule.
      3. Operators tune the window via the `lookback_days` config
         field; the same value applies uniformly across all
         selection algorithms.

    Runs every pick (not cached) because the candidate set is a live
    snapshot of a live buffer: new MQTT arrivals, deque rotation at
    `maxlen=100`, and filter-rule edits all mutate the eligible set
    between picks without firing any explicit "settings changed"
    event. The cost is ~100 epoch comparisons per pick — sub-ms in
    CPython, negligible against the selector's downstream work.

    Args:
        message_manager: A `MessageManager` (or duck-typed equivalent)
            exposing `get_messages(limit=None, suppress=True) -> list[MessageView]`.
        now: Epoch seconds (float) representing the current time.
        lookback_seconds: Messages with `received_at_epoch < now -
            lookback_seconds` are excluded.

    Returns:
        A list of `Message` instances, sorted newest-first (the
        manager's `get_messages` already enforces the order).
        Returns an empty list when no message qualifies or the
        manager holds nothing.
    """
    if lookback_seconds <= 0.0:
        return []
    cutoff = now - lookback_seconds
    entries = message_manager.get_messages(limit=None, suppress=True)
    eligible: list[Message] = []
    for entry in entries:
        try:
            received_at = entry.message.received_at_epoch()
        except Exception:
            # A malformed received_at should not crash the picker —
            # the rotation pauses on None rather than raising.
            continue
        if received_at <= 0.0:
            # `received_at_epoch()` returns 0.0 on parse failure;
            # treat as ineligible (same defensive contract
            # `WeightedSelector._is_eligible` had pre-refactor).
            continue
        if received_at >= cutoff:
            eligible.append(entry.message)
    return eligible


def make_selector(algorithm: str) -> MessageSelector:
    """Resolve a `selector_algorithm` config string to a fresh selector instance.

    The coordinator calls this at every pick (`make_selector` is cheap —
    `WeightedSelector.__init__` is two attribute writes). Live config
    edits to `selector_algorithm` land on the next pick without
    requiring a coordinator rebuild.

    Args:
        algorithm: One of `VALID_SELECTOR_ALGORITHMS`. Strict case —
            "weighted" / "random" lowercase only.

    Returns:
        A fresh `MessageSelector` instance (the call site owns the
        lifecycle; the coordinator does not cache the instance).

    Raises:
        ValueError: When `algorithm` is not in `VALID_SELECTOR_ALGORITHMS`.
            Callers (the coordinator's `_pick_message_via_selector`) catch
            and translate to a "selector unavailable" log line — the
            rotation pauses rather than crashing.
    """
    if algorithm == "weighted":
        return WeightedSelector()
    if algorithm == "random":
        return RandomSelector()
    raise ValueError(f"unknown selector_algorithm {algorithm!r}; must be one of {VALID_SELECTOR_ALGORITHMS}")


class MessageSelector(ABC):
    """Abstract base for message selection algorithms.

    A `MessageSelector` picks the next `Message` to display from a pool
    of candidates. Subclasses encapsulate the algorithm — random pick,
    weighted score, round-robin, etc. The `EffectsCoordinator` accepts
    any subclass via its `selector` kwarg; the default is resolved
    from `effects_settings.selector_algorithm` via `make_selector(...)`
    on every pick.

    Contract:
      - `pick()` MUST be pure with respect to its arguments for any
        subclass that advertises determinism (e.g. `WeightedSelector`).
        Subclasses that are inherently non-deterministic (e.g.
        `RandomSelector`) MUST document the non-determinism.
      - `pick()` MUST return `None` when the candidate pool is empty.
        The coordinator treats `None` as "rotation pauses; pre-emption
        is unaffected".
      - `pick()` MUST NOT mutate its inputs.
      - `pick()` MUST honor `exclude_id` when supplied: the message
        whose id matches `exclude_id` MUST NOT be returned unless
        every candidate matches (the pool would otherwise be empty
        and the sign would go dark).
      - The `messages` iterable passed in is the pre-filtered eligible
        set — `build_eligible_messages` already filtered by
        `received_at`, suppressed, and capped at the ring-buffer's
        maxlen. Selectors can assume every entry is within
        `lookback_seconds` of `now`. No per-selector eligibility check
        is needed.
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
            messages: Iterable of pre-filtered `Message` candidates
                (eligible set, built by `build_eligible_messages`).
                Order does NOT matter — selectors sort internally as
                needed.
            now: Epoch seconds (float) representing the current time.
                Passed in rather than read from `time.time()` so tests
                can pin a clock.
            event_log: Optional event log for selectors that need
                display-recency (e.g. `WeightedSelector`). Selectors
                that don't use it (e.g. `RandomSelector`) ignore the
                argument. Matches both `EventLog` (Pi, JSONL-backed)
                and `EventLog` (browser, deque-backed) — both expose
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
    """Uniformly-random pick from the eligible candidate pool.

    Mirrors the historical rotation: the previous
    `random.choice(entries)` call in `EffectsCoordinator.get_display_message()`.
    Use this when no behavior change is desired — the operator selects
    it via the `selector_algorithm="random"` config field.

    Non-deterministic — same inputs may return different outputs across
    calls. Tests that need determinism should seed `random.seed(...)`
    before driving the selector, or use `WeightedSelector` instead.

    Honors `exclude_id` by dropping that id from the candidate pool
    before the random pick. The exclusion is the coordinator's
    anti-repeat hint — the next pick should avoid the message that
    was just consumed. When exclusion would empty the pool, the
    unfiltered pool is used so the sign keeps rotating instead of
    pausing on the only-available message.

    Receives a pre-filtered eligible set from `build_eligible_messages`
    — does NOT run its own eligibility check (per the shared-pool
    design: the coordinator filters once, every selector operates on
    the filtered result).
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

      1. The coordinator's `build_eligible_messages` already filtered
         the candidate pool to messages within `lookback_seconds` of
         `now` — `pick()` operates on the pre-filtered set.
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
            messages: Iterable of pre-filtered `Message` candidates.
                Every entry is within `lookback_seconds` of `now`
                (the coordinator's `build_eligible_messages` already
                filtered).
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

        candidates = [m for m in messages if m.received_at_epoch() > 0.0]
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
