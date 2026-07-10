"""Unit tests for the MIME → `.ext` lookup tables.

Both `heart-message-manager/s3.py:_MIME_EXT_TABLE` (the S3 key
synthesis) and `lib_shared/patterns/media_cycler.py:_ext_for_mime`
(the cycler's local-path extension fallback) must agree on the
extension for Twilio MMS video formats, otherwise the S3 key
gets a `.bin` fallback the browser preview's `<video>` element
can't infer a codec from.

The Twilio-specific case is `video/3gpp` — iPhone/Android MMS
videos arrive as `video/3gpp` (H.263 in a 3GP container). Without
an entry, `_media_key` falls through to `.bin` and the browser
preview logs a misleading "video failed to load" message.
OpenCV's `VideoCapture` sniffs by content and would have opened
the `.bin` anyway, but the browser reads by URL extension.

These tests pin:
- `video/3gpp` → `.3gp`
- `video/3gpp2` → `.3g2`
- The host + s3 tables agree on every Twilio-common MIME

The `_MIME_EXT_TABLE` dict is read by AST-scanning the source
file directly — we avoid importing `s3.py` because that pulls in
`boto3`, `requests`, and a config singleton that the host test
runner doesn't satisfy. The cycler-side table is exercised
through the actual function (no heavy deps there).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _s3_mime_table():
    """Read `_MIME_EXT_TABLE` from `s3.py` via AST scan.

    Avoids importing the module (which pulls `boto3`/`requests` and
    triggers `get_config()` — both unwanted in the host test env).
    The dict is a module-level constant of literal string keys and
    `(".ext",)` tuple values; an AST walk handles both forms.

    If the table becomes complex enough to warrant a real import,
    swap this for `importlib.import_module("heart_message_manager.s3")._MIME_EXT_TABLE`
    after registering the synthetic package and seeding
    `config_reader._cfg`.
    """
    src_path = _PROJECT_ROOT / "heart-message-manager" / "s3.py"
    tree = ast.parse(src_path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "_MIME_EXT_TABLE" for t in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        out: dict[str, tuple[str, ...]] = {}
        for k, v in zip(node.value.keys, node.value.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                continue
            # Value is `(".ext",)` — a single-element tuple literal.
            if not isinstance(v, ast.Tuple) or not v.elts:
                continue
            first = v.elts[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            out[k.value] = (first.value,)
        return out
    raise AssertionError(
        "could not find `_MIME_EXT_TABLE` constant in s3.py — has the literal form changed?"
    )


def test_s3_mime_table_3gpp():
    """`video/3gpp` resolves to `.3gp` (not the `.bin` fallback)."""
    table = _s3_mime_table()
    entry = table.get("video/3gpp")
    assert entry is not None, "video/3gpp missing from _MIME_EXT_TABLE"
    assert entry[0] == ".3gp"


def test_s3_mime_table_3gpp2():
    """`video/3gpp2` resolves to `.3g2`."""
    table = _s3_mime_table()
    entry = table.get("video/3gpp2")
    assert entry is not None, "video/3gpp2 missing from _MIME_EXT_TABLE"
    assert entry[0] == ".3g2"


def test_cycler_ext_for_mime_3gpp():
    """`_ext_for_mime("video/3gpp")` returns `.3gp`."""
    from lib_shared.patterns.media_cycler import _ext_for_mime

    assert _ext_for_mime("video/3gpp") == ".3gp"


def test_cycler_ext_for_mime_3gpp2():
    """`_ext_for_mime("video/3gpp2")` returns `.3g2`."""
    from lib_shared.patterns.media_cycler import _ext_for_mime

    assert _ext_for_mime("video/3gpp2") == ".3g2"


def test_cycler_and_s3_tables_agree_on_common_video_mimes():
    """The host cycler and the server S3 key pipeline must agree on
    extensions for every common Twilio video MIME — otherwise the
    S3 upload writes `<key>.3gp` but the cycler's local cache
    fallback would re-fetch `<key>.bin`, double-fetching."""
    from lib_shared.patterns.media_cycler import _ext_for_mime

    s3 = _s3_mime_table()
    for mime, expected_ext in [
        ("video/mp4", ".mp4"),
        ("video/quicktime", ".mov"),
        ("video/webm", ".webm"),
        ("video/3gpp", ".3gp"),
        ("video/3gpp2", ".3g2"),
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
    ]:
        s3_ext = s3[mime][0]
        cycler_ext = _ext_for_mime(mime)
        assert s3_ext == cycler_ext, (
            f"extension mismatch for {mime!r}: "
            f"s3={s3_ext!r} cycler={cycler_ext!r} expected={expected_ext!r}"
        )
        assert s3_ext == expected_ext


def test_cycler_ext_unknown_mime_returns_empty():
    """Unknown MIME types return empty (the caller falls back to
    `os.path.splitext(key)[1]` or, failing that, to no extension
    at all — `cv2.VideoCapture` sniffs by content)."""
    from lib_shared.patterns.media_cycler import _ext_for_mime

    assert _ext_for_mime("video/x-matroska") == ".mkv"  # documented case
    assert _ext_for_mime("application/octet-stream") == ""  # unknown
    assert _ext_for_mime("") == ""


# --- Parameter stripping (fix for user-reported .bin symptom) -------------


def test_cycler_ext_3gpp_strips_parameters():
    """`video/3gpp; codecs="h263"` resolves the same as plain
    `video/3gpp`. Twilio appends charset / codec hints that aren't
    part of the canonical MIME, and without the strip the table
    exact-match miss falls through to empty (the cycler side) or
    `.bin` (the s3 side). Symptom: a perfectly good Twilio video
    lands in S3 as `.bin` and the browser preview's `<video>`
    element can't infer a codec from the URL extension.
    """
    from lib_shared.patterns.media_cycler import _ext_for_mime

    assert _ext_for_mime("video/3gpp") == ".3gp"
    assert _ext_for_mime('video/3gpp; codecs="h263"') == ".3gp"
    assert _ext_for_mime("video/3gpp; charset=binary") == ".3gp"
    assert _ext_for_mime("VIDEO/3GPP;  charset=binary") == ".3gp"


def test_s3_safe_ext_strips_parameters():
    """The s3-side `_safe_ext` (server-side sibling of the cycler
    fix) also strips MIME parameters. Pinned via AST scan — same
    reason as the table-level tests (avoid importing s3.py which
    pulls boto3 + config).

    Layered on d0f3a9a, which added `video/3gpp → .3gp` to the
    `_MIME_EXT_TABLE`. d0f3a9a's entry is exact-match — without the
    parameter strip, Twilio's `video/3gpp; codecs="h263"` form
    misses the table and falls through to `.bin` (the user's
    reported symptom). This test pins the strip at the call site
    so a future regression that drops the split without a
    replacement is caught immediately.
    """
    import re as _re

    src_path = _PROJECT_ROOT / "heart-message-manager" / "s3.py"
    src = src_path.read_text(encoding="utf-8")

    # Slice from `def _safe_ext(` to the next top-level `def `.
    # Using `\n\n` would match inside the docstring; using `\ndef `
    # reliably hits the boundary between this function and whatever
    # follows it (regardless of whether there's a blank line).
    safe_ext_start = src.index("def _safe_ext(")
    m = _re.search(r"^def ", src[safe_ext_start + 1:], _re.MULTILINE)
    assert m is not None, "no following top-level `def ` found — file shape changed?"
    safe_ext_end = safe_ext_start + 1 + m.start()
    body = src[safe_ext_start:safe_ext_end]
    assert 'split(";", 1)' in body, (
        "_safe_ext should strip MIME parameters (e.g. `; charset=binary`) "
        "before the table lookup — Twilio sends parameterized Content-Types "
        "that would otherwise miss the exact-match table and fall through to "
        "`.bin`. Symptom: live `media-...bin` URLs for video/3gpp MMS."
    )


def test_cycler_ext_unknown_with_parameters_returns_empty():
    """Parameter stripping doesn't accidentally match an unknown
    type — `application/octet-stream; foo=bar` is still unknown."""
    from lib_shared.patterns.media_cycler import _ext_for_mime

    assert _ext_for_mime("application/octet-stream; foo=bar") == ""
    assert _ext_for_mime("image/svg+xml; charset=utf-8") == ""
