"""App-side `check-for-update` command handler.

Flask publishes a one-shot `MessageEnvelope("command", {"action":
"check-for-update"})` envelope at startup. The Pi's existing
`MessageManager` subscriber receives it and routes it here. This
module decides whether to exec into the loader — and does so if
the SHA Flask reports differs from the SHA we're currently running.

The active SHA is passed by the loader via the `LINDSAY50_ACTIVE_SHA`
env var (`os.execvpe` in `loader.py`), so this handler never has to
run `git rev-parse HEAD` on the hot path. That's deliberate: the
hot path is "did the version change?", and we already know the
answer.

Why this lives here and not in the loader:
  - The app already has MQTT — the loader doesn't, and adding it
    just for one command brings a pile of broker reconnect logic.
  - The app is the canonical subscriber for "live signals from
    Flask"; the loader is purely a "system upgrade tool" that
    runs at boot or after the app exec's into it.
  - Heroku rollback propagates by re-publishing the hint; the
    app handles that case identically to a fresh deploy.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from lib_shared.boot_config import fetch_boot_config

logger = logging.getLogger(__name__)


# Env vars the loader sets via os.execvpe when handing off to us.
ENV_ACTIVE_SHA = "LINDSAY50_ACTIVE_SHA"
ENV_REPO_DIR = "LINDSAY50_REPO_DIR"

# Where the loader entrypoint lives, relative to the repo root.
# Resolved through the `current/` symlink because the loader is the
# thing that manages which SHA is live.
LOADER_PATH = Path("current") / "heart-matrix-controller" / "loader.py"


# AUTO_UPDATE quick-disable — same flag the loader reads. Local-dev runs
# want the MQTT `check-for-update` envelope to be a no-op so a Flask
# deploy doesn't clobber an in-progress local checkout. Production
# installs set `AUTO_UPDATE = true` in the canonical settings.toml (or
# via the AUTO_UPDATE env var) to preserve pre-existing behavior.
_TRUTHY = {"1", "true", "yes", "on"}


def _auto_update_enabled() -> bool:
    """Return True iff AUTO_UPDATE is set to a truthy value.

    Reads a fresh `ConfigReader` (no required_keys) instead of the
    `get_config()` singleton — this function is reachable from tests
    that import `check_for_update` without first running `main.py`,
    and we don't want a missing singleton to flip behavior.
    """
    from lib_shared.config_reader import ConfigReader

    raw = (ConfigReader().if_exists("AUTO_UPDATE") or "").strip().lower()
    return raw in _TRUTHY


def _resolve_repo_dir() -> Path:
    """Return the repo root from LINDSAY50_REPO_DIR, defaulting to the
    conventional `/srv/lindsay-50` path.

    The env var is always set by the loader (via `os.execvpe`); the
    default is a defensive fallback for tests or manual `python
    main.py` runs that didn't go through the loader.
    """
    env = os.environ.get(ENV_REPO_DIR, "").strip()
    if env:
        return Path(env)
    return Path("/srv/lindsay-50")


def _resolve_active_sha() -> Optional[str]:
    """Read `LINDSAY50_ACTIVE_SHA` from the env.

    Returns None if the var is missing or empty — the caller treats
    that as "we don't know what we're running", which means we should
    NOT exec into the loader on a hint (we'd risk swapping to a
    version that doesn't match what the loader thinks is active).
    """
    return os.environ.get(ENV_ACTIVE_SHA, "").strip() or None


def _exec_into_loader(repo_dir: Path, expected_sha: str) -> None:
    """Replace this process with `loader.py`.

    Uses `os.execvpe` so systemd sees `loader.py` as the direct
    child of our PID — preserving signal handling. The env dict
    inherits our own (so the env the loader set on us carries over),
    and we freshen the active SHA to the expected one.
    """
    loader_py = repo_dir / LOADER_PATH
    env = dict(os.environ)
    env[ENV_ACTIVE_SHA] = expected_sha
    logger.info(
        "app: exec'ing loader at %s with LINDSAY50_ACTIVE_SHA=%s",
        loader_py,
        expected_sha,
    )
    os.execvpe(sys.executable, [sys.executable, str(loader_py)], env)


def check_for_update(
    *,
    api_url: str,
    api_key: str,
    repo_dir: Optional[Path] = None,
) -> None:
    """Handle a `command=check-for-update` MQTT envelope.

    Fetches `/api/sign/boot-config` and compares the expected SHA
    to the SHA we were started with. If they differ, execs into
    the loader. If they match (or we can't tell), no-op.

    Args:
        api_url: Flask base URL (the messages endpoint URL — the
            boot-config path is appended by `fetch_boot_config`).
        api_key: X-API-Key header value.
        repo_dir: Override for the repo root. Defaults to
            `LINDSAY50_REPO_DIR` env var, then the conventional
            `/srv/lindsay-50`. Tests inject a tmp dir.

    Returns None; either we no-op or we `os.execvpe` (which never
    returns).
    """
    if not _auto_update_enabled():
        logger.info(
            "app: check-for-update ignored: AUTO_UPDATE is not enabled",
        )
        return

    resolved_repo = repo_dir if repo_dir is not None else _resolve_repo_dir()
    active = _resolve_active_sha()
    if active is None:
        logger.warning(
            "app: check-for-update ignored: %s not set; cannot compare",
            ENV_ACTIVE_SHA,
        )
        return

    config = fetch_boot_config(api_url=api_url, api_key=api_key)
    if config is None:
        logger.warning(
            "app: check-for-update ignored: could not fetch boot config",
        )
        return

    if config.expected_sha == active:
        logger.info(
            "app: check-for-update no-op (active=%s matches expected)",
            active[:7],
        )
        return

    logger.info(
        "app: upgrade needed: active=%s expected=%s; exec'ing loader",
        active[:7],
        config.expected_sha[:7],
    )
    _exec_into_loader(resolved_repo, config.expected_sha)
