"""Tests for lib_shared.phone_utils.normalize_phone (task 1.2)."""

import pytest

from lib_shared.phone_utils import normalize_phone


@pytest.mark.parametrize(
    "raw, expected",
    [
        # E.164 already-canonical → returns itself.
        ("+15551234567", "+15551234567"),
        # Bare US 10-digit → gains the +1 prefix.
        ("5551234567", "+15551234567"),
        # Parenthesized + dashed with a leading +1 → last 10 digits.
        ("+1 (555) 123-4567", "+15551234567"),
        # Dotted + spaced → last 10 digits.
        ("555.123.4567", "+15551234567"),
        # 11 digits starting with 1 → country code folded into the prefix.
        ("15551234567", "+15551234567"),
        # Empty string → passthrough (no digits).
        ("", ""),
        # Non-numeric text → passthrough unchanged.
        ("not-a-phone", "not-a-phone"),
        # Fewer than 10 digits → passthrough (not a full US number).
        ("12345", "12345"),
    ],
)
def test_normalize_phone_truth_table(raw, expected):
    assert normalize_phone(raw) == expected


def test_more_than_11_digits_passthrough():
    """A value with more than 11 digits is not a US number → passthrough."""
    assert normalize_phone("+445551234567890") == "+445551234567890"


def test_11_digits_not_starting_with_one_passthrough():
    """11 digits not starting with 1 is not a US number → passthrough."""
    assert normalize_phone("25551234567") == "25551234567"
