"""Regression tests for the PyScript shared-globals shadowing bug.

When PyScript 2024.9.x evaluates two `<py-script>` blocks, both
modules share the same globals dict (the per-module isolation
that `importlib` gives us on the host does not apply inside
Pyodide). If `app_main.py` binds the bare name `_coordinator` to
an `EffectsCoordinator` instance and `preview_main.py` ALSO defines
`def _coordinator():` at module scope, whichever name wins last is
what every reference resolves to. When `preview_main.py` ran first
during bootstrap, its function definition was clobbered by
`app_main.py`'s `_coordinator = EffectsCoordinator(...)` later in
the asyncio event loop, and `_coordinator()` started raising
`TypeError: 'EffectsCoordinator' object is not callable`.

These tests pin the invariant: `preview_main.py` must not bind the
name `_coordinator` at module scope. The looker helper is
`_get_app_coordinator()` (or any other name that doesn't collide).
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# `preview_main.py` uses pyodide_js.loadPackage (top-level await)
# which only works inside Pyodide. The CPython host can read the
# source file directly without executing it.


def _read_preview_main_source() -> str:
    """Return the source of `preview_main.py` without executing it."""
    src = _PROJECT_ROOT / "heart-message-manager" / "preview_main.py"
    return src.read_text(encoding="utf-8")


def test_preview_main_does_not_define_coordinator_at_module_level():
    """`def _coordinator():` at module scope collides with `app_main.py`.

    Pin: preview_main.py must not have a module-level `def _coordinator(`
    line. Renamed looker functions like `_get_app_coordinator()` are
    fine because they don't share the bare name.
    """
    src = _read_preview_main_source()
    # A bare `_coordinator(` reference at module level — outside a
    # function body — would create a module-level binding.
    # We approximate "module level" by scanning lines that don't start
    # with whitespace (i.e., not nested inside `def` / `class`).
    for n, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent > 0:
            continue
        # Module-level statement — look for the binding forms.
        assert not stripped.startswith("def _coordinator("), (
            f"preview_main.py:{n} defines module-level `def _coordinator(` "
            f"which collides with app_main.py's `_coordinator = EffectsCoordinator(...)`. "
            f"Rename to `_get_app_coordinator()` (or similar)."
        )
        assert not stripped.startswith("_coordinator ="), (
            f"preview_main.py:{n} binds `_coordinator` at module level; " f"collides with app_main.py."
        )


def test_preview_main_exposes_get_app_coordinator():
    """The renamed looker must exist so callers can use it."""
    src = _read_preview_main_source()
    assert "def _get_app_coordinator" in src, (
        "preview_main.py must define `_get_app_coordinator()` " "as the renamed looker for `window._coordinator`."
    )


def test_preview_main_does_not_call_bare_coordinator():
    """No `_coordinator()` calls — those would resolve to the bound
    instance from `app_main.py` after shared-globals shadowing.

    `_wait_for_coordinator()` is a different function (waits for the
    property to appear); we strip those references before scanning.
    We also skip lines that are inside a triple-quoted docstring
    (which is where the rename's rationale lives).
    """
    import re

    src = _read_preview_main_source()
    in_docstring = False
    for n, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        if '"""' in stripped:
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        # Strip comments to avoid false positives on docstrings.
        code = line.split("#", 1)[0]
        # Strip `_wait_for_coordinator` (a different function — the
        # polling helper).
        cleaned = code.replace("_wait_for_coordinator", "")
        # Use a regex with negative look-behind so `_get_app_coordinator()`
        # doesn't false-positive on the substring `_coordinator()`.
        match = re.search(r"(?<![a-zA-Z_])_coordinator\(", cleaned)
        assert not match, (
            f"preview_main.py:{n} calls `_coordinator()`; " f"use `_get_app_coordinator()` to avoid the shadowing bug."
        )
