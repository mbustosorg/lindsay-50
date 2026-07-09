"""Tests for `heart-matrix-controller/loader.py`.

v2 design:
  - status.json probe replaces `--healthcheck` subprocess probe
  - `run_upgrade_flow` returns None; loader execvpe's the active version
    (no Popen returned, no post-swap watchdog)
  - Env vars (LINDSAY50_ACTIVE_SHA, LINDSAY50_REPO_DIR) travel with the
    child via os.execvpe's env dict
  - Failure cases (Flask unreachable, status.json probe fails, stage fails)
    all fall through to "exec the existing current/.../main.py"
  - v3: worktree directory names use the short SHA (7 chars). The
    full SHA still flows through comparison + git operations; only
    the directory name is normalized. Test fixtures mirror that:
    they capture the full SHA from `rev-parse` for git ops, then
    derive the short form for path assertions.

Hermetic: each test uses tmp_path for the repo layout (bare-style git
repo with worktrees + symlink), so we don't touch the real
`/srv/lindsay-50` checkout.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LOADER_PATH = Path(__file__).parent.parent / "heart-matrix-controller" / "loader.py"


def _short(sha: str) -> str:
    """Test-local short_sha — keeps the fixture hermetic from lib_shared imports."""
    return sha[:7] if len(sha) > 7 else sha


def _load_loader():
    """Load loader.py fresh from disk by path.

    Registers the module under the name `loader` (NOT just
    `hmc_loader_under_test`) so tests can use `with patch("loader.os.execvpe")`
    — `unittest.mock.patch` resolves its first argument by looking
    the name up in `sys.modules`, and the long form fails with
    `ModuleNotFoundError: No module named 'loader'` if we only register
    the private name.
    """
    spec = importlib.util.spec_from_file_location("hmc_loader_under_test", str(LOADER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hmc_loader_under_test"] = mod
    sys.modules["loader"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def loader():
    return _load_loader()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run a git command in `cwd`, return stdout."""
    result = subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr}")
    return result.stdout


