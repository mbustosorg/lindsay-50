"""Pi-side command handlers for the action registry (issue #51).

Flask publishes three new operator-driven command envelopes:
`force-upgrade`, `restart`, and `shutdown`. Each is routed to the
matching zero-arg handler here via `MessageManager.register_handler`
(see `lib_shared/message_manager.py:register_handler`). The handlers
themselves do NOT do routing or auth — that's the dispatcher's job.

These handlers run in the Pi's MQTT receive thread (paho's
`loop_start` daemon). The dispatcher catches and logs any exception
they raise, so a faulty handler is a deployment bug, not a render-loop
bug. We still defend each handler against the failure modes that are
specific to it:

  - `force_upgrade` — `os.execvpe` raises on a missing loader script.
    We log and return so the render loop continues.
  - `restart` / `shutdown` — `subprocess.run(..., check=False)` already
    swallows non-zero exits. Permission failures (no `NOPASSWD`) come
    back as a non-zero returncode, which we log at WARNING. The
    render loop continues.

The existing `check-for-update` handler lives in
`heart-matrix-controller/check_for_update.py` and is unchanged here;
the dispatcher wires it via the same `register_handler` call in
`main.py` (post-issue-#51). Pre-issue-#51 builds used the
`on_check_for_update` constructor kwarg — `_handle_command` honors
both paths for the transitional period.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# Env vars the loader sets via os.execvpe when handing off to us.
# Mirrors check_for_update.py:39 — same env contract.
ENV_ACTIVE_SHA = "LINDSAY50_ACTIVE_SHA"
ENV_REPO_DIR = "LINDSAY50_REPO_DIR"


def _resolve_repo_dir() -> Path:
    """Return the repo root from LINDSAY50_REPO_DIR, defaulting to the
    conventional install path.

    The env var is set by the loader (via `os.execvpe`); the default
    is a defensive fallback for tests or manual `python main.py`
    runs that didn't go through the loader. The default is `/srv/lindsay-50`
    (matches `scripts/lindsay_50.service` and the systemd unit file).
    """
    env = os.environ.get(ENV_REPO_DIR, "").strip()
    if env:
        return Path(env)
    return Path("/srv/lindsay-50")


def _exec_into_loader(repo_dir: Path) -> None:
    """Replace this process with `loader.py`'s force-upgrade entrypoint.

    Uses `os.execvpe` so systemd sees `loader.py` as the direct
    child of our PID — preserving signal handling. The env dict
    inherits our own, plus the active-SHA env var the loader reads.

    Args:
        mode: The loader mode to exec. `None` (default) runs the
            standard `main()` entrypoint (AUTO_UPDATE-gated);
            `"force-upgrade"` runs `force_upgrade_main()` which
            bypasses AUTO_UPDATE. The Pi's `command_handlers.
            force_upgrade` uses the latter.

    Raises on a missing loader script — the dispatcher catches and
    logs so the render loop continues.
    """
    loader_py = repo_dir / "heart-matrix-controller" / "loader.py"
    if not loader_py.exists():
        raise FileNotFoundError(f"loader not found at {loader_py}")
    env = dict(os.environ)
    # The mode is conveyed via LINDSAY50_FORCE_UPGRADE env var so the
    # loader's existing `os.execvpe` contract is unchanged (the
    # loader simply branches on the env var at the top of `main`).
    env["LINDSAY50_FORCE_UPGRADE"] = "1"
    logger.info(
        "command_handlers: exec'ing loader at %s (action=force-upgrade)",
        loader_py,
    )
    os.execvpe(sys.executable, [sys.executable, str(loader_py)], env)


# ---------------------------------------------------------------------------
# force-upgrade — exec into the loader at the resolved target version
# ---------------------------------------------------------------------------


def force_upgrade(*, repo_dir: Path | None = None) -> None:
    """Handle a `command=force-upgrade` envelope.

    Execs into the loader unconditionally (bypassing AUTO_UPDATE —
    that's the whole point of "force"). The loader reads the resolved
    target from `GET /api/sign/settings` on its next boot phase,
    compares to the local HEAD, and either swaps or no-ops. Any
    failure inside the loader (probe fails, stage fails) falls through
    to running `current/.../main.py` so the Pi cannot brick itself.

    Args:
        repo_dir: Override for the repo root. Defaults to
            `LINDSAY50_REPO_DIR` env var, then `/srv/lindsay-50`.
            Tests inject a tmp dir.
    """
    resolved_repo = repo_dir if repo_dir is not None else _resolve_repo_dir()
    try:
        _exec_into_loader(resolved_repo)
    except FileNotFoundError as exc:
        logger.error("command_handlers.force_upgrade: %s", exc)
        # Render loop continues — caller catches and logs.
        return
    except OSError as exc:
        # os.execvpe raises OSError on missing executable etc.
        logger.error("command_handlers.force_upgrade: exec failed: %s", exc)
        return


# ---------------------------------------------------------------------------
# restart — subprocess.run(["sudo", "reboot"], check=False)
# ---------------------------------------------------------------------------


def restart(*, timeout: float = 30.0) -> None:
    """Handle a `command=restart` envelope.

    Invokes `sudo reboot`. `check=False` so a non-zero exit
    (permission denied, missing sudo, etc.) becomes a logged WARNING
    rather than an exception that the dispatcher would catch and log
    at ERROR — the visible failure mode is "I told sudo to reboot
    and it said no", which is an operator-actionable signal, not a
    crash.

    The render loop continues regardless; on a successful reboot
    systemd restarts the loader, which boots the app fresh. On
    permission failure the Pi keeps running the current version.
    """
    logger.info("command_handlers.restart: invoking `sudo reboot`")
    try:
        result = subprocess.run(
            ["sudo", "reboot"],
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("command_handlers.restart: `sudo reboot` timed out: %s", exc)
        return
    except OSError as exc:
        # sudo binary missing, etc.
        logger.error("command_handlers.restart: subprocess failed: %s", exc)
        return

    if result.returncode != 0:
        logger.warning(
            "command_handlers.restart: `sudo reboot` returned %d (stderr=%r). "
            "Is NOPASSWD configured for the lindsay-50 service user?",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return
    logger.info("command_handlers.restart: `sudo reboot` returned 0 (Pi is going down)")


# ---------------------------------------------------------------------------
# shutdown — subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
# ---------------------------------------------------------------------------


def shutdown(*, timeout: float = 30.0) -> None:
    """Handle a `command=shutdown` envelope.

    Invokes `sudo shutdown -h now`. Same failure semantics as
    `restart` — non-zero exit becomes a logged WARNING, the render
    loop continues. On success the Pi halts; the operator must power
    it back on manually (no auto-reboot).
    """
    logger.info("command_handlers.shutdown: invoking `sudo shutdown -h now`")
    try:
        result = subprocess.run(
            ["sudo", "shutdown", "-h", "now"],
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("command_handlers.shutdown: `sudo shutdown -h now` timed out: %s", exc)
        return
    except OSError as exc:
        logger.error("command_handlers.shutdown: subprocess failed: %s", exc)
        return

    if result.returncode != 0:
        logger.warning(
            "command_handlers.shutdown: `sudo shutdown -h now` returned %d (stderr=%r). "
            "Is NOPASSWD configured for the lindsay-50 service user?",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return
    logger.info("command_handlers.shutdown: `sudo shutdown -h now` returned 0 (Pi is going down)")
