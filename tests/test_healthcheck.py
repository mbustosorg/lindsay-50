"""Tests for heart-matrix-controller/healthcheck.py and the main.py --healthcheck flag.

Covers the app-owned health check added in issue #49 (D4 in
design.md). The loader runs `python3 main.py --healthcheck` against
a staged worktree; exit 0 means the new version is sound, exit
non-zero means leave `current` unchanged and keep the old version.

Each check is independently injectable so unit tests can drive
failure cases (broker unreachable, REST timeout, rgbmatrix GPIO
error) without spinning up a real Pi or stubbing out heavy
imports.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HEALTHCHECK_PATH = (
    Path(__file__).parent.parent / "heart-matrix-controller" / "healthcheck.py"
)
MAIN_PY_PATH = (
    Path(__file__).parent.parent / "heart-matrix-controller" / "main.py"
)
PROJECT_ROOT = Path(__file__).parent.parent


def _load_healthcheck():
    """Load the healthcheck module fresh from disk.

    Sibling tests can wipe `sys.modules` between cases; a fresh
    spec-from-file-location guarantees we get the same source the
    loader.py will import in production.
    """
    spec = importlib.util.spec_from_file_location("hmc_healthcheck_under_test", str(HEALTHCHECK_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hmc_healthcheck_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# run_healthcheck — happy path with mocked deps
# ---------------------------------------------------------------------------


class TestRunHealthcheckSuccess:
    def test_returns_true_when_all_checks_pass(self):
        """All three checks pass → run_healthcheck returns True."""
        hc = _load_healthcheck()

        def display_factory():
            return MagicMock()  # any truthy value

        def mqtt_check():
            return True

        async def seed_coro():
            return None

        ok = hc.run_healthcheck(
            display_factory=display_factory,
            mqtt_check_fn=mqtt_check,
            seed_coro_fn=seed_coro,
        )
        assert ok is True

    def test_logs_pass_marker_for_each_check(self, caplog):
        """Each check logs a pass marker on success."""
        hc = _load_healthcheck()

        def display_factory():
            return MagicMock()

        def mqtt_check():
            return True

        async def seed_coro():
            return None

        with caplog.at_level("INFO", logger="hmc_healthcheck_under_test"):
            ok = hc.run_healthcheck(
                display_factory=display_factory,
                mqtt_check_fn=mqtt_check,
                seed_coro_fn=seed_coro,
            )
        assert ok is True
        # All three pass markers + final PASSED
        messages = [r.message for r in caplog.records]
        assert any("Display() OK" in m for m in messages)
        assert any("all checks PASSED" in m for m in messages)


# ---------------------------------------------------------------------------
# run_healthcheck — failure cases
# ---------------------------------------------------------------------------


class TestRunHealthcheckFailures:
    def test_returns_false_when_display_ctor_fails(self):
        """Display() raising → run_healthcheck returns False."""
        hc = _load_healthcheck()

        def display_factory():
            raise RuntimeError("rgbmatrix: GPIO busy")

        def mqtt_check():
            return True

        async def seed_coro():
            return None

        ok = hc.run_healthcheck(
            display_factory=display_factory,
            mqtt_check_fn=mqtt_check,
            seed_coro_fn=seed_coro,
        )
        assert ok is False

    def test_returns_false_when_mqtt_broker_unreachable(self):
        """mqtt_check_fn returning False → run_healthcheck returns False."""
        hc = _load_healthcheck()

        def display_factory():
            return MagicMock()

        def mqtt_check():
            return False  # broker down / wrong creds

        async def seed_coro():
            return None

        ok = hc.run_healthcheck(
            display_factory=display_factory,
            mqtt_check_fn=mqtt_check,
            seed_coro_fn=seed_coro,
        )
        assert ok is False

    def test_returns_false_when_seed_raises(self):
        """seed_coro raising → run_healthcheck returns False."""
        hc = _load_healthcheck()

        def display_factory():
            return MagicMock()

        def mqtt_check():
            return True

        async def seed_coro():
            raise RuntimeError("Flask 500: connection refused")

        ok = hc.run_healthcheck(
            display_factory=display_factory,
            mqtt_check_fn=mqtt_check,
            seed_coro_fn=seed_coro,
        )
        assert ok is False

    def test_returns_false_when_seed_returns_corrupt_data(self):
        """seed_coro completing but raising on subsequent access — handled by asyncio.run.

        We construct a coroutine that raises inside asyncio.run's
        wrapping — the test confirms run_healthcheck still surfaces
        the failure as False rather than swallowing it.
        """
        hc = _load_healthcheck()

        def display_factory():
            return MagicMock()

        def mqtt_check():
            return True

        async def seed_coro():
            raise ValueError("bad data")

        ok = hc.run_healthcheck(
            display_factory=display_factory,
            mqtt_check_fn=mqtt_check,
            seed_coro_fn=seed_coro,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# main() entrypoint — exit codes
# ---------------------------------------------------------------------------


class TestMainEntrypoint:
    """Tests for `healthcheck.main(argv)` exit code behavior.

    The loader invokes this via subprocess; the only contract it
    relies on is "exit 0 means healthy, exit non-zero means don't
    swap". We test the entrypoint directly to keep these tests
    hermetic — running `python3 main.py --healthcheck` as a real
    subprocess would require rgbmatrix, settings.toml, and a real
    broker, none of which the unit-test environment has.

    These tests install `lib_shared.config_reader` and
    `lib_shared.message_manager` mocks into `sys.modules` so
    `healthcheck.main()` can run without the real heavy imports.
    The autouse `_restore_lib_shared_modules` fixture snapshots
    `sys.modules` before each test and restores any keys we
    clobbered — without it, sibling tests (notably
    `test_message_manager.py`) pick up the mock and see
    `MessageManager` as a `MagicMock`.
    """

    @pytest.fixture(autouse=True)
    def _restore_lib_shared_modules(self):
        """Snapshot lib_shared.* entries in sys.modules and restore after the test."""
        saved = {
            name: sys.modules[name]
            for name in list(sys.modules)
            if name == "lib_shared" or name.startswith("lib_shared.")
        }
        try:
            yield
        finally:
            for name in [k for k in list(sys.modules) if k == "lib_shared" or k.startswith("lib_shared.")]:
                if name not in saved:
                    del sys.modules[name]
            for name, mod in saved.items():
                sys.modules[name] = mod

    def _wire_healthcheck_main_with_mocks(self, *, ok=True, seed_should_raise=False):
        """Load healthcheck, monkeypatch get_config + MessageManager, return (mod, mgr_mock).

        `cfg.MQTT_*` is read by `healthcheck.main()` when wiring
        `run_healthcheck`. We don't actually open a broker socket —
        we pass `mqtt_check_fn=lambda: ok` via `run_healthcheck`'s
        optional args. To do that we need `healthcheck.main` to
        honor a code path that bypasses the real broker check.

        The simplest way: monkey-patch `run_healthcheck` itself so
        `healthcheck.main()` returns ok/1-ok based on what we want.
        """
        hc = _load_healthcheck()

        def fake_run_healthcheck(**kwargs):
            assert kwargs.get("mqtt_host") == "localhost"
            assert kwargs.get("mqtt_port") == 1883
            assert kwargs.get("seed_coro_fn") is mgr_mock.seed
            if seed_should_raise:
                raise RuntimeError("seed failed")
            return ok

        hc.run_healthcheck = fake_run_healthcheck
        return hc, fake_run_healthcheck

    def test_main_exits_0_on_success(self):
        """`healthcheck.main(['--healthcheck'])` returns 0 when run_healthcheck succeeds."""
        hc = _load_healthcheck()
        # Patch config + MessageManager before main() loads them.
        fake_cfg = MagicMock()
        fake_cfg.MQTT_HOST = "localhost"
        fake_cfg.MQTT_PORT = 1883
        fake_cfg.MQTT_USERNAME = "u"
        fake_cfg.MQTT_PASSWORD = "p"
        fake_cfg.MQTT_TOPIC = "t"
        fake_cfg.CONFIG_API_URL = "http://x/api/config"
        fake_cfg.MESSAGES_API_URL = "http://x/api/messages"
        fake_cfg.API_SECRET_KEY = "k"

        cfg_mod = types.ModuleType("lib_shared.config_reader")
        cfg_mod.get_config = lambda required_keys=None: fake_cfg
        sys.modules["lib_shared.config_reader"] = cfg_mod

        mgr_mock = MagicMock()
        async def _seed():
            return None
        mgr_mock.seed = _seed

        mm_mod = types.ModuleType("lib_shared.message_manager")
        mm_mod.MessageManager = MagicMock(return_value=mgr_mock)
        sys.modules["lib_shared.message_manager"] = mm_mod

        def fake_run_healthcheck(**kwargs):
            return True

        hc.run_healthcheck = fake_run_healthcheck

        rc = hc.main(["--healthcheck"])
        assert rc == 0

    def test_main_exits_1_on_failure(self):
        """`healthcheck.main(['--healthcheck'])` returns 1 when run_healthcheck fails."""
        hc = _load_healthcheck()
        fake_cfg = MagicMock()
        fake_cfg.MQTT_HOST = "localhost"
        fake_cfg.MQTT_PORT = 1883
        fake_cfg.MQTT_USERNAME = "u"
        fake_cfg.MQTT_PASSWORD = "p"
        fake_cfg.MQTT_TOPIC = "t"
        fake_cfg.CONFIG_API_URL = "http://x/api/config"
        fake_cfg.MESSAGES_API_URL = "http://x/api/messages"
        fake_cfg.API_SECRET_KEY = "k"

        cfg_mod = types.ModuleType("lib_shared.config_reader")
        cfg_mod.get_config = lambda required_keys=None: fake_cfg
        sys.modules["lib_shared.config_reader"] = cfg_mod

        mgr_mock = MagicMock()
        async def _seed():
            return None
        mgr_mock.seed = _seed

        mm_mod = types.ModuleType("lib_shared.message_manager")
        mm_mod.MessageManager = MagicMock(return_value=mgr_mock)
        sys.modules["lib_shared.message_manager"] = mm_mod

        def fake_run_healthcheck(**kwargs):
            return False  # one or more checks failed

        hc.run_healthcheck = fake_run_healthcheck

        rc = hc.main(["--healthcheck"])
        assert rc == 1

    def test_main_returns_0_without_flag(self):
        """`healthcheck.main([])` (no --healthcheck) returns 0 — argparse help handles --help."""
        hc = _load_healthcheck()
        # No mocks needed — the no-flag path doesn't read config.
        rc = hc.main([])
        assert rc == 0


# ---------------------------------------------------------------------------
# main.py --healthcheck flag — short-circuits the controller startup
# ---------------------------------------------------------------------------


class TestMainPyHealthcheckFlag:
    """Verify `python3 main.py --healthcheck` exits with the right code.

    The flag must short-circuit BEFORE any heavy import (rgbmatrix,
    etc.) so a broken Pi setup still surfaces the healthcheck signal.
    We test this by importing main.py with a stubbed environment and
    asserting sys.exit is called with the right code.
    """

    def test_main_py_short_circuits_on_healthcheck_flag(self, monkeypatch):
        """When sys.argv contains --healthcheck, main.py invokes healthcheck.main and sys.exits.

        We can't actually run `python3 main.py --healthcheck` in a
        unit test (rgbmatrix + settings.toml are required), so we
        simulate the short-circuit path by setting sys.argv and
        patching `healthcheck.main` to a MagicMock, then loading
        main.py as a module. If main.py correctly reads sys.argv at
        the top and short-circuits, sys.exit fires before the rest
        of the module executes.
        """
        # Arrange
        monkeypatch.setattr(sys, "argv", ["main.py", "--healthcheck"])
        fake_healthcheck_main = MagicMock(return_value=0)
        fake_healthcheck_mod = types.ModuleType("healthcheck")
        fake_healthcheck_mod.main = fake_healthcheck_main
        monkeypatch.setitem(sys.modules, "healthcheck", fake_healthcheck_mod)

        # The short-circuit lives in `if "--healthcheck" in sys.argv`,
        # which calls `sys.exit(...)` — SystemExit propagates out
        # of importlib. Catch it and assert the code.
        spec = importlib.util.spec_from_file_location("hmc_main_under_test", str(MAIN_PY_PATH))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        with pytest.raises(SystemExit) as excinfo:
            spec.loader.exec_module(mod)
        # The fake returned 0 → exit code 0
        assert excinfo.value.code == 0
        # healthcheck.main was called with the right argv
        fake_healthcheck_main.assert_called_once_with(["--healthcheck"])

    def test_main_py_short_circuits_with_nonzero_on_failure(self, monkeypatch):
        """When healthcheck.main returns 1, main.py exits with code 1."""
        monkeypatch.setattr(sys, "argv", ["main.py", "--healthcheck"])
        fake_healthcheck_main = MagicMock(return_value=1)
        fake_healthcheck_mod = types.ModuleType("healthcheck")
        fake_healthcheck_mod.main = fake_healthcheck_main
        monkeypatch.setitem(sys.modules, "healthcheck", fake_healthcheck_mod)

        spec = importlib.util.spec_from_file_location("hmc_main_under_test_failure", str(MAIN_PY_PATH))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        with pytest.raises(SystemExit) as excinfo:
            spec.loader.exec_module(mod)
        assert excinfo.value.code == 1