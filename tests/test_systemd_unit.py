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
        """StartLimitIntervalSec=120 and StartLimitBurst=3 throttle crash loops.

        These directives live under [Unit], not [Service] — systemd
        silently ignores them under [Service] (we hit "Unknown key
        'StartLimitIntervalSec' in section [Service]" warnings at
        boot on 2026-07-09). The unit file has them in [Unit]; the
        test must follow.
        """
        interval = unit.get("Unit", "StartLimitIntervalSec", fallback=None)
        burst = unit.get("Unit", "StartLimitBurst", fallback=None)
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
    """Light-touch sanity tests for the simplified setup-pi.sh.

    The script is a fresh-clone-or-pull bootstrap that creates a
    `current/` symlink the loader expects. It does NOT do the
    bare-repo + worktree conversion the pre-#49 version did — that
    complexity lived entirely in setup-pi.sh itself, and the loader
    now works fine with a non-bare clone plus `current → .`.
    """

    def test_script_is_executable(self):
        """scripts/setup-pi.sh must be chmod +x — operator runs it directly."""
        mode = SETUP_PI_PATH.stat().st_mode
        assert mode & 0o111, f"setup-pi.sh is not executable (mode={oct(mode)})"

    def test_documents_prerequisite(self):
        """Header documents the OLD-install conversion prereq."""
        text = SETUP_PI_PATH.read_text()
        for needle in (
            "OLD_REPO_DIR",
            "settings.toml",
            "systemd",
            "current",
            "auto-upgrade",
        ):
            assert needle in text, f"setup-pi.sh header must mention {needle!r}"

    def test_clones_repo_when_missing(self):
        """First-run branch clones $REPO_URL into $REPO_DIR."""
        text = SETUP_PI_PATH.read_text()
        assert 'git clone "$REPO_URL" "$REPO_DIR"' in text, (
            "setup-pi.sh must `git clone` on first install"
        )

    def test_pulls_latest_when_repo_exists(self):
        """Re-run branch fetches + resets to origin/<branch>."""
        text = SETUP_PI_PATH.read_text()
        assert "git fetch origin" in text, "setup-pi.sh must `git fetch origin` on re-run"
        assert "git reset --hard" in text, (
            "setup-pi.sh must `git reset --hard origin/<branch>` to track remote HEAD"
        )

    def test_creates_current_symlink_after_clone(self):
        """setup-pi.sh creates `$REPO_DIR/current → .` to satisfy loader.py.

        This is the load-bearing new step in the simplified flow.
        Without it, the loader's `current_symlink()` resolution fails
        and the service crashes at boot. `ln -sfn . current` is
        idempotent — a stale `current → v-<sha>` (e.g. left over
        from a previous setup) gets reset to the main repo on
        re-run, which is the desired baseline for the loader's
        first `current_sha()` check.
        """
        text = SETUP_PI_PATH.read_text()
        assert "ln -sfn . current" in text, (
            "setup-pi.sh must `ln -sfn . current` so loader.py's "
            "current_symlink() resolves to the working tree"
        )

    def test_wires_up_post_checkout_hook(self):
        """setup-pi.sh installs the post-checkout hook so new worktrees
        inherit settings.toml.

        The loader creates worktrees via `git worktree add v-<sha>` on
        each upgrade. Without the hook installed at
        `.git/hooks/post-checkout`, new worktrees don't get
        settings.toml (it's .gitignore'd) and main.py crashes at
        config_reader.get_config() time.
        """
        text = SETUP_PI_PATH.read_text()
        assert ".git/hooks/post-checkout" in text, (
            "setup-pi.sh must install the post-checkout hook"
        )
        assert "hooks/post-checkout" in text, (
            "setup-pi.sh must reference the source hook path"
        )

    def test_copies_settings_from_old_install(self):
        """First-run: copies settings.toml from $OLD_REPO_DIR if present."""
        text = SETUP_PI_PATH.read_text()
        assert "OLD_SETTINGS" in text, "setup-pi.sh must reference $OLD_SETTINGS"
        assert "cp " in text and "settings.toml" in text, (
            "setup-pi.sh must copy settings.toml from the old install"
        )

    def test_hard_stops_if_no_settings(self):
        """Hard-stops with scp instructions when both $SETTINGS and $OLD_SETTINGS are missing."""
        text = SETUP_PI_PATH.read_text()
        # The error message must reference the missing path and the
        # scp-based fallback.
        assert "$SETTINGS is missing" in text or "settings.toml is missing" in text, (
            "setup-pi.sh must mention the missing settings.toml path"
        )
        assert "scp" in text, (
            "setup-pi.sh must provide scp instructions when settings.toml is missing"
        )

    def test_installs_systemd_unit(self):
        """setup-pi.sh installs the lindsay_50.service unit + enables + restarts."""
        text = SETUP_PI_PATH.read_text()
        assert "lindsay_50.service" in text, (
            "setup-pi.sh must reference the systemd unit"
        )
        assert "daemon-reload" in text, (
            "setup-pi.sh must run `systemctl daemon-reload`"
        )
        assert (
            "systemctl restart lindsay_50" in text
            or 'systemctl restart "$SERVICE_NAME"' in text
            or "systemctl restart '$SERVICE_NAME'" in text
        ), "setup-pi.sh must restart the lindsay_50 service"

    def test_idempotent_apt(self):
        """apt install step is skipped when packages are already present.

        The script uses dpkg-query to check each package's install
        state (returning 0 when present, 1 when missing) before
        running `apt install`. A naive `apt install -y` every run
        would re-run apt's cache update on every invocation.
        """
        text = SETUP_PI_PATH.read_text()
        assert "dpkg-query" in text or "dpkg -s" in text, (
            "setup-pi.sh must check apt package presence (dpkg-query/dpkg -s) "
            "to avoid running apt install on every run"
        )

    def test_idempotent_pip(self):
        """pip install step is skipped when rgbmatrix is already importable."""
        text = SETUP_PI_PATH.read_text()
        assert "rgbmatrix" in text, (
            "setup-pi.sh must check rgbmatrix importability to gate "
            "the pip install (rgbmatrix's C build takes minutes)"
        )


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
        assert (
            "settings.toml not found at" in text
        ), "provision-pi.sh must check settings.toml existence and surface a clear error"
        # And it must do so BEFORE doing any ssh/scp work — so the
        # operator with a missing file gets a fast failure, not a
        # half-bootstrapped Pi.
        not_found_idx = text.find("settings.toml not found at")
        first_ssh_idx = text.find("\nssh ", 0)  # first ssh call after the check
        assert not_found_idx > 0, "missing-file error message not found"
        assert first_ssh_idx > 0, "no ssh invocation in script — must fail fast before network calls"
        assert not_found_idx < first_ssh_idx, (
            "settings.toml check must come BEFORE any ssh/scp work so the " "operator with a missing file fails fast"
        )

    def test_detects_repo_root_or_fails(self):
        """Script must verify cwd is the lindsay-50 repo root (has .git + heart-matrix-controller/)."""
        text = PROVISION_PI_PATH.read_text()
        assert (
            "has no .git" in text or "not the lindsay-50 repo root" in text
        ), "provision-pi.sh must verify cwd is the repo root before proceeding"

    def test_ssh_preflight_is_used(self):
        """A BatchMode ssh pre-flight prevents the rest of the script running against an unreachable Pi."""
        text = PROVISION_PI_PATH.read_text()
        assert (
            "BatchMode" in text or "ConnectTimeout" in text
        ), "provision-pi.sh must preflight ssh before doing destructive work"

    def test_ships_settings_via_ssh_pipe(self):
        """settings.toml is shipped via pipe-over-ssh (not sftp or scp).

        We use `cat LOCAL | ssh PI 'cat > FILE && mv ...'` because BOTH sftp
        and scp fail to honor `SSH_ASKPASS_REQUIRE=force` reliably across
        OpenSSH versions. On macOS (Apple OpenSSH + LibreSSL) in particular,
        sftp silently refuses to engage the askpass even with
        SSH_ASKPASS_REQUIRE=force set — discovered end-to-end during #49
        testing (July 2026).

        Pipe-over-ssh works because the `ssh` binary itself honors
        SSH_ASKPASS_REQUIRE=force, and the askpass helper decrypts via stdin.
        The .tmp + mv pattern preserves the original "no partial overwrite
        on connection drop" guarantee.
        """
        text = PROVISION_PI_PATH.read_text()
        # The pipe pattern: pipe a file's content into `ssh ... cat > FILE.tmp`,
        # then atomic `mv` into place. Find each piece — they're easier to
        # verify separately than a single combined regex.
        assert 'cat "$LOCAL_SETTINGS"' in text, (
            "provision-pi.sh must pipe the local settings.toml through ssh "
            "(cat $LOCAL_SETTINGS | ssh ... ) to honor SSH_ASKPASS for the "
            "password path"
        )
        assert "settings.toml.tmp" in text, (
            "provision-pi.sh must write to settings.toml.tmp on the Pi then "
            "atomic-mv into place (no partial overwrite on connection drop)"
        )
        assert "heart-matrix-controller/settings.toml" in text, (
            "provision-pi.sh must place settings.toml at the canonical "
            "<repo_dir>/heart-matrix-controller/settings.toml path"
        )
        # Regression guards: neither sftp nor scp should be used for the
        # ship-settings step. Both fail SSH_ASKPASS on macOS OpenSSH.
        import re

        for forbidden in ("sftp ", "scp "):
            invocations = re.findall(rf"^\s*{re.escape(forbidden)}", text, re.MULTILINE)
            assert not invocations, (
                f"provision-pi.sh must not invoke {forbidden.strip()} for "
                f"the settings.toml ship step (regression — both ignore "
                f"SSH_ASKPASS_REQUIRE on macOS OpenSSH). "
                f"Found invocations: {invocations}"
            )

    def test_hands_off_to_setup_pi_over_ssh(self):
        """After shipping settings.toml, the script invokes setup-pi.sh on the Pi over ssh."""
        text = PROVISION_PI_PATH.read_text()
        # setup-pi.sh invocation via ssh must be present.
        assert "setup-pi.sh" in text
        # The settings-toml ship step (cat|ssh) must come BEFORE the
        # final ssh-to-pi-setup-pi.sh handoff.
        ship_idx = text.find('cat "$LOCAL_SETTINGS"')
        handoff_idx = text.find("./scripts/setup-pi.sh")
        assert ship_idx > 0, "no settings.toml ship step found (cat $LOCAL_SETTINGS | ssh ...)"
        assert handoff_idx > ship_idx, (
            "the ssh pipe-over-ssh settings.toml ship step must come before "
            "the final ssh-to-pi hand-off to setup-pi.sh"
        )

    def test_does_not_checkout_in_bare_repo(self):
        """provision-pi.sh must NOT `git checkout -f` against /srv/lindsay-50.

        After setup-pi.sh's bare conversion, /srv/lindsay-50/.git is a
        bare database — `git checkout` against a bare repo fataled with
        "this operation must be run in a work tree". The checkout was
        also redundant on a fresh non-bare clone (setup-pi.sh's bare
        conversion overwrites the working tree anyway).

        Fix: drop the checkout, keep only `git fetch origin` with an
        explicit refspec. The active version is controlled by the
        `current` symlink, which setup-pi.sh manages in Phase 3.
        """
        text = PROVISION_PI_PATH.read_text()
        # Reject any actual `git checkout` invocation (comments about the
        # old behavior are fine — they're a regression record). Look for
        # the command pattern, not the substring.
        import re

        invocations = re.findall(r"^\s*git\s+checkout\b", text, re.MULTILINE)
        assert not invocations, (
            "provision-pi.sh must not invoke `git checkout` against "
            "/srv/lindsay-50 — it's bare after setup-pi.sh and bare repos "
            "reject checkout. Use `git fetch origin` instead. "
            f"Found invocations: {invocations}"
        )
        # The fetch must use an explicit refspec (works on bare + non-bare,
        # doesn't depend on a pre-existing refs/remotes/origin/).
        assert "+refs/heads/*:refs/remotes/origin/*" in text, (
            "provision-pi.sh must fetch with the explicit heads-* refspec "
            "(same pattern as setup-pi.sh's bare-refresh fetch)"
        )

    def test_clones_laptop_branch_not_default(self):
        """provision-pi.sh clones the laptop's branch, not GitHub's default.

        Discovered during #49 end-to-end testing: `git clone <url>` defaults
        to GitHub's default branch (main), which leaves in-progress
        feature branches (like feat/issue-49) WITHOUT setup-pi.sh on disk —
        even though git's refdb has the branch ref. The next ssh handoff to
        setup-pi.sh then fataled with "No such file or directory" because
        the working tree was on main, not on the laptop's branch.

        Fix: clone with `--branch $LAPTOP_BRANCH --single-branch` so the
        working tree lands on the laptop's branch from the start.

        LAPTOP_BRANCH detection: `git rev-parse --abbrev-ref HEAD` returns
        the actual branch name on a branch checkout, or "HEAD" on detached
        HEAD — the script must handle both (and on detached HEAD, fall
        back to cloning the default branch and letting setup-pi.sh's
        `git worktree add ... $GIT_REF` pin the version).
        """
        text = PROVISION_PI_PATH.read_text()
        # The clone invocation must pass --branch with the laptop's branch.
        assert "git clone --branch" in text, (
            "provision-pi.sh must clone with `--branch $LAPTOP_BRANCH` so "
            "the Pi's working tree matches the laptop's branch. Without "
            "this, fresh clones land on GitHub's default branch (main), "
            "which can leave setup-pi.sh itself missing on in-progress "
            "feature branches."
        )
        # LAPTOP_BRANCH detection: --abbrev-ref HEAD, with HEAD→empty fallthrough.
        assert "rev-parse --abbrev-ref HEAD" in text, (
            "provision-pi.sh must detect the laptop's branch via "
            "`git rev-parse --abbrev-ref HEAD` to pass it to `git clone --branch`"
        )
        # Detached HEAD case: when abbrev-ref returns "HEAD", we set it
        # to empty and fall through to a default-branch clone.
        assert 'LAPTOP_BRANCH=""' in text or "LAPTOP_BRANCH=''" in text, (
            "provision-pi.sh must detect detached HEAD (abbrev-ref returns 'HEAD') "
            "and set LAPTOP_BRANCH to empty so the --branch path is skipped"
        )

    def test_wipes_existing_repo_before_cloning(self):
        """provision-pi.sh wipes /srv/lindsay-50 before cloning.

        The branch-aligned clone only works on a fresh state — if
        /srv/lindsay-50 already has the wrong branch, `git clone` fails
        with "destination path already exists". Wiping + cloning in one
        ssh command guarantees the laptop's branch always wins, even on
        a Pi the operator switched branches on. setup-pi.sh will run
        setup fresh on the next step and rebuild the bare+worktree
        layout from scratch.
        """
        text = PROVISION_PI_PATH.read_text()
        # Find the rm-and-clone sequence: the git clone line should be
        # preceded by an rm -rf of the same path on the same remote
        # command.
        import re

        # Look for a single ssh command containing both rm -rf and git clone.
        rm_clone_pattern = re.compile(
            r"ssh\s+\"\$PI_HOST\"\s+\"[^\"]*rm\s+-rf\s+'?\"?\$PI_REPO_DIR\"?'?[^\"]*git\s+clone[^\"]*\"",
            re.DOTALL,
        )
        assert rm_clone_pattern.search(text), (
            "provision-pi.sh must wipe /srv/lindsay-50 and re-clone in "
            "one ssh command — otherwise a stale branch could survive "
            "and the --branch alignment wouldn't take effect."
        )

    def test_resolves_python_from_repo_venv(self):
        """The password helper uses the repo's .venv python, not system python.

        cryptography.fernet is a pip dep, only installed in the venv. The
        askpass binary needs it to decrypt the password file, so the
        helper must point at $LOCAL_REPO_DIR/.venv/bin/python3.
        """
        text = PROVISION_PI_PATH.read_text()
        assert "$LOCAL_REPO_DIR/.venv/bin/python3" in text, (
            "setup_password_auth must resolve python from the repo venv "
            "($LOCAL_REPO_DIR/.venv/bin/python3) — cryptography.fernet "
            "is a pip dep, not guaranteed to be in system python"
        )
        # Fail-fast check: the helper should refuse to proceed if the
        # venv python is missing, with a clear message.
        assert "not found" in text or "not executable" in text, (
            "setup_password_auth should fail fast with a clear message " "if the venv python is missing"
        )

    def test_prompts_for_password_on_fallback(self):
        """When publickey preflight fails, the helper prompts for the password.

        No env-var input by design — see header doc. The prompt must
        be silent (no terminal echo) and a TTY check must gate it
        (so non-interactive invocations fail cleanly).
        """
        text = PROVISION_PI_PATH.read_text()
        assert "read -rs" in text, "setup_password_auth must use `read -rs` (silent, raw) " "to prompt for the password"
        assert "Pi root password" in text, "setup_password_auth must include a 'Pi root password' prompt"
        # Non-TTY gate: the helper must check `[ ! -t 0 ]` and abort
        # with a clear message rather than try to prompt on stdin.
        assert "[ ! -t 0 ]" in text, (
            "setup_password_auth must check that stdin is a TTY and "
            "abort cleanly if not (no env-var fallback by design)"
        )

    def test_cleanups_tempdir_on_trap(self):
        """The password temp dir is cleaned up on EXIT/INT/TERM/HUP.

        The trap registration is the only thing standing between a
        crash mid-script and a leaked encrypted password file. Verify
        all four signals are covered (SIGKILL can't be trapped, but
        the other common kill signals can).
        """
        text = PROVISION_PI_PATH.read_text()
        import re

        # Find trap ... EXIT INT TERM HUP — must include all four.
        trap_pattern = re.compile(r"trap\s+['\"].*?['\"]\s+EXIT\s+INT\s+TERM\s+HUP", re.DOTALL)
        assert trap_pattern.search(text), (
            "provision-pi.sh must register a trap on EXIT/INT/TERM/HUP " "to clean up the password temp dir"
        )
        # The trap must rm -rf the password temp dir — without it a
        # crashed mid-script would leave the encrypted password file
        # on disk until manual cleanup.
        assert 'rm -rf "$PW_TMPDIR"' in text, "the trap must rm -rf $PW_TMPDIR (the password temp dir)"

    def test_uses_ssh_askpass_for_subsequent_calls(self):
        """After password auth is set up, the helper exports SSH_ASKPASS.

        All subsequent ssh invocations inherit SSH_ASKPASS from
        the script's env, so they need never prompt again. Note: only
        `ssh` honors SSH_ASKPASS_REQUIRE=force reliably — sftp and scp
        silently ignore it on macOS OpenSSH, which is why we use a
        pipe-over-ssh `cat` for the settings.toml ship step instead.
        """
        text = PROVISION_PI_PATH.read_text()
        assert "SSH_ASKPASS=" in text, "setup_password_auth must export SSH_ASKPASS for subsequent " "ssh invocations"
        assert "SSH_ASKPASS_REQUIRE" in text, (
            "setup_password_auth must set SSH_ASKPASS_REQUIRE=force so "
            "ssh uses the askpass binary even when it has a tty"
        )

    def test_uses_fernet_for_encryption(self):
        """Password is encrypted with Fernet (AES + HMAC) before being written.

        Fernet is a vetted AEAD construction from the cryptography
        package. The script must use it (not, say, plain base64 or
        a hand-rolled XOR) to keep the on-disk artifact worthless
        without the in-memory key.
        """
        text = PROVISION_PI_PATH.read_text()
        assert "Fernet" in text, "setup_password_auth must use cryptography.fernet.Fernet " "to encrypt the password"
        assert "Fernet.generate_key" in text, (
            "setup_password_auth must generate a per-run Fernet key " "(not reuse a hard-coded one)"
        )

    def test_fail_fast_points_at_provisioner_requirements(self):
        """provision-pi.sh's venv-missing error references the right requirements file.

        The provisioner has its own requirements-provisioner.txt (cryptography
        only) — distinct from requirements-flask.txt / requirements-pi.txt.
        A bare `pip install -r requirements.txt` reference would imply the
        old layout and fail (no root requirements.txt exists after the split).
        """
        text = PROVISION_PI_PATH.read_text()
        assert "requirements-provisioner.txt" in text, (
            "provision-pi.sh fail-fast messages must point at "
            "requirements-provisioner.txt (the laptop-side deps), "
            "not the old root requirements.txt"
        )
        # Regression guard: the old root path should not be referenced
        # as a requirements source anywhere in the provisioner.
        assert "requirements.txt" not in text, (
            "provision-pi.sh must not reference the old root requirements.txt "
            "(split into requirements-flask.txt / requirements-pi.txt / "
            "requirements-provisioner.txt)"
        )


