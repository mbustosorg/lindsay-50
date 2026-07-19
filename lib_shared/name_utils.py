"""Helpers for parsing and formatting sender display names.

Two public functions:

  - `parse_name(name)` — split a stored operator-supplied name into
    `(first, last)` components. Tolerant of whitespace runs and
    multi-word last names.

  - `format_display_name(name, fmt, all_first_names=None)` — apply a
    chosen display format (one of `EffectsSettings.VALID_NAME_DISPLAY_FORMATS`)
    to a stored name. The optional `all_first_names` list carries the
    full sender set's first names so the `first_initial_if_duplicates`
    format can disambiguate two senders named "Alice" by appending the
    last initial.

Used by `FilteredMessages._enrich_messages` to compute `MessageView.sender_name`
from `cfg.senders[<normalized>]["name"]` and
`cfg.effects_settings.name_display_format`. The format is a pure function of
the stored name; the stored name is NEVER mutated (the operator can flip
formats without retyping names).
"""

from __future__ import annotations

from typing import Optional, Tuple


def parse_name(name: str) -> Tuple[str, str]:
    """Split a name into (first, last) components.

    Splits on whitespace runs (one or more whitespace characters).
    Strips leading/trailing whitespace from the input first. Handles
    multi-word last names by joining the rest with single spaces.

    Args:
        name: Operator-supplied name string (may be empty or whitespace).

    Returns:
        Tuple `(first, last)`. Both empty if the input has zero tokens.
        If the input has exactly one token, returns `(token, "")` — the
        token is the first name with no last name.
        If the input has two or more tokens, returns
        `(tokens[0], " ".join(tokens[1:]))`.

    Examples:
        >>> parse_name("Alice Smith")
        ('Alice', 'Smith')
        >>> parse_name("Madonna")
        ('Madonna', '')
        >>> parse_name("Alice Smith Jones")
        ('Alice', 'Smith Jones')
        >>> parse_name("  Alice   Smith  ")
        ('Alice', 'Smith')
        >>> parse_name("")
        ('', '')
    """
    tokens = name.strip().split()
    if not tokens:
        return ("", "")
    if len(tokens) == 1:
        return (tokens[0], "")
    return (tokens[0], " ".join(tokens[1:]))


def format_display_name(
    name: str,
    fmt: str,
    all_first_names: Optional[list[str]] = None,
) -> str:
    """Apply the chosen display format to a stored name.

    The function is pure — the input `name` is never mutated.

    Args:
        name: Stored operator-supplied name (the operator-typed string,
            typically "First Last", but possibly a single-word name, a
            nickname, or a multi-word last name like "Alice Smith Jones").
        fmt: One of `EffectsSettings.VALID_NAME_DISPLAY_FORMATS`:
            - "full": the full name with normalized whitespace.
            - "first_initial": first name + last initial + period
              (e.g. "Alice S."); single-word names return just the first.
            - "first": first name only.
            - "first_initial_if_duplicates": first name only when the
              first name is unique across the supplied set; first + last
              initial when it appears in two or more entries (single-word
              duplicates stay as just the first name — no last to take
              the initial from).
        all_first_names: Optional list of `first` parts across the
            sender set; used only for `first_initial_if_duplicates`.
            `None` (or omitted) is treated as `[first]` — the duplicate
            check sees only the current entry and is therefore a no-op.

    Returns:
        The formatted display string. Empty string when the input
        name has no tokens (no name to format).

    Examples:
        >>> format_display_name("Alice Smith", "full")
        'Alice Smith'
        >>> format_display_name("Alice Smith", "first_initial")
        'Alice S.'
        >>> format_display_name("Alice Smith", "first")
        'Alice'
        >>> format_display_name("Alice Smith", "first_initial_if_duplicates",
        ...                      all_first_names=["Alice"])
        'Alice'
        >>> format_display_name("Alice Smith", "first_initial_if_duplicates",
        ...                      all_first_names=["Alice", "Alice"])
        'Alice S.'
        >>> format_display_name("Madonna", "first_initial")
        'Madonna'
        >>> format_display_name("Alice Smith Jones", "first_initial")
        'Alice S.'
    """
    first, last = parse_name(name)
    if not first:
        return ""
    if fmt == "full":
        return f"{first} {last}".strip()
    if fmt == "first_initial":
        if last:
            return f"{first} {last[0]}."
        return first
    if fmt == "first":
        return first
    if fmt == "first_initial_if_duplicates":
        # Treat None as [first] so the duplicate check is a no-op.
        candidates = all_first_names if all_first_names is not None else [first]
        if candidates.count(first) >= 2 and last:
            return f"{first} {last[0]}."
        return first
    # Unknown format — fall back to first name. The caller (validate)
    # rejects unknown values upstream, so this branch is defensive only.
    return first
