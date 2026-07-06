"""Tests for `heart-matrix-controller/loader.py`.

v2 design:
  - status.json probe replaces `--healthcheck` subprocess probe
  - `run_upgrade_flow` returns None; loader execvpe's the active version
    (no Popen returned, no post-swap watchdog)
  - Env vars (LINDSAY50_ACTIVE_SHA, LINDSAY50_REPO_DIR) travel with the
    child via os.execvpe's env dict
  - Failure cases (Flask unreachable, status.json probe fails, stage fails)
    all fall through to "exec the existing current/.../main.py"

Hermetic: each test uses tmp_path for the repo layout (bare-style git
repo with worktrees + symlink), so we don't touch the real
`/home/pi/projects/lindsay-50` checkout.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LOADER_PATH = Path(__file__).parent.parent / "heart-matrix-controller" / "loader.py"


def _load_loader():
    """Load loader.py fresh from disk by path."""
    spec = importlib.util.spec_from_file_location("hmc_loader_under_test", str(LOADER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hmc_loader_under_test"] = mod
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

    v1 = repo_dir / f"v-{sha1}"
    v2 = repo_dir / f"v-{sha2}"
    _git(repo_dir, "worktree", "add", str(v1), sha1)
    _git(repo_dir, "worktree", "add", str(v2), sha2)

    current = repo_dir / "current"
    if current.exists() or current.is_symlink():
        current.unlink()
    os.symlink(f"v-{sha1}", current)

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
        status = {
            "mqtt_connected": True,
            "last_tick_age_ms": 100,
            "last_error": None,
        }
        assert loader._is_status_healthy(status) is True

    def test_unhealthy_when_mqtt_disconnected(self, loader):
        assert loader._is_status_healthy({"mqtt_connected": False, "last_tick_age_ms": 0, "last_error": None}) is False

    def test_unhealthy_when_last_error_set(self, loader):
        assert loader._is_status_healthy({"mqtt_connected": True, "last_tick_age_ms": 0, "last_error": "boom"}) is False

    def test_unhealthy_when_tick_stale(self, loader):
        assert (
            loader._is_status_healthy({"mqtt_connected": True, "last_tick_age_ms": 9999, "last_error": None}) is False
        )

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
            "pid": 1,
            "active_sha": "x",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "uptime_seconds": 1.0,
            "mqtt_connected": True,
            "last_tick_age_ms": 10,
            "messages_rendered": 0,
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
            return repo_dir / f"v-{sha}"

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
            stage_fn=lambda repo_dir, sha, **_kw: (stage_calls.append((repo_dir, sha)) or repo_dir / f"v-{sha}"),
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
        assert os.readlink(repo / "current") == f"v-{sha1}"

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
        # Remove the pre-created v-<sha2> so the stage actually runs.
        v2 = repo / f"v-{sha2}"
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
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=loader.atomic_swap,
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        # Loader staged v-<sha2>
        assert (repo / f"v-{sha2}").exists()
        # Loader swapped current -> v-<sha2>
        assert os.readlink(repo / "current") == f"v-{sha2}"
        # Loader exec'd the new SHA (not the old one).
        assert len(exec_calls) == 1
        assert exec_calls[0][0][1] == sha2

    def test_skips_staging_when_worktree_already_exists(self, loader, bare_repo_with_two_commits):
        repo, _sha1, sha2 = bare_repo_with_two_commits
        # v-<sha2> already exists from the fixture → stage returns it.
        assert (repo / f"v-{sha2}").exists()
        exec_calls = []

        loader.run_upgrade_flow(
            repo,
            api_url="https://x/api/messages",
            api_key="k",
            fetch_fn=lambda **_kw: sha2,
            stage_fn=loader.stage_version,
            probe_fn=lambda *_a, **_kw: True,
            swap_fn=loader.atomic_swap,
            exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
        )
        # current swapped to new SHA, exec called with new SHA.
        assert os.readlink(repo / "current") == f"v-{sha2}"
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
                        return repo_dir / f"v-{sha}"

                    loader.run_upgrade_flow(
                        repo,
                        api_url="https://x/api/messages",
                        api_key="k",
                        fetch_fn=lambda **_kw: fetch_result,
                        stage_fn=fake_stage,
                        probe_fn=lambda *_a, **_kw: probe_result,
                        swap_fn=lambda *_a, **_kw: None,
                        exec_fn=lambda *_a, **_kw: exec_calls.append((_a, _kw)),
                    )
                    assert len(exec_calls) == 1, (
                        f"exec_fn not called for fetch={fetch_result}, "
                        f"stage_raises={stage_raises}, probe_result={probe_result}"
                    )
