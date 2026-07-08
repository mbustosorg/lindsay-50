"""Loader process — system upgrade flow for the matrix controller.

This is the systemd `ExecStart`. On every boot it:

  1. Resolves the local active SHA (resolved through `current/`).
  2. Queries Flask for the expected SHA via `/api/sign/boot-config`
     (using the shared `lib_shared.boot_config.fetch_boot_config`).
  3. If they match, execs `current/heart-matrix-controller/main.py`
     with the env vars the app needs (LINDSAY50_ACTIVE_SHA, etc.).
  4. If they differ, stages the new SHA into a worktree, spawns
     the staged `main.py` briefly and reads its `.status.json` to
     decide whether to swap, atomically swaps `current` if healthy,
     then execs the new version. The loader is gone after exec —
     systemd's `StartLimitBurst=3` bounds crash loops, and
     `heroku rollback v<N>` is the operator's rollback primitive.

Failure modes — Flask unreachable, status.json probe fails, worktree
create fails — all fall through to "exec the existing
current/.../main.py" so the Pi is never bricked.

Design notes:
  - All side effects go through small, named functions so unit
    tests can drive failure cases without touching real hardware
    or the network.
  - `os.execvpe` is used (not `subprocess.run`) for the active
    version so systemd sees `main.py` as the direct child — PID
    stays the same, signal handling is preserved.
  - Atomic swap uses `ln -sfn`, which on the same filesystem is
    atomic relative to any concurrent reader.
  - Env vars (`LINDSAY50_ACTIVE_SHA`, `LINDSAY50_REPO_DIR`) travel
    with the child via `os.execvpe`, so the app never has to run
    `git rev-parse HEAD` on the hot path.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from lib_shared.boot_config import (
    BootConfig,
    current_sha,
    fetch_boot_config,
    short_sha,
)

logger = logging.getLogger("loader")


# Env vars the loader sets via os.execvpe when handing off to the
# app. Centralized so tests and the app agree on the spelling.
ENV_ACTIVE_SHA = "LINDSAY50_ACTIVE_SHA"
ENV_REPO_DIR = "LINDSAY50_REPO_DIR"

# Pre-swap probe parameters. The probe spawns the staged main.py
# and waits for its status.json to reflect a healthy render loop.
DEFAULT_PROBE_TOTAL_S = 12.0  # wall-clock budget for the whole probe
DEFAULT_PROBE_KILL_GRACE_S = 2.0  # after SIGTERM before SIGKILL
DEFAULT_STATUS_PATH = ".status.json"

# `git worktree add` timeout. The checkout step on a Raspberry Pi SD
# card can easily take 30-90s for a repo of this size (the bare repo
# has hundreds of objects; checkout under SD-card IO pressure is the
# slow part, not the network fetch). 30s was too tight — empirically
# the loader hit it on the 2026-07-07 v-0232104→f960136 swap. 120s
# stays bounded (operator won't wait minutes) but tolerates slow
# storage.
DEFAULT_WORKTREE_ADD_TIMEOUT_S = 120.0

# Refspec used to refresh the bare repo's remote-tracking branches.
# Mirrors what `scripts/setup-pi.sh` does at provision time so the
# fetch behavior is consistent whether we boot or re-provision.
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"

# Default timeout for `git fetch origin`. Generous enough that a
# cold SSL handshake to GitHub doesn't trip the timer on slow
# networks; short enough that a fully-down link doesn't stall the
# boot sequence for more than ~30s.
DEFAULT_FETCH_TIMEOUT_S = 30.0

# Worktree pruning — cap on the number of historical `v-<sha>` dirs
# the loader keeps on disk. `current` is always preserved (so a
# rollback or in-flight deploy never loses its target); the rest are
# removed by mtime, newest-first. The default of 3 leaves current +
# previous + one spare for offline inspection. Without this, the
# bare repo's worktree metadata accumulates entries forever — on a
# 15 GB Pi SD card, ~28 deploys × 203 MB/worktree filled the rootfs
# and journald started logging `[Errno 28] No space left on device`
# (2026-07-08).
DEFAULT_KEEP_WORKTREES = 3


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

    Defaults to `<repo_root>/` by walking three parents up from this
    script: `heart-matrix-controller/loader.py` → `heart-matrix-controller/`
    → `current/` → `<repo_root>/`. The `current` symlink makes the
    first hop name-resolve to `v-<sha>/`, so we have to walk through
    it explicitly. Override via the `LINDSAY50_REPO_DIR` env var for
    tests + non-standard deployments.
    """
    env = os.environ.get(ENV_REPO_DIR)
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent.parent


