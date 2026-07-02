"""Sanity tests for scripts/lindsay_50.service and setup-pi.sh.

The systemd unit itself is hard to validate without a Linux host
(`systemd-analyze verify` isn't available on macOS). Instead, we
parse the file as INI and check the keys we depend on for issue #49
are present:

  - `ExecStart` must point at the loader (via startup_matrix_server.sh).
  - `WorkingDirectory` must be the repo root.
  - `StartLimitIntervalSec` and `StartLimitBurst` must be set
    (defense in depth against loader crash loops).

`setup-pi.sh` is checked for executable bit and the bootstrap steps
(convert .git to bare, create first worktree, create `current` symlink).
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
SERVICE_PATH = PROJECT_ROOT / "scripts" / "lindsay_50.service"
SETUP_PI_PATH = PROJECT_ROOT / "scripts" / "setup-pi.sh"
STARTUP_PATH = PROJECT_ROOT / "scripts" / "startup_matrix_server.sh"


class TestSystemdUnit:
    @pytest.fixture
    def unit(self):
        # systemd unit files are INI-like but allow duplicate keys
        # and inline comments with `;`. Use RawConfigParser to read
        # values verbatim.
        parser = configparser.RawConfigParser(strict=False)
        # `optionxform = str` preserves key case (default lowercases keys).
        # We don't actually need the case — the test assertions all use
        # the standard INI casing — but the assignment documents intent.
        # The type-checker complains about the assignment because
        # RawConfigParser's optionxform is typed as a strict overload;
        # the runtime assignment works fine.
        parser.optionxform = str  # type: ignore[reportAttributeAccessIssue]
        parser.read(SERVICE_PATH)
        return parser

    def test_execstart_invokes_loader_via_startup_script(self, unit):
        """ExecStart points at startup_matrix_server.sh, which in turn execs loader.py."""
        execstart = unit.get("Service", "ExecStart", fallback=None)
        assert execstart is not None, "Service.ExecStart missing"
        # The systemd unit intentionally invokes the startup shell wrapper
        # (not loader.py directly) so the venv activation + PYTHONPATH
        # setup is preserved. The wrapper exec's loader.py.
        assert "startup_matrix_server.sh" in execstart, (
            f"ExecStart should reference the startup wrapper, got: {execstart!r}"
        )

    def test_startup_script_invoke_loader_py(self):
        """scripts/startup_matrix_server.sh's final exec line runs loader.py, not main.py directly."""
        text = STARTUP_PATH.read_text()
        assert "loader.py" in text, "startup_matrix_server.sh must invoke loader.py"
        # The `main.py` substring is allowed (in comments and the
        # `cd heart-matrix-controller/` doc references). What we
        # actually want to verify is that the final `exec` line
        # invokes loader.py, not main.py.
        exec_lines = [
            line for line in text.splitlines()
            if line.strip().startswith("exec ")
        ]
        assert exec_lines, "startup script has no `exec` line"
        last_exec = exec_lines[-1]
        assert "loader.py" in last_exec, (
            f"final exec should invoke loader.py, got: {last_exec!r}"
        )

    def test_working_directory_is_repo_root(self, unit):
        """WorkingDirectory is the repo root, not heart-matrix-controller."""
        wd = unit.get("Service", "WorkingDirectory", fallback=None)
        assert wd is not None, "Service.WorkingDirectory missing"
        assert not wd.rstrip("/").endswith("heart-matrix-controller"), (
            f"WorkingDirectory should be repo root, got: {wd!r}"
        )

    def test_startlimit_interval_and_burst_set(self, unit):
        """StartLimitIntervalSec=120 and StartLimitBurst=3 throttle crash loops."""
        interval = unit.get("Service", "StartLimitIntervalSec", fallback=None)
        burst = unit.get("Service", "StartLimitBurst", fallback=None)
        assert interval == "120", f"StartLimitIntervalSec should be 120, got: {interval!r}"
        assert burst == "3", f"StartLimitBurst should be 3, got: {burst!r}"

    def test_restart_is_always(self, unit):
        """Restart=always is preserved from the original unit."""
        restart = unit.get("Service", "Restart", fallback=None)
        assert restart == "always", f"Restart should be 'always', got: {restart!r}"

    def test_user_is_root(self, unit):
        """User=root is preserved — rgbmatrix needs GPIO access."""
        user = unit.get("Service", "User", fallback=None)
        assert user == "root", f"User should be 'root', got: {user!r}"

    def test_after_network_online(self, unit):
        """After=network-online.target is preserved."""
        after = unit.get("Unit", "After", fallback=None)
        assert after is not None and "network-online.target" in after


