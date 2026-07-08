"""Shared coordinator for the lifecycle state machine on the Pi and in the browser.

Both the Pi device (`heart-matrix-controller/main.py`) and the browser preview
(`heart-message-manager/preview_main.py`) drive the same six-mode fade state
machine for the sign's lifecycle:

    intro    — a beating heart for `intro_seconds`, no text.
    out      — cross-fade the current effect + any text to black.
    in       — fade the next effect (+ message text) up.
    hold     — keep a message fully visible for `hold_seconds`.
    text_out — fade only the text out, leaving the background effect lit.
    background — just the effect, no text, until the next message or idle.

The composite step (clear canvas, draw effect, draw scroller, swap to panel)
is polymorphism on the `display` (a `DisplayBase` subclass), not the
coordinator — `tick()` ends with one line: `self.display.render(...)`.

The render layer is OPTIONAL: an unbound coordinator (`bind(...)` not yet
called) is the shape `app_main.py` instantiates at PyScript startup. The
preview page's `preview_main.py` is the per-page shim that calls
`bind(display, scroller, effects, heart)` once the page-local canvas +
scroller + effects are constructed. `tick()` is a no-op when the
coordinator has no render layer (the app is alive on every admin page
but the canvas only exists on /preview).

The coordinator is the consumer; `MessageManager` is the source of truth.
`message_manager` is a required constructor argument. `tick()` throttles a
~4 Hz pull from `manager.messages.get_messages(...)` and the cached pull
result becomes the next text shown by the fade state machine.

Pacing comes from an `EffectsSettings` block (the v2 config shape),
passed as `settings` (an `EffectsSettings` instance or `None` for
the historic per-kwarg defaults).
"""

import logging
import random
import time

from lib_shared.display_base import DisplayBase
from lib_shared.effect_base import Effect
from lib_shared.message_manager import MessageManager
from lib_shared.models import MessageView, EffectsSettings, TextSettings
from lib_shared.scroller_base import ScrollerBase

log = logging.getLogger("heart")


def build_effects(
    effects_settings: EffectsSettings,
    effect_class_factory=None,
    *,
    display=None,
) -> list:
    """Build the effects rotation from an `EffectsSettings` block.

    Iterates `effects_settings.effects` in declared order. For each
    enabled entry, calls `effect_class_factory(name)` to resolve the
    Effect class, then instantiates it with `display` (every Effect
    constructor takes the display as its first positional arg).
    Disabled entries are skipped. Names the factory doesn't
    recognize are skipped silently (already logged inside the
    factory).

    If the resulting rotation is empty (every entry disabled or every
    name unknown), falls back to the first canonical effect from the
    declared rotation order so the sign never goes dark. The fallback
    is deterministic — same config always picks the same fallback —
    so the sign's idle behavior is predictable across reloads. If
    even the fallback is unresolvable (every canonical name is
    unknown to the factory), the rotation stays empty; callers that
    need a non-empty list at any cost should provide their own
    effects.

    Args:
        effects_settings: The v2 `EffectsSettings` block from `SignConfig`,
            or None (returns [] — there's no settings to derive a
            fallback from).
        effect_class_factory: Callable `name -> type | None`. Defaults
            to `lib_shared.effects_factory.make_effect_class`. Tests
            can pass a stub that returns simple effect classes.
        display: The display object handed to each Effect's constructor.
            Required when `effects_settings` is not None — every Effect
            subclass needs a display.

    Returns:
        A list of instantiated Effect objects in the order
        declared in `effects_settings.effects`. Enabled effects
        only. Empty when `effects_settings is None` or when the
        fallback is also unresolvable.
    """
    if effects_settings is None:
        return []
    if effect_class_factory is None:
        from lib_shared.effects_factory import make_effect_class

        effect_class_factory = make_effect_class
    if display is None:
        raise ValueError("build_effects requires `display` to instantiate effects")
    out = []
    for entry in effects_settings.effects:
        if not entry.get("enabled"):
            continue
        cls = effect_class_factory(entry.get("name", ""))
        if cls is None:
            continue
        out.append(cls(display))
    if not out:
        # Fallback: the first canonical effect (the head of the
        # declared rotation order). Resolved through the same
        # factory so an unknown canonical name is caught and
        # skipped — the rotation stays empty in that case (better
        # dark panel than a crash). Deterministic: same config
        # always picks the same fallback, so the sign's idle
        # behavior is predictable across reloads.
        for entry in effects_settings.effects:
            cls = effect_class_factory(entry.get("name", ""))
            if cls is not None:
                log.warning(
                    "build_effects: rotation empty after filter, " "falling back to first canonical effect %r",
                    entry.get("name"),
                )
                return [cls(display)]
    return out


