import os
import signal
import logging
import asyncio
import time
from pathlib import Path

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
# `MQTT_STATUS_TOPIC` is intentionally NOT in REQUIRED_KEYS: it has a
# derived default of `f"{MQTT_TOPIC}-status"` (see `_status_topic_resolved`
# below) and operators shouldn't have to set an empty string just to get
# the default. Set it explicitly only if you want a different topic name.
cfg = get_config(REQUIRED_KEYS)

from lib_shared.log_setup import configure_logging

configure_logging(getattr(logging, os.getenv("LOG_LEVEL", "INFO")))
log = logging.getLogger("heart")

from rgb_matrix_display import MatrixDisplay
from scroller import MatrixScroller
from lib_shared.patterns.heartbeat import Heartbeat
from lib_shared.message_manager import MessageManager
from lib_shared.boot_config import short_sha
from lib_shared.paho_mqtt_client import PahoMqttClient
from lib_shared.effects_coordinator import EffectsCoordinator, build_effects
from lib_shared.models import EffectsSettings, TextSettings
from status import StatusSnapshot, make_status_writer
from status_publisher import StatusPublisher

# LINDSAY50_ACTIVE_SHA is the SHA the loader started us with.
# `check_for_update` reads it; we also include it in status.json.
_ACTIVE_SHA = os.environ.get("LINDSAY50_ACTIVE_SHA", "")
# LINDSAY50_REPO_DIR lets us know where the repo lives. Default
# to the conventional Pi path so a manual `python3 main.py` run
# works for development.
_REPO_DIR = Path(os.environ.get("LINDSAY50_REPO_DIR", "/srv/lindsay-50"))
_STARTED_AT_MONOTONIC = time.monotonic()
_STARTED_AT_ISO = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()) or ""


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


def _on_check_for_update() -> None:
    """Handle a `command=check-for-update` envelope.

    Compares the SHA the loader started us with (LINDSAY50_ACTIVE_SHA)
    to Flask's expected SHA. On mismatch, `os.execvpe`s into the loader
    — the loader then stages the new SHA, probes via status.json,
    swaps, and execs us again with a fresh env. Same env vars, new
    SHA. No MQTT logic needed in the loader.
    """
    from check_for_update import check_for_update as _cfu

    _cfu(
        api_url=cfg.MESSAGES_API_URL,
        api_key=cfg.API_SECRET_KEY,
    )


# Build the manager first — the coordinator needs it as a constructor
# arg, and the manager doesn't depend on the display.
manager = MessageManager(
    messages_api_url=cfg.MESSAGES_API_URL,
    config_api_url=cfg.CONFIG_API_URL,
    api_key=cfg.API_SECRET_KEY,
    on_change=_on_change,
    on_check_for_update=_on_check_for_update,
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
    # MediaCycler wiring (issue #38). The coordinator constructs a
    # MediaCycler at the out→in transition when the picked message
    # has a non-empty `media` list; the cycler fetches each
    # attachment via "{MEDIA_API_BASE_URL}/api/media/{s3_key}".
    # Default to deriving the origin from MESSAGES_API_URL when
    # MEDIA_API_BASE_URL isn't set (operators usually point all
    # three URLs at the same Flask host).
    media_api_base_url=cfg.if_exists("MEDIA_API_BASE_URL") or cfg.MESSAGES_API_URL.rsplit("/api/", 1)[0],
    media_cache_dir=str(_REPO_DIR / "data" / "media-cache"),
    # X-API-Key for the MediaCycler's Flask fetcher. Flask's
    # `/api/media/<key>` route is gated by `@api_login_required`,
    # which checks `X-API-Key` before falling through to the
    # browser session — the Pi has no session cookie, so without
    # this header every media fetch 401s and the cycler drops the
    # item (D12 codec-failure semantics). Same value the Flask
    # server reads as `cfg.API_SECRET_KEY` in its auth.py:82 lookup.
    media_api_key=cfg.API_SECRET_KEY,
)

# Kick off the boot splash. The coordinator's first pull (every 250 ms)
# produces the most recent message in the manager's buffer; no
# separate "show this body after the heart" hook is needed.
coordinator.start()


