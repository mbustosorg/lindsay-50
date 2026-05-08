import os
import time
import wifi
import socketpool
import adafruit_connection_manager
import adafruit_logging as logging
import adafruit_minimqtt.adafruit_minimqtt as MQTT
from adafruit_io.adafruit_io import IO_MQTT
from adafruit_matrixportal.matrix import Matrix
from scroller import Scroller
from fireworks import Fireworks
from flame import Flame

# Credentials are loaded from settings.toml
SSID = os.getenv("WIFI_SSID")
PASSWORD = os.getenv("WIFI_PASSWORD")

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC = os.getenv("MQTT_TOPIC")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

log = logging.getLogger("heart")
log.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO")))

# IO_MQTT.subscribe() takes the feed name, not the full "{user}/feeds/{feed}" path.
MQTT_FEED = MQTT_TOPIC.rsplit("/feeds/", 1)[-1]


def connect_wifi():
    log.info("Connecting to WiFi: %s", SSID)
    wifi.radio.connect(SSID, PASSWORD)
    log.info("Connected, IP: %s", wifi.radio.ipv4_address)


connect_wifi()
pool = socketpool.SocketPool(wifi.radio)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)

matrix = Matrix(width=64, height=64, serpentine=True, tile_rows=2)
scroller = Scroller(matrix)
fireworks = Fireworks(matrix.display, scroller.group)
flame = Flame(matrix.display, scroller.group)
flame.tilegrid.hidden = True


class EffectCoordinator:
    """Toggles between effects and fades the display when a new message arrives."""

    def __init__(self, display, scroller, effects, fade_seconds=0.5):
        self.display = display
        self.scroller = scroller
        self.effects = effects
        self.idx = 0
        self.fade_seconds = fade_seconds
        self.mode = "idle"  # idle | out | in
        self.fade_start = 0.0
        self.pending_text = None

    def request_message(self, text):
        self.pending_text = text
        self.mode = "out"
        self.fade_start = time.monotonic()

    def tick(self):
        now = time.monotonic()
        if self.mode != "idle":
            progress = (now - self.fade_start) / self.fade_seconds
            if progress > 1.0:
                progress = 1.0
            if self.mode == "out":
                b = 1.0 - progress
                self.effects[self.idx].set_brightness(b)
                self.scroller.set_brightness(b)
                if progress >= 1.0:
                    self.effects[self.idx].tilegrid.hidden = True
                    self.idx = (self.idx + 1) % len(self.effects)
                    self.effects[self.idx].set_brightness(0.0)
                    self.effects[self.idx].tilegrid.hidden = False
                    self.scroller.set_text(self.pending_text)
                    self.pending_text = None
                    self.mode = "in"
                    self.fade_start = now
            else:  # "in"
                self.effects[self.idx].set_brightness(progress)
                self.scroller.set_brightness(progress)
                if progress >= 1.0:
                    self.effects[self.idx].set_brightness(1.0)
                    self.scroller.set_brightness(1.0)
                    self.mode = "idle"
        self.effects[self.idx].tick()
        self.scroller.tick()


coordinator = EffectCoordinator(matrix.display, scroller, [fireworks, flame], fade_sec=2.2)


def connected(client):
    log.info("Connected to Adafruit IO")
    client.subscribe(MQTT_FEED)

def disconnected(client):
    log.warning("Disconnected from Adafruit IO")


def subscribe(client, userdata, topic, granted_qos):
    log.info("Subscribed to %s with QOS level %s", topic, granted_qos)


def message(client, feed_id, payload):
    log.info("Feed %s received: %r", feed_id, payload)
    coordinator.request_message(payload)


mqtt = MQTT.MQTT(
    broker=MQTT_HOST,
    port=MQTT_PORT,
    username=MQTT_USERNAME,
    password=MQTT_PASSWORD,
    is_ssl=(MQTT_PORT == 8883),
    socket_pool=pool,
    ssl_context=ssl_context,
    socket_timeout=0.01,
)

io = IO_MQTT(mqtt)
io.on_connect = connected
io.on_disconnect = disconnected
io.on_subscribe = subscribe
io.on_message = message

log.info("Connecting to MQTT broker...")
io.connect()

while True:
    try:
        io.loop(timeout=0.01)
    except Exception as e:
        log.error("MQTT error: %s — reconnecting...", e)
        try:
            wifi.reset()
            io.reconnect()
        except Exception as e2:
            log.error("Reconnect failed: %s", e2)
    coordinator.tick()