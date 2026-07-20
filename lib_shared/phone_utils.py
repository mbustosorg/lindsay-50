"""Phone number normalization for the senders allowlist.

Single source of truth for converting arbitrary phone-string inputs
(E.164, US 10-digit, formatted with parens / dashes / dots / spaces)
into the canonical "+1XXXXXXXXXX" key used as the `SignConfig.senders`
dict key.

The rule is intentionally simple:

  - Strip everything except digits.
  - If 10 digits remain, return "+1" + digits.
  - If 11 digits remain and the first is "1", return "+1" + digits[1:].
  - Otherwise return the original input verbatim (passthrough).

The last rule covers malformed / international / empty inputs — we
don't want `normalize_phone` to raise on bad data; the caller can
decide what to do with a non-normalizable string.

Used by:
  - `SignConfig.from_dict` and `update_from_dict` to compute the
    senders dict key on wire-shape ingest.
  - `FilteredMessages._enrich_messages` to look up the incoming
    sender against the senders dict (so "+1 (555) 123-4567" and
    "555.123.4567" both resolve to the same entry whose key is
    "+15551234567").
  - The /settings POST handler to compute the key from operator input.
"""

from __future__ import annotations


def normalize_phone(s: str) -> str:
    """Return the canonical "+1XXXXXXXXXX" key for the given phone string.

    Strips non-digit characters, then:
      - if exactly 10 digits remain, returns "+1" + digits.
      - if exactly 11 digits remain and the first is "1",
        returns "+1" + digits[1:].
      - otherwise (0 digits, fewer than 10, more than 11,
        non-numeric, etc.), returns the original string verbatim.

    Args:
        s: arbitrary phone-string input. May be None-coerced to "".

    Returns:
        The canonical key, or the original input on malformed values.
    """
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return "+1" + digits[1:]
    return s