class TestSetupPiScript:
    def test_script_is_executable(self):
        """scripts/setup-pi.sh must be chmod +x — operator runs it directly."""
        mode = SETUP_PI_PATH.stat().st_mode
        assert mode & 0o111, f"setup-pi.sh is not executable (mode={oct(mode)})"

    def test_documents_one_time_bootstrap(self):
        """setup-pi.sh docstring mentions the bare-repo + worktree + symlink flow."""
        text = SETUP_PI_PATH.read_text()
        for needle in ("bare", "worktree", "current", "symlink", ".git"):
            assert needle in text, f"setup-pi.sh must mention {needle!r}"

    def test_converts_existing_clone_to_bare(self):
        """setup-pi.sh contains the git clone --bare step."""
        text = SETUP_PI_PATH.read_text()
        assert "git clone --bare" in text, "setup-pi.sh missing 'git clone --bare'"
        assert ".git.tmp" in text, "setup-pi.sh missing the .git.tmp dance"

    def test_creates_first_worktree_from_head(self):
        """setup-pi.sh stages a worktree at HEAD before swapping current."""
        text = SETUP_PI_PATH.read_text()
        assert "git rev-parse HEAD" in text
        assert "worktree add" in text
        assert "v-$HEAD_SHA" in text or "v-$head_sha" in text

    def test_creates_current_symlink(self):
        """setup-pi.sh creates the `current` symlink pointing at v-<sha>."""
        text = SETUP_PI_PATH.read_text()
        assert "ln -sfn" in text, "setup-pi.sh must use atomic ln -sfn"
        assert "v-$HEAD_SHA" in text and "current" in text

    def test_is_idempotent(self):
        """Re-running setup-pi.sh on an already-bootstrapped repo is a no-op."""
        text = SETUP_PI_PATH.read_text()
        assert "already bootstrapped" in text or "already" in text, (
            "setup-pi.sh should detect a previously-bootstrapped repo"
        )

    def test_reloads_systemd_on_completion(self):
        """setup-pi.sh reloads systemd + restarts the service when present."""
        text = SETUP_PI_PATH.read_text()
        assert "daemon-reload" in text
        assert "systemctl restart lindsay_50" in text


class TestStartupScript:
    def test_exec_loader_py(self):
        """startup_matrix_server.sh's final exec line runs loader.py, not main.py."""
        text = STARTUP_PATH.read_text()
        # The exec line is the last command in the script.
        exec_lines = [line for line in text.splitlines() if line.strip().startswith("exec ")]
        assert exec_lines, "startup_matrix_server.sh has no `exec` line"
        last_exec = exec_lines[-1]
        assert "loader.py" in last_exec, (
            f"final exec should invoke loader.py, got: {last_exec!r}"
        )

    def test_preserves_log_level_env(self):
        """LOG_LEVEL export is preserved from the original startup script."""
        text = STARTUP_PATH.read_text()
        assert "LOG_LEVEL" in text

    def test_preserves_pythonpath_env(self):
        """PYTHONPATH export is preserved — lib_shared needs to resolve."""
        text = STARTUP_PATH.read_text()
        assert "PYTHONPATH" in text
        assert "REPO_DIR" in text