# Status writer — the loader probes us by reading this file (see
# loader.py: probe). One tick per render loop iteration; the writer
# is self-throttled to DEFAULT_TICK_INTERVAL_S so SD-card write
# amplification is bounded. StatusSnapshot (the dataclass) lives in
# status.py alongside the writer; this closure sources the live
# values from the manager + thread state at write time.
def _is_mqtt_connected() -> bool:
    """Best-effort MQTT liveness check.

    We don't have a public `is_connected()` on PahoMqttClient (and
    paho keeps it private too). The pragmatic signal is "the
    subscriber thread is still alive" — which is True unless the
    daemon thread died. The loader's status.json probe tolerates
    either True or False as long as the rest of the snapshot is
    healthy; this is just a soft signal.
    """
    thread = getattr(_mqtt_client, "_thread", None)
    return bool(thread is not None and thread.is_alive())


def _build_status_snapshot() -> StatusSnapshot:
    """Build a fresh snapshot of the app's runtime state.

    Final shape (Decision 10): 8 keys — schema_version, active_sha,
    short_sha, started_at, updated_at, uptime_seconds (int),
    mqtt_connected, last_error. The previous pid, messages_rendered,
    and last_tick_age_ms fields had no consumer and were dropped;
    the `_LAST_TICK_MONOTONIC` global and the `_msgs` deque read were
    dropped too. `short_sha` is derived once at write time from
    `lib_shared.boot_config.short_sha` (the single source of truth
    for the 7-char truncation).
    """
    now_monotonic = time.monotonic()
    return StatusSnapshot(
        schema_version=1,
        active_sha=_ACTIVE_SHA,
        short_sha=short_sha(_ACTIVE_SHA),
        started_at=_STARTED_AT_ISO,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()) or "",
        uptime_seconds=int(now_monotonic - _STARTED_AT_MONOTONIC),
        mqtt_connected=_is_mqtt_connected(),
        last_error=None,
    )


# Derive the MQTT status topic from MQTT_TOPIC if not set explicitly.
# Default rule (Decision 2 in openspec/changes/archive/2026-07-09-add-sign-status-reports/
# design.md): "{MQTT_TOPIC}-status". Operators can override either via
# settings.toml or the `MQTT_STATUS_TOPIC` env var (env wins via
# config_reader.get_raw). Adafruit IO requires an explicit feed
# creation — the operator must create the derived feed in the AIO
# dashboard before the first publish lands.
#
# `cfg.if_exists(...)` (not `cfg.MQTT_STATUS_TOPIC`) — `MQTT_STATUS_TOPIC`
# is intentionally NOT in REQUIRED_KEYS so it has no attribute-style
# accessor; `if_exists` returns None for unset keys instead of raising.
_status_topic_resolved = cfg.if_exists("MQTT_STATUS_TOPIC") or f"{cfg.MQTT_TOPIC}-status"

_status_publisher = StatusPublisher(
    host=cfg.MQTT_HOST,
    port=cfg.MQTT_PORT,
    username=cfg.MQTT_USERNAME,
    password=cfg.MQTT_PASSWORD,
    topic=_status_topic_resolved,
)
log.info(
    "StatusPublisher publishing to topic=%r at 5s cadence (unified with .status.json writes)",
    _status_topic_resolved,
)


def _publish_status_envelope(payload: dict) -> None:
    """Hand the serialized payload to the long-lived StatusPublisher.

    Defined as a thin wrapper so `make_status_writer` can be called
    with a single dispatch argument; the wrapper swallows return
    values (StatusPublisher.publish already logs failures).
    """
    _status_publisher.publish(payload)


status_writer = make_status_writer(
    repo_dir=_REPO_DIR,
    snapshot_builder=_build_status_snapshot,
    status_publisher=_publish_status_envelope,
)


# SIGTERM (systemd stop / `kill`) doesn't raise an exception by default, so the
# `finally` below would never run. Turn it into SystemExit so cleanup happens on
# every stop path; SIGINT (Ctrl-C) already raises KeyboardInterrupt.
def _on_sigterm(_signum, _frame):  # type: ignore  # noqa: ARG001 — signal handler signature
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _on_sigterm)


try:
    while True:
        coordinator.tick()
        status_writer.tick()
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
    try:
        _status_publisher.close()
        log.info("status publisher closed")
    except Exception:
        log.exception("failed to close status publisher on shutdown")