@pytest.fixture
def bare_repo_with_two_commits(tmp_path):
    """Real-git repo layout with two commits and two worktrees."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    _git(repo_dir, "init", "--initial-branch=main", check=False)
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")
    _git(repo_dir, "config", "commit.gpgsign", "false")

    (repo_dir / "README.md").write_text("first commit\n")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-m", "first commit")
    sha1 = _git(repo_dir, "rev-parse", "HEAD").strip()

    (repo_dir / "README.md").write_text("second commit\n")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-m", "second commit")
    sha2 = _git(repo_dir, "rev-parse", "HEAD").strip()

    # Worktree dirs use the SHORT form. Keep the full SHA in the return
    # value because tests need it for git ops (e.g. `git worktree remove`);
    # path assertions derive short via `_short(...)`.
    v1 = repo_dir / f"v-{_short(sha1)}"
    v2 = repo_dir / f"v-{_short(sha2)}"
    _git(repo_dir, "worktree", "add", str(v1), sha1)
    _git(repo_dir, "worktree", "add", str(v2), sha2)

    current = repo_dir / "current"
    if current.exists() or current.is_symlink():
        current.unlink()
    os.symlink(f"v-{_short(sha1)}", current)

    return repo_dir, sha1, sha2


# ---------------------------------------------------------------------------
# Env var constants — loader and app agree on the spelling
# ---------------------------------------------------------------------------


class TestEnvVarConstants:
    def test_active_sha_constant(self, loader):
        assert loader.ENV_ACTIVE_SHA == "LINDSAY50_ACTIVE_SHA"

    def test_repo_dir_constant(self, loader):
        assert loader.ENV_REPO_DIR == "LINDSAY50_REPO_DIR"

    def test_no_boot_id_constant(self, loader):
        """The loader does not mint or carry an instance identifier anymore."""
        assert not hasattr(loader, "ENV_BOOT_ID")


# ---------------------------------------------------------------------------
# atomic_swap
# ---------------------------------------------------------------------------


class TestAtomicSwap:
    def test_atomic_swap_updates_current_symlink(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "v-old").mkdir()
        (repo / "v-new").mkdir()
        current = repo / "current"
        os.symlink("v-old", current)

        loader.atomic_swap(repo, "new")

        assert current.is_symlink()
        assert os.readlink(current) == "v-new"
        assert (repo / "v-old").exists()  # rollback target preserved

    def test_atomic_swap_replaces_existing_symlink_silently(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        for name in ("v-a", "v-b", "v-c"):
            (repo / name).mkdir()
        current = repo / "current"
        os.symlink("v-a", current)

        loader.atomic_swap(repo, "b")
        assert os.readlink(current) == "v-b"
        loader.atomic_swap(repo, "c")
        assert os.readlink(current) == "v-c"

    def test_atomic_swap_with_short_sha(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "v-abc1234").mkdir()
        current = repo / "current"
        os.symlink("v-old", current)
        loader.atomic_swap(repo, "abc1234")
        assert os.readlink(current) == "v-abc1234"


# ---------------------------------------------------------------------------
# Repo layout helpers
# ---------------------------------------------------------------------------


class TestRepoLayoutHelpers:
    def test_worktree_dir_uses_v_sha_pattern(self, loader):
        assert loader.worktree_dir(Path("/r"), "abc123") == Path("/r/v-abc123")

    def test_worktree_dir_truncates_full_sha_to_short(self, loader):
        """Full 40-char SHA input must produce the same directory as its
        short form, so the loader stage path and the symlink stay in sync
        regardless of which representation Flask returned."""
        full = "b5e191c5df481d51c4e7d1cced51cf7c656f1ead"
        assert loader.worktree_dir(Path("/r"), full) == Path("/r/v-b5e191c")
        assert loader.worktree_dir(Path("/r"), "b5e191c") == Path("/r/v-b5e191c")

    def test_current_symlink_is_repo_current(self, loader):
        assert loader.current_symlink(Path("/r")) == Path("/r/current")

    def test_main_py_for_resolves_through_current(self, loader):
        assert loader.main_py_for(Path("/r")) == "/r/current/heart-matrix-controller/main.py"

    def test_resolve_repo_dir_uses_env_override(self, loader, tmp_path, monkeypatch):
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))
        assert loader.resolve_repo_dir() == tmp_path.resolve()

    def test_resolve_repo_dir_walks_through_current_symlink(self, loader, tmp_path, monkeypatch):
        """resolve_repo_dir() must return the repo root, not the worktree dir.

        The loader's __file__ lives at `<repo_root>/current/heart-matrix-controller/loader.py`
        (the `current` symlink resolves to `v-<sha>/...`, but the symlink
        name is what matters for the walk). Three parents up gets us to
        `<repo_root>/`; two parents lands on the worktree dir, which then
        makes the loader look for the `current` symlink INSIDE the
        worktree (where it doesn't exist). This was the issue #49 startup
        failure on 2026-07-06.
        """
        monkeypatch.delenv("LINDSAY50_REPO_DIR", raising=False)
        repo_root = tmp_path / "r"
        wt_dir = repo_root / "v-abc123" / "heart-matrix-controller"
        wt_dir.mkdir(parents=True)
        loader_py = wt_dir / "loader.py"
        loader_py.write_text("# stub")
        # Force __file__ onto the loader.py in the worktree, simulating
        # the real production layout where `current` is a symlink.
        monkeypatch.setattr(loader, "__file__", str(loader_py))
        assert loader.resolve_repo_dir() == repo_root.resolve()


class TestCurrentSha:
    def test_returns_sha_when_current_symlink_resolves_to_git_worktree(self, loader, bare_repo_with_two_commits):
        repo, sha1, _ = bare_repo_with_two_commits
        assert loader.current_sha(repo) == sha1

    def test_returns_none_when_current_symlink_missing(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        assert loader.current_sha(repo) is None


# ---------------------------------------------------------------------------
# _build_exec_env — env vars the loader sets when handing off
# ---------------------------------------------------------------------------


class TestBuildExecEnv:
    def test_sets_active_sha(self, loader):
        env = loader._build_exec_env(Path("/repo"), "newsha")
        assert env[loader.ENV_ACTIVE_SHA] == "newsha"

    def test_sets_repo_dir(self, loader):
        env = loader._build_exec_env(Path("/repo"), "newsha")
        assert env[loader.ENV_REPO_DIR] == "/repo"

    def test_inherits_base_env(self, loader, monkeypatch):
        monkeypatch.setenv("LINDSAY50_REPO_DIR", "/whatever")
        env = loader._build_exec_env(Path("/r"), "newsha")
        # The os.environ base was inherited (we set + override).
        assert env[loader.ENV_ACTIVE_SHA] == "newsha"
        assert env[loader.ENV_REPO_DIR] == "/r"  # overrides the env var

    def test_uses_injected_base_env(self, loader):
        env = loader._build_exec_env(
            Path("/r"),
            "newsha",
            base_env={"CUSTOM_VAR": "v", loader.ENV_REPO_DIR: "/stale"},
        )
        assert env["CUSTOM_VAR"] == "v"
        assert env[loader.ENV_REPO_DIR] == "/r"  # the new repo_dir overrides

    def test_sets_pythonpath_to_repo_current(self, loader):
        """PYTHONPATH must point at <repo>/current so main.py imports
        the worktree's lib_shared/, not the main clone's stale copy.

        Background: the systemd unit's ExecStart uses the absolute
        path /srv/lindsay-50/scripts/startup_matrix_server.sh, which
        bypasses the `current` symlink. That script exports
        PYTHONPATH=$REPO_DIR (= the main clone), so without the
        loader's override the exec'd main.py would import the stale
        lib_shared/ from the main clone — not the worktree the
        loader just staged. This is the load-bearing reason the
        override lives here.
        """
        env = loader._build_exec_env(Path("/repo"), "newsha")
        assert env["PYTHONPATH"] == "/repo/current"

    def test_pythonpath_overrides_inherited_value(self, loader, monkeypatch):
        """A stale PYTHONPATH from the systemd-launched startup
        script (which exports $REPO_DIR, not $REPO_DIR/current)
        must be overridden — never carried through."""
        monkeypatch.setenv("PYTHONPATH", "/srv/lindsay-50")
        env = loader._build_exec_env(Path("/srv/lindsay-50"), "newsha")
        assert env["PYTHONPATH"] == "/srv/lindsay-50/current"

    def test_pythonpath_overrides_injected_base_env(self, loader):
        """Same override behavior when base_env is injected directly
        (not inherited from os.environ)."""
        env = loader._build_exec_env(
            Path("/r"),
            "newsha",
            base_env={"PYTHONPATH": "/stale/wrong/path"},
        )
        assert env["PYTHONPATH"] == "/r/current"


# ---------------------------------------------------------------------------
# exec_active — uses os.execvpe (not subprocess)
# ---------------------------------------------------------------------------


class TestExecActive:
    def test_execvpe_called_with_python_and_main_py(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "current").symlink_to(tmp_path / "placeholder")
        with patch("loader.os.execvpe") as mock_exec:
            loader.exec_active(repo, "newsha")
        mock_exec.assert_called_once()
        # os.execvpe(file, args, env) — file is args[0], argv list is args[1].
        args = mock_exec.call_args.args
        assert args[0].endswith("python") or "python" in args[0]
        assert args[1][1].endswith("/current/heart-matrix-controller/main.py")

    def test_execvpe_env_includes_active_sha(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "current").symlink_to(tmp_path / "placeholder")
        with patch("loader.os.execvpe") as mock_exec:
            loader.exec_active(repo, "newsha")
        # env is positional args[2] (os.execvpe(file, args, env)).
        env: dict = mock_exec.call_args.args[2]
        assert env[loader.ENV_ACTIVE_SHA] == "newsha"
        assert env[loader.ENV_REPO_DIR] == str(repo)


# Use unittest.mock.patch for the execvpe spy — `patch` is shadowed in the
# loader module too, so we use a local import.
from unittest.mock import patch  # noqa: E402

# ---------------------------------------------------------------------------
# _is_status_healthy — pure function over a status dict
# ---------------------------------------------------------------------------


class TestIsStatusHealthy:
    def test_healthy_when_all_signs_good(self, loader):
        """Two-signal contract: mqtt_connected AND last_error is None."""
        status = {
            "mqtt_connected": True,
            "last_error": None,
        }
        assert loader._is_status_healthy(status) is True

    def test_unhealthy_when_mqtt_disconnected(self, loader):
        assert loader._is_status_healthy({"mqtt_connected": False, "last_error": None}) is False

    def test_unhealthy_when_last_error_set(self, loader):
        assert loader._is_status_healthy({"mqtt_connected": True, "last_error": "boom"}) is False

    def test_healthy_ignores_extraneous_keys(self, loader):
        """`last_tick_age_ms` is no longer in the snapshot — the loader must
        not gate on it. Passing it in an old-style payload must not flip
        the result to unhealthy."""
        assert loader._is_status_healthy({"mqtt_connected": True, "last_error": None, "last_tick_age_ms": 9999}) is True

    def test_unhealthy_when_status_none(self, loader):
        assert loader._is_status_healthy(None) is False  # type: ignore[arg-type]

    def test_unhealthy_when_status_not_dict(self, loader):
        assert loader._is_status_healthy("not a dict") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# probe — uses status.json reads instead of --healthcheck subprocess
# ---------------------------------------------------------------------------


class TestProbe:
    def test_probe_returns_true_on_healthy_status(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        status_path = tmp_path / ".status.json"

        # Write a healthy status.json so the probe's first read is healthy.
        import json as _json

        healthy = {
            "schema_version": 1,
            "active_sha": "x",
            "short_sha": "x",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "uptime_seconds": 1,
            "mqtt_connected": True,
            "last_error": None,
        }
        status_path.write_text(_json.dumps(healthy))

        # The probe spawns the staged main.py via _spawn_staged_for_probe.
        # Patch that to return a stub Popen that always pretends to be
        # running (raises TimeoutExpired on wait), so the probe loop
        # reads status.json instead.
        fake_proc = MagicMock()
        fake_proc.wait = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.5))
        fake_proc.terminate = MagicMock()
        fake_proc.kill = MagicMock()
        with patch.object(loader, "_spawn_staged_for_probe", return_value=fake_proc):
            result = loader.probe(
                repo,
                "anysha",
                status_path=status_path,
                read_status_fn=lambda p, **_: _json.loads(p.read_text()),
                total_timeout_s=2.0,
                kill_grace_s=0.5,
            )
        assert result is True
        # Spawn was cleaned up.
        fake_proc.terminate.assert_called()

    def test_probe_returns_false_on_unhealthy_status(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        status_path = tmp_path / ".status.json"

        # mqtt disconnected → unhealthy
        import json as _json

        status_path.write_text(_json.dumps({"mqtt_connected": False}))

        fake_proc = MagicMock()
        fake_proc.wait = MagicMock(side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.5))
        fake_proc.terminate = MagicMock()
        fake_proc.kill = MagicMock()
        with patch.object(loader, "_spawn_staged_for_probe", return_value=fake_proc):
            result = loader.probe(
                repo,
                "anysha",
                status_path=status_path,
                read_status_fn=lambda p, **_: _json.loads(p.read_text()),
                total_timeout_s=0.5,
                kill_grace_s=0.2,
            )
        assert result is False


# ---------------------------------------------------------------------------
# fetch_expected_sha — thin wrapper around shared boot_config.fetch_boot_config
# ---------------------------------------------------------------------------


class TestFetchExpectedSha:
    def test_returns_expected_sha_on_success(self, loader):
        bc_mock = MagicMock()
        bc_mock.expected_sha = "newsha"
        # The loader is loaded into a fresh module by the `loader` fixture;
        # patch the name on THAT module so we don't accidentally hit the
        # real fetch (which would resolve hostname 'x' and fail).
        with patch.object(loader, "fetch_boot_config", return_value=bc_mock):
            result = loader.fetch_expected_sha(api_url="https://x/api/messages", api_key="k")
        assert result == "newsha"

    def test_returns_none_when_boot_config_returns_none(self, loader):
        with patch.object(loader, "fetch_boot_config", return_value=None):
            result = loader.fetch_expected_sha(api_url="https://x/api/messages", api_key="k")
        assert result is None


class TestRefreshBareRepo:
    """`refresh_bare_repo` runs `git fetch origin` to update the bare repo's
    remote-tracking refs before staging. Without this, the bare repo's
    refdb is frozen at provision time and `git worktree add <new_sha>`
    fails for any commit the operator pushed AFTER provisioning —
    which is the normal case."""

    def test_returns_true_on_success(self, loader, monkeypatch, tmp_path):
        called = []

        def fake_check_call(args, **kwargs):
            called.append(args)
            return 0

        monkeypatch.setattr(loader.subprocess, "check_call", fake_check_call)
        result = loader.refresh_bare_repo(tmp_path)
        assert result is True
        assert len(called) == 1
        # Verify the args: git -C <repo> fetch <remote> <refspec>
        args = called[0]
        assert args[0] == "git"
        assert args[1] == "-C"
        assert args[2] == str(tmp_path)
        assert args[3] == "fetch"
        assert args[4] == "origin"
        assert "+refs/heads/*" in args[5]

    def test_returns_false_on_calledprocesserror(self, loader, monkeypatch, tmp_path):
        def fake_check_call(args, **kwargs):
            raise loader.subprocess.CalledProcessError(128, args, stderr=b"fatal: could not resolve host")

        monkeypatch.setattr(loader.subprocess, "check_call", fake_check_call)
        result = loader.refresh_bare_repo(tmp_path)
        assert result is False

    def test_returns_false_on_timeout(self, loader, monkeypatch, tmp_path):
        def fake_check_call(args, **kwargs):
            raise loader.subprocess.TimeoutExpired(args, 30)

        monkeypatch.setattr(loader.subprocess, "check_call", fake_check_call)
        result = loader.refresh_bare_repo(tmp_path)
        assert result is False

    def test_returns_false_when_git_missing(self, loader, monkeypatch, tmp_path):
        def fake_check_call(args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(loader.subprocess, "check_call", fake_check_call)
        result = loader.refresh_bare_repo(tmp_path)
        assert result is False

    def test_uses_provided_remote_name(self, loader, monkeypatch, tmp_path):
        called = []

        def fake_check_call(args, **kwargs):
            called.append(args)
            return 0

        monkeypatch.setattr(loader.subprocess, "check_call", fake_check_call)
        result = loader.refresh_bare_repo(tmp_path, remote="upstream")
        assert result is True
        assert called[0][4] == "upstream"


class TestStageVersionExceptionCapturesStderr:
    """Regression: `stage_version` used `subprocess.check_call`, which raises
    `CalledProcessError(returncode, cmd)` WITHOUT attaching captured stderr.
    The except clause then crashed on `e.stderr.decode(...)` and reported
    the misleading `'NoneType' object has no attribute 'decode'`.

    After the fix to `subprocess.run(check=True)`, `CalledProcessError`
    carries `e.stderr` populated, so the operator sees the real git error
    (e.g. "fatal: invalid reference: f960136..."), not a Python
    AttributeError. These tests pin that contract."""

    def test_stage_error_carries_subprocess_stderr_not_attributeerror(self, loader, monkeypatch, tmp_path):
        """When `git worktree add` fails with a real error, the StageError
        message must contain the stderr text — NOT the misleading
        "'NoneType' object has no attribute 'decode'."""

        def fake_run(args, **kwargs):
            # Simulate what `subprocess.run(check=True)` produces: a
            # CalledProcessError with `e.stderr` populated.
            raise loader.subprocess.CalledProcessError(
                returncode=128,
                cmd=args,
                stderr=b"fatal: invalid reference: f9601364b80f92452a662d69ecb69eb0a6aa6ff5",
            )

        monkeypatch.setattr(loader.subprocess, "run", fake_run)

        with pytest.raises(loader.StageError) as excinfo:
            loader.stage_version(tmp_path, "f9601364b80f92452a662d69ecb69eb0a6aa6ff5")

        # The real git stderr text is present — proving the new shape
        # surfaces the actual failure.
        assert "invalid reference" in str(excinfo.value)
        # The misleading AttributeError message is NOT present — proving
        # we never crash on `e.stderr.decode(...)`.
        assert "'NoneType' object" not in str(excinfo.value)

    def test_stage_error_handles_none_stderr_gracefully(self, loader, monkeypatch, tmp_path):
        """Defensive: if a future `subprocess.run` somehow produces a
        CalledProcessError with `stderr=None`, the StageError message
        stays informative (no spurious AttributeError)."""

        def fake_run(args, **kwargs):
            raise loader.subprocess.CalledProcessError(returncode=128, cmd=args, stderr=None)

        monkeypatch.setattr(loader.subprocess, "run", fake_run)

        with pytest.raises(loader.StageError) as excinfo:
            loader.stage_version(tmp_path, "f9601364b80f92452a662d69ecb69eb0a6aa6ff5")

        # Message references the SHA + the failure mode, even without
        # stderr captured. Does NOT raise AttributeError.
        assert "f960136" in str(excinfo.value)
        assert "'NoneType' object" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# run_upgrade_flow — full orchestration (no Popen return; execvpe instead)
# ---------------------------------------------------------------------------


class TestRunUpgradeFlowFlaskUnreachable:
    def test_falls_through_when_fetch_returns_none(self, loader, bare_repo_with_two_commits):
        repo, sha1, _sha2 = bare_repo_with_two_commits
        stage_calls = []
        swap_calls = []
        exec_calls = []

        def fake_fetch(**_kw):
            return None  # Flask unreachable

        def fake_stage(repo_dir, sha, **_kw):
            stage_calls.append((repo_dir, sha))
            return repo_dir / f"v-{_short(sha)}"

        def fake_probe(*_a, **_kw):
            return True

        def fake_swap(*_a, **_kw):
            swap_calls.append(_a)

        def fake_exec(*_a, **_kw):
            exec_calls.append((_a, _kw))

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=fake_fetch,
            stage_fn=fake_stage,
            probe_fn=fake_probe,
            swap_fn=fake_swap,
            exec_fn=fake_exec,
        )
        # No stage, no swap — fell through to exec existing current.
        assert stage_calls == []
        assert swap_calls == []
        # Exec called exactly once (with the local SHA we already had).
        assert len(exec_calls) == 1
        assert exec_calls[0][0][1] == sha1  # active_sha

    def test_falls_through_when_local_matches_expected(self, loader, bare_repo_with_two_commits):
        repo, sha1, _sha2 = bare_repo_with_two_commits
        stage_calls = []
        exec_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha1,  # local == expected
            stage_fn=lambda repo_dir, sha, **_kw: (
                stage_calls.append((repo_dir, sha)) or repo_dir / f"v-{_short(sha)}"
            ),
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=lambda *_a, **_kw: None,
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        assert stage_calls == []
        assert len(exec_calls) == 1

    def test_falls_through_when_probe_fails(self, loader, bare_repo_with_two_commits):
        repo, sha1, sha2 = bare_repo_with_two_commits
        swap_calls = []
        exec_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: False,  # probe unhealthy
            swap_fn=lambda *_a, **_kw: swap_calls.append(_a),
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        # Must not swap when probe fails.
        assert swap_calls == []
        # Fall through to exec existing current.
        assert len(exec_calls) == 1
        assert exec_calls[0][0][1] == sha1
        # current symlink unchanged.
        assert os.readlink(repo / "current") == f"v-{_short(sha1)}"

    def test_falls_through_when_stage_raises(self, loader, bare_repo_with_two_commits):
        repo, sha1, sha2 = bare_repo_with_two_commits
        swap_calls = []
        exec_calls = []

        def bad_stage(*_a, **_kw):
            raise loader.StageError("simulated")

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            stage_fn=bad_stage,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=lambda *_a, **_kw: swap_calls.append(_a),
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        assert swap_calls == []
        assert len(exec_calls) == 1
        assert exec_calls[0][0][1] == sha1