class TestSetupPiRequirements:
    """Verify setup-pi.sh installs only the Pi's deps, not Flask's.

    The repo's three requirements files split cleanly by consumer:
    - requirements-flask.txt: Heroku / laptop Flask dev
    - requirements-pi.txt: Pi display device (this script)
    - requirements-provisioner.txt: laptop-side provisioner

    setup-pi.sh must install ONLY from requirements-pi.txt — pulling in
    flask/boto3/twilio on the Pi would be wasted bandwidth and image size.
    """

    def test_setup_pi_installs_only_pi_deps(self):
        """setup-pi.sh pip-installs requirements-pi.txt and nothing else."""
        text = SETUP_PI_PATH.read_text()
        assert "requirements-pi.txt" in text, "setup-pi.sh must install from requirements-pi.txt (the Pi's deps)"
        # Regression guards: the Flask and provisioner files must not be
        # pip-installed on the Pi. Heroku / provisioner deps have no
        # business on the display device. Match on `pip install ... -r`
        # lines specifically — comments may legitimately mention the
        # other filenames to explain *why* they're excluded.
        # Walk the file joining line-continuations (`\`-terminated) so a
        # `pip install -r foo.txt` that spans two lines (the actual layout
        # in setup-pi.sh) is matched as one logical line.
        logical_lines: list[str] = []
        for line in text.splitlines():
            if logical_lines and logical_lines[-1].rstrip().endswith("\\"):
                logical_lines[-1] = logical_lines[-1].rstrip()[:-1] + " " + line.lstrip()
            else:
                logical_lines.append(line)
        pip_install_lines = [line for line in logical_lines if "pip install" in line and "-r" in line]
        assert any(
            "requirements-pi.txt" in line for line in pip_install_lines
        ), "setup-pi.sh must have a `pip install ... -r requirements-pi.txt` line"
        for forbidden in ("requirements-flask.txt", "requirements-provisioner.txt"):
            assert not any(
                forbidden in line for line in pip_install_lines
            ), f"setup-pi.sh must NOT pip-install {forbidden} on the Pi"

    def test_pi_requirements_file_exists(self):
        """requirements-pi.txt exists at the repo root and lists rgbmatrix."""
        path = PROJECT_ROOT / "requirements-pi.txt"
        assert path.exists(), (
            "requirements-pi.txt must exist at the repo root — setup-pi.sh " "and Pi operators both reference this path"
        )
        contents = path.read_text()
        # Sanity: rgbmatrix is the one dep that's expensive to build (C
        # extension) and distinctive to the Pi. If it's missing, someone
        # may have copied the Flask list over by mistake.
        assert "rgbmatrix" in contents, (
            "requirements-pi.txt must include rgbmatrix (the Pi-specific "
            "C-extension build that's the whole reason this file is separate)"
        )

    def test_provisioner_requirements_exists(self):
        """requirements-provisioner.txt exists with only laptop-side deps."""
        path = PROJECT_ROOT / "requirements-provisioner.txt"
        assert path.exists(), (
            "requirements-provisioner.txt must exist at the repo root — "
            "provision-pi.sh points operators at this file"
        )
        contents = path.read_text()
        assert "cryptography" in contents, (
            "requirements-provisioner.txt must include cryptography (Fernet "
            "for the password-auth fallback in provision-pi.sh)"
        )
        # Regression guards: Flask- and Pi-only deps don't belong on the
        # laptop. Check dep lines (non-comment) — comments may legitimately
        # mention these names to explain why they're excluded.
        dep_lines = [
            line.strip() for line in contents.splitlines() if line.strip() and not line.strip().startswith("#")
        ]
        joined = "\n".join(dep_lines).lower()
        assert "flask" not in joined, (
            "requirements-provisioner.txt must NOT include flask as a dep " "(that's requirements-flask.txt)"
        )
        assert "rgbmatrix" not in joined, (
            "requirements-provisioner.txt must NOT include rgbmatrix as a dep "
            "(that's requirements-pi.txt — the C extension would "
            "fail to build on the laptop anyway)"
        )

    def test_root_requirements_txt_is_heroku_shim(self):
        """The root requirements.txt must exist ONLY as a Heroku buildpack shim
        that defers to `requirements-flask.txt`.

        Heroku's python buildpack only recognizes `requirements.txt` at the
        repo root — `PIP_REQUIREMENTS_PATH` is not honored at the initial
        detection step. So the root file must be a one-line shim containing
        `-r requirements-flask.txt`, not a copy of the deps (which would
        silently diverge from the source-of-truth file).

        The earlier test that asserted the file must not exist predates the
        discovery of the buildpack quirk; the shim is the correct shape.
        """
        path = PROJECT_ROOT / "requirements.txt"
        assert path.exists(), (
            f"{path} must exist as a Heroku buildpack shim — the buildpack "
            "ignores PIP_REQUIREMENTS_PATH at detection time."
        )
        text = path.read_text()
        assert "-r requirements-flask.txt" in text, (
            f"{path} must defer to requirements-flask.txt via `-r` include, "
            f"not duplicate the deps inline. Got:\n{text}"
        )
        # Belt-and-braces: the shim must not list a dep of its own (which
        # would silently shadow requirements-flask.txt).
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-r"):
                continue
            raise AssertionError(
                f"{path} must be a pure shim; only `-r` includes allowed. " f"Offending line: {stripped!r}"
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