class EffectsCoordinator:
    """Drives the boot splash, message lifecycle, and idle rotation.

    Used directly by both the Pi entrypoint and the browser preview;
    no subclass. The coordinator is constructed unbound (no
    `display`/`scroller`/`effects`/`heart`); the page that owns the
    canvas (the preview's `preview_main.py`) calls `bind(...)` once
    those objects are in scope. `tick()` no-ops when the coordinator
    is unbound so an admin page that loaded the app-scoped
    coordinator can run a no-rAF idle without crashing.

    The coordinator is the consumer; `MessageManager` is the source
    of truth. `message_manager` is a required keyword argument —
    there is no fallback to a None manager. The constructor raises
    `TypeError` if a caller omits it. `tick()` calls
    `get_display_message()` throttled to ~4 Hz; the cached result
    becomes the next text shown by the fade state machine.

    Args:
        message_manager: required `MessageManager` instance. The
            coordinator reads messages and config from it. Construct
            one before constructing the coordinator and pass it in.
            The coordinator holds no copy of the config; pacing,
            rotation, and text settings are observed live via
            `message_manager.get_effects_settings()` /
            `.get_text_settings()` at tick time. Small structural
            diffs (`_last_rotation`, `_last_text_color`,
            `_last_text_speed`) gate the rotation rebuild and
            scroller setter calls when nothing has changed.
        fade_step: throttle (seconds) between palette writes during a fade.
        gamma: gamma exponent applied to the linear fade progress.
    """

    # Throttle the coordinator's pull from MessageManager.messages. 4 Hz is
    # 4x faster than any human perceives text change and far below the 30+
    # FPS cost we are avoiding.
    _PULL_INTERVAL = 0.25

    def __init__(
        self,
        message_manager: MessageManager,
        display: DisplayBase | None = None,
        scroller: ScrollerBase | None = None,
        effects: list[Effect] | None = None,
        heart: Effect | None = None,
        fade_step: float = 0.04,
        gamma: float = 2.2,
    ) -> None:
        # Required — no default. Raises TypeError if a caller omits it.
        # All live config (pacing fields, rotation, text settings) is
        # read from `message_manager.config` at tick time — the
        # coordinator holds no copy, so config updates land
        # automatically without an explicit `apply_settings` call.
        self.message_manager = message_manager
        self.display = display
        self.scroller = scroller
        self.effects = list(effects) if effects is not None else []
        self.heart = heart
        # Throttles palette writes during a fade. Without this, a fast main loop
        # rewrites the palette far faster than the panel refreshes, wasting work.
        self.fade_step = fade_step
        # Gamma correction: linear time → perceptually linear brightness.
        self.gamma = gamma

        self.idx = -1  # first message shown advances this to 0
        self.current = heart  # effect being rendered right now (may be None)
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = 0.0  # start of intro / hold / background
        self.showing_text = False
        self.last_shown_text = None
        # Pull-throttle state. `_last_message_pull` is the monotonic time of
        # the last pull from the manager; `_last_display_message` is the
        # cached body that non-pull ticks consume.
        self._last_message_pull: float = 0.0
        self._last_display_message: str | None = None
        self._last_shown_message_id: str | None = None
        # `_consumed_message_ids` is the set of message ids that have
        # actually FADED IN on the scroller during this process's
        # lifetime. Distinct from `_last_shown_message_id`, which
        # `get_display_message` mutates on every pull (random.choice
        # included) — too noisy to use as a "is this a genuinely new
        # SMS?" sentinel because random.choice over a multi-message
        # pool bounces the comparison and falsely trips every other
        # pull. We need the full history (a set, not a single id)
        # because a "fresh" id is one that has never been shown yet;
        # recency of the *last* consumed id is not enough when there
        # are N ≥ 2 messages in the recent pool.
        self._consumed_message_ids: set[str] = set()
        # Render-layer diff state: structural fingerprints of the
        # rotation list and text settings, used to gate the
        # rotation rebuild and scroller setter calls in `tick()`.
        # Not a copy of the source-of-truth config — the values
        # live on `message_manager.config` and are read live each
        # tick; these fields just let us skip the rebuild /
        # setter when nothing has changed. Initialized to the
        # signature of the default `EffectsSettings()` /
        # `TextSettings()` so the first tick after construction
        # is a no-op when the manager's config is still at its
        # default (the typical boot state — the v2 config
        # arrives over MQTT shortly after and the diff then
        # fires, triggering the rebuild). The bootstrap
        # `effects=` / scroller color / speed constructor args
        # survive until that first real config update.
        # `bind()` resets all three to None so the first tick
        # after a fresh bind refreshes the now-attached render
        # layer.
        self._last_rotation: tuple | None = tuple((e.get("name"), e.get("enabled")) for e in EffectsSettings().effects)
        self._last_text_color: int | None = TextSettings().color
        self._last_text_speed: int | None = TextSettings().speed

    def is_bound(self) -> bool:
        """True when the coordinator has a render layer (display + scroller + effects + heart).

        An unbound coordinator is a no-op: `tick()` returns without
        touching state. The Pi always binds before its first tick
        (it constructs the display + scroller + effects in the
        same module-level pass). The browser's `app_main.py`
        instantiates the coordinator unbound; the preview page
        binds once the canvas is in scope.
        """
        return self.display is not None and self.scroller is not None and bool(self.effects) and self.heart is not None

    def bind(
        self,
        display: DisplayBase | None = None,
        scroller: ScrollerBase | None = None,
        effects: list[Effect] | None = None,
        heart: Effect | None = None,
    ) -> None:
        """Attach (or swap) the render layer.

        Replaces `display`/`scroller`/`effects`/`heart` in place.
        `heart` defaults to the head of the new effects list when
        not supplied (preview uses the first effect as the
        boot-splash; the Pi passes an explicit Heartbeat instance).

        The rotation / scroller text-settings caches are reset on
        bind, so the first `tick()` after bind refreshes the
        render layer with the manager's current config (the
        app-scoped coordinator's `on_change` callback fires
        before the preview has had a chance to `bind`, so the
        pre-bind ticks were no-ops that never touched the
        render layer).

        Safe to call mid-life: the next `tick()` uses the new
        layer. The Pi's `main.py` calls it once at startup; the
        browser's `preview_main.py` calls it once the page-local
        canvas is constructed.
        """
        self.display = display
        self.scroller = scroller
        self.effects = list(effects) if effects is not None else []
        self.heart = heart if heart is not None else (self.effects[0] if self.effects else None)
        if self.current is None:
            self.current = self.heart
            if self.heart is not None:
                self.heart.set_brightness(1.0)
            self.phase_start = time.monotonic()
        # Reset the render-layer diff sentinels so the next tick
        # rebuilds the rotation and reapplies the scroller color /
        # speed against the now-attached render layer. The
        # manager's config is the source of truth; these fields
        # are just structural diffs against it.
        self._last_rotation = None
        self._last_text_color = None
        self._last_text_speed = None

    def start(self) -> None:
        """Begin the boot splash.

        No-op when the coordinator is unbound — the app-scoped
        coordinator on non-preview admin pages is constructed
        without a render layer; `start()` is only meaningful on
        the preview's per-page shim. The first message after the
        heart fades out comes from the manager's buffer via
        `get_display_message()`, which the throttled tick pulls
        from — there is no separate "show this body after the
        heart" hook (the push path is gone).
        """
        if not self.is_bound():
            return
        assert self.heart is not None
        self.current = self.heart
        self.heart.set_brightness(1.0)
        self.mode = "intro"
        self.phase_start = time.monotonic()

    @property
    def current_messages(self) -> list[MessageView]:
        """Returns the current consideration set of messages to display,
        ie. the latest unsupressed recent_count messages"""
        return self.message_manager.get_messages(limit=self.effects_settings.recent_count, suppress=True)

    @property
    def effects_settings(self) -> EffectsSettings:
        """Returns the current effects settings"""
        return self.message_manager.get_effects_settings()

    @property
    def text_settings(self) -> TextSettings:
        """Returns the current text settings"""
        return self.message_manager.get_text_settings()

    def _consumed_message_id_at_pick(self, body: str) -> str | None:
        """Return the id of the buffered message whose body matches `body`.

        Called at the out→in transition to mark "this is the message we
        just faded in." The buffer (held by the manager) is searched in
        recent-first order so the most-recent match wins — relevant when
        the same body was sent twice (rare but possible). Returns None
        when the buffer no longer holds the body (e.g. evicted by a
        flood of newer messages before the fade-in completed); the
        caller treats that as "no consumption tracking" and the next
        fresh-id check defaults to a no-match.
        """
        for entry in self.current_messages:
            if entry.message.body == body:
                return entry.message.id
        return None

    def get_display_message(self) -> str | None:
        """Pick the body to display next, from the manager's buffered messages.

        Algorithm:
          1. Read `message_manager.config.effects_settings.recent_count`
             most-recent non-suppressed messages.
          2. If the list is empty, return None.
          3. If the head entry's id differs from `self._last_shown_message_id`,
             return its body and update `_last_shown_message_id` (fresh-message
             priority).
          4. Otherwise pick uniformly at random from the list and return that
             entry's body, updating `_last_shown_message_id` to the picked id.

        Returns:
            The body string to show next, or None when the buffer is empty.
        """
        entries = self.current_messages
        if len(entries) == 0:
            return None
        head = entries[0]
        if head.message.id != self._last_shown_message_id:
            self._last_shown_message_id = head.message.id
            return head.message.body
        picked = random.choice(entries)
        self._last_shown_message_id = picked.message.id
        return picked.message.body

    def _step_fade(self, now, fading_out, fade_effect=True, fade_text=True):
        """Advance the active fade one throttled step; return True when complete."""
        progress = (now - self.fade_start) / self.message_manager.config.effects_settings.fade_seconds
        if progress > 1.0:
            progress = 1.0
        if now - self.last_step >= self.fade_step or progress >= 1.0:
            self.last_step = now
            linear = (1.0 - progress) if fading_out else progress
            b = linear**self.gamma
            current = self.current
            scroller = self.scroller
            if fade_effect and current is not None:
                current.set_brightness(b)
            if fade_text and scroller is not None:
                scroller.set_brightness(b)
        return progress >= 1.0

    def _begin_out(self, now):
        # `_begin_out` fires for two distinct reasons:
        #   1. intro-second elapses (first-ever fade after boot)
        #   2. a fresh SMS arrives during hold/background and interrupts
        # The Pi can't toggle LOG_LEVEL at runtime, so log both reasons at
        # INFO level — operators need to see sign-lifecycle events in the
        # journal without service-restart gymnastics. The `trigger` kwarg
        # disambiguates the two paths.
        scroller_text = ""
        if self.scroller is not None:
            scroller_text = self.scroller.text or ""
        log.info(
            "Coordinator._begin_out: from mode=%s effect=%s scroller_text=%r",
            self.mode, self.current_effect_name, scroller_text,
        )
        # DEBUG: stdout mirror.
        print(
            f"DEBUG coordinator._begin_out: from mode={self.mode} "
            f"effect={self.current_effect_name} scroller_text={scroller_text!r}",
            flush=True,
        )
        self.mode = "out"
        self.fade_start = now
        self.last_step = 0.0

    @property
    def current_effect_name(self):
        """The class name of the active effect (browser status block)."""
        if self.current is None:
            return ""
        return type(self.current).__name__

    @property
    def current_text(self):
        """The body of the message currently being scrolled (or '' for idle)."""
        if self.scroller is None:
            return ""
        return self.scroller.text or ""

    def tick(self):
        """Advance the state machine one frame.

        No-op when the coordinator is unbound (an admin page
        without a canvas). When bound, the lifecycle runs
        intro → out → in → hold → text_out → background, the
        current effect's `tick()` advances, the scroller
        scrolls, and `display.render(...)` composites the
        frame.

        Pulls the next display message from the manager on a
        ~4 Hz throttle (`_PULL_INTERVAL`). Non-pull ticks consume
        the cached result from the previous pull.

        Pacing fields (intro_seconds, hold_seconds, etc.) and the
        rotation / scroller text settings are read live from
        `message_manager.config`; no per-coordinator copy, no
        explicit `apply_settings` call needed. Config updates land
        at most one frame later.
        """
        # DEBUG: checkpoint BEFORE the is_bound() guard. If this fires
        # but the post-guard print below doesn't, the guard is the
        # suspect. Throttled to once every 200 calls (≈1.5s at the
        # observed ~200Hz loop rate) and offset from the post-guard
        # print so the two never collide on the same line.
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 200 == 0:
            print(
                f"DEBUG coordinator.tick ENTRY: count={self._tick_count} is_bound={self.is_bound()}",
                flush=True,
            )
        if not self.is_bound():
            return
        # Local aliases for the bound layer — Pyright doesn't narrow
        # `self.display` etc. through `is_bound()`, but the guard above
        # makes these accesses safe.
        display = self.display
        scroller = self.scroller
        effects = self.effects
        assert display is not None
        assert scroller is not None
        assert effects

        # Refresh the render layer from the manager's current config.
        # The manager is the single source of truth — read the
        # settings live via the `effects_settings` / `text_settings`
        # properties (which delegate to `message_manager.get_*`).
        # The coordinator holds no copy of the config; the small
        # structural diffs (`_last_rotation`, `_last_text_color`,
        # `_last_text_speed`) just gate the rotation rebuild and
        # scroller setter calls when nothing has changed. Pacing
        # fields are read at the call sites below directly from
        # the manager.
        effects_settings = self.effects_settings
        rotation = tuple((e.get("name"), e.get("enabled")) for e in effects_settings.effects)
        if rotation != self._last_rotation:
            log.info(
                "Coordinator rotation rebuild: prev=%s new=%s",
                self._last_rotation, rotation,
            )
            self.effects = build_effects(effects_settings, display=display)
            self.idx = -1  # next fade picks the head of the new list
            self._last_rotation = rotation
        text_settings = self.text_settings
        if text_settings.color != self._last_text_color:
            log.info(
                "Coordinator scroller color change: prev=%s new=%s",
                self._last_text_color, text_settings.color,
            )
            scroller.set_color(text_settings.color)
            self._last_text_color = text_settings.color
        if text_settings.speed != self._last_text_speed:
            log.info(
                "Coordinator scroller speed change: prev=%s new=%s",
                self._last_text_speed, text_settings.speed,
            )
            scroller.set_speed(text_settings.speed)
            self._last_text_speed = text_settings.speed

        now = time.monotonic()
        mode = self.mode

        # DEBUG: bypass the "heart" logger silence with a direct stdout
        # print so we can confirm tick() is being called and at what
        # rate. Throttled to one print per second to keep the journal
        # readable. Uses `getattr` so the first-tick increment happens
        # without an __init__ change.
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 60 == 0:
            print(
                f"DEBUG coordinator.tick: count={self._tick_count} mode={self.mode} "
                f"effect={self.current_effect_name} showing_text={self.showing_text} "
                f"last_shown={self.last_shown_text!r}",
                flush=True,
            )

        # Throttled pull: only fetch a fresh body every _PULL_INTERVAL.
        # The cached value drives the state-machine transitions.
        if now - self._last_message_pull >= self._PULL_INTERVAL:
            new_text = self.get_display_message()
            if new_text != self._last_display_message:
                log.info(
                    "Coordinator pull changed: prev=%r new=%r last_shown_id=%s",
                    self._last_display_message, new_text, self._last_shown_message_id,
                )
                # DEBUG: same event, printed to stdout to bypass any
                # logger-side filtering that is hiding the log.info calls.
                print(
                    f"DEBUG coordinator pull changed: prev={self._last_display_message!r} "
                    f"new={new_text!r} last_shown_id={self._last_shown_message_id}",
                    flush=True,
                )
            self._last_display_message = new_text
            self._last_message_pull = now
        text = self._last_display_message

        if mode == "intro":
            if now - self.phase_start >= effects_settings.intro_seconds:
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then swap in
            # the next effect and (if there's text) the next message.
            if self._step_fade(now, fading_out=True):
                self.idx = (self.idx + 1) % len(effects)
                self.current = effects[self.idx]
                self.current.set_brightness(0.0)
                if text:
                    scroller.set_text(text, display.width)
                    scroller.set_brightness(0.0)
                    self.showing_text = True
                    self.last_shown_text = text
                    # Mark this message id as "consumed" — a
                    # follow-on hold or background should not treat
                    # the same id as fresh. A genuine fresh-id
                    # interrupt requires an id that has never been
                    # consumed (i.e. an SMS we haven't shown yet).
                    consumed = self._consumed_message_id_at_pick(text)
                    if consumed is not None:
                        self._consumed_message_ids.add(consumed)
                else:
                    scroller.set_text("", display.width)
                    self.showing_text = False
                log.info(
                    "Coordinator out→in: idx=%d effect=%s text=%r showing_text=%s",
                    self.idx, self.current_effect_name,
                    text if text else "", self.showing_text,
                )
                # DEBUG: stdout mirror — bypass logger filter if "heart" is silenced.
                print(
                    f"DEBUG coordinator out->in: idx={self.idx} effect={self.current_effect_name} "
                    f"text={text if text else ''!r} showing_text={self.showing_text}",
                    flush=True,
                )
                self.mode = "in"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "in":
            if self._step_fade(now, fading_out=False):
                assert self.current is not None
                self.current.set_brightness(1.0)
                scroller.set_brightness(1.0)
                self.phase_start = now
                next_mode = "hold" if self.showing_text else "background"
                log.info(
                    "Coordinator in→%s: effect=%s text=%r",
                    next_mode, self.current_effect_name,
                    self.last_shown_text or "",
                )
                # DEBUG: stdout mirror.
                print(
                    f"DEBUG coordinator in->{next_mode}: effect={self.current_effect_name} "
                    f"text={self.last_shown_text or ''!r} phase_start=now",
                    flush=True,
                )
                self.mode = next_mode

        elif mode == "hold":
            # Hold semantics (v2):
            #   - Stay on the current message until `hold_seconds` elapses,
            #     UNLESS a genuinely *new* SMS arrives — i.e. the head of the
            #     recent pool has an id we haven't shown yet. Random re-picks
            #     from already-shown messages do NOT interrupt the hold;
            #     they only kick a re-roll in the `background` mode below.
            #   - The previous comparison (`text != self.last_shown_text`)
            #     compared BODY strings, which meant `random.choice` over a
            #     5-entry pool could land on a different body every pull and
            #     effectively keep hold duration clamped to the throttle
            #     interval (~0.25s). That bug is why `hold_seconds` was
            #     observed "taking a long time, then disappearing after a
            #     few seconds" — every random pick interrupted the hold
            #     instantly. The fix: gate the interrupt on the ID, not the
            #     body, and idle `hold_seconds` otherwise.
            fresh_id_landed = (
                self.current_messages
                and self.current_messages[0].message.id not in self._consumed_message_ids
            )
            if fresh_id_landed:
                log.info(
                    "Coordinator hold interrupt (new id): pending_text=%r last_shown=%r new_id=%s",
                    text, self.last_shown_text,
                    self.current_messages[0].message.id,
                )
                # DEBUG: stdout mirror.
                print(
                    f"DEBUG coordinator hold interrupt (new id): pending_text={text!r} "
                    f"last_shown={self.last_shown_text!r} new_id={self.current_messages[0].message.id}",
                    flush=True,
                )
                self._begin_out(now)  # new SMS interrupts the hold
            elif now - self.phase_start >= effects_settings.hold_seconds:
                log.info(
                    "Coordinator hold→text_out: effect=%s held_text=%r held_for=%.1fs hold_seconds=%.1f",
                    self.current_effect_name, self.last_shown_text,
                    now - self.phase_start, effects_settings.hold_seconds,
                )
                # DEBUG: stdout mirror.
                print(
                    f"DEBUG coordinator hold->text_out: effect={self.current_effect_name} "
                    f"held_text={self.last_shown_text!r} held_for={now - self.phase_start:.1f}s "
                    f"hold_seconds={effects_settings.hold_seconds}",
                    flush=True,
                )
                self.mode = "text_out"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "text_out":
            # Only the text fades; the background effect stays lit.
            if self._step_fade(now, fading_out=True, fade_effect=False):
                scroller.set_text("", display.width)
                scroller.set_brightness(1.0)
                self.showing_text = False
                self.phase_start = now
                log.info(
                    "Coordinator text_out→background: effect=%s",
                    self.current_effect_name,
                )
                # DEBUG: stdout mirror.
                print(
                    f"DEBUG coordinator text_out->background: effect={self.current_effect_name}",
                    flush=True,
                )
                self.mode = "background"

        elif mode == "background":
            # Background semantics (v2):
            #   - A genuinely new SMS (head.id differs from last-shown) kicks
            #     a fade immediately — the operator just texted, show it now.
            #   - A random re-pick from the already-shown pool ALSO kicks a
            #     fade, so the rotating buffer of recent messages keeps the
            #     sign alive even with zero inbound traffic.
            #   - `idle_seconds` is honored as a hard ceiling: if neither
            #     trigger fires within `idle_seconds` of entering background,
            #     force a re-roll. This was the prior bug — idle_seconds was
            #     defined and exposed in the admin UI but never read by the
            #     coordinator, so the sign could sit dormant for 5+ minutes
            #     even with idle_seconds=10. Now: background kicks a re-roll
            #     on whichever of (fresh_id, new_random_pick, idle_timeout)
            #     fires first.
            fresh_id_landed = (
                self.current_messages
                and self.current_messages[0].message.id not in self._consumed_message_ids
            )
            random_pick_changed = bool(text) and text != self.last_shown_text
            idle_timed_out = (
                now - self.phase_start >= effects_settings.idle_seconds
            )
            if fresh_id_landed or random_pick_changed or idle_timed_out:
                trigger = (
                    "new_id" if fresh_id_landed
                    else "random_repick" if random_pick_changed
                    else "idle"
                )
                log.info(
                    "Coordinator background→out (%s): waited=%.1fs idle_seconds=%.1f next_text=%r",
                    trigger, now - self.phase_start,
                    effects_settings.idle_seconds, text or "",
                )
                # DEBUG: stdout mirror.
                print(
                    f"DEBUG coordinator background->out ({trigger}): waited={now - self.phase_start:.1f}s "
                    f"idle_seconds={effects_settings.idle_seconds} next_text={text or ''!r}",
                    flush=True,
                )
                self._begin_out(now)  # show the queued message

        current = self.current
        assert current is not None
        current.tick()
        scroller.tick(display.width)
        # Composite the active effect + scroller onto the panel. The display
        # owns the clear/draw/swap sequence (and, on the Pi, the SwapOnVSync
        # pacing).
        display.render(current, scroller)
        # DEBUG: checkpoint AFTER render. Offset 100 from the entry
        # print so the two never collide on the same tick. If this
        # fires but the entry print doesn't, the throttle window is
        # off; if neither fires, tick() is not being called (which
        # would contradict the visible animations).
        if self._tick_count % 200 == 100:
            print(
                f"DEBUG coordinator.tick COMPLETE: count={self._tick_count} mode={self.mode}",
                flush=True,
            )
