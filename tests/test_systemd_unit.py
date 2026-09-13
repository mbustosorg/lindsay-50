"""Sanity tests for scripts/lindsay_50.service, scripts/setup-pi.sh, and
scripts/startup_matrix_server.sh.

The systemd unit itself is hard to validate without a Linux host
(`systemd-analyze verify` isn't available on macOS). Instead, we
parse the file as INI and check the keys we depend on are present:

  - `ExecStart` must point at startup_matrix_server.sh.
  - `WorkingDirectory` must be the repo root.
  - `StartLimitIntervalSec` and `StartLimitBurst` must be set
    (defense in depth against crash loops).

`setup-pi.sh` is checked for executable bit and the bootstrap steps
(clone or pull, copy settings.toml, install systemd unit).

`startup_matrix_server.sh` is checked for the final `exec` line
running main.py directly.
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

    def test_execstart_invokes_startup_script(self, unit):
        """ExecStart points at startup_matrix_server.sh, which in turn execs main.py."""
        execstart = unit.get("Service", "ExecStart", fallback=None)
        assert execstart is not None, "Service.ExecStart missing"
        # The systemd unit intentionally invokes the startup shell wrapper
        # (not main.py directly) so the cwd + PYTHONPATH setup is
        # preserved. The wrapper exec's main.py.
        assert (
            "startup_matrix_server.sh" in execstart
        ), f"ExecStart should reference the startup wrapper, got: {execstart!r}"

    def test_startup_script_invokes_main_py(self):
        """scripts/startup_matrix_server.sh's final exec line runs main.py."""
        text = STARTUP_PATH.read_text()
        # The exec line is the last command in the script.
        exec_lines = [line for line in text.splitlines() if line.strip().startswith("exec ")]
        assert exec_lines, "startup_matrix_server.sh has no `exec` line"
        last_exec = exec_lines[-1]
        assert "main.py" in last_exec, f"final exec should invoke main.py, got: {last_exec!r}"
        # Regression guard: must not exec loader.py — that's the
        # dropped self-upgrade machinery.
        assert "loader.py" not in last_exec, (
            f"final exec should NOT invoke loader.py (self-upgrade machinery "
            f"was dropped). got: {last_exec!r}"
        )

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
    def test_script_is_executable(self):
        """scripts/setup-pi.sh must be chmod +x — operator runs it directly."""
        mode = SETUP_PI_PATH.stat().st_mode
        assert mode & 0o111, f"setup-pi.sh is not executable (mode={oct(mode)})"

    def test_documents_bootstrap(self):
        """setup-pi.sh docstring mentions the bootstrap flow."""
        text = SETUP_PI_PATH.read_text()
        for needle in ("clone", "settings.toml", "systemd", "apt", "pip"):
            assert needle in text, f"setup-pi.sh must mention {needle!r}"

    def test_documents_prerequisite(self):
        """setup-pi.sh header documents the stop/disable-old-service step.

        Converting a Pi that has the sign running from a different path
        (e.g. /home/mauricio/lindsay-50) requires stopping and disabling
        the OLD systemd unit BEFORE running this script. Otherwise the
        OLD service keeps running and the operator thinks setup-pi.sh
        is broken when the new path never starts.
        """
        text = SETUP_PI_PATH.read_text()
        for needle in ("systemctl stop", "systemctl disable"):
            assert needle in text, (
                f"setup-pi.sh must document the {needle!r} prerequisite "
                f"for converting from an existing install"
            )

    def test_clones_repo_when_missing(self):
        """On a fresh Pi (no $REPO_DIR), setup-pi.sh runs `git clone`."""
        text = SETUP_PI_PATH.read_text()
        assert "git clone" in text, "setup-pi.sh must clone the repo on a fresh Pi"
        # The clone invocation must take both the URL and the destination.
        assert "$REPO_URL" in text, "setup-pi.sh must reference $REPO_URL in the clone"
        assert "$REPO_DIR" in text, "setup-pi.sh must reference $REPO_DIR as the clone target"

    def test_pulls_latest_when_repo_exists(self):
        """On a Pi with $REPO_DIR present, setup-pi.sh fetches and resets to origin/HEAD."""
        text = SETUP_PI_PATH.read_text()
        assert "git fetch origin" in text, (
            "setup-pi.sh must `git fetch origin` so re-runs pick up "
            "new commits pushed to the remote"
        )
        assert "git reset --hard" in text, (
            "setup-pi.sh must `git reset --hard` to advance the "
            "working tree to the latest fetched commit"
        )
        # The reset target must be the current branch's upstream
        # (origin/<branch>), not origin/HEAD — symbolic-ref is the
        # conventional way to get the current branch name.
        assert "symbolic-ref --short HEAD" in text, (
            "setup-pi.sh must derive the current branch via "
            "`git symbolic-ref --short HEAD` so the reset targets "
            "origin/<branch>, not origin/HEAD"
        )

    def test_copies_settings_from_old_install(self):
        """setup-pi.sh copies settings.toml from $OLD_REPO_DIR when present.

        One-step conversion: a Pi being moved from /home/mauricio/lindsay-50
        to /srv/lindsay-50 already has a working settings.toml at
        $OLD_REPO_DIR/heart-matrix-controller/settings.toml. setup-pi.sh
        copies it into the new install so the operator doesn't have
        to scp it manually.
        """
        text = SETUP_PI_PATH.read_text()
        assert "OLD_REPO_DIR" in text, (
            "setup-pi.sh must reference $OLD_REPO_DIR for the settings.toml "
            "fallback copy"
        )
        # The copy path must point at the canonical heart-matrix-controller
        # subdir, both as the source and the destination.
        assert "$OLD_REPO_DIR/heart-matrix-controller/settings.toml" in text, (
            "setup-pi.sh must source settings.toml from "
            "$OLD_REPO_DIR/heart-matrix-controller/settings.toml"
        )
        assert "$REPO_DIR/heart-matrix-controller/settings.toml" in text, (
            "setup-pi.sh must place it at "
            "$REPO_DIR/heart-matrix-controller/settings.toml"
        )

    def test_hard_stops_if_no_settings(self):
        """If $REPO_DIR has no settings.toml AND $OLD_REPO_DIR has none either, hard-stop.

        The error message must give the operator the scp command so
        they know what to do next.
        """
        text = SETUP_PI_PATH.read_text()
        # Find the hard-stop block.
        # The error message uses the $SETTINGS variable which expands
        # at runtime to the actual canonical path; the variable name
        # appears verbatim in setup-pi.sh.
        assert "$SETTINGS is missing" in text, (
            "setup-pi.sh must hard-stop with a clear message if "
            "settings.toml can't be sourced from anywhere"
        )
        assert "sudo scp" in text, (
            "setup-pi.sh hard-stop message must include the scp "
            "command for the operator to source settings.toml"
        )
        # Regression guards: must NOT use the bare-repo + worktree
        # flow that the old setup-pi.sh relied on.
        for forbidden in ("git clone --bare", "worktree add", "v-$HEAD_SHA", "ln -sfn"):
            assert forbidden not in text, (
                f"setup-pi.sh should NOT contain {forbidden!r} — that was "
                f"the dropped bare-repo + worktree bootstrap"
            )

    def test_installs_systemd_unit(self):
        """setup-pi.sh installs the systemd unit and restarts the service."""
        text = SETUP_PI_PATH.read_text()
        assert "daemon-reload" in text
        # The script uses a SERVICE_NAME variable for the service identifier;
        # accept either the literal or the variable form.
        assert (
            "systemctl restart lindsay_50" in text
            or 'systemctl restart "$SERVICE_NAME"' in text
            or "systemctl restart '$SERVICE_NAME'" in text
        ), "setup-pi.sh must restart the lindsay_50 service"
        assert "systemctl enable" in text, "setup-pi.sh must enable the service at boot"

    def test_idempotent_apt(self):
        """Phase 1 (apt) skips packages already installed via dpkg -s."""
        text = SETUP_PI_PATH.read_text()
        assert "dpkg -s" in text, (
            "setup-pi.sh must probe installed packages via `dpkg -s` "
            "to skip them on re-runs"
        )

    def test_idempotent_pip(self):
        """Phase 2 (pip) skips the rgbmatrix build if already importable."""
        text = SETUP_PI_PATH.read_text()
        assert "import rgbmatrix" in text, (
            "setup-pi.sh must probe rgbmatrix via `python3 -c 'import rgbmatrix'` "
            "to skip the slow C build on re-runs"
        )
        assert "--break-system-packages" in text, (
            "setup-pi.sh must use --break-system-packages for the "
            "Pi's system python (no venv on this single-purpose Pi)"
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

    def test_does_not_checkout_after_clone(self):
        """provision-pi.sh must NOT `git checkout -f` against /srv/lindsay-50.

        After setup-pi.sh clones, /srv/lindsay-50 is a non-bare working
        tree, but `git checkout -f` against it is unnecessary — the
        clone landed on the right ref already (via `--branch`). The
        checkout was a holdover from the bare-repo era where bare
        repos reject checkout. The current code only needs `git fetch
        origin` to refresh refs.

        Symptom if missing: `git checkout` fataled with "this operation
        must be run in a work tree" on Pis whose setup-pi.sh conversion
        completed (post-#49 simplification).
        """
        text = PROVISION_PI_PATH.read_text()
        # Reject any actual `git checkout` invocation (comments about the
        # old behavior are fine — they're a regression record). Look for
        # the command pattern, not the substring.
        import re

        invocations = re.findall(r"^\s*git\s+checkout\b", text, re.MULTILINE)
        assert not invocations, (
            "provision-pi.sh must not invoke `git checkout` against "
            "/srv/lindsay-50 — the working tree is already on the right "
            "branch from the `--branch` clone. Use `git fetch origin` instead. "
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
        back to cloning the default branch).
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
        setup fresh on the next step.
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
    def test_exec_main_py(self):
        """startup_matrix_server.sh's final exec line runs main.py directly."""
        text = STARTUP_PATH.read_text()
        # The exec line is the last command in the script.
        exec_lines = [line for line in text.splitlines() if line.strip().startswith("exec ")]
        assert exec_lines, "startup_matrix_server.sh has no `exec` line"
        last_exec = exec_lines[-1]
        assert "main.py" in last_exec, f"final exec should invoke main.py, got: {last_exec!r}"
        # Regression guard: must not exec loader.py — that was the
        # dropped self-upgrade machinery.
        assert "loader.py" not in last_exec, (
            f"final exec should NOT invoke loader.py (self-upgrade machinery "
            f"was dropped). got: {last_exec!r}"
        )

    def test_preserves_log_level_env(self):
        """LOG_LEVEL export is preserved from the original startup script."""
        text = STARTUP_PATH.read_text()
        assert "LOG_LEVEL" in text

    def test_preserves_pythonpath_env(self):
        """PYTHONPATH export is preserved — lib_shared needs to resolve.

        Regression guard: PYTHONPATH must point at $REPO_DIR (not
        $REPO_DIR/current — that was the dropped worktree path).
        """
        text = STARTUP_PATH.read_text()
        assert "PYTHONPATH" in text
        assert "REPO_DIR" in text
        # Must not reference the dropped worktree path.
        assert '"$REPO_DIR/current"' not in text and "'$REPO_DIR/current'" not in text, (
            "PYTHONPATH must not include the dropped `$REPO_DIR/current` "
            "path — the working tree is now at $REPO_DIR"
        )