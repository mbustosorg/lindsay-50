"""Boot config — what Flask tells the Pi to run.

The Pi queries `/api/sign/boot-config` to learn the expected SHA
(HEROKU_SLUG_COMMIT on Heroku, `git rev-parse HEAD` in local dev).
The loader uses the same module to read the expected SHA before
staging a new version; the app uses it to decide whether an
incoming `check-for-update` MQTT command justifies an exec into
the loader.

All HTTP fetch + git rev-parse + endpoint path lives here so
Flask, the loader, and the app-side check-for-update handler all
agree on the wire shape. There is no other source of truth for
the path or the payload.

Failure policy: every error path returns None (or a BootConfig
with `expected_sha=""`). Callers must treat None as "I couldn't
reach the broker" and fall through to whatever the safe default
is (loader: keep running on the existing current/, app: ignore
the check-for-update).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Heroku's dyno manager writes this JSON file at dyno startup when the
# `runtime-dyno-build-metadata` labs feature is enabled. It contains the
# release id, the slug commit SHA, and the dyno/app identifiers. Used as
# a fallback for `HEROKU_SLUG_COMMIT` on the heroku-22 stack, which does
# not auto-set that env var (and ships no `git` binary, so the git
# fallback below fails too).
HEROKU_DYNO_METADATA_PATH = Path("/etc/heroku/dyno")


# Endpoint path — single source of truth for renaming. Both Flask
# (the server) and the loader / app (the clients) import this
# constant so a typo or rename is caught at import time.
BOOT_CONFIG_PATH = "/api/sign/boot-config"

# Settings endpoint — issue #51. Parallel to BOOT_CONFIG_PATH; the
# loader calls this on boot to learn the operator-pinned target
# version (or Flask's self-SHA when no pin is set).
SIGN_SETTINGS_PATH = "/api/sign/settings"

# Conservative HTTP timeout. The broker is the same network as
# MQTT; 5s is enough for a healthy connection and short enough
# that a network blip doesn't stall the loader or the app.
DEFAULT_TIMEOUT = 5.0

# Git's default short form is 7 chars since SHA-1 collisions at that
# length are computationally infeasible inside a single repo (Git
# extends the abbreviation only if a real collision appears). The
# worktree directory naming convention, loader logs, and any future
# display surface all derive the short form from this single place.
SHORT_SHA_LEN = 7


def short_sha(full_sha: str) -> str:
    """Return the first `SHORT_SHA_LEN` chars of a SHA.

    Accepts any length >= `SHORT_SHA_LEN`; the input is not validated
    beyond that because callers hand us strings that just came out of
    `git rev-parse` or the `/api/sign/boot-config` payload, both of
    which always return a 40-char full SHA. A shorter input is
    passed through unchanged — that branch keeps existing
    short-form test fixtures working without mapping glue.
    """
    if len(full_sha) <= SHORT_SHA_LEN:
        return full_sha
    return full_sha[:SHORT_SHA_LEN]


@dataclass(frozen=True)
class BootConfig:
    """The version Flask expects the Pi to be running.

    `expected_sha` carries the full 40-char SHA — the loader needs it
    for `git worktree add` and for matching against the Pi's local
    HEAD. `short_sha` is the 7-char form used for display everywhere
    else (logs, worktree directory names like `v-<short>`, the admin
    UI). Always derived from `expected_sha[:7]`; the loader never
    receives the short form on the wire.

    Both fields are populated by `from_heroku_or_git()` so the wire
    format and the display form come from a single source of truth —
    the dataclass construction is the only place the short form is
    computed, eliminating drift between the two.

    Empty `expected_sha` ⇒ empty `short_sha` (consistent empty-state,
    not None — JSON serialization keeps the keys present).

    Future fields (sign_name, feature_flags, etc.) can be added
    without changing the wire shape — Flask returns the dataclass
    as JSON, callers ignore unknown keys.
    """

    expected_sha: str
    short_sha: str = ""

    def __post_init__(self) -> None:
        # Derive short_sha from expected_sha at construction time. We
        # don't trust callers to keep them in sync — `from_heroku_or_git`
        # always sets both, but a test or future caller might construct
        # with only one. Enforce the invariant here.
        expected = self.expected_sha or ""
        derived_short = expected[:SHORT_SHA_LEN] if expected else ""
        if self.short_sha != derived_short:
            object.__setattr__(self, "short_sha", derived_short)


def from_response(payload: Any) -> Optional[BootConfig]:
    """Parse a Flask `/api/sign/boot-config` JSON payload.

    Returns None on any malformed input (non-dict, missing key,
    wrong type, empty string). Callers treat None as "couldn't
    parse the response" — the same as a network failure.
    """
    if not isinstance(payload, dict):
        logger.warning("boot_config: response is not a dict: %r", payload)
        return None
    sha = payload.get("expected_sha")
    if not isinstance(sha, str) or not sha:
        logger.warning("boot_config: expected_sha missing or empty: %r", payload)
        return None
    return BootConfig(expected_sha=sha)


def from_sign_settings_response(payload: Any) -> Optional[str]:
    """Parse a Flask `/api/sign/settings` JSON payload — issue #51.

    Returns the `target_version` short SHA on success, None on any
    malformed input (non-dict, missing/empty `target_version`,
    wrong type). Callers treat None as "couldn't parse the response"
    — same as a network failure.

    The endpoint guarantees `target_version` is always a concrete
    short SHA server-side (Flask resolves operator-empty input to
    its own running short SHA before responding). A None or empty
    value here means either Flask is broken or the response is
    stale — the loader falls through to running `current/.../main.py`
    in either case (safe default).
    """
    if not isinstance(payload, dict):
        logger.warning("sign_settings: response is not a dict: %r", payload)
        return None
    target = payload.get("target_version")
    if not isinstance(target, str) or not target:
        logger.warning("sign_settings: target_version missing or empty: %r", payload)
        return None
    return target


def fetch_sign_settings(
    *,
    api_url: str,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT,
    requests_module: Any = None,
) -> Optional[str]:
    """GET `/api/sign/settings` with the `X-API-Key` header — issue #51.

    Returns the resolved `target_version` (always a concrete short
    SHA on a healthy server) on success, None on any failure
    (network, timeout, non-200, malformed JSON, missing/empty
    `target_version`). Callers must not raise on None — it's a soft
    failure that means "we don't know what Flask wants; fall through
    to the safe default."

    Mirrors `fetch_boot_config`'s shape so the loader can call either
    endpoint with the same import-and-call pattern.
    """
    if not api_url:
        logger.warning("sign_settings: api_url is empty; cannot fetch")
        return None
    if not api_key:
        logger.warning("sign_settings: api_key is empty; cannot fetch")
        return None

    if requests_module is None:
        try:
            import requests as requests_module  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("sign_settings: requests not available; cannot fetch")
            return None

    from urllib.parse import urlparse

    parsed = urlparse(api_url)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("sign_settings: could not derive origin from %r", api_url)
        return None
    url = f"{parsed.scheme}://{parsed.netloc}{SIGN_SETTINGS_PATH}"

    try:
        response = requests_module.get(
            url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("sign_settings: fetch failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning("sign_settings: HTTP %s from %s", response.status_code, url)
        return None

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("sign_settings: bad JSON from %s: %s", url, exc)
        return None

    return from_sign_settings_response(payload)


def fetch_boot_config(
    *,
    api_url: str,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT,
    requests_module: Any = None,
) -> Optional[BootConfig]:
    """GET `/api/sign/boot-config` with the `X-API-Key` header.

    Returns a `BootConfig` on success, None on any failure (network,
    timeout, non-200, malformed JSON, missing key). Callers must
    not raise on None — it's a soft failure that means "we don't
    know what Flask wants; fall through to the safe default."

    Args:
        api_url: The full URL of the Flask messages endpoint
            (e.g. `https://example.com/api/messages`). The boot-config
            path is appended via `BOOT_CONFIG_PATH`; the host is
            derived by stripping the path from this URL.
        api_key: The `X-API-Key` header value (the same key the
            device uses for /api/config and /api/messages).
        timeout: HTTP timeout in seconds.
        requests_module: Override for the `requests` module —
            tests inject a mock. Defaults to `import requests`
            at call time so the import error is lazy (the loader
            doesn't pay the cost of an import it won't use).
    """
    if not api_url:
        logger.warning("boot_config: api_url is empty; cannot fetch")
        return None
    if not api_key:
        logger.warning("boot_config: api_key is empty; cannot fetch")
        return None

    if requests_module is None:
        try:
            import requests as requests_module  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("boot_config: requests not available; cannot fetch")
            return None

    # Derive origin from the messages URL. Flask serves both
    # endpoints on the same host; stripping the path keeps the
    # loader agnostic to which API URL it was given.
    from urllib.parse import urlparse

    parsed = urlparse(api_url)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("boot_config: could not derive origin from %r", api_url)
        return None
    url = f"{parsed.scheme}://{parsed.netloc}{BOOT_CONFIG_PATH}"

    try:
        response = requests_module.get(
            url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("boot_config: fetch failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning("boot_config: HTTP %s from %s", response.status_code, url)
        return None

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("boot_config: bad JSON from %s: %s", url, exc)
        return None

    return from_response(payload)


def current_sha(repo_dir: Path) -> Optional[str]:
    """Resolve the SHA the `current/` symlink points at.

    Reads `git -C <current_worktree> rev-parse HEAD` so the value
    reflects what's actually live (not what's in the bare repo's
    detached HEAD). Returns None if the symlink is missing or the
    git invocation fails — the caller treats both as "I don't
    know what's running; fall through to the safe default."
    """
    current = repo_dir / "current"
    if not current.exists():
        logger.warning("boot_config: current symlink missing at %s", current)
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(current), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0,
        )
        sha = result.stdout.strip()
        return sha or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("boot_config: git rev-parse failed: %s", exc)
        return None


def from_heroku_or_git(repo_dir: Path) -> BootConfig:
    """Server-side: read HEROKU_SLUG_COMMIT, dyno metadata, or fall back to git rev-parse.

    Used by Flask to compute the `expected_sha` for `/api/sign/boot-config`.
    On Heroku, `HEROKU_SLUG_COMMIT` is set automatically on older stacks
    for every `git push heroku main`, including `heroku rollback v<N>` —
    that is how operator-initiated rollback propagates to the Pi.

    On the heroku-22 stack, `HEROKU_SLUG_COMMIT` is NOT auto-set and the
    slug has no `git` binary, so neither the env var nor the git fallback
    succeed. We then read `/etc/heroku/dyno` — a JSON file the dyno manager
    writes at startup when the `runtime-dyno-build-metadata` labs feature
    is enabled — and parse `release.commit` from it.

    In local dev there is no Heroku env var and no `/etc/heroku/dyno`,
    so we fall back to the local HEAD. If git is missing (e.g. a slim
    Docker image without git), we return an empty SHA — the endpoint
    will respond 500 with `could not resolve expected SHA` and the
    loader will treat it as "no upgrade".
    """
    slug = os.environ.get("HEROKU_SLUG_COMMIT", "").strip()
    if slug:
        return BootConfig(expected_sha=slug)
    dyn_commit = _from_dyno_metadata()
    if dyn_commit:
        return BootConfig(expected_sha=dyn_commit)
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0,
        )
        sha = result.stdout.strip()
        return BootConfig(expected_sha=sha or "")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            "boot_config: HEROKU_SLUG_COMMIT unset, /etc/heroku/dyno missing, and git rev-parse failed: %s",
            exc,
        )
        return BootConfig(expected_sha="")


def _from_dyno_metadata(path: Path = HEROKU_DYNO_METADATA_PATH) -> Optional[str]:
    """Read the slug commit from the runtime-dyno-build-metadata JSON file.

    Returns `None` on any failure (file missing, unreadable, malformed
    JSON, missing `release.commit`). The caller falls through to the
    git rev-parse fallback. Tests inject a different `path` to exercise
    both the success and the failure paths without writing to
    `/etc/heroku/dyno`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        logger.debug("boot_config: dyno metadata file not readable at %s: %s", path, exc)
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("boot_config: dyno metadata JSON parse failed at %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "boot_config: dyno metadata is not a JSON object at %s: %r",
            path,
            type(payload).__name__,
        )
        return None
    release = payload.get("release")
    if not isinstance(release, dict):
        logger.warning("boot_config: dyno metadata missing release object at %s: %r", path, payload)
        return None
    commit = release.get("commit")
    if not isinstance(commit, str) or not commit:
        logger.warning(
            "boot_config: dyno metadata release.commit missing or empty at %s: %r",
            path,
            release,
        )
        return None
    return commit
