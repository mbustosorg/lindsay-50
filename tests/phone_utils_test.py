"""Tests for lib_shared.phone_utils.normalize_phone.

Truth-table coverage per the spec:
  - E.164 passes through.
  - 10-digit US local gets a +1 prefix.
  - Parens / dashes / dots / spaces are stripped.
  - 11-digit starting with 1 returns the last 10 with +1.
  - Empty / non-numeric / too-short pass through unchanged.
"""

from lib_shared.phone_utils import normalize_phone

# --- happy path: well-formed inputs --


def test_e164_passes_through():
    """E.164 (`+15551234567`) normalizes to itself."""
    assert normalize_phone("+15551234567") == "+15551234567"


def test_ten_digit_gets_plus_one_prefix():
    """A US 10-digit local number gets a `+1` prefix."""
    assert normalize_phone("5551234567") == "+15551234567"


def test_eleven_digit_starting_with_one_strips_leading_one():
    """An 11-digit number starting with `1` strips the leading `1`
    and returns the last 10 with `+1`."""
    assert normalize_phone("15551234567") == "+15551234567"


# --- formatting tolerance --


def test_parens_and_dashes_are_stripped():
    """Parens, dashes, and the leading `+1` are all stripped."""
    assert normalize_phone("+1 (555) 123-4567") == "+15551234567"


def test_dots_and_spaces_are_stripped():
    """Dots and spaces are stripped."""
    assert normalize_phone("555.123.4567") == "+15551234567"


def test_mixed_formatting():
    """Mixed formatting (parens, dashes, dots, spaces) all collapse."""
    assert normalize_phone("+1 (555).123 - 4567") == "+15551234567"


def test_no_plus_prefix_strips_correctly():
    """A number without `+` but with the leading 1 normalizes correctly."""
    assert normalize_phone("1-555-123-4567") == "+15551234567"


# --- malformed input passthrough --


def test_empty_string_passes_through():
    """An empty string returns empty (passthrough)."""
    assert normalize_phone("") == ""


def test_non_numeric_passes_through():
    """A non-numeric string passes through unchanged."""
    assert normalize_phone("not-a-phone") == "not-a-phone"


def test_shorter_than_ten_passes_through():
    """Fewer than 10 digits passes through unchanged."""
    assert normalize_phone("12345") == "12345"


def test_too_many_digits_passes_through():
    """More than 11 digits passes through unchanged."""
    assert normalize_phone("155512345678") == "155512345678"


def test_eleven_digit_not_starting_with_one_passes_through():
    """11 digits where the leading digit is NOT `1` passes through."""
    assert normalize_phone("25551234567") == "25551234567"


def test_thirteen_digit_passes_through():
    """A clearly-international number passes through unchanged."""
    assert normalize_phone("+447911123456") == "+447911123456"


# --- edge cases --


def test_all_zeros_passes_through():
    """All zeros is fewer than 10 digits → passthrough."""
    assert normalize_phone("0000") == "0000"


def test_just_one_passes_through():
    """A bare `1` is fewer than 10 digits → passthrough."""
    assert normalize_phone("1") == "1"


def test_ten_zeros_normalizes():
    """Ten zeros normalize to `+10000000000`."""
    assert normalize_phone("0000000000") == "+10000000000"


def test_ten_digit_starting_with_zero_normalizes():
    """A ten-digit number with a leading 0 (not 1) normalizes to +1-prefix form.

    The spec rule is purely length-based: 10 digits → "+1" + digits,
    regardless of the leading digit. Real US numbers won't have a
    leading 0 in the local portion, but the function should still
    normalize a 10-digit input.
    """
    assert normalize_phone("0555123456") == "+10555123456"
