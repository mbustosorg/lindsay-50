import os
import time
import signal
import logging
import random

# Create the config singleton FIRST: modules imported below (rgb_display,
# message_manager, and the MQTT client built by mqtt_factory) call get_config()
# at import time, so it must already exist. Wi-Fi is managed by the Pi OS.
from lib_shared.config_reader import get_config

REQUIRED_KEYS: set[str] = {
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_TOPIC",
    "CONFIG_API_URL",
    "MESSAGES_API_URL",
    "API_SECRET_KEY",
}
cfg = get_config(REQUIRED_KEYS)

from lib_shared.log_setup import configure_logging

configure_logging(getattr(logging, os.getenv("LOG_LEVEL", "INFO")))
log = logging.getLogger("heart")

from rgb_display import Display
from scroller import MatrixScroller
from patterns.fireworks import Fireworks
from patterns.flame import Flame
from patterns.nightsky import NightSky
from patterns.png_display import PngDisplay
from patterns.video_display import VideoDisplay
from patterns.honeycomb import Honeycomb
from patterns.hyperspace import Hyperspace
from patterns.heartbeat import Heartbeat
from lib_shared.message_manager import MessageManager
from lib_shared.mqtt_factory import make_mqtt_client

display = Display()
scroller = MatrixScroller(display)
fireworks = Fireworks(display)
flame = Flame(display)
nightsky = NightSky(display)
png = PngDisplay(display)
video = VideoDisplay(display)
honeycomb = Honeycomb(display)
hyperspace = Hyperspace(display)
heartbeat = Heartbeat(display)


class EffectCoordinator:
    """Drives the startup splash, message lifecycle, and idle rotation.

    Boot flow:
        intro   — a beating heart for `intro_seconds`, no text.
        out     — cross-fade the current effect+text to black.
        in      — fade the next effect (+ message text) up.
        hold    — keep a message fully visible for `hold_seconds`.
        text_out— fade only the text out, leaving the background effect lit.
        background — just the effect, no text, until the next message.

    A new SMS interrupts `hold`/`background` to show immediately. After
    `idle_seconds` with nothing new, a random one of the last few messages is
    shown so the sign never sits silent for long. Every fade lasts
    `fade_seconds`; the effect advances by one on each message shown.
    """

    def __init__(
        self,
        display,
        scroller,
        effects,
        heart,
        recent_provider,
        fade_seconds=2.0,
        hold_seconds=15.0,
        intro_seconds=5.0,
        idle_seconds=300.0,
        recent_count=5,
        fade_step=0.04,
        gamma=2.2,
    ):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.heart = heart
        self.recent_provider = recent_provider
        self.fade_seconds = fade_seconds
        self.hold_seconds = hold_seconds
        self.intro_seconds = intro_seconds
        self.idle_seconds = idle_seconds
        self.recent_count = recent_count
        # Throttles palette writes during a fade. Without this, a fast main loop
        # rewrites the palette far faster than the panel refreshes, wasting work.
        self.fade_step = fade_step
        # Gamma correction: linear time → perceptually linear brightness.
        self.gamma = gamma

        self.idx = -1  # first message shown advances this to 0
        self.current = heart  # effect being rendered right now
        self.mode = "intro"
        self.fade_start = 0.0
        self.last_step = 0.0
        self.phase_start = 0.0  # start of intro / hold / background
        self.pending_text = None  # next message body to show (None = nothing)
        self.showing_text = False
        self.last_shown_text = None

    def start(self, startup_text):
        """Begin the boot splash, queuing the seeded message to show after it."""
        self.pending_text = startup_text
        self.current = self.heart
        self.heart.set_brightness(1.0)
        self.mode = "intro"
        self.phase_start = time.monotonic()

    def request_message(self, text):
        """Queue a freshly-arrived message; shown at the next stable point."""
        if text:
            self.pending_text = text

    def _step_fade(self, now, fading_out, fade_effect=True, fade_text=True):
        """Advance the active fade one throttled step; return True when complete."""
        progress = (now - self.fade_start) / self.fade_seconds
        if progress > 1.0:
            progress = 1.0
        if now - self.last_step >= self.fade_step or progress >= 1.0:
            self.last_step = now
            linear = (1.0 - progress) if fading_out else progress
            b = linear**self.gamma
            if fade_effect:
                self.current.set_brightness(b)
            if fade_text:
                self.scroller.set_brightness(b)
        return progress >= 1.0

    def _begin_out(self, now):
        self.mode = "out"
        self.fade_start = now
        self.last_step = 0.0

    def _random_recent(self):
        """A random body from the last `recent_count` messages (avoid repeat)."""
        try:
            entries = self.recent_provider() or []
        except Exception:
            log.exception("recent_provider failed")
            return None
        bodies = [
            e.message.body for e in entries[: self.recent_count] if e.message.body
        ]
        if not bodies:
            return None
        choices = [b for b in bodies if b != self.last_shown_text] or bodies
        return random.choice(choices)

    def tick(self):
        now = time.monotonic()
        mode = self.mode

        if mode == "intro":
            if now - self.phase_start >= self.intro_seconds:
                self._begin_out(now)

        elif mode == "out":
            # Cross-fade the current effect + any text to black, then swap in
            # the next effect and (if queued) the next message.
            if self._step_fade(now, fading_out=True):
                self.idx = (self.idx + 1) % len(self.effects)
                self.current = self.effects[self.idx]
                self.current.set_brightness(0.0)
                text = self.pending_text
                self.pending_text = None
                if text:
                    self.scroller.set_text(text, self.display.width)
                    self.scroller.set_brightness(0.0)
                    self.showing_text = True
                    self.last_shown_text = text
                else:
                    self.scroller.set_text("", self.display.width)
                    self.showing_text = False
                self.mode = "in"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "in":
            if self._step_fade(now, fading_out=False):
                self.current.set_brightness(1.0)
                self.scroller.set_brightness(1.0)
                self.phase_start = now
                self.mode = "hold" if self.showing_text else "background"

        elif mode == "hold":
            if self.pending_text is not None:
                self._begin_out(now)  # new SMS interrupts the hold
            elif now - self.phase_start >= self.hold_seconds:
                self.mode = "text_out"
                self.fade_start = now
                self.last_step = 0.0

        elif mode == "text_out":
            # Only the text fades; the background effect stays lit.
            if self._step_fade(now, fading_out=True, fade_effect=False):
                self.scroller.set_text("", self.display.width)
                self.scroller.set_brightness(1.0)
                self.showing_text = False
                self.phase_start = now
                self.mode = "background"

        elif mode == "background":
            if self.pending_text is not None:
                self._begin_out(now)  # show the queued message
            elif now - self.phase_start >= self.idle_seconds:
                text = self._random_recent()
                if text:
                    self.pending_text = text
                    self._begin_out(now)
                else:
                    self.phase_start = now  # nothing to show; reset the timer

        self.current.tick()
        self.scroller.tick(self.display.width)
        # Composite the active effect + text onto the panel. SwapOnVSync inside
        # render() blocks until the next refresh, which paces this loop.
        self.display.render(self.current, self.scroller)


