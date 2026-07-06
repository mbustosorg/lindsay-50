"""SHA shortening — full 40-char → 7-char short form.

Git's default short form is 7 chars since SHA-1 collisions at that
length are computationally infeasible inside a single repo (Git
extends the abbreviation only if a real collision appears).

This module exists so the worktree directory naming convention,
loader logs, and any future display surface all derive the short
form from the same place. The constant `SHORT_SHA_LEN` is the
single source of truth — change it in one spot when (not if)
someone wants `git rev-parse --short=10`.
"""

from __future__ import annotations

SHORT_SHA_LEN = 7


def short_sha(full_sha: str) -> str:
    """Return the first `SHORT_SHA_LEN` chars of a SHA.

    Accepts any length ≥ `SHORT_SHA_LEN`; the input is not validated
    beyond that because callers hand us strings that just came out of
    `git rev-parse` or the `/api/sign/boot-config` payload, both of
    which always return a 40-char full SHA. A shorter input is
    passed through unchanged — that branch keeps existing
    short-form test fixtures working without mapping glue.
    """
    if len(full_sha) <= SHORT_SHA_LEN:
        return full_sha
    return full_sha[:SHORT_SHA_LEN]
