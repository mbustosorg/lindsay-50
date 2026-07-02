"""Tests for heart-matrix-controller/loader.py.

Covers the blue/green upgrade flow added in issue #49:

  - atomic_swap updates the `current` symlink without disturbing
    the old worktree directory on disk.
  - run_upgrade_flow against a fixture bare repo with two commits
    stages the second SHA, swaps, and returns a Popen for the
    active version.
  - run_upgrade_flow falls through cleanly when Flask is
    unreachable (fetch_fn returns None) — no stage, no swap.
  - watch_subprocess rollback targets the previous SHA, not the
    new one, when the new subprocess exits non-zero within the
    grace window.

Each test uses a `tmp_path` repo layout (bare repo + worktree +
symlink) so they're hermetic and don't touch the real
`/home/pi/projects/lindsay-50` checkout.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

LOADER_PATH = (
    Path(__file__).parent.parent / "heart-matrix-controller" / "loader.py"
)


def _load_loader():
    """Load loader.py fresh from disk.

    Sibling tests can wipe `sys.modules` between cases; loading by
    path every time guarantees we exercise the same module the
    production code imports.
    """
    spec = importlib.util.spec_from_file_location("hmc_loader_under_test", str(LOADER_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hmc_loader_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def loader():
    return _load_loader()


# ---------------------------------------------------------------------------
# Bare-repo fixture: real git with two commits
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run a git command in `cwd`, return stdout. Raises on failure unless `check=False`."""
    result = subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr}"
        )
    return result.stdout


@pytest.fixture
def bare_repo_with_two_commits(tmp_path):
    """Create a real bare-style repo layout under `tmp_path` with two commits.

    Returns `(repo_dir, sha1, sha2)`:
      - `repo_dir` is the parent of `.git/`, `v-<sha1>/`, `v-<sha2>/`, and `current/`.
      - `sha1` is the first commit (becomes the rolled-back target).
      - `sha2` is the second commit (becomes the new version).

    The fixture mirrors the production Pi layout:
      - `.git/` is a real git dir (not bare — sufficient for worktree add).
      - `v-<sha>/` are worktrees at the two commits.
      - `current` is a symlink pointing at `v-<sha1>/`.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Initialize a real (non-bare) repo with a single file, commit, then
    # make a second commit. Both commits are real git SHAs the loader can
    # resolve via `git rev-parse HEAD` and stage via `git worktree add`.
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

    # Create worktrees. The first one becomes `v-<sha1>` and the source
    # of the `current` symlink. The second one becomes `v-<sha2>` —
    # already present on disk so we exercise the "skip stage" path.
    v1 = repo_dir / f"v-{sha1}"
    v2 = repo_dir / f"v-{sha2}"
    _git(repo_dir, "worktree", "add", str(v1), sha1)
    _git(repo_dir, "worktree", "add", str(v2), sha2)

    # current -> v-<sha1>
    current = repo_dir / "current"
    if current.exists() or current.is_symlink():
        current.unlink()
    os.symlink(f"v-{sha1}", current)

    return repo_dir, sha1, sha2


# ---------------------------------------------------------------------------
# 4.11 — atomic_swap updates current symlink target; old target preserved
# ---------------------------------------------------------------------------


class TestAtomicSwap:
    def test_atomic_swap_updates_current_symlink(self, loader, tmp_path):
        """atomic_swap(v-<new>) makes `current` point to v-<new>; v-<old> still on disk."""
        repo = tmp_path / "r"
        repo.mkdir()
        # Two fake "worktrees" — directories only, no git needed for this test.
        old_dir = repo / "v-old"
        new_dir = repo / "v-new"
        old_dir.mkdir()
        new_dir.mkdir()
        current = repo / "current"
        os.symlink("v-old", current)

        loader.atomic_swap(repo, "new")

        # current now points to v-new
        assert current.is_symlink()
        assert os.readlink(current) == "v-new"
        # v-old is still on disk (rollback target preserved)
        assert old_dir.exists()

    def test_atomic_swap_replaces_existing_symlink_silently(self, loader, tmp_path):
        """A second atomic_swap overwrites the first without erroring."""
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
        """atomic_swap accepts short SHAs (matches git's short-hash output)."""
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "v-abc1234").mkdir()
        current = repo / "current"
        os.symlink("v-old", current)

        loader.atomic_swap(repo, "abc1234")
        assert os.readlink(current) == "v-abc1234"


# ---------------------------------------------------------------------------
# 4.13 — Flask-unreachable path: fall through to exec without staging
# ---------------------------------------------------------------------------


