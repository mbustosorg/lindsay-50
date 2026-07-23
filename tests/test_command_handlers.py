"""Tests for `heart-matrix-controller/command_handlers` (issue #51).

The Pi registers three handlers via `MessageManager.register_handler`:
`force_upgrade`, `restart`, `shutdown`. Each is a zero-arg callable
that performs its side effect when invoked by the dispatcher.

These tests:
  - Verify the handlers do what they say (force_upgrade execs into
    the loader; restart/shutdown call subprocess with the right
    argv).
  - Verify failure modes are handled gracefully (missing loader
    script → log + return; sudo non-zero exit → log + return).
  - Verify the `MessageManager.register_handler` registry routes
    correctly and isolates exceptions.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_PI_DIR = _PROJECT_ROOT / "heart-matrix-controller"


def _import_command_handlers():
    spec = importlib.util.spec_from_file_location("command_handlers", str(_PI_DIR / "command_handlers.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handlers():
    return _import_command_handlers()


# ---------------------------------------------------------------------------
# force_upgrade
# ---------------------------------------------------------------------------


class TestForceUpgrade:
    def test_exec_calls_os_execvpe_with_loader_path(self, handlers, tmp_path, monkeypatch):
        loader_dir = tmp_path / "heart-matrix-controller"
        loader_dir.mkdir()
        loader_py = loader_dir / "loader.py"
        loader_py.write_text("# fake loader for test\n")

        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))

        with patch.object(handlers.os, "execvpe") as mocked:
            handlers.force_upgrade()

        # execvpe(file, args, env)
        args = mocked.call_args.args
        assert args[0] == sys.executable
        assert args[1] == [sys.executable, str(loader_py)]
        env = args[2]
        assert env["LINDSAY50_REPO_DIR"] == str(tmp_path)

    def test_missing_loader_logs_and_returns(self, handlers, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))
        with caplog.at_level("ERROR", logger="command_handlers"):
            handlers.force_upgrade()
        assert any("loader not found" in rec.message for rec in caplog.records)

    def test_exec_failure_logs_and_returns(self, handlers, tmp_path, monkeypatch, caplog):
        loader_dir = tmp_path / "heart-matrix-controller"
        loader_dir.mkdir()
        (loader_dir / "loader.py").write_text("# fake loader\n")
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))

        with patch.object(handlers.os, "execvpe", side_effect=OSError("exec failed")):
            with caplog.at_level("ERROR", logger="command_handlers"):
                handlers.force_upgrade()
        assert any("exec failed" in rec.message for rec in caplog.records)

    def test_uses_repo_dir_override_kwarg(self, handlers, tmp_path):
        loader_dir = tmp_path / "heart-matrix-controller"
        loader_dir.mkdir()
        loader_py = loader_dir / "loader.py"
        loader_py.write_text("# fake loader\n")

        with patch.object(handlers.os, "execvpe") as mocked:
            handlers.force_upgrade(repo_dir=tmp_path)

        args = mocked.call_args.args
        assert args[0] == sys.executable
        assert args[1] == [sys.executable, str(loader_py)]


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


class TestRestart:
    def test_calls_sudo_reboot(self, handlers):
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        with patch.object(handlers.subprocess, "run", return_value=fake_result) as mocked:
            handlers.restart()

        args = mocked.call_args.args[0]
        assert args == ["sudo", "reboot"]
        assert mocked.call_args.kwargs["check"] is False

    def test_nonzero_exit_logs_warning(self, handlers, caplog):
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "sudo: a password is required"
        with patch.object(handlers.subprocess, "run", return_value=fake_result):
            with caplog.at_level("WARNING", logger="command_handlers"):
                handlers.restart()
        assert any("returned 1" in rec.message for rec in caplog.records)

    def test_missing_sudo_logs_error(self, handlers, caplog):
        with patch.object(
            handlers.subprocess,
            "run",
            side_effect=FileNotFoundError("sudo not found"),
        ):
            with caplog.at_level("ERROR", logger="command_handlers"):
                handlers.restart()
        assert any("subprocess failed" in rec.message for rec in caplog.records)

    def test_timeout_logs_error(self, handlers, caplog):
        with patch.object(
            handlers.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="sudo reboot", timeout=30.0),
        ):
            with caplog.at_level("ERROR", logger="command_handlers"):
                handlers.restart()
        assert any("timed out" in rec.message for rec in caplog.records)

    def test_success_does_not_log_warning(self, handlers, caplog):
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        with patch.object(handlers.subprocess, "run", return_value=fake_result):
            with caplog.at_level("WARNING", logger="command_handlers"):
                handlers.restart()
        assert not any("returned" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_calls_sudo_shutdown_h_now(self, handlers):
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        with patch.object(handlers.subprocess, "run", return_value=fake_result) as mocked:
            handlers.shutdown()

        args = mocked.call_args.args[0]
        assert args == ["sudo", "shutdown", "-h", "now"]
        assert mocked.call_args.kwargs["check"] is False

    def test_nonzero_exit_logs_warning(self, handlers, caplog):
        fake_result = MagicMock()
        fake_result.returncode = 126
        fake_result.stderr = "sudo: unable to execute"
        with patch.object(handlers.subprocess, "run", return_value=fake_result):
            with caplog.at_level("WARNING", logger="command_handlers"):
                handlers.shutdown()
        assert any("returned 126" in rec.message for rec in caplog.records)

    def test_missing_sudo_logs_error(self, handlers, caplog):
        with patch.object(
            handlers.subprocess,
            "run",
            side_effect=FileNotFoundError("sudo not found"),
        ):
            with caplog.at_level("ERROR", logger="command_handlers"):
                handlers.shutdown()
        assert any("subprocess failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Dispatcher routing — MessageManager._handle_command uses the registry
# ---------------------------------------------------------------------------


class TestMessageManagerDispatch:
    def _build_manager(self, **kwargs):
        from lib_shared.message_manager import MessageManager

        return MessageManager(
            messages_api_url="http://x/api/messages",
            config_api_url="http://x/api/config",
            api_key="k",
            **kwargs,
        )

    def _cmd_raw(self, action):
        from lib_shared.models import MessageEnvelope

        return MessageEnvelope(type="command", payload={"action": action}).to_json()

    def test_register_handler_routes_to_callback(self):
        manager = self._build_manager()
        called = []
        manager.register_handler("force-upgrade", lambda: called.append("force-upgrade"))
        manager.dispatch(self._cmd_raw("force-upgrade"))
        assert called == ["force-upgrade"]

    def test_unknown_action_dropped(self):
        manager = self._build_manager()
        called = []
        manager.register_handler("force-upgrade", lambda: called.append("force-upgrade"))
        manager.dispatch(self._cmd_raw("future-unknown"))
        assert called == []

    def test_handler_exception_isolated(self):
        manager = self._build_manager()
        good_called = []

        def boom():
            raise RuntimeError("boom")

        manager.register_handler("force-upgrade", boom)
        manager.register_handler("restart", lambda: good_called.append("restart"))
        manager.dispatch(self._cmd_raw("force-upgrade"))
        manager.dispatch(self._cmd_raw("restart"))
        assert good_called == ["restart"]

    def test_check_for_update_falls_back_to_kwarg(self):
        called = []
        manager = self._build_manager(on_check_for_update=lambda: called.append("legacy-cfu"))
        manager.dispatch(self._cmd_raw("check-for-update"))
        assert called == ["legacy-cfu"]

    def test_check_for_update_uses_registry_over_kwarg_when_both_wired(self):
        calls = []
        manager = self._build_manager(
            on_check_for_update=lambda: calls.append("legacy"),
        )
        manager.register_handler("check-for-update", lambda: calls.append("registry"))
        manager.dispatch(self._cmd_raw("check-for-update"))
        assert calls == ["registry"]

    def test_register_handler_rejects_non_string_action(self):
        manager = self._build_manager()
        with pytest.raises(ValueError, match="action must be a non-empty string"):
            manager.register_handler("", lambda: None)

    def test_register_handler_rejects_non_callable(self):
        manager = self._build_manager()
        with pytest.raises(ValueError, match="handler must be callable"):
            manager.register_handler("force-upgrade", "not a function")  # type: ignore[arg-type]

    def test_reregister_replaces_handler(self):
        manager = self._build_manager()
        first_called = []
        second_called = []
        manager.register_handler("force-upgrade", lambda: first_called.append("first"))
        manager.register_handler("force-upgrade", lambda: second_called.append("second"))
        manager.dispatch(self._cmd_raw("force-upgrade"))
        assert first_called == []
        assert second_called == ["second"]

    def test_message_envelope_still_routes_to_message_handler(self):
        # Disable the senders allowlist so the +1 sender (not in the
        # default empty allowlist) isn't filtered out before we can
        # assert the buffer was populated.
        manager = self._build_manager()
        from lib_shared.models import MessageEnvelope, Message

        msg = Message(id="m1", sender="+1", body="hi", received_at="2026-05-22T00:00:00Z")
        manager.dispatch(MessageEnvelope(type="message", payload=msg.to_dict()).to_json())
        # Message lands in the buffer (1+ entries). `suppress=False`
        # is needed because the default config has
        # `enforce_allowed_senders=True` and the test sender isn't in
        # the empty allowlist — that's the *filter* path, not the
        # *dispatch* path this test is asserting.
        assert manager.get_messages(limit=10, suppress=False) != []

    def test_config_envelope_with_target_version_caches_field(self):
        """type=config envelope carries sign_settings.target_version
        through to the in-memory config (issue #51 § Config envelope
        is the carrier for `sign_settings.target_version`)."""
        manager = self._build_manager()
        from lib_shared.models import MessageEnvelope

        cfg_dict = {
            "filters": [],
            "senders": [],
            "sign_settings": {"sign_name": "Test", "target_version": "abc1234"},
            "timezone": "US/Pacific",
            "version": 2,
            "effects_settings": {"effects": [], "fade_seconds": 0.5, "hold_seconds": 7.0},
            "text_settings": {"speed": 1, "color": 0xFFFFFF, "text_effect": "scroll"},
        }
        manager.dispatch(MessageEnvelope(type="config", payload=cfg_dict).to_json())

        cached = manager._config.sign_settings.to_dict()
        assert cached["target_version"] == "abc1234"
