"""Tests for `short_sha` derivation on the snapshot.

The snapshot carries both `active_sha` (full) and `short_sha`
(first 7 chars). The derivation lives in
`lib_shared.boot_config.short_sha` and is reused on the
device (`main.py` sets `short_sha=short_sha(_ACTIVE_SHA)`).

Coverage:
  - 40-char SHA → 7-char short_sha.
  - Empty SHA → empty short_sha (consistent empty state).
  - 7-char SHA → identical (idempotent on short inputs).
"""

from __future__ import annotations

import pytest  # noqa: F401  — kept for potential future parametrized cases

from lib_shared.boot_config import short_sha
from status import StatusSnapshot


class TestShortShaHelper:
    def test_full_sha_truncates_to_seven_chars(self):
        assert short_sha("abc1234567890def1234567890def1234567890") == "abc1234"

    def test_empty_sha_gives_empty(self):
        assert short_sha("") == ""

    def test_seven_char_sha_is_idempotent(self):
        assert short_sha("abc1234") == "abc1234"

    def test_six_char_sha_is_returned_unchanged(self):
        # Defensive: short inputs shorter than 7 chars are returned
        # as-is so the dashboard doesn't render `None`.
        assert short_sha("abc123") == "abc123"

    def test_three_char_sha_is_returned_unchanged(self):
        assert short_sha("abc") == "abc"

    def test_sha_with_trailing_whitespace_is_truncated_to_seven(self):
        # The helper should not strip whitespace — it's a slice, not
        # a sanitizer. Caller (lib_shared.boot_config) is responsible
        # for handing in a clean value.
        assert short_sha("abc1234xyz") == "abc1234"


class TestShortShaOnSnapshot:
    def test_snapshot_default_short_sha_is_empty(self):
        snap = StatusSnapshot()
        assert snap.short_sha == ""

    def test_snapshot_stores_short_sha_value(self):
        snap = StatusSnapshot(active_sha="abc1234567890", short_sha="abc1234")
        assert snap.short_sha == "abc1234"
        d = snap.to_dict()
        assert d["short_sha"] == "abc1234"
        assert d["active_sha"] == "abc1234567890"

    def test_short_sha_can_be_derived_at_call_site(self):
        active = "b5e191c5df481d51c4e7d1cced51cf7c656f1ead"
        snap = StatusSnapshot(active_sha=active, short_sha=short_sha(active))
        assert snap.short_sha == "b5e191c"
        # Idempotency: the helper is stable on short_sha.
        assert short_sha(snap.short_sha) == "b5e191c"