class TestFlaskUnreachable:
    def test_returns_none_when_fetch_returns_none(self, loader, bare_repo_with_two_commits):
        """When fetch_fn returns None, run_upgrade_flow returns None without staging or swapping."""
        repo, sha1, _sha2 = bare_repo_with_two_commits
        stage_calls = []
        swap_calls = []
        health_calls = []

        def fetch_fn():
            return None  # simulates Flask unreachable / 5xx / timeout

        def stage_fn(repo_dir, sha):
            stage_calls.append(sha)
            return repo_dir / f"v-{sha}"

        def health_fn(repo_dir, sha):
            health_calls.append(sha)
            return 0

        def swap_fn(repo_dir, sha):
            swap_calls.append(sha)

        result = loader.run_upgrade_flow(
            repo,
            fetch_fn=fetch_fn,
            stage_fn=stage_fn,
            health_check_fn=health_fn,
            swap_fn=swap_fn,
        )
        assert result is None
        # Crucially: nothing was staged, nothing was swapped.
        assert stage_calls == []
        assert swap_calls == []
        assert health_calls == []
        # And `current` is unchanged — still points to v-<sha1>.
        assert os.readlink(repo / "current") == f"v-{sha1}"

    def test_returns_none_when_local_matches_expected(self, loader, bare_repo_with_two_commits):
        """When local SHA matches expected SHA, run_upgrade_flow returns None without action."""
        repo, sha1, _sha2 = bare_repo_with_two_commits
        stage_calls = []

        def fetch_fn():
            return sha1  # local == expected → no upgrade needed

        def stage_fn(repo_dir, sha):
            stage_calls.append(sha)
            return repo_dir / f"v-{sha}"

        result = loader.run_upgrade_flow(
            repo,
            fetch_fn=fetch_fn,
            stage_fn=stage_fn,
            health_check_fn=lambda *a: 0,
            swap_fn=lambda *a: None,
        )
        assert result is None
        assert stage_calls == []

    def test_returns_none_when_healthcheck_fails(self, loader, bare_repo_with_two_commits):
        """Health check exit non-zero → do not swap, return None, leave current unchanged."""
        repo, sha1, sha2 = bare_repo_with_two_commits
        swap_calls = []

        result = loader.run_upgrade_flow(
            repo,
            fetch_fn=lambda: sha2,
            stage_fn=loader.stage_version,
            health_check_fn=lambda *a: 1,  # health check fails
            swap_fn=lambda *a: swap_calls.append(a),
        )
        assert result is None
        assert swap_calls == [], "must not swap when healthcheck fails"
        # current still points to v-<sha1>
        assert os.readlink(repo / "current") == f"v-{sha1}"


# ---------------------------------------------------------------------------
# 4.12 — Full upgrade flow against a fixture bare repo with two commits
# ---------------------------------------------------------------------------


class TestFullUpgradeFlow:
    def test_stages_swaps_and_returns_popen_on_mismatch(self, loader, bare_repo_with_two_commits):
        """SHA mismatch → real `git worktree add` for v-<sha2>, atomic_swap, exec Popen."""
        repo, sha1, sha2 = bare_repo_with_two_commits
        # Remove the pre-created v-<sha2> to prove the loader stages it fresh.
        v2 = repo / f"v-{sha2}"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(v2)],
            check=True,
            capture_output=True,
        )
        assert not v2.exists(), "precondition: v-<sha2> removed"

        # Use the real stage_version + atomic_swap + _spawn_active, but a
        # fake fetch returning sha2. _spawn_active returns a real Popen
        # against `current/heart-matrix-controller/main.py` — which we
        # satisfy by writing a stub script that just sleeps and exits.
        stub_main = repo / "current" / "heart-matrix-controller" / "main.py"
        stub_main.parent.mkdir(parents=True, exist_ok=True)
        stub_main.write_text(
            "#!/usr/bin/env python3\n"
            "import time, sys\n"
            "sys.stdout.write('stub running\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.3)\n"
            "sys.exit(0)\n"
        )
        stub_main.chmod(0o755)

        proc = loader.run_upgrade_flow(
            repo,
            fetch_fn=lambda: sha2,
            stage_fn=loader.stage_version,
            health_check_fn=lambda *a: 0,
            swap_fn=loader.atomic_swap,
        )
        try:
            # Loader returned a Popen — the new subprocess is live.
            assert proc is not None
            assert proc.poll() is None or proc.returncode == 0
            # Loader staged v-<sha2>
            assert (repo / f"v-{sha2}").exists()
            # Loader swapped current -> v-<sha2>
            assert os.readlink(repo / "current") == f"v-{sha2}"
            # Wait for the stub to finish naturally
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_full_flow_skips_staging_when_worktree_already_exists(
        self, loader, bare_repo_with_two_commits
    ):
        """v-<sha2>/ already exists from the fixture → stage_version returns it without re-staging."""
        repo, sha1, sha2 = bare_repo_with_two_commits
        # Both worktrees exist from the fixture.
        assert (repo / f"v-{sha2}").exists()

        # Stub main.py so _spawn_active can fork without rgbmatrix etc.
        stub_main = repo / "current" / "heart-matrix-controller" / "main.py"
        stub_main.parent.mkdir(parents=True, exist_ok=True)
        stub_main.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "time.sleep(0.3)\n"
            "sys.exit(0)\n"
        )
        stub_main.chmod(0o755)

        proc = loader.run_upgrade_flow(
            repo,
            fetch_fn=lambda: sha2,
            stage_fn=loader.stage_version,
            health_check_fn=lambda *a: 0,
            swap_fn=loader.atomic_swap,
        )
        try:
            assert proc is not None
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
        # current now points to v-<sha2>
        assert os.readlink(repo / "current") == f"v-{sha2}"