class TestRunUpgradeFlowHappyPath:
    def test_stages_probes_swaps_and_execs_on_mismatch(self, loader, bare_repo_with_two_commits):
        repo, _sha1, sha2 = bare_repo_with_two_commits
        # Remove the pre-created v-<short_sha2> so the stage actually runs.
        v2 = repo / f"v-{_short(sha2)}"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(v2)],
            check=True,
            capture_output=True,
        )
        exec_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            refresh_fn=lambda *_a, **_kw: True,
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=loader.atomic_swap,
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        # Loader staged v-<short_sha2>
        assert (repo / f"v-{_short(sha2)}").exists()
        # Loader swapped current -> v-<short_sha2>
        assert os.readlink(repo / "current") == f"v-{_short(sha2)}"
        # Loader exec'd the new SHA (not the old one).
        assert len(exec_calls) == 1
        assert exec_calls[0][0][1] == sha2

    def test_skips_staging_when_worktree_already_exists(self, loader, bare_repo_with_two_commits):
        repo, _sha1, sha2 = bare_repo_with_two_commits
        # v-<short_sha2> already exists from the fixture → stage returns it.
        assert (repo / f"v-{_short(sha2)}").exists()
        exec_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            refresh_fn=lambda *_a, **_kw: True,
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=loader.atomic_swap,
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        # current swapped to new SHA, exec called with new SHA.
        assert os.readlink(repo / "current") == f"v-{_short(sha2)}"
        assert exec_calls[0][0][1] == sha2


