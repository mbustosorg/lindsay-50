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
PROVISION_PI_PATH = PROJECT_ROOT / "scripts" / "provision-pi.sh"
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
        assert (
            "startup_matrix_server.sh" in execstart
        ), f"ExecStart should reference the startup wrapper, got: {execstart!r}"

    def test_startup_script_invoke_loader_py(self):
        """scripts/startup_matrix_server.sh's final exec line runs loader.py, not main.py directly."""
        text = STARTUP_PATH.read_text()
        assert "loader.py" in text, "startup_matrix_server.sh must invoke loader.py"
        # The `main.py` substring is allowed (in comments and the
        # `cd heart-matrix-controller/` doc references). What we
        # actually want to verify is that the final `exec` line
        # invokes loader.py, not main.py.
        exec_lines = [line for line in text.splitlines() if line.strip().startswith("exec ")]
        assert exec_lines, "startup script has no `exec` line"
        last_exec = exec_lines[-1]
        assert "loader.py" in last_exec, f"final exec should invoke loader.py, got: {last_exec!r}"

    def test_working_directory_is_repo_root(self, unit):
        """WorkingDirectory is the repo root, not heart-matrix-controller."""
        wd = unit.get("Service", "WorkingDirectory", fallback=None)
        assert wd is not None, "Service.WorkingDirectory missing"
        assert not wd.rstrip("/").endswith(
            "heart-matrix-controller"
        ), f"WorkingDirectory should be repo root, got: {wd!r}"

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
        assert (
            "already bootstrapped" in text or "already" in text
        ), "setup-pi.sh should detect a previously-bootstrapped repo"

    def test_handles_partial_bootstrap_state(self):
        """Bare repo with no `current` symlink is a valid state — finish, don't reconvert.

        This was the bug on issue #49: the original idempotency check only
        covered "fully bootstrapped" and "non-bare clone", missing the
        partial-bootstrap state (bare repo present but `current` symlink
        missing because a prior run died mid-worktree-add). Re-running on
        that state would re-do the bare conversion and hit
        `worktree add: already exists`. The fix branches on bare-vs-not
        rather than just symlink presence.
        """
        text = SETUP_PI_PATH.read_text()
        # The new state machine mentions all three branches:
        assert "bare repo detected, bootstrap incomplete" in text, (
            "setup-pi.sh must detect a bare repo with no current symlink "
            "and finish the bootstrap without re-converting"
        )
        assert (
            "non-bare clone" in text.lower() or "Non-bare clone" in text
        ), "setup-pi.sh must explicitly handle the non-bare clone branch"
        # And it must NOT re-run the bare conversion when one already exists
        # (the bug was: bare + no symlink → re-convert → worktree add fails).
        # Use a unique substring for the actual command so we don't match
        # the explanatory comment that mentions `git clone --bare` earlier.
        bare_convert_path = text.find('.git.tmp" "$REPO_DIR/.git"')
        partial_branch = text.find("bare repo detected, bootstrap incomplete")
        assert bare_convert_path != -1, "bare conversion step missing"
        assert partial_branch != -1, "partial-bootstrap branch missing"
        # Partial-bootstrap branch must be reached BEFORE the bare-conversion
        # branch so it short-circuits on partial state. (If conversion came
        # first, the bug recurs.)
        assert partial_branch < bare_convert_path, (
            "partial-bootstrap branch should be reached BEFORE the bare "
            "conversion (otherwise partial-state runs would re-convert)"
        )

    def test_worktree_add_is_idempotent(self):
        """setup-pi.sh prunes stale v-<sha>/ dirs before worktree add.

        This was the issue #49 retry failure mode: a prior failed run left
        a v-<oldsha>/ directory behind. `git worktree prune` clears the
        metadata but not the directory, and the next `git worktree add`
        bails on 'already exists'. The fix: prune + remove orphan dirs
        in Phase 3 before invoking worktree add.
        """
        text = SETUP_PI_PATH.read_text()
        assert "worktree prune" in text, "setup-pi.sh must run `git worktree prune` to clean stale metadata"
        assert (
            "stale/orphan worktree dir" in text or "stale worktree dir" in text
        ), "setup-pi.sh must remove stale v-<sha>/ dirs before worktree add"

    def test_uses_canonical_bare_detector(self):
        """Bare-detector must be `git rev-parse --is-bare-repository`, not `[ -f .git ]`.

        `git clone --bare` produces a bare repo as a *directory* (just one
        without a working tree), not a file. The original `[ -f .git ]`
        check was always false and the partial-bootstrap branch never fired.
        """
        text = SETUP_PI_PATH.read_text()
        assert "rev-parse --is-bare-repository" in text, (
            "setup-pi.sh must use `git rev-parse --is-bare-repository` for "
            "bare detection — `[ -f .git ]` is wrong because bare repos "
            "are directories"
        )

    def test_already_bootstrapped_branch_uses_bare_check_too(self):
        """The 'already bootstrapped' path requires the repo to actually be bare.

        A `current -> v-<sha>` symlink on a non-bare repo is an orphan, not a
        valid worktree — the loader would crash on its first `git rev-parse`
        inside the (non-existent) worktree. The pre-flight loop must clear it
        and the state machine must require IS_BARE before taking the skip path.

        Symptom if missing: `git rev-parse v-<sha>` fataled with
        'Needed a single revision' on the Pi after a wipe+reclone where
        a stale symlink survived in the repo root.
        """
        text = SETUP_PI_PATH.read_text()
        # Both the pre-flight orphan-clear and the skip-path bare gate
        # must be present. Check the skip-path gate directly.
        assert "IS_BARE" in text and '"true"' in text, "the bare-guard must survive the refactor"
        # The skip-path branch should derive HEAD_SHA_SHORT from the
        # basename instead of calling `git rev-parse v-<sha>` (which
        # fataled on the Pi with non-bare orphan-state).
        skip_branch_idx = text.find("repo already bootstrapped")
        assert skip_branch_idx > 0
        # The next 600 chars is the skip-path branch. Make sure
        # HEAD_SHA_SHORT is derived from the basename there.
        skip_branch = text[skip_branch_idx : skip_branch_idx + 600]
        assert 'HEAD_SHA_SHORT="${CURRENT_TARGET#v-}"' in skip_branch, (
            "skip-path must derive HEAD_SHA_SHORT from the symlink " "target basename, not `git rev-parse v-<sha>`"
        )

    def test_fetch_uses_explicit_refspec(self):
        """The fetch block must use an explicit refspec, not enumerate via for-each-ref.

        On a freshly-cloned bare repo `refs/remotes/origin/` is empty, so
        `for-each-ref` produces no refspecs and `git fetch origin` (with
        no argument) fataled with 'Needed a single revision'. An explicit
        `+refs/heads/*:refs/remotes/origin/*` refspec is robust across
        fresh clones and already-current repos.
        """
        text = SETUP_PI_PATH.read_text()
        assert "+refs/heads/*:refs/remotes/origin/*" in text, (
            "setup-pi.sh fetch must use the explicit heads-* refspec, "
            "not the dynamically-discovered for-each-ref form"
        )

    def test_reloads_systemd_on_completion(self):
        """setup-pi.sh reloads systemd + restarts the service when present."""
        text = SETUP_PI_PATH.read_text()
        assert "daemon-reload" in text
        # The script uses a SERVICE_NAME variable for the service identifier;
        # accept either the literal or the variable form.
        assert (
            "systemctl restart lindsay_50" in text
            or 'systemctl restart "$SERVICE_NAME"' in text
            or "systemctl restart '$SERVICE_NAME'" in text
        ), "setup-pi.sh must restart the lindsay_50 service"


