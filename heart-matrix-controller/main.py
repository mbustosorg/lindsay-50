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
from lib_shared.patterns.hyperspace import Hyperspace
from lib_shared.patterns.heartbeat import Heartbeat
from lib_shared.message_manager import MessageManager
from lib_shared.paho_mqtt_client import PahoMqttClient
from lib_shared.effects_coordinator import EffectsCoordinator, build_effects
from lib_shared.models import EffectsSettings, TextSettings

display = MatrixDisplay()
# The scroller takes its text settings from the v2 config. The boot-time
# defaults are the same TextSettings().to_dict() values the admin UI
# would write; the v2 envelope that arrives over MQTT shortly after
# re-binds color and speed via `scroller.set_color()` and
# `scroller.set_speed()`.
text_settings = TextSettings()
scroller = MatrixScroller(
    display,
    color=text_settings.color,
    speed=text_settings.speed,
)
heartbeat = Heartbeat(display)


def _build_effects(settings):
    """Build the rotation list from the v2 EffectsSettings config.

    Delegates to `build_effects` (the shared orchestrator) which uses
    `lib_shared.effects_factory.make_effect_class` to resolve each
    enabled name. Falls back to Hyperspace if the result is empty
    (e.g. all effects disabled in the admin UI), so the sign never
    goes dark.
    """
    out = build_effects(settings, display=display)
    if not out:
        log.warning("No effects enabled in config; falling back to Hyperspace")
        out = [Hyperspace(display)]
    return out


# Boot with the default effect settings (the v2 config arrives over MQTT
# shortly after and refreshes the rotation + scroller + pacing).
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
    # Re-bind pacing + recent_count in place.
    coordinator.apply_settings(new_cfg.effect_settings)
    # Apply text settings to the scroller.
    ts = new_cfg.text_settings
    scroller.set_color(ts.color)
    scroller.set_speed(ts.speed)
    log.info(
        "Applied config update: %d effects, text_color=#%06x, speed=%d",
        len(new_effects),
        ts.color,
        ts.speed,
    )


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
