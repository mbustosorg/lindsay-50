import os
import time
import signal
import logging

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
from scroller import Scroller
from patterns.fireworks import Fireworks
from patterns.flame import Flame
from patterns.nightsky import NightSky
from patterns.png_display import PngDisplay
from patterns.video_display import VideoDisplay
from patterns.honeycomb import Honeycomb
from patterns.hyperspace import Hyperspace
from lib_shared.message_manager import MessageManager
from lib_shared.mqtt_factory import make_mqtt_client


display = Display()
scroller = Scroller(display)
fireworks = Fireworks(display)
flame = Flame(display)
nightsky = NightSky(display)
png = PngDisplay(display)
video = VideoDisplay(display)
honeycomb = Honeycomb(display)
hyperspace = Hyperspace(display)


class EffectCoordinator:
    """Toggles between effects and fades the display when a new message arrives."""

    def __init__(self, display, scroller, effects, fade_seconds=0.5, fade_step=0.04, gamma=2.2):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.idx = 0
        self.fade_seconds = fade_seconds
        # Throttles palette writes during a fade. Without this, a fast main loop
        # rewrites the palette far faster than the panel refreshes, wasting work.
        self.fade_step = fade_step
        # Gamma correction: linear time → perceptually linear brightness.
        self.gamma = gamma
        self.mode = "idle"  # idle | out | in
        self.fade_start = 0.0
        self.last_step = 0.0
        self.pending_text = None

    def request_message(self, text):
        self.pending_text = text
        self.mode = "out"
        self.fade_start = time.monotonic()
        self.last_step = 0.0

    def tick(self):
        now = time.monotonic()
        if self.mode != "idle":
            progress = (now - self.fade_start) / self.fade_seconds
            if progress > 1.0:
                progress = 1.0

            if now - self.last_step >= self.fade_step or progress >= 1.0:
                self.last_step = now
                linear = 1.0 - progress if self.mode == "out" else progress
                b = linear ** self.gamma
                self.effects[self.idx].set_brightness(b)
                self.scroller.set_brightness(b)
                log.debug("fade %s linear=%.3f b=%.3f", self.mode, linear, b)

            if progress >= 1.0:
                if self.mode == "out":
                    self.idx = (self.idx + 1) % len(self.effects)
                    self.effects[self.idx].set_brightness(0.0)
                    self.scroller.set_text(self.pending_text)
                    self.pending_text = None
                    self.mode = "in"
                    self.fade_start = now
                    self.last_step = 0.0
                else:  # "in" complete
                    self.effects[self.idx].set_brightness(1.0)
                    self.scroller.set_brightness(1.0)
                    self.mode = "idle"

        self.effects[self.idx].tick()
        self.scroller.tick()
        # Composite the active effect + text onto the panel. SwapOnVSync inside
        # render() blocks until the next refresh, which paces this loop.
        self.display.render(self.effects[self.idx], self.scroller)


coordinator = EffectCoordinator(display, scroller, [hyperspace, video, png, honeycomb, flame, fireworks, nightsky], fade_seconds=4)

_message_mgr = MessageManager(on_message=lambda msg: coordinator.request_message(msg.body))
_message_mgr.seed()

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