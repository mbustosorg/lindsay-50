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
from lib_shared.models import TextSettings

display = MatrixDisplay()
# The scroller takes its text settings from the v2 config. We pass defaults
# here; `coordinator` reads `text_settings` from the same config and pushes
# color / frame_delay / offset_seconds updates through `_apply_text_settings`
# whenever a new config envelope arrives.
text_settings = TextSettings()
scroller = MatrixScroller(
    display,
    color=text_settings.color,
    frame_delay=text_settings.frame_delay,
    offset_seconds=text_settings.offset_seconds,
)
heartbeat = Heartbeat(display)


# Map from canonical effect name to its concrete class. The key matches
# `_DEFAULT_EFFECTS_LIST_FULL` in lib_shared.models so config-driven
# enabled flags and rotation order stay aligned with the Flask admin UI.
_EFFECT_CLASSES = {
    "Hyperspace": Hyperspace,
    "VideoDisplay": VideoDisplay,
    "PngDisplay": PngDisplay,
    "Honeycomb": Honeycomb,
    "Flame": Flame,
    "Fireworks": Fireworks,
    "NightSky": NightSky,
}


def _build_effects(settings):
    """Build the rotation list from the v2 EffectsSettings config.

    Reads `settings.effects` (a list of {name, enabled} dicts) and instantiates
    one of each enabled name in the listed order. Unknown names are skipped
    with a warning; disabled entries are dropped entirely.
    """
    out = []
    for entry in settings.effects or []:
        if not entry.get("enabled"):
            continue
        cls = _EFFECT_CLASSES.get(entry["name"])
        if cls is None:
            log.warning("Unknown effect in config: %r (skipped)", entry.get("name"))
            continue
        out.append(cls(display))
    if not out:
        log.warning("No effects enabled in config; falling back to Hyperspace")
        out = [Hyperspace(display)]
    return out


# Boot with the default effect settings (the v2 config arrives over MQTT
# shortly after and refreshes the rotation + scroller + pacing).
from lib_shared.models import EffectsSettings

_boot_settings = EffectsSettings()
effects = _build_effects(_boot_settings)

coordinator = EffectsCoordinator(
    display,
    scroller,
    effects,
    heart=heartbeat,
    recent_provider=lambda: _message_mgr.get_messages(limit=5),
    settings=_boot_settings,
)


def _on_message(msg):
    """Forward a freshly-received SMS to the coordinator.

    Wired up below once `_message_mgr` exists.
    """
    coordinator.set_text(msg.body)


_message_mgr = MessageManager(
    messages_api_url=cfg.MESSAGES_API_URL,
    config_api_url=cfg.CONFIG_API_URL,
    api_key=cfg.API_SECRET_KEY,
    on_message=_on_message,
)


def _on_config_update(cfg_dict):
    """Apply a freshly-received config dict to the coordinator + scroller."""
    from lib_shared.models import SignConfig

    new_cfg = SignConfig.from_dict(cfg_dict or {})
    new_effects = _build_effects(new_cfg.effect_settings)
    coordinator.effects = new_effects
    coordinator.idx = -1  # next fade picks the head of the new list
    # Re-bind pacing knobs.
    coordinator.fade_seconds = new_cfg.effect_settings.fade_seconds
    coordinator.hold_seconds = new_cfg.effect_settings.hold_seconds
    coordinator.intro_seconds = new_cfg.effect_settings.intro_seconds
    coordinator.idle_seconds = new_cfg.effect_settings.idle_seconds
    coordinator.recent_count = new_cfg.effect_settings.recent_count
    # Re-size the recent-messages deque.
    from collections import deque

    coordinator._recent = deque(coordinator._recent, maxlen=new_cfg.effect_settings.recent_count)
    # Apply text settings to the scroller.
    ts = new_cfg.text_settings
    scroller._color = ts.color
    scroller.frame_delay = ts.frame_delay
    scroller.offset_seconds = ts.offset_seconds
    log.info("Applied config update: %d effects, text_color=#%06x", len(new_effects), ts.color)


# Wrap MessageManager's dispatch so a config envelope also triggers
# `_on_config_update`. We keep the original `dispatch` for messages.
_orig_dispatch = _message_mgr.dispatch


def _dispatch_with_config(raw: str) -> None:
    import json as _json

    try:
        envelope = _json.loads(raw)
    except Exception:
        return _orig_dispatch(raw)
    if envelope.get("type") == "config":
        _on_config_update(envelope.get("payload") or {})
    _orig_dispatch(raw)


_message_mgr.dispatch = _dispatch_with_config

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
