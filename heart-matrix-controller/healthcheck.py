"""App-owned health check for the matrix controller.

Runs the same init sequence as main.py but exits 0/non-0 instead of
entering the render loop. Used by `loader.py` to validate a staged
version before swapping the active symlink (D4 in design.md).

The loader only sees the exit code — it doesn't care what was
checked. As we add checks (frame hash, MQTT message receipt, systemd
watchdog), they go into this same function. The loader never changes.

Each check is independently injectable so tests can mock the heavy
dependencies (rgbmatrix GPIO init, broker reachability, Flask REST
endpoints) without spinning up a real Pi.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _check_display(display_factory: Optional[Callable] = None) -> bool:
    """Construct the display to verify rgbmatrix can init GPIO.

    Args:
        display_factory: Optional zero-arg callable that returns a
            display object. Defaults to `MatrixDisplay()` from
            rgb_matrix_display. Tests pass a mock factory.
    """
    if display_factory is None:
        from rgb_matrix_display import MatrixDisplay  # type: ignore[import-not-found]

        display_factory = MatrixDisplay
    try:
        display_factory()
        logger.info("[healthcheck] Display() OK")
        return True
    except Exception as e:
        logger.error("[healthcheck] Display() failed: %s", e)
        return False


def _check_mqtt_broker(
    host: str,
    port: int,
    username: str,
    password: str,
    timeout: float = 5.0,
) -> bool:
    """Verify the MQTT broker accepts a CONNACK.

    Does not subscribe — opens a short-lived client just to test
    reachability and credentials. Returns True on CONNACK rc==0
    within `timeout` seconds.
    """
    try:
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
    except ImportError as e:
        logger.error("[healthcheck] paho-mqtt not available: %s", e)
        return False
    try:
        client = mqtt.Client(clean_session=True)
        client.username_pw_set(username, password)
        if int(port) == 8883:
            client.tls_set_context()

        rc_holder: list[int] = []

        def _on_connect(_client, _userdata, _flags, rc):
            rc_holder.append(rc)
            try:
                _client.disconnect()
            except Exception:
                pass

        client.on_connect = _on_connect  # type: ignore[reportAttributeAccessIssue]
        client.connect(host, int(port), keepalive=10)
        client.loop_start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not rc_holder:
            time.sleep(0.1)
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

        if not rc_holder:
            logger.error(
                "[healthcheck] MQTT broker %s:%s did not respond within %.1fs",
                host,
                port,
                timeout,
            )
            return False
        if rc_holder[0] != 0:
            logger.error(
                "[healthcheck] MQTT broker %s:%s refused: rc=%s",
                host,
                port,
                rc_holder[0],
            )
            return False
        logger.info("[healthcheck] MQTT broker %s:%s OK", host, port)
        return True
    except Exception as e:
        logger.error("[healthcheck] MQTT broker %s:%s unreachable: %s", host, port, e)
        return False


def _check_rest_seed(seed_coro_fn: Callable) -> bool:
    """Run `MessageManager.seed()` to verify Flask REST endpoints are reachable.

    Args:
        seed_coro_fn: callable returning a coroutine that performs the
            seed. Tests pass a coroutine that mocks both endpoints.
    """
    try:
        asyncio.run(seed_coro_fn())
        logger.info("[healthcheck] MessageManager.seed() OK")
        return True
    except Exception as e:
        logger.error("[healthcheck] MessageManager.seed() failed: %s", e)
        return False


def run_healthcheck(
    display_factory: Optional[Callable] = None,
    mqtt_check_fn: Optional[Callable[[], bool]] = None,
    seed_coro_fn: Optional[Callable] = None,
    *,
    # Real-deps wiring — only used when the optional callables above
    # are not provided. Tests ignore these and pass mocks directly.
    mqtt_host: str = "localhost",
    mqtt_port: int = 1883,
    mqtt_username: str = "",
    mqtt_password: str = "",
) -> bool:
    """Run every health check. Returns True iff all pass.

    Each check is independently injectable so unit tests can drive
    failure cases (broker unreachable, REST timeout, rgbmatrix GPIO
    error) without touching real hardware or networks.

    Args:
        display_factory: zero-arg factory returning a display. Default
            constructs the real `MatrixDisplay()`.
        mqtt_check_fn: zero-arg callable returning bool. Default runs
            the real `_check_mqtt_broker` with the `mqtt_*` params.
        seed_coro_fn: zero-arg callable returning a coroutine. Default
            constructs a real `MessageManager` and calls `.seed()`.
        mqtt_host/port/username/password: broker coordinates used only
            when `mqtt_check_fn` is not provided.
    """
    logger.info("[healthcheck] starting...")
    ok = True
    ok &= _check_display(display_factory)
    if mqtt_check_fn is None:

        def _real_mqtt_check() -> bool:
            return _check_mqtt_broker(
                mqtt_host,
                mqtt_port,
                mqtt_username,
                mqtt_password,
            )

        mqtt_check_fn = _real_mqtt_check
    ok &= mqtt_check_fn()
    if seed_coro_fn is not None:
        ok &= _check_rest_seed(seed_coro_fn)
    if ok:
        logger.info("[healthcheck] all checks PASSED")
    else:
        logger.error("[healthcheck] one or more checks FAILED")
    return bool(ok)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argparse for `--healthcheck` and friends."""
    parser = argparse.ArgumentParser(
        description="Heart matrix controller (health-check mode)",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Run health checks and exit (0 on success, 1 on failure).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point. Returns process exit code (0 success, 1 failure).

    When `--healthcheck` is set, loads the device's settings.toml and
    runs every check against the real broker + Flask REST endpoints.
    On success returns 0, on any failure returns 1. The loader invokes
    this via subprocess and only inspects the exit code.
    """
    args = _build_arg_parser().parse_args(argv)
    if args.healthcheck:
        # Lazy import — `lib_shared.config_reader` requires the
        # settings.toml of the device path. Importing it at the
        # module top would force every test to provide a settings
        # file even when only the pure-function helpers are tested.
        from lib_shared.config_reader import get_config  # type: ignore[import-not-found]

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

        # Wire a real seed function so the check exercises Flask REST.
        # The `--healthcheck` path is a subprocess run by the loader —
        # not a unit-test path — so depending on the real MessageManager
        # here is correct.
        from lib_shared.message_manager import MessageManager  # type: ignore[import-not-found]

        manager = MessageManager(
            messages_api_url=cfg.MESSAGES_API_URL,
            config_api_url=cfg.CONFIG_API_URL,
            api_key=cfg.API_SECRET_KEY,
        )

        ok = run_healthcheck(
            mqtt_host=cfg.MQTT_HOST,
            mqtt_port=int(cfg.MQTT_PORT),
            mqtt_username=cfg.MQTT_USERNAME,
            mqtt_password=cfg.MQTT_PASSWORD,
            seed_coro_fn=manager.seed,
        )
        return 0 if ok else 1
    # Without --healthcheck, this entrypoint has no other behavior —
    # main.py is the real controller entrypoint. Help/usage is handled
    # by argparse automatically when invoked with --help or no args.
    return 0


if __name__ == "__main__":
    sys.exit(main())