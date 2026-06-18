import os
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
from lib_shared.patterns.heartbeat import Heartbeat
from lib_shared.message_manager import MessageManager
from lib_shared.paho_mqtt_client import PahoMqttClient
from lib_shared.effects_coordinator import EffectsCoordinator, build_effects
from lib_shared.models import EffectsSettings, TextSettings


def _on_change():
    """Re-render the message table when the buffer changes.

    Wired as the MessageManager's universal `on_change` callback. Fires
    for every `_emit_change()` (new message, config update, etc.).
    The coordinator has no state of its own that needs an explicit
    sync — it reads the manager's config and buffer at every `tick()`,
    so message-only emits do not need any action here. The
    coordinator's own `tick()` is the single point that applies
    config changes (rotation rebuild + scroller color/speed,
    hash-guarded so message-only ticks cost only a small repr).
    """
    return None


# Build the manager first — the coordinator needs it as a constructor
# arg, and the manager doesn't depend on the display.
manager = MessageManager(
    messages_api_url=cfg.MESSAGES_API_URL,
    config_api_url=cfg.CONFIG_API_URL,
    api_key=cfg.API_SECRET_KEY,
    on_change=_on_change,
)

asyncio.run(manager.seed())


# Platform MQTT client (paho on every platform)
_mqtt_client = PahoMqttClient(
    dispatch_callback=manager.dispatch,
    host=cfg.MQTT_HOST,
    port=cfg.MQTT_PORT,
    username=cfg.MQTT_USERNAME,
    password=cfg.MQTT_PASSWORD,
    topic=cfg.MQTT_TOPIC,
)
logging.info("Starting MQTT client at boot...")
_mqtt_client.start()


display = MatrixDisplay()
# The scroller takes its text settings from the v2 config. The boot-time
# defaults are the same TextSettings().to_dict() values the admin UI
# would write; the v2 envelope that arrives over MQTT shortly after
# re-binds color and speed via the coordinator's tick-time
# `_sync_render_layer()`.
text_settings = TextSettings()
scroller = MatrixScroller(
    display,
    color=text_settings.color,
    speed=text_settings.speed,
)
heartbeat = Heartbeat(display)


# Boot with the default effect settings (the v2 config arrives over MQTT
# shortly after and refreshes the rotation + scroller + pacing). The
# shared `build_effects` falls back to the first canonical effect if
# the rotation ends up empty, so the sign never goes dark.
_boot_settings = EffectsSettings()
effects = build_effects(_boot_settings, display=display)

coordinator = EffectsCoordinator(
    message_manager=manager,
    display=display,
    scroller=scroller,
    effects=effects,
    heart=heartbeat,
)

# Kick off the boot splash. The coordinator's first pull (every 250 ms)
# produces the most recent message in the manager's buffer; no
# separate "show this body after the heart" hook is needed.
coordinator.start()


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
