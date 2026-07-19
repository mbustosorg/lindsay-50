"""Phone-number normalization shared by the Flask app and the Raspberry Pi.

The sole public helper, :func:`normalize_phone`, collapses the many ways a US
phone number can be written (E.164, bare 10 digits, parenthesized, dotted,
dashed, spaced) into a single canonical ``+1XXXXXXXXXX`` form so the senders
lookup matches regardless of how the operator typed the number vs. how Twilio
routes it. Malformed / non-US inputs pass through unchanged — this is a
best-effort canonicalizer, not a validator.
"""

import re

_NON_DIGIT = re.compile(r"\D")


def normalize_phone(s: str) -> str:
    """Return the canonical ``+1XXXXXXXXXX`` form of a US phone number.

    Strips every non-digit character, then:

    - if exactly 10 digits remain, returns ``"+1" + digits``;
    - if exactly 11 digits remain and the first is ``"1"``, returns
      ``"+1" + digits[1:]`` (the country code is folded into the prefix);
    - otherwise (no digits, fewer than 10, more than 11, or an 11-digit
      value not starting with ``1``) returns the original input verbatim.

    The passthrough behavior means a non-phone string (``"not-a-phone"``) or
    an international number is never mangled — it simply won't match a
    normalized senders key, which is the intended outcome for a US-only sign.

    Args:
        s: The raw phone string (any format) or arbitrary text.

    Returns:
        The normalized ``+1``-prefixed 10-digit string, or ``s`` unchanged
        when it does not look like a US phone number.
    """
    digits = _NON_DIGIT.sub("", s)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return "+1" + digits[1:]
    return s
