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

    def test_wires_up_post_checkout_hook(self):
        """setup-pi.sh must symlink hooks/post-checkout → .git/hooks/post-checkout
        before each `git worktree add`, so the hook fires on the bootstrap and on
        every future loader.py upgrade.

        Background: git only fires hooks from <gitdir>/hooks/. Without this
        symlink, git falls back to post-checkout.sample (disabled), the hook
        silently no-ops, and Phase 4 hard-stops on missing settings.toml —
        the failure mode seen at 1676e70 during the wipe+provision test.
        """
        text = SETUP_PI_PATH.read_text()
        # The function must exist (so the install is reusable across
        # the three branches) and must use ln -sfn (idempotent).
        assert "install_post_checkout_hook" in text, "setup-pi.sh must define install_post_checkout_hook"
        # The symlink target and source must both be referenced.
        assert ".git/hooks/post-checkout" in text
        assert "hooks/post-checkout" in text
        # The install call must happen BEFORE each `git worktree add`
        # in both the partial-bootstrap and non-bare-conversion
        # branches. (The already-bootstrapped branch doesn't add a
        # worktree, but should still install the hook self-heal-style
        # for future loader.py upgrades.)
        #
        # Find each `install_post_checkout_hook` call site and the
        # nearest-following `worktree add`. The call must come first
        # in each branch.
        for _ in range(2):  # exactly 2 worktree-add call sites in the state machine
            call_idx = text.find("install_post_checkout_hook")
            worktree_idx = text.find("worktree add", call_idx)
            assert call_idx > 0, "no install_post_checkout_hook call found"
            assert worktree_idx > call_idx, (
                "install_post_checkout_hook must be called BEFORE "
                "`git worktree add` (otherwise the hook won't fire "
                "on this add)"
            )
            # Advance past this pair to find the next one. Crude but
            # sufficient for the 2-call pattern in this script.
            text = text[worktree_idx + 1 :]

    def test_preserves_origin_across_bare_conversion(self):
        """Bare conversion must restore origin; `git clone --bare <local-path>`
        rewrites origin to the local source path, which then gets rm-rf'd.

        Symptom if missing: every subsequent `git fetch origin` (loader.py
        upgrades, re-running provision-pi.sh) fataled with
        "'.git.tmp' does not appear to be a git repository / Could not read
        from remote repository" — first surfaced in the wipe+provision retry
        after the 7c43589 hook-install commit.
        """
        text = SETUP_PI_PATH.read_text()
        # Find the non-bare conversion branch and check its body.
        else_branch_idx = text.find("# Non-bare clone")
        assert else_branch_idx > 0, "non-bare conversion branch missing"
        else_branch = text[else_branch_idx : else_branch_idx + 1500]
        # The conversion must read the origin URL from the OLD .git/config
        # BEFORE the `mv .git .git.tmp` line destroys it.
        capture_idx = else_branch.find("remote.origin.url")
        mv_idx = else_branch.find('mv "$REPO_DIR/.git"')
        assert capture_idx > 0, (
            "non-bare conversion must capture remote.origin.url before " "the mv clobbers .git/config"
        )
        assert mv_idx > 0, "non-bare conversion mv line missing"
        assert capture_idx < mv_idx, (
            "origin capture must happen BEFORE `mv .git .git.tmp` " "(otherwise the source config is already gone)"
        )
        # The conversion must restore origin AFTER `git clone --bare` (which
        # rewrote it to the local source path).
        restore_idx = else_branch.find("remote set-url origin")
        clone_idx = else_branch.find("git clone --bare")
        assert restore_idx > 0, (
            "non-bare conversion must restore origin after `git clone --bare` "
            "(which set it to the local source path that gets rm-rf'd)"
        )
        assert clone_idx > 0, "non-bare conversion git clone --bare line missing"
        assert restore_idx > clone_idx, (
            "origin restoration must happen AFTER `git clone --bare` " "(restoring before the rewrite is a no-op)"
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

    def test_no_root_requirements_txt(self):
        """The old root requirements.txt has been split — it must not exist.

        A lingering root requirements.txt would silently get picked up by
        Heroku (which defaults to that path) — masking the new file layout
        and re-introducing the split bug.
        """
        path = PROJECT_ROOT / "requirements.txt"
        assert not path.exists(), (
            "requirements.txt must NOT exist at the repo root after the "
            "split — Heroku's default lookup would silently re-export the "
            "old combined deps. Use requirements-flask.txt via the "
            "PIP_REQUIREMENTS_PATH heroku config var instead."
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