class TestRunUpgradeFlowDoesNotReturn:
    def test_run_upgrade_flow_calls_exec_fn_on_every_path(self, loader, bare_repo_with_two_commits):
        """Every code path through run_upgrade_flow calls exec_fn exactly once.

        The function execvpe's the active version (the loader is gone
        after exec) — there is no return path where the loader
        continues running. If exec_fn is not called, the loader
        falls off the end and the systemd service restarts
        indefinitely.
        """
        repo, sha1, sha2 = bare_repo_with_two_commits

        for fetch_result in [None, sha1, sha2]:
            for stage_raises in [True, False]:
                for probe_result in [True, False]:
                    # Skip the "stage raised" + "probe true" combination
                    # because stage raising means probe never runs.
                    if stage_raises and probe_result:
                        continue

                    exec_calls = []

                    def fake_stage(repo_dir, sha, **_kw):
                        if stage_raises:
                            raise loader.StageError("simulated")
                        return repo_dir / f"v-{_short(sha)}"

                    loader.run_upgrade_flow(
                        repo,
                        api_url="https://x/api/messages",
                        api_key="k",
                        fetch_fn=lambda **_kw: fetch_result,
                        refresh_fn=lambda *_a, **_kw: True,
                        stage_fn=fake_stage,
                        probe_fn=lambda *_a, **_kw: probe_result,
                        swap_fn=lambda *_a, **_kw: None,
                        exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
                    )
                    assert len(exec_calls) == 1, (
                        f"exec_fn not called for fetch={fetch_result}, "
                        f"stage_raises={stage_raises}, probe_result={probe_result}"
                    )