# ---------------------------------------------------------------------------
# watch_subprocess — rollback to v-<previous_sha>, not v-<current_sha>
# ---------------------------------------------------------------------------


class TestWatchSubprocess:
    def test_rollback_targets_previous_sha_on_early_nonzero_exit(self, loader, tmp_path):
        """If the subprocess exits non-zero within grace, swap current back to v-<previous_sha>."""
        repo = tmp_path / "r"
        repo.mkdir()
        # Two worktrees + current -> v-<previous>
        (repo / "v-previous").mkdir()
        (repo / "v-current").mkdir()
        os.symlink("v-previous", repo / "current")

        # Popen stub that exits non-zero immediately
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])

        # exec_active would replace the test process — monkey-patch it
        # so we can observe the rollback symlink change instead.
        exec_calls = []

        def fake_exec_active(repo_dir):
            exec_calls.append(repo_dir)

        loader.exec_active = fake_exec_active

        loader.watch_subprocess(proc, repo, previous_sha="previous", grace_seconds=2.0)

        # current now points to v-previous (the rollback target)
        assert os.readlink(repo / "current") == "v-previous"
        # exec_active was called so the loader would re-exec the old version
        assert exec_calls == [repo]

    def test_no_rollback_when_subprocess_stays_up_past_grace(self, loader, tmp_path):
        """A subprocess that survives the grace window leaves `current` pointing at the new version."""
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "v-previous").mkdir()
        (repo / "v-current").mkdir()
        # current -> v-current (the new version)
        os.symlink("v-current", repo / "current")

        # Popen stub that sleeps longer than the grace window
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.0)"])

        try:
            loader.watch_subprocess(proc, repo, previous_sha="previous", grace_seconds=0.3)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)

        # current still points to v-current — no rollback
        assert os.readlink(repo / "current") == "v-current"

    def test_no_rollback_on_clean_exit_within_grace(self, loader, tmp_path):
        """Subprocess exiting 0 within grace is treated as success — no rollback."""
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "v-previous").mkdir()
        (repo / "v-current").mkdir()
        os.symlink("v-current", repo / "current")

        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        loader.watch_subprocess(proc, repo, previous_sha="previous", grace_seconds=2.0)
        # current still v-current
        assert os.readlink(repo / "current") == "v-current"


# ---------------------------------------------------------------------------
# resolve_repo_dir + worktree_dir + current_symlink + main_py_for helpers
# ---------------------------------------------------------------------------


class TestRepoLayoutHelpers:
    def test_worktree_dir_uses_v_sha_pattern(self, loader):
        """worktree_dir(repo, sha) returns repo/v-<sha>."""
        assert loader.worktree_dir(Path("/r"), "abc123") == Path("/r/v-abc123")

    def test_current_symlink_is_repo_current(self, loader):
        assert loader.current_symlink(Path("/r")) == Path("/r/current")

    def test_main_py_for_resolves_through_current(self, loader):
        """main_py_for joins current -> heart-matrix-controller/main.py."""
        assert loader.main_py_for(Path("/r")) == "/r/current/heart-matrix-controller/main.py"

    def test_resolve_repo_dir_uses_env_override(self, loader, tmp_path, monkeypatch):
        """LINDSAY50_REPO_DIR overrides the default script-relative path."""
        monkeypatch.setenv("LINDSAY50_REPO_DIR", str(tmp_path))
        result = loader.resolve_repo_dir()
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# current_sha — git rev-parse through the symlink
# ---------------------------------------------------------------------------


class TestCurrentSha:
    def test_returns_sha_when_current_symlink_resolves_to_git_worktree(self, loader, bare_repo_with_two_commits):
        """current_sha reads HEAD from the worktree the symlink points at."""
        repo, sha1, _ = bare_repo_with_two_commits
        assert loader.current_sha(repo) == sha1

    def test_returns_none_when_current_symlink_missing(self, loader, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        # No `current` symlink at all
        assert loader.current_sha(repo) is None