coordinator = EffectCoordinator(
    display,
    scroller,
    [hyperspace, video, png, honeycomb, flame, fireworks, nightsky],
    heart=heartbeat,
    recent_provider=lambda: _message_mgr.get_messages(limit=5),
)

_message_mgr = MessageManager(
    on_message=lambda msg: coordinator.request_message(msg.body)
)
_message_mgr.seed()

# Kick off the boot splash, queuing the most recent seeded message to play once
# the heart fades out.
_recent = _message_mgr.get_messages(limit=1)
_startup_text = _recent[0].message.body if _recent else None
coordinator.start(_startup_text)

# Platform MQTT client (paho on the Pi; adafruit available via MQTT_CLIENT)
_mqtt_client = make_mqtt_client(_message_mgr.dispatch)
logging.info("Starting MQTT client at boot...")
_mqtt_client.start()


# SIGTERM (systemd stop / `kill`) doesn't raise an exception by default, so the
# `finally` below would never run. Turn it into SystemExit so cleanup happens on
# every stop path; SIGINT (Ctrl-C) already raises KeyboardInterrupt.
def _on_sigterm(signum, frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _on_sigterm)

try:
    while True:
        coordinator.tick()
except (KeyboardInterrupt, SystemExit):
    log.info("interrupted, shutting down")
finally:
    # Blank the panel on any exit — interrupt, stop signal, or crash — so the
    # LEDs don't hold the last frame. Guard it: a failure here would otherwise
    # replace whatever exception triggered the shutdown, hiding the root cause.
    try:
        display.clear()
        log.info("display cleared")
    except Exception:
        log.exception("failed to clear display on shutdown")
