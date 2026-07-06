"""Tests for lib_shared.short_sha."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on the path so lib_shared is importable
_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from lib_shared.short_sha import SHORT_SHA_LEN, short_sha  # noqa: E402


def test_short_sha_truncates_to_first_seven():
    assert short_sha("b5e191c5df481d51c4e7d1cced51cf7c656f1ead") == "b5e191c"


def test_short_sha_passes_through_short_input():
    assert short_sha("abc1234") == "abc1234"


def test_short_sha_handles_exact_length():
    """A string exactly equal to SHORT_SHA_LEN should be passed through,
    not truncated to zero or otherwise mangled."""
    assert SHORT_SHA_LEN == 7
    assert short_sha("abc1234") == "abc1234"
    assert len(short_sha("abc1234")) == 7


def test_short_sha_handles_empty_string():
    """Empty input is a degenerate case (a SHA-less ref) — passed through
    rather than exploded, so callers can log it without crashing."""
    assert short_sha("") == ""
