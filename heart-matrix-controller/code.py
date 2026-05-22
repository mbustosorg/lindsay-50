import os
import time
import wifi
import adafruit_logging as logging
from adafruit_matrixportal.matrix import Matrix
from scroller import Scroller
from fireworks import Fireworks
from flame import Flame
from nightsky import NightSky
from mqtt_client import CircuitPythonMqttClient
from lib_shared.message_manager import MessageManager

from lib_shared.config_reader import get_config
REQUIRED_KEYS: set[str] = {
    "WIFI_SSID",
    "WIFI_PASSWORD",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_TOPIC",
    "CONFIG_API_URL",
    "MESSAGES_API_URL",
}
cfg = get_config(REQUIRED_KEYS)

log = logging.getLogger("heart")
log.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO")))


def connect_wifi():
    log.info("Connecting to WiFi: %s", cfg.WIFI_SSID)
    wifi.radio.connect(cfg.WIFI_SSID, cfg.WIFI_PASSWORD)
    log.info("Connected, IP: %s", wifi.radio.ipv4_address)

connect_wifi()


matrix = Matrix(width=64, height=64, serpentine=True, tile_rows=2, bit_depth=4)
scroller = Scroller(matrix)
fireworks = Fireworks(matrix.display, scroller.group)
fireworks.tilegrid.hidden = True
flame = Flame(matrix.display, scroller.group)
flame.tilegrid.hidden = True
nightsky = NightSky(matrix.display, scroller.group)


class EffectCoordinator:
    """Toggles between effects and fades the display when a new message arrives."""

    def __init__(self, display, scroller, effects, fade_seconds=0.5, fade_step=0.04, gamma=2.2):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.idx = 0
        self.fade_seconds = fade_seconds
        # Throttles palette writes during a fade. Without this, a fast main loop
        # saturates the displayio compositor and the fade visually freezes.
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
                log.info("fade %s linear=%.3f b=%.3f", self.mode, linear, b)

            if progress >= 1.0:
                if self.mode == "out":
                    self.effects[self.idx].tilegrid.hidden = True
                    self.idx = (self.idx + 1) % len(self.effects)
                    self.effects[self.idx].set_brightness(0.0)
                    self.effects[self.idx].tilegrid.hidden = False
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


coordinator = EffectCoordinator(matrix.display, scroller, [nightsky, fireworks, flame], fade_seconds=5.2)

_message_mgr = MessageManager(on_message=lambda msg: coordinator.request_message(msg.body))
_message_mgr.seed()

_mqtt_client = CircuitPythonMqttClient(dispatch_callback=_message_mgr.dispatch)
_mqtt_client.start()

while True:
    try:
        _mqtt_client.loop(timeout=0.001)
    except Exception as e:
        log.error("MQTT error: %s — reconnecting...", e)
        try:
            wifi.reset()
            _mqtt_client.reconnect()
        except Exception as e2:
            log.error("Reconnect failed: %s", e2)
    coordinator.tick()