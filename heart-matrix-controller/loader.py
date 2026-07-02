"""Loader process — system upgrade flow for the matrix controller.

This is the systemd `ExecStart`. On every boot it:

  1. Resolves the local active SHA (resolved through `current/`).
  2. Queries Flask for the expected SHA via /api/sign/expected-sha.
  3. If they match, execs `current/heart-matrix-controller/main.py`.
  4. If they differ, stages the new SHA into a worktree, runs
     `main.py --healthcheck`, atomically swaps `current` if healthy,
     then execs the new version.
  5. Watches the subprocess for 30s. If it exits non-zero within
     that window, swap `current` back to the previous known-good
     SHA and re-exec.

Failure modes — Flask unreachable, health check fails, worktree
create fails — all fall through to "exec the existing
current/.../main.py" so the Pi is never bricked.

Design notes:
  - All side effects go through small, named functions so unit
    tests can drive failure cases without touching real hardware
    or the network.
  - `os.execvp` is used (not `subprocess.run`) for the active
    version so systemd sees `main.py` as the direct child — PID
    stays the same, signal handling is preserved.
  - Atomic swap uses `ln -sfn`, which on the same filesystem is
    atomic relative to any concurrent reader.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("loader")


# ---------------------------------------------------------------------------
# Custom exceptions (typed so callers can catch specific failures)
# ---------------------------------------------------------------------------


class StageError(Exception):
    """Raised when `git worktree add` fails for any reason.

    Carries the underlying stderr string so the operator's journalctl
    output surfaces the real failure (network down, dirty tree,
    missing commit on the remote, etc).
    """


# ---------------------------------------------------------------------------
# Repo layout helpers
# ---------------------------------------------------------------------------


def resolve_repo_dir() -> Path:
    """Return the absolute path to the repo root.

    Defaults to the parent of this script's directory
    (`heart-matrix-controller/loader.py` → `<repo_root>/`). Override
    via the `LINDSAY50_REPO_DIR` env var for tests + non-standard
    deployments.
    """
    env = os.environ.get("LINDSAY50_REPO_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def worktree_dir(repo_dir: Path, sha: str) -> Path:
    """Return the per-SHA worktree directory: `<repo_dir>/v-<sha>`."""
    return repo_dir / f"v-{sha}"


def current_symlink(repo_dir: Path) -> Path:
    """Return the path to the `current` symlink."""
    return repo_dir / "current"


def main_py_for(repo_dir: Path) -> str:
    """Return the absolute path to `main.py` for the active version."""
    return str(current_symlink(repo_dir) / "heart-matrix-controller" / "main.py")


# ---------------------------------------------------------------------------
# Stage / swap / status helpers
# ---------------------------------------------------------------------------


def current_sha(repo_dir: Path) -> Optional[str]:
    """Resolve the active SHA through the `current/` symlink.

    Reads `git -C <current_worktree> rev-parse HEAD` so the value
    reflects what's actually live (not what's in the bare repo's
    detached HEAD). Returns None if the symlink is missing or the
    git invocation fails — the caller treats both as "boot into the
    existing current/.../main.py" without staging anything new.
    """
    cur = current_symlink(repo_dir)
    if not cur.exists():
        logger.warning("loader: current symlink does not exist at %s", cur)
        return None
    try:
        out = subprocess.check_output(
            ["git", "-C", str(cur), "rev-parse", "HEAD"],
            stderr=subprocess.PIPE,
            timeout=5,
        )
        return out.decode().strip() or None
    except Exception as e:
        logger.warning("loader: could not read current SHA via %s: %s", cur, e)
        return None


def stage_version(repo_dir: Path, expected_sha: str) -> Path:
    """Stage `expected_sha` into a new git worktree at `<repo_dir>/v-<sha>`.

    On a dirty working tree in the existing version, runs
    `git reset --hard` first to clear it (the operator should not
    be editing files on the Pi). On any other failure (network
    down, missing commit), raises `StageError` with stderr.
    """
    target = worktree_dir(repo_dir, expected_sha)
    if target.exists():
        logger.info("loader: worktree %s already exists, skipping stage", target)
        return target

    # Clear any dirty working tree in the existing `current/`. The
    # operator should not be editing files on the Pi — this is
    # defense-in-depth, not a feature. A failure here bubbles up
    # so we don't accidentally overwrite local edits.
    cur = current_symlink(repo_dir)
    if cur.exists():
        try:
            subprocess.check_call(
                ["git", "-C", str(cur), "reset", "--hard", "HEAD"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            raise StageError(
                f"reset --hard failed in {cur}: {e.stderr.decode(errors='replace')}"
            ) from e

    # Stage the new worktree from the bare repo. The bare repo
    # already has the history (we cloned once via setup-pi.sh),
    # so this is a fast local operation — only fails if `expected_sha`
    # isn't actually in the bare repo's refs.
    try:
        subprocess.check_call(
            [
                "git",
                "-C",
                str(repo_dir),
                "worktree",
                "add",
                str(target),
                expected_sha,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        raise StageError(
            f"worktree add failed for {expected_sha}: {e.stderr.decode(errors='replace')}"
        ) from e
    except Exception as e:
        raise StageError(f"worktree add raised: {e}") from e

    logger.info("loader: staged %s at %s", expected_sha, target)
    return target


def run_health_check(repo_dir: Path, expected_sha: str, timeout: float = 60.0) -> int:
    """Run `v-<sha>/heart-matrix-controller/main.py --healthcheck`.

    Returns the subprocess exit code (0 = pass, non-zero = fail).
    The loader only inspects the exit code — what the check
    actually verifies is the app's concern, not ours.
    """
    cmd = [
        sys.executable,
        str(worktree_dir(repo_dir, expected_sha) / "heart-matrix-controller" / "main.py"),
        "--healthcheck",
    ]
    logger.info("loader: running healthcheck: %s", " ".join(cmd))
    try:
        return subprocess.call(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("loader: healthcheck timed out after %.0fs", timeout)
        return 124  # convention from coreutils timeout(1)
    except Exception as e:
        logger.error("loader: healthcheck failed to spawn: %s", e)
        return 125


def atomic_swap(repo_dir: Path, expected_sha: str) -> None:
    """Atomically retarget the `current` symlink to `v-<expected_sha>`.

    Uses `ln -sfn`, which is atomic on the same filesystem: a
    concurrent reader either sees the old target or the new one,
    never a half-constructed link. The `-f` flag replaces an
    existing symlink silently.
    """
    target_rel = f"v-{expected_sha}"
    cur = current_symlink(repo_dir)
    logger.info("loader: swapping %s -> %s", cur, target_rel)
    subprocess.check_call(
        ["ln", "-sfn", target_rel, str(cur)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=5,
    )


# ---------------------------------------------------------------------------
# Version source of truth (Flask)
# ---------------------------------------------------------------------------


def fetch_expected_sha(
    *,
    requests_module=None,
    timeout: float = 5.0,
) -> Optional[str]:
    """GET /api/sign/expected-sha from Flask, return the SHA or None.

    Reads the Flask URL + API key from `settings.toml` (located via
    cwd — the systemd unit's WorkingDirectory is the repo root).
    Returns None on ANY failure — network error, non-200 status,
    missing key, malformed JSON — so the caller can fall through to
    "boot the existing current/.../main.py" without staging anything
    new. A Pi that can't reach Flask must keep running on the last
    good version; retry on next boot.

    Args:
        requests_module: Override for the `requests` module — tests
            inject a mock. Defaults to `import requests` at call time.
        timeout: HTTP timeout in seconds.
    """
    try:
        from lib_shared.config_reader import get_config  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("loader: cannot import config_reader")
        return None

    REQUIRED_KEYS: set[str] = {
        "CONFIG_API_URL",
        "MESSAGES_API_URL",
        "API_SECRET_KEY",
    }
    try:
        cfg = get_config(REQUIRED_KEYS)
    except Exception as e:
        logger.warning("loader: config_reader failed: %s", e)
        return None

    base = cfg.if_exists("CONFIG_API_URL") or ""
    api_key = cfg.if_exists("API_SECRET_KEY") or ""
    if not base or not api_key:
        logger.warning("loader: missing CONFIG_API_URL or API_SECRET_KEY in settings")
        return None

    # CONFIG_API_URL is the config endpoint URL; expected-sha lives
    # at the same host but a different path. Strip the trailing
    # path and re-derive the origin so the endpoint is host-relative.
    from urllib.parse import urlparse

    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
    if not origin:
        logger.warning("loader: could not derive origin from %s", base)
        return None
    url = f"{origin}/api/sign/expected-sha"

    if requests_module is None:
        try:
            import requests as requests_module  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("loader: requests not available; cannot query Flask")
            return None

    try:
        resp = requests_module.get(
            url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        sha = data.get("expected_sha") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not sha:
            logger.warning("loader: empty expected_sha in response: %r", data)
            return None
        return sha
    except Exception as e:
        logger.warning("loader: fetch_expected_sha failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Subprocess supervision
# ---------------------------------------------------------------------------


def exec_active(repo_dir: Path) -> None:
    """Replace the current process with `current/.../main.py`.

    Uses `os.execvp` (NOT `subprocess.run`) so systemd sees
    `main.py` as the direct child — PID stays the same, signal
    handling is preserved. Does not return on success; raises
    `OSError` if `python3` or `main.py` is missing/unexecutable.
    """
    main_py = main_py_for(repo_dir)
    logger.info("loader: execvp python3 %s", main_py)
    os.execvp(sys.executable, [sys.executable, main_py])


def watch_subprocess(
    proc: subprocess.Popen,
    repo_dir: Path,
    previous_sha: str,
    grace_seconds: float = 30.0,
) -> None:
    """Wait up to `grace_seconds` for `proc`; rollback if it exits non-zero.

    The Pi's loader uses this to detect "starts then crashes 10s in"
    failures that --healthcheck cannot see (the render loop crashes
    after the initial checks pass). If the subprocess exits non-zero
    within the grace window, swap `current` back to `v-<previous_sha>`
    and re-exec. If it stays up past the grace window, exit normally —
    systemd will restart the loader on next boot, which will exec the
    same `current/.../main.py` again (because `current` still points
    at the new version).

    Note: the rollback target is `v-<previous_sha>` (NOT
    `v-<current_sha>`). After the swap, `current` points to
    `v-<new_sha>`; `previous_sha` is what was active before the
    swap. Rolling back to `v-<new_sha>` would be a no-op.
    """
    try:
        rc = proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.info(
            "loader: subprocess stayed up past grace %.0fs; exiting normally",
            grace_seconds,
        )
        return
    if rc == 0:
        logger.info(
            "loader: subprocess exited cleanly rc=0 within grace; exiting normally"
        )
        return
    logger.warning(
        "loader: subprocess exited rc=%s within %.0fs grace; rolling back to %s",
        rc,
        grace_seconds,
        previous_sha,
    )
    try:
        atomic_swap(repo_dir, previous_sha)
    except Exception as e:
        logger.error("loader: rollback atomic_swap failed: %s", e)
    exec_active(repo_dir)


# ---------------------------------------------------------------------------
# Full upgrade flow (orchestration)
# ---------------------------------------------------------------------------


def run_upgrade_flow(
    repo_dir: Path,
    *,
    fetch_fn: Callable[[], Optional[str]] = fetch_expected_sha,
    stage_fn: Callable[[Path, str], Path] = stage_version,
    health_check_fn: Callable[[Path, str], int] = run_health_check,
    swap_fn: Callable[[Path, str], None] = atomic_swap,
) -> Optional[subprocess.Popen]:
    """Decide whether to stage + swap + exec, or fall through.

    Returns the subprocess.Popen if a new version was swapped and
    exec'd (so the caller can `watch_subprocess` on it). Returns
    None if the loader should fall through to `exec_active` on the
    existing `current/.../main.py` (Flask unreachable, health check
    failed, SHAs match, etc).

    Splitting exec from run_upgrade_flow lets tests verify the
    orchestration logic without actually starting a Python
    subprocess — they monkey-patch `swap_fn` etc. and assert on
    the call sequence.
    """
    local = current_sha(repo_dir)
    logger.info("loader: local SHA = %s", local)

    expected = fetch_fn()
    if expected is None:
        logger.warning("loader: could not fetch expected SHA; using existing current")
        return None
    logger.info("loader: expected SHA = %s", expected)

    if local == expected:
        logger.info("loader: local SHA matches expected; no upgrade needed")
        return None

    # Mismatch — stage the new version.
    try:
        stage_fn(repo_dir, expected)
    except StageError as e:
        logger.error("loader: staging %s failed: %s; using existing current", expected, e)
        return None
    except Exception as e:
        logger.error("loader: staging %s raised: %s; using existing current", expected, e)
        return None

    rc = health_check_fn(repo_dir, expected)
    if rc != 0:
        logger.error(
            "loader: healthcheck for %s exited rc=%s; NOT swapping; using existing current",
            expected,
            rc,
        )
        return None

    swap_fn(repo_dir, expected)
    logger.info("loader: swapped to %s; exec'ing", expected)
    # We exec here in production — but for testability we spawn a
    # subprocess instead. The wrapper `main()` does the actual
    # os.execvp; tests use `run_upgrade_flow` with monkey-patched
    # `swap_fn` and verify the orchestration without exec.
    return _spawn_active(repo_dir)


def _spawn_active(repo_dir: Path) -> subprocess.Popen:
    """Spawn the active version as a child process (test-friendly path).

    Production uses `os.execvp` via `exec_active` so systemd sees
    `main.py` as the direct child. Tests use this helper to keep
    the loader alive after staging — they can inspect the child
    PID, terminate it, etc.
    """
    main_py = main_py_for(repo_dir)
    return subprocess.Popen([sys.executable, main_py])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    """Loader entrypoint.

    On upgrade mismatch, this does:
      1. Run the upgrade flow.
      2. If a new subprocess was spawned, `watch_subprocess` on it
         with the previous known-good SHA so we can rollback.
      3. Otherwise, fall through to `exec_active` so the loader
         process becomes `current/.../main.py`.

    Returns the child's exit code when watch_subprocess returns
    normally (subprocess stayed up past grace); returns 0 if the
    flow didn't stage anything (we exec'd in place).
    """
    repo_dir = resolve_repo_dir()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.info("loader: starting; repo_dir=%s", repo_dir)

    local = current_sha(repo_dir)
    proc = run_upgrade_flow(repo_dir)
    if proc is None:
        # Either SHAs matched, or staging/healthcheck failed — in
        # both cases, just exec the existing current/.../main.py.
        exec_active(repo_dir)
        # exec_active does not return on success.
        return 0

    # New subprocess spawned. Watch it; if it dies early, watch_subprocess
    # swaps current back to local and execs the previous version.
    assert local is not None, "spawning implies we had a local SHA to roll back to"
    watch_subprocess(proc, repo_dir, previous_sha=local)
    # If we get here, the subprocess stayed up past the grace window.
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())