# ---------------------------------------------------------------------------
# prune_worktrees — caps on-disk worktree count so the SD card can't fill
# ---------------------------------------------------------------------------


class TestPruneWorktrees:
    """After a successful swap, the loader removes v-<sha>/ dirs beyond the
    last `keep` (default 3), preserving whatever `current` points at. Without
    this, the bare repo's worktree metadata accumulates entries forever — on
    a 15 GB Pi SD card, ~28 deploys × 203 MB/worktree filled the rootfs and
    journald started logging `[Errno 28] No space left on device` (2026-07-08).
    """

    def _make_v_dirs(self, repo: Path, count: int) -> list[Path]:
        """Create `count` v-<sha>/ dirs with distinct mtimes (newest first)."""
        dirs = []
        for i in range(count):
            d = repo / f"v-abc{i:04d}"
            d.mkdir()
            time.sleep(0.01)  # distinct mtimes
            dirs.append(d)
        return dirs

    def _collect_remove_targets(self, calls):
        """Extract the worktree path from each `git worktree remove` subprocess.run call.

        `git -C <repo> worktree remove --force <path>` — path is the last arg.
        """
        out = []
        for args in calls:
            if len(args) >= 7 and args[3] == "worktree" and args[4] == "remove":
                out.append(Path(args[-1]).name)
        return out

    def test_first_call_runs_git_worktree_prune(self, loader, monkeypatch, tmp_path):
        """prune_worktrees' first action must be `git worktree prune` so
        any stale .git/worktrees/ entries (a dir rm'd externally) are
        cleared before we try to read or remove others."""
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr(loader.subprocess, "run", fake_run)
        loader.prune_worktrees(tmp_path)

        assert calls, "expected at least one subprocess.run call"
        assert calls[0][0] == "git"
        assert calls[0][1] == "-C"
        assert calls[0][2] == str(tmp_path)
        assert calls[0][3] == "worktree"
        assert calls[0][4] == "prune"

    def test_keeps_current_under_keep_count(self, loader, monkeypatch, tmp_path):
        """When there are fewer v-<sha>/ dirs than `keep`, none get removed —
        `current` is already safe, and we shouldn't churn disk for nothing."""
        self._make_v_dirs(tmp_path, 2)
        os.symlink("v-abc0000", tmp_path / "current")

        calls = []
        monkeypatch.setattr(
            loader.subprocess,
            "run",
            lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0),
        )

        loader.prune_worktrees(tmp_path, keep=3)

        removed = self._collect_remove_targets(calls)
        assert removed == [], f"expected no removals; got {removed}"

    def test_prunes_dirs_beyond_keep_count(self, loader, monkeypatch, tmp_path):
        """With 5 v-<sha>/ dirs and keep=3, the two oldest (by mtime) get removed."""
        self._make_v_dirs(tmp_path, 5)
        # current → abc0000 (OLDEST by mtime — we created dirs in numeric
        # order with a sleep between each).
        os.symlink("v-abc0000", tmp_path / "current")

        calls = []
        monkeypatch.setattr(
            loader.subprocess,
            "run",
            lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0),
        )

        loader.prune_worktrees(tmp_path, keep=3)

        removed = self._collect_remove_targets(calls)
        # mtime order (newest first): abc0004, abc0003, abc0002, abc0001, abc0000.
        # current = abc0000 (oldest) is always preserved. headroom = keep - 1 = 2.
        # We pad the keep-set with the 2 newest non-current: abc0004 + abc0003.
        # Keep set: {abc0000 (current), abc0004, abc0003}. Remove abc0001 + abc0002.
        assert sorted(removed) == [
            "v-abc0001",
            "v-abc0002",
        ], f"expected the 2 oldest non-current removed; got {sorted(removed)}"

    def test_always_preserves_current_even_when_oldest(self, loader, monkeypatch, tmp_path):
        """`current` must always survive even if it's the OLDEST v-<sha> dir
        (e.g., a downgrade or a rollback target). Test by making current
        point at the last-created dir, then advancing mtimes on the others."""
        dirs = self._make_v_dirs(tmp_path, 5)
        # Point current at the OLDEST dir (abc0004 was created first, but
        # we sorted by mtime; touch all the others to make them newer).
        os.symlink("v-abc0000", tmp_path / "current")
        # Bump mtimes of newer ones to be even newer
        new_t = time.time() + 100
        for d in dirs[1:]:  # all except the current one
            os.utime(d, (new_t, new_t))

        calls = []
        monkeypatch.setattr(
            loader.subprocess,
            "run",
            lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0),
        )

        loader.prune_worktrees(tmp_path, keep=2)

        # current (v-abc0000) is OLDEST by mtime but must survive.
        # keep=2 means we keep current + 1 more (the newest non-current).
        removed = self._collect_remove_targets(calls)
        assert "v-abc0000" not in removed, "current must never be removed, even if it's the oldest"

    def test_no_remove_call_when_under_keep(self, loader, monkeypatch, tmp_path):
        """Belt-and-braces: under keep, NO `git worktree remove` is invoked
        (only the metadata prune)."""
        self._make_v_dirs(tmp_path, 1)
        os.symlink("v-abc0000", tmp_path / "current")

        calls = []
        monkeypatch.setattr(
            loader.subprocess,
            "run",
            lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0),
        )

        loader.prune_worktrees(tmp_path, keep=3)

        remove_calls = [c for c in calls if len(c) >= 7 and c[4] == "remove"]
        assert remove_calls == [], "no remove calls expected under keep"

    def test_handles_missing_git_gracefully(self, loader, monkeypatch, tmp_path):
        """If git itself is missing, prune_worktrees logs and returns
        silently — never raises. (Cleanup is hygiene, not a deploy gate.)"""

        def fake_run(args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(loader.subprocess, "run", fake_run)
        # Must not raise.
        loader.prune_worktrees(tmp_path)

    def test_continues_after_individual_remove_failure(self, loader, monkeypatch, tmp_path):
        """If one `worktree remove` fails, the others still attempt. Hygiene
        shouldn't bail at the first hiccup."""
        self._make_v_dirs(tmp_path, 5)
        os.symlink("v-abc0000", tmp_path / "current")

        call_count = {"n": 0}

        def fake_run(args, **kwargs):
            call_count["n"] += 1
            # Let the metadata prune succeed. Fail every `worktree remove`.
            if len(args) >= 7 and args[4] == "remove":
                raise loader.subprocess.CalledProcessError(
                    returncode=1,
                    cmd=args,
                    stderr=b"locked",
                )
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr(loader.subprocess, "run", fake_run)
        # Must not raise.
        loader.prune_worktrees(tmp_path, keep=3)


# ---------------------------------------------------------------------------
# run_upgrade_flow calls prune_worktrees after a successful swap
# ---------------------------------------------------------------------------


class TestRunUpgradeFlowCallsPruneAfterSwap:
    """The prune is what keeps the SD card from filling up — wire it into
    the success path of run_upgrade_flow so EVERY successful deploy leaves
    the on-disk worktree count bounded."""

    def test_prune_called_after_successful_swap(self, loader, bare_repo_with_two_commits):
        repo, _sha1, sha2 = bare_repo_with_two_commits
        # Remove the pre-created v-<short_sha2> so the stage actually runs.
        v2 = repo / f"v-{_short(sha2)}"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(v2)],
            check=True,
            capture_output=True,
        )

        prune_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            refresh_fn=lambda *_a, **_kw: True,
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=loader.atomic_swap,
            exec_fn=lambda *_a, **_kw: None,
            prune_fn=lambda *_a, **_kw: prune_calls.append((_a, _kw)),
        )

        assert len(prune_calls) == 1, (
            f"prune_fn must be called exactly once on the success path; " f"got {len(prune_calls)} calls"
        )
        assert prune_calls[0][0][0] == repo, "prune_fn called with repo_dir"

    def test_prune_not_called_when_local_matches_expected(self, loader, bare_repo_with_two_commits):
        """No deploy happened → no prune. Old v-<sha>/ dirs are still useful
        as rollback targets / historical state."""
        repo, sha1, _sha2 = bare_repo_with_two_commits
        prune_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha1,  # local == expected
            refresh_fn=lambda *_a, **_kw: True,
            stage_fn=lambda *_a, **_kw: repo / f"v-{_short(sha1)}",
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=lambda *_a, **_kw: None,
            exec_fn=lambda *_a, **_kw: None,
            prune_fn=lambda *_a, **_kw: prune_calls.append((_a, _kw)),
        )

        assert prune_calls == [], "no swap → no prune"

    def test_prune_not_called_when_probe_fails(self, loader, bare_repo_with_two_commits):
        """Failed probe → fall through to existing current → no prune.
        (The old worktrees are the rollback target; we mustn't touch them.)"""
        repo, _sha1, sha2 = bare_repo_with_two_commits
        prune_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            refresh_fn=lambda *_a, **_kw: True,
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: False,  # probe unhealthy
            swap_fn=lambda *_a, **_kw: None,
            exec_fn=lambda *_a, **_kw: None,
            prune_fn=lambda *_a, **_kw: prune_calls.append((_a, _kw)),
        )

        assert prune_calls == [], "probe failure must not trigger a prune"

    def test_prune_not_called_when_stage_fails(self, loader, bare_repo_with_two_commits):
        """Stage failure → no prune. The old worktrees are still the only
        thing on disk that lets us boot."""
        repo, _sha1, sha2 = bare_repo_with_two_commits
        prune_calls = []

        def bad_stage(*_a, **_kw):
            raise loader.StageError("simulated")

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            refresh_fn=lambda *_a, **_kw: True,
            stage_fn=bad_stage,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=lambda *_a, **_kw: None,
            exec_fn=lambda *_a, **_kw: None,
            prune_fn=lambda *_a, **_kw: prune_calls.append((_a, _kw)),
        )

        assert prune_calls == [], "stage failure must not trigger a prune"