class TestProvisionPiScript:
    """Light-touch sanity tests for scripts/provision-pi.sh.

    The script runs over SSH/SCP against a real Pi, so we don't
    execute it here. Instead we check the contract: it's executable,
    it documents the laptop-side flow, it detects local settings.toml
    or fails, and it hands off to setup-pi.sh on the Pi.
    """

    def test_script_is_executable(self):
        """provision-pi.sh must be executable — operator runs it directly."""
        mode = PROVISION_PI_PATH.stat().st_mode
        assert mode & 0o111, f"provision-pi.sh is not executable (mode={oct(mode)})"

    def test_documents_laptop_invocation(self):
        """Header explains the laptop-side, repo-root invocation."""
        text = PROVISION_PI_PATH.read_text()
        for needle in (
            "Provision a Raspberry Pi",
            "operator's laptop",
            "repo root",
            "settings.toml",
        ):
            assert needle in text, f"provision-pi.sh missing {needle!r}"

    def test_has_escape_env_vars(self):
        """Env-var escape hatches for host / repo dir / settings path / git ref."""
        text = PROVISION_PI_PATH.read_text()
        for needle in (
            "LINDSAY50_PI_HOST",
            "LINDSAY50_PI_REPO_DIR",
            "LINDSAY50_LOCAL_SETTINGS",
            "LINDSAY50_GIT_REF",
        ):
            assert needle in text, f"provision-pi.sh missing env var {needle!r}"

    def test_fails_when_settings_toml_missing(self):
        """When LOCAL_SETTINGS doesn't exist, the script must exit non-zero with a clear message."""
        text = PROVISION_PI_PATH.read_text()
        # The "file not found" path:
        assert "settings.toml not found at" in text, (
            "provision-pi.sh must check settings.toml existence and surface a clear error"
        )
        # And it must do so BEFORE doing any ssh/scp work — so the
        # operator with a missing file gets a fast failure, not a
        # half-bootstrapped Pi.
        not_found_idx = text.find("settings.toml not found at")
        first_ssh_idx = text.find("\nssh ", 0)  # first ssh call after the check
        assert not_found_idx > 0, "missing-file error message not found"
        assert first_ssh_idx > 0, "no ssh invocation in script — must fail fast before network calls"
        assert not_found_idx < first_ssh_idx, (
            "settings.toml check must come BEFORE any ssh/scp work so the "
            "operator with a missing file fails fast"
        )

    def test_detects_repo_root_or_fails(self):
        """Script must verify cwd is the lindsay-50 repo root (has .git + heart-matrix-controller/)."""
        text = PROVISION_PI_PATH.read_text()
        assert "has no .git" in text or "not the lindsay-50 repo root" in text, (
            "provision-pi.sh must verify cwd is the repo root before proceeding"
        )

    def test_ssh_preflight_is_used(self):
        """A BatchMode ssh pre-flight prevents the rest of the script running against an unreachable Pi."""
        text = PROVISION_PI_PATH.read_text()
        assert "BatchMode" in text or "ConnectTimeout" in text, (
            "provision-pi.sh must preflight ssh before doing destructive work"
        )

    def test_ships_settings_via_scp(self):
        """settings.toml is shipped via scp to the canonical Pi path."""
        text = PROVISION_PI_PATH.read_text()
        assert "scp" in text, "provision-pi.sh must use scp to ship settings.toml"
        assert "heart-matrix-controller/settings.toml" in text, (
            "provision-pi.sh must place settings.toml at the canonical "
            "<repo_dir>/heart-matrix-controller/settings.toml path"
        )

    def test_hands_off_to_setup_pi_over_ssh(self):
        """After scp, the script invokes setup-pi.sh on the Pi over ssh."""
        text = PROVISION_PI_PATH.read_text()
        # setup-pi.sh invocation via ssh must be present.
        assert "setup-pi.sh" in text
        # scp must come BEFORE the ssh-to-pi-setup-pi.sh step.
        scp_idx = text.find("scp ")
        handoff_idx = text.find("setup-pi.sh", scp_idx)
        assert scp_idx > 0, "no scp call found"
        assert handoff_idx > scp_idx, (
            "scp of settings.toml must come before the final ssh-to-pi "
            "hand-off to setup-pi.sh"
        )


class TestStartupScript:
    def test_exec_loader_py(self):
        """startup_matrix_server.sh's final exec line runs loader.py, not main.py."""
        text = STARTUP_PATH.read_text()
        # The exec line is the last command in the script.
        exec_lines = [line for line in text.splitlines() if line.strip().startswith("exec ")]
        assert exec_lines, "startup_matrix_server.sh has no `exec` line"
        last_exec = exec_lines[-1]
        assert "loader.py" in last_exec, f"final exec should invoke loader.py, got: {last_exec!r}"

    def test_preserves_log_level_env(self):
        """LOG_LEVEL export is preserved from the original startup script."""
        text = STARTUP_PATH.read_text()
        assert "LOG_LEVEL" in text

    def test_preserves_pythonpath_env(self):
        """PYTHONPATH export is preserved — lib_shared needs to resolve."""
        text = STARTUP_PATH.read_text()
        assert "PYTHONPATH" in text
        assert "REPO_DIR" in text