def worktree_dir(repo_dir: Path, sha: str) -> Path:
    """Return the per-SHA worktree directory: `<repo_dir>/v-<short_sha>`.

    Accepts a full or short SHA; the directory name always uses the
    short form (7 chars) so paths like `v-b5e191c/` stay readable in
    `ls`, journalctl, and recovery commands. Comparison and git
    operations still use whatever the caller passed — only the
    directory name is normalized here.
    """
    return repo_dir / f"v-{short_sha(sha)}"


def current_symlink(repo_dir: Path) -> Path:
    """Return the path to the `current` symlink."""
    return repo_dir / "current"


def main_py_for(repo_dir: Path) -> str:
    """Return the absolute path to `main.py` for the active version."""
    return str(current_symlink(repo_dir) / "heart-matrix-controller" / "main.py")


# ---------------------------------------------------------------------------
# Stage / swap / status helpers
# ---------------------------------------------------------------------------


def stage_version(repo_dir: Path, expected_sha: str) -> Path:
    """Stage `expected_sha` into a new git worktree at `<repo_dir>/v-<short_sha>`.

    The directory name uses the short form (7 chars) so the on-disk
    layout stays readable; the full SHA is the one git resolves
    against — `git worktree add` accepts a full ref, short SHA, or
    branch name, and the bare repo's refdb has the full form.

    On a dirty working tree in the existing version, runs
    `git reset --hard` first to clear it (the operator should not
    be editing files on the Pi). On any other failure (network
    down, missing commit), raises `StageError` with stderr.
    """
    # Clear stale worktree registrations before adding. Without this,
    # `git worktree add <new-sha>` fails with "missing but already
    # registered worktree" if a prior version's dir was rm'd
    # externally (e.g., the 2026-07-08 disk-fill recovery rm'd
    # `v-b258aa2/` but left `.git/worktrees/v-b258aa2/gitdir`
    # pointing at the now-missing path). `prune` is a no-op when
    # there's nothing to clean, so it's safe to call every stage.
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False,  # prune returns 1 when nothing to prune; that's fine
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception as exc:
        # Hygiene, not a deploy gate — log and continue.
        logger.warning("loader: git worktree prune before stage failed: %s", exc)

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
            subprocess.run(
                ["git", "-C", str(cur), "reset", "--hard", "HEAD"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            # `subprocess.run(check=True)` populates `e.stderr` with
            # the captured stderr bytes — `subprocess.check_call`
            # would NOT, which is why the old code crashed on the
            # `e.stderr.decode(...)` call and reported a misleading
            # "'NoneType' object has no attribute 'decode'".
            stderr_text = e.stderr.decode(errors="replace") if e.stderr else ""
            raise StageError(f"reset --hard failed in {cur}: {stderr_text}") from e

    # Stage the new worktree from the bare repo. The bare repo
    # already has the history (refresh_bare_repo fetched it earlier
    # in the flow), so this is a fast local operation — only fails
    # if `expected_sha` isn't actually in the bare repo's refs.
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "worktree",
                "add",
                str(target),
                expected_sha,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_WORKTREE_ADD_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as e:
        stderr_text = e.stderr.decode(errors="replace") if e.stderr else ""
        raise StageError(f"worktree add failed for {expected_sha}: {stderr_text}") from e
    except Exception as e:
        raise StageError(f"worktree add raised: {e}") from e

    logger.info("loader: staged %s at %s", short_sha(expected_sha), target)
    return target


def refresh_bare_repo(
    repo_dir: Path,
    *,
    remote: str = "origin",
    refspec: str = FETCH_REFSPEC,
    timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
) -> bool:
    """Refresh the bare repo's remote-tracking refs.

    Required before `stage_version`: the bare repo's refdb only
    carries the refs that were present at provision time (when
    `scripts/setup-pi.sh` ran `git clone --bare` and a one-shot
    `git fetch`). Without this refresh, `git worktree add
    <expected_sha>` fails with "invalid reference" whenever the
    expected SHA was pushed to the source branch AFTER the Pi was
    provisioned — which is the normal case (the operator pushes
    to main on their laptop, the Pi picks it up on next boot).

    Uses the same refspec as setup-pi.sh's bootstrap fetch so the
    behavior is consistent across both paths. Refspec pins to
    `refs/remotes/origin/*` (not `refs/heads/*`) so the bare
    repo's `HEAD` stays where setup-pi.sh pinned it; the loader
    only consumes the remote-tracking branches, never the bare
    repo's HEAD.

    Returns True on success, False on any failure (no remote,
    network down, git missing, timeout). The caller treats False
    as "couldn't refresh refs, fall through to existing current"
    — same posture as a failed boot-config fetch.

    The loader runs as root (matches the systemd unit's `User=root`)
    and the bare repo's `origin` points at GitHub over HTTPS, so
    no SSH agent or credential material is needed.
    """
    try:
        subprocess.check_call(
            [
                "git",
                "-C",
                str(repo_dir),
                "fetch",
                remote,
                refspec,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
        logger.info("loader: fetched %s on %s", remote, repo_dir)
        return True
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        logger.warning("loader: fetch from %s failed: %s", remote, exc)
        return False


def prune_worktrees(
    repo_dir: Path,
    *,
    keep: int = DEFAULT_KEEP_WORKTREES,
) -> None:
    """Cap on-disk worktree count so the bare repo can't fill the SD card.

    Two operations, in order:

      1. `git worktree prune` — clears `.git/worktrees/` entries for
         directories that no longer exist on disk. The metadata-only
         step (no-op when nothing is stale). `stage_version` already
         runs this inline before `worktree add`; running it here too
         covers callers that invoke this function standalone (tests,
         recovery scripts).

      2. `git worktree remove --force` for each `v-<sha>/` directory
         beyond the last `keep` (by mtime, newest-first), always
         preserving whatever `current` points at. The bare repo's
         refdb keeps every commit we've ever fetched, but the
         worktree dirs themselves are working copies and not needed
         once the SHA they're pinned to is no longer `current`.

    Failures (git missing, permission errors, a worktree that's
    locked for some other reason) are logged but never raised —
    pruning is hygiene, not a deploy gate. A failed prune means
    we'll try again on the next deploy, which is the right
    posture: the deploy should never be blocked by cleanup that
    doesn't affect correctness.
    """
    # Step 1: clear stale worktree metadata.
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("loader: git worktree prune failed: %s", exc)
        return

    # Step 2: remove v-<sha>/ dirs beyond the last `keep`.
    try:
        cur_link = repo_dir / "current"
        current_name: Optional[str] = None
        if cur_link.is_symlink() or cur_link.exists():
            try:
                current_name = cur_link.resolve().name
            except OSError:
                # Broken symlink — leave the keep-set empty of `current`;
                # the sort by mtime still gives a deterministic pick.
                pass

        v_dirs = [
            p for p in repo_dir.iterdir()
            if p.is_dir() and p.name.startswith("v-")
        ]
        # Newest first by mtime; on Linux ext4 mtime resolution is
        # good enough to distinguish sequential deploys.
        v_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        keep_set: set[str] = set()
        if current_name:
            keep_set.add(current_name)
        # Pad with the (keep - len(keep_set)) newest by mtime so we
        # always preserve `current` even if its mtime isn't among the
        # very newest.
        headroom = max(0, keep - len(keep_set))
        for p in v_dirs[:headroom]:
            keep_set.add(p.name)

        for v in v_dirs:
            if v.name in keep_set:
                continue
            try:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "worktree",
                        "remove",
                        "--force",
                        str(v),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )
                logger.info("loader: pruned old worktree %s", v.name)
            except subprocess.CalledProcessError as exc:
                stderr_text = exc.stderr.decode(errors="replace") if exc.stderr else ""
                logger.warning(
                    "loader: failed to prune %s: %s", v.name, stderr_text,
                )
    except Exception as exc:
        logger.warning("loader: prune_worktrees raised: %s", exc)


def atomic_swap(repo_dir: Path, expected_sha: str) -> None:
    """Atomically retarget the `current` symlink to `v-<short_sha>`.

    The symlink target uses the short SHA so the visible result in
    `ls -l /srv/lindsay-50/` matches `v-b5e191c`, not
    `v-b5e191c5df481d51c4e7d1cced51cf7c656f1ead`. The full SHA is
    what `stage_version` already wrote on disk at `v-<short>/`, so
    this name is consistent.

    Uses `ln -sfn`, which is atomic on the same filesystem: a
    concurrent reader either sees the old target or the new one,
    never a half-constructed link. The `-f` flag replaces an
    existing symlink silently.
    """
    target_rel = f"v-{short_sha(expected_sha)}"
    cur = current_symlink(repo_dir)
    logger.info("loader: swapping %s -> %s", cur, target_rel)
    # `subprocess.run(check=True)` (not `subprocess.check_call`) so the
    # `CalledProcessError` carries the captured stderr; the fall-through
    # `except Exception` in `run_upgrade_flow` will log the real error
    # string instead of the misleading "'NoneType' object has no
    # attribute 'decode'" that the previous shape produced.
    subprocess.run(
        ["ln", "-sfn", target_rel, str(cur)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=5,
    )


def _build_exec_env(
    repo_dir: Path,
    active_sha: str,
    *,
    base_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build the env dict we pass to `os.execvpe`.

    Inherits the loader's own env (so LOG_LEVEL, the user's PATH,
    and anything else systemd/the startup script set, carry
    through), then sets/refreshes the two `LINDSAY50_*` vars the
    app reads at module load, and — critically — overrides
    PYTHONPATH to point at the active worktree.

    PYTHONPATH override (the load-bearing part): the systemd unit's
    ExecStart uses the absolute path
    `/srv/lindsay-50/scripts/startup_matrix_server.sh`, which
    bypasses the `current` symlink — it always runs the main
    clone's `scripts/startup_matrix_server.sh`, which is whatever
    commit `origin/main` happens to be on (often stale). The
    main-clone script sets `PYTHONPATH=$REPO_DIR` (i.e.
    `/srv/lindsay-50`), so `import lib_shared` resolves to the
    main clone's `lib_shared/` — not the worktree the loader just
    staged and probed. Result: main.py runs the worktree's
    `main.py` but the OLD `lib_shared/effects_coordinator.py`,
    which is why every commit's debug prints and state-machine
    fixes have appeared to land but never actually run.

    The loader knows exactly which worktree is active (it just
    swapped the `current` symlink, or verified it was already
    pointing at `v-<short_sha>`), so it sets PYTHONPATH to
    `<repo_dir>/current` here. The exec'd main.py is then
    guaranteed to import from the worktree's `lib_shared/`
    regardless of what the systemd unit or startup script did.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env[ENV_ACTIVE_SHA] = active_sha
    env[ENV_REPO_DIR] = str(repo_dir)
    env["PYTHONPATH"] = str(repo_dir / "current")
    return env


def exec_active(
    repo_dir: Path,
    active_sha: str,
) -> None:
    """Replace the current process with `current/.../main.py`.

    Uses `os.execvpe` (NOT `subprocess.run`) so systemd sees
    `main.py` as the direct child — PID stays the same, signal
    handling is preserved. Sets `LINDSAY50_ACTIVE_SHA`, the running
    version, so the app doesn't have to run `git rev-parse HEAD`
    on the hot path.
    """
    main_py = main_py_for(repo_dir)
    env = _build_exec_env(repo_dir, active_sha)
    # Belt-and-braces: log the EXACT PYTHONPATH we're about to hand the
    # kernel via os.execvpe. If this matches /proc/<pid>/environ, the
    # override is working. If they differ, something between
    # _build_exec_env and the kernel is rewriting PYTHONPATH (likely
    # the systemd unit, the bash script, or a stale `.pyc`).
    logger.info(
        "loader: execvpe python3 %s with PYTHONPATH=%r LINDSAY50_REPO_DIR=%r",
        main_py,
        env.get("PYTHONPATH"),
        env.get(ENV_REPO_DIR),
    )
    os.execvpe(sys.executable, [sys.executable, main_py], env)


# ---------------------------------------------------------------------------
# Pre-swap probe (status.json from the running app)
# ---------------------------------------------------------------------------


def _spawn_staged_for_probe(
    repo_dir: Path,
    expected_sha: str,
    *,
    status_path: Path,
) -> subprocess.Popen:
    """Spawn the staged `main.py` for the pre-swap probe.

    Returns the Popen; the caller is responsible for terminating
    it and reading `status_path`. The probe child writes to
    `status_path` (a tmp file in the repo dir for hermetic tests);
    in production `status_path` is `<repo_dir>/.status.json`.
    """
    staged_main_py = worktree_dir(repo_dir, expected_sha) / "heart-matrix-controller" / "main.py"
    env = _build_exec_env(repo_dir, expected_sha)
    env["LINDSAY50_STATUS_PATH"] = str(status_path)
    return subprocess.Popen(
        [sys.executable, str(staged_main_py)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def probe(
    repo_dir: Path,
    expected_sha: str,
    *,
    total_timeout_s: float = DEFAULT_PROBE_TOTAL_S,
    kill_grace_s: float = DEFAULT_PROBE_KILL_GRACE_S,
    status_path: Optional[Path] = None,
    read_status_fn: Optional[Callable[..., Optional[dict]]] = None,
) -> bool:
    """Validate a staged worktree by spawning it briefly.

    Spawns `v-<sha>/heart-matrix-controller/main.py`, waits up to
    `total_timeout_s` for it to write a healthy `status.json`,
    then terminates the child and returns whether the snapshot
    was fresh + healthy. A healthy snapshot is one whose:
      - `mqtt_connected` is True
      - `last_tick_age_ms` is < 2000 (recent render activity)
      - `last_error` is None

    Failure cases (any of these returns False):
      - child exits non-zero before the timeout (e.g. ImportError)
      - status.json is missing or stale at the timeout
      - status.json reports unhealthy (mqtt disconnected, stale
        ticks, last_error set)

    The status_path defaults to `<repo_dir>/.status.json` — the
    same path the running production app writes to. Tests inject
    a tmp dir to keep the probe hermetic.

    Note: this is a pre-swap probe only. There is intentionally
    no post-swap watchdog — the loader is gone after exec, and
    the operator does `heroku rollback v<N>` if a render bug
    manifests after the swap.
    """
    if read_status_fn is None:
        # Local import to keep the loader importable in tests
        # without pulling in heavy deps at module load.
        from status import read_status as _read_status

        _read_status_fn: Callable[..., Optional[dict]] = _read_status
    else:
        _read_status_fn = read_status_fn

    status_file = status_path if status_path is not None else repo_dir / DEFAULT_STATUS_PATH
    proc = _spawn_staged_for_probe(repo_dir, expected_sha, status_path=status_file)
    deadline = time.monotonic() + total_timeout_s
    healthy = False
    try:
        while time.monotonic() < deadline:
            try:
                rc = proc.wait(timeout=0.5)
                # Process exited early (good or bad) — read the
                # status file one last time then return.
                status = _read_status_fn(status_file, now_monotonic=deadline)
                healthy = _is_status_healthy(status)
                if rc != 0:
                    logger.warning(
                        "loader: probe child exited rc=%s; status healthy=%s",
                        rc,
                        healthy,
                    )
                return healthy
            except subprocess.TimeoutExpired:
                pass
            status = _read_status_fn(status_file, now_monotonic=time.monotonic())
            if _is_status_healthy(status):
                healthy = True
                break
    finally:
        # Always terminate the probe child. The loader is the only
        # supervisor; we can't leave a child running.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=kill_grace_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=kill_grace_s)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as exc:
            logger.warning("loader: failed to terminate probe child: %s", exc)
    return healthy


def _is_status_healthy(
    status: Optional[dict],
    *,
    max_tick_age_ms: int = 2000,
) -> bool:
    """Decide whether a status.json snapshot indicates a healthy app."""
    if not isinstance(status, dict):
        return False
    if status.get("mqtt_connected") is not True:
        return False
    if status.get("last_error") is not None:
        return False
    age_ms = status.get("last_tick_age_ms")
    if not isinstance(age_ms, int) or age_ms < 0 or age_ms > max_tick_age_ms:
        return False
    return True


# ---------------------------------------------------------------------------
# Version source of truth (Flask)
# ---------------------------------------------------------------------------


def fetch_expected_sha(
    *,
    api_url: str,
    api_key: str,
    requests_module=None,
    timeout: float = 5.0,
) -> Optional[str]:
    """GET `/api/sign/boot-config` from Flask, return the SHA or None.

    Thin wrapper around `lib_shared.boot_config.fetch_boot_config`
    so the loader shares the URL parsing + auth header logic with
    the app-side `check_for_update` handler. Returns None on any
    failure (network error, non-200 status, missing key, malformed
    JSON) — the caller falls through to "boot the existing
    current/.../main.py" without staging. A Pi that can't reach
    Flask keeps running on the last good version.

    Args:
        api_url: The Flask messages endpoint URL (e.g.
            `https://example.com/api/messages`); the loader derives
            the boot-config origin from this.
        api_key: X-API-Key header value (same key as /api/config).
        requests_module: Test override for the `requests` module.
        timeout: HTTP timeout in seconds.
    """
    config: Optional[BootConfig] = fetch_boot_config(
        api_url=api_url,
        api_key=api_key,
        timeout=timeout,
        requests_module=requests_module,
    )
    return config.expected_sha if config is not None else None


# ---------------------------------------------------------------------------
# Full upgrade flow (orchestration)
# ---------------------------------------------------------------------------


def run_upgrade_flow(
    repo_dir: Path,
    *,
    api_url: str,
    api_key: str,
    fetch_fn: Callable[..., Optional[str]] = fetch_expected_sha,
    refresh_fn: Callable[[Path], bool] = refresh_bare_repo,
    stage_fn: Callable[[Path, str], Path] = stage_version,
    probe_fn: Callable[..., bool] = probe,
    swap_fn: Callable[[Path, str], None] = atomic_swap,
    exec_fn: Callable[..., None] = exec_active,
    prune_fn: Callable[[Path], None] = prune_worktrees,
) -> None:
    """Decide whether to stage + probe + swap + exec, or fall through.

    Does not return: either we exec the new version, or we exec
    the existing `current/.../main.py` — `exec_fn` is
    `os.execvpe`-based and does not return. The function is split
    out so tests can verify the orchestration logic without
    actually starting Python subprocesses — they monkey-patch
    `swap_fn` etc. and assert on the call sequence.
    """
    local = current_sha(repo_dir)
    logger.info("loader: local SHA = %s", short_sha(local) if local else "(none)")

    expected = fetch_fn(api_url=api_url, api_key=api_key)
    if expected is None:
        logger.warning("loader: could not fetch expected SHA; using existing current")
        exec_fn(repo_dir, local or "")
        return
    logger.info("loader: expected SHA = %s", short_sha(expected))

    if local == expected:
        logger.info("loader: local SHA matches expected; no upgrade needed")
        exec_fn(repo_dir, local or "")
        return

    # Mismatch — refresh the bare repo's remote-tracking refs first.
    # The bare repo's refdb is frozen at provision time (setup-pi.sh
    # does one fetch on bootstrap), so any commit the operator pushed
    # AFTER the Pi was installed isn't reachable via `git worktree add`
    # until we fetch. Refreshing before staging is the fix.
    # On failure, fall through to existing current — same posture as a
    # failed boot-config fetch above.
    if not refresh_fn(repo_dir):
        exec_fn(repo_dir, local or "")
        return

    # Mismatch — stage the new version.
    try:
        stage_fn(repo_dir, expected)
    except StageError as e:
        logger.error("loader: staging %s failed: %s; using existing current", expected, e)
        exec_fn(repo_dir, local or "")
        return
    except Exception as e:
        logger.error("loader: staging %s raised: %s; using existing current", expected, e)
        exec_fn(repo_dir, local or "")
        return

    if not probe_fn(repo_dir, expected):
        logger.error(
            "loader: probe for %s reported unhealthy; NOT swapping; using existing current",
            expected,
        )
        exec_fn(repo_dir, local or "")
        return

    swap_fn(repo_dir, expected)
    # Cap the on-disk worktree count to `keep` after a successful
    # swap — every successful deploy is the natural cadence for
    # cleanup, and we always keep `current` (just swapped) so the
    # prune can never undo the deploy that just happened.
    prune_fn(repo_dir)
    logger.info("loader: swapped to %s; exec'ing", short_sha(expected))
    exec_fn(repo_dir, expected)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    """Loader entrypoint.

    Reads config, runs the upgrade flow, and execs the right
    version. Returns 0 only if exec did not happen (which means
    something went wrong upstream of exec_active); exec_active
    itself does not return.
    """
    repo_dir = resolve_repo_dir()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.info("loader: starting; repo_dir=%s", repo_dir)

    # Pull API credentials from settings.toml. The lib_shared.config_reader
    # imports happen at function-scope so the loader can be imported in
    # unit tests without a settings file present.
    try:
        from lib_shared.config_reader import get_config  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("loader: cannot import config_reader; using existing current")
        exec_active(repo_dir, current_sha(repo_dir) or "")
        return 0

    required = {"CONFIG_API_URL", "API_SECRET_KEY"}
    try:
        cfg = get_config(required)
    except Exception as e:
        logger.warning("loader: config_reader failed: %s", e)
        exec_active(repo_dir, current_sha(repo_dir) or "")
        return 0

    api_url = cfg.if_exists("CONFIG_API_URL") or ""
    api_key = cfg.if_exists("API_SECRET_KEY") or ""

    if not api_url or not api_key:
        logger.warning("loader: missing CONFIG_API_URL or API_SECRET_KEY; using existing current")
        exec_active(repo_dir, current_sha(repo_dir) or "")
        return 0

    run_upgrade_flow(
        repo_dir,
        api_url=api_url,
        api_key=api_key,
    )
    # run_upgrade_flow always exec's — reaching here means something
    # threw without being caught. Fall through to a safe default.
    exec_active(repo_dir, current_sha(repo_dir) or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
