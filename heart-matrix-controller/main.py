import os
import time
import signal
import logging
import asyncio

# Create the config singleton FIRST: modules imported below (rgb_matrix_display,
# message_manager, and the MQTT client) call get_config() at import time, so it
# must already exist. Wi-Fi is managed by the Pi OS.
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

from rgb_matrix_display import MatrixDisplay
from scroller import MatrixScroller
from lib_shared.patterns.fireworks import Fireworks
from lib_shared.patterns.flame import Flame
from lib_shared.patterns.nightsky import NightSky
from lib_shared.patterns.png_display import PngDisplay
from lib_shared.patterns.video_display import VideoDisplay
from lib_shared.patterns.honeycomb import Honeycomb
from lib_shared.patterns.hyperspace import Hyperspace
from lib_shared.patterns.heartbeat import Heartbeat
from lib_shared.message_manager import MessageManager
from lib_shared.paho_mqtt_client import PahoMqttClient
from lib_shared.effects_coordinator import EffectsCoordinator

display = MatrixDisplay()
scroller = MatrixScroller(display)
fireworks = Fireworks(display)
flame = Flame(display)
nightsky = NightSky(display)
png = PngDisplay(display)
video = VideoDisplay(display)
honeycomb = Honeycomb(display)
hyperspace = Hyperspace(display)
heartbeat = Heartbeat(display)


coordinator = EffectsCoordinator(
    display,
    scroller,
    [hyperspace, video, png, honeycomb, flame, fireworks, nightsky],
    heart=heartbeat,
    recent_provider=lambda: _message_mgr.get_messages(limit=5),
)

_message_mgr = MessageManager(
    messages_api_url=cfg.MESSAGES_API_URL,
    config_api_url=cfg.CONFIG_API_URL,
    api_key=cfg.API_SECRET_KEY,
    on_message=lambda msg: coordinator.request_message(msg.body),
)
asyncio.run(_message_mgr.seed())

# Kick off the boot splash, queuing the most recent seeded message to play once
# the heart fades out.
_recent = _message_mgr.get_messages(limit=1)
_startup_text = _recent[0].message.body if _recent else None
coordinator.start(_startup_text)

# Platform MQTT client (paho on every platform)
_mqtt_client = PahoMqttClient(
    dispatch_callback=_message_mgr.dispatch,
    host=cfg.MQTT_HOST,
    port=cfg.MQTT_PORT,
    username=cfg.MQTT_USERNAME,
    password=cfg.MQTT_PASSWORD,
    topic=cfg.MQTT_TOPIC,
)
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
