"""Pin: IndexedDB is removed from the dashboard runtime (issue #48, §3.5).

The browser-side `IndexedDBEventLog` class is gone — replaced by a
deque-backed `EventLog` whose default cap is 100 entries. There's
no JS-side IndexedDB shim anymore (the `preview.js` polling path
talks to Python directly, no DOM storage involved).

These tests pin the absence of IndexedDB anywhere in the dashboard
runtime path. If anyone re-introduces it, this test fails before
the change ships.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# --- Module-level imports -------------------------------------------------


def test_no_indexeddb_eventlog_class_in_event_log_module():
    """The browser-side `IndexedDBEventLog` is gone. The deque
    `EventLog` is the only event-log class exported from
    `heart-message-manager/event_log.py`."""
    from heart_message_manager import event_log as mod

    assert not hasattr(mod, "IndexedDBEventLog"), (
        "IndexedDBEventLog must be removed — the browser event log is "
        "the deque-backed EventLog. Re-introducing IndexedDBEventLog "
        "contradicts §3.5 of the standalone-preview-dashboard spec."
    )


def test_no_indexeddb_imports_in_dashboard_bootstrap():
    """The dashboard runtime bootstrap must not import IndexedDB."""
    src_path = _PROJECT_ROOT / "heart-message-manager" / "dashboard_bootstrap.py"
    src = src_path.read_text(encoding="utf-8")
    # Comment-only mentions of "IndexedDB" are OK (the docstring at
    # the top references the prior design as historical context).
    # Look for actual `import`/`from` statements touching indexeddb.
    matches = re.findall(
        r"^\s*(?:from\s+[\w.]*indexeddb[\w.]*|import\s+[\w.]*indexeddb[\w.]*|"
        r"import\s+js\.indexedDB|js\.indexedDB\.)",
        src,
        re.IGNORECASE | re.MULTILINE,
    )
    assert matches == [], (
        f"dashboard_bootstrap.py imports IndexedDB: {matches}. "
        "The browser event log is deque-backed; no IndexedDB is "
        "involved in the dashboard runtime path."
    )


def test_no_indexeddb_references_in_dashboard_controller():
    """The controller is browser-agnostic but should not own any
    IndexedDB-specific state."""
    src_path = _PROJECT_ROOT / "heart-message-manager" / "dashboard_controller.py"
    src = src_path.read_text(encoding="utf-8")
    # The phrase "IndexedDB" must not appear at all (no historical
    # context for the controller — the deque EventLog is the only
    # contract it knows about).
    assert "IndexedDB" not in src, (
        "dashboard_controller.py must not reference IndexedDB; "
        "the controller's contract is browser-agnostic and the "
        "deque EventLog is the only EventLog surface."
    )


def test_no_indexeddb_in_app_main():
    """`app_main.py` is the PyScript entry point — no IndexedDB
    imports allowed there either."""
    src_path = _PROJECT_ROOT / "heart-message-manager" / "app_main.py"
    src = src_path.read_text(encoding="utf-8")
    # `js.indexedDB` is a browser-API attribute; if anyone wires
    # `js.indexedDB.open(...)` or similar into `app_main.py`, fail.
    assert "js.indexedDB" not in src, (
        "app_main.py must not reference js.indexedDB; the browser "
        "event log is deque-backed and the runtime is browser-only "
        "in-memory."
    )


def test_no_indexeddb_in_static_preview_js():
    """The preview-side JS is a polling tick + DOM renderer. It
    should never touch IndexedDB."""
    src_path = _PROJECT_ROOT / "heart-message-manager" / "static" / "preview" / "preview.js"
    if not src_path.exists():
        pytest.skip("preview.js not present")
    src = src_path.read_text(encoding="utf-8")
    # `indexedDB` (browser API) and any wrapper shim references.
    assert "indexedDB" not in src.lower(), (
        "preview.js must not reference the browser IndexedDB API; "
        "the runtime is fully in-memory."
    )


def test_no_indexeddb_message_buffer_store_js():
    """The prior `message_buffer_store.js` shim was the JS-side
    mirror of `IndexedDBEventLog`. It's gone."""
    shim_path = (
        _PROJECT_ROOT / "heart-message-manager" / "static" / "preview"
        / "message_buffer_store.js"
    )
    if not shim_path.exists():
        # Already gone — the test passes by absence.
        return
    src = shim_path.read_text(encoding="utf-8")
    assert "indexedDB" not in src.lower(), (
        "message_buffer_store.js (if it still exists) must not "
        "reference IndexedDB; the deque EventLog replaces it."
    )


def test_no_message_buffer_store_py_wrapper():
    """The PyScript wrapper `MessageBufferStore.py` was the Python
    facade over the IDB shim. It's gone."""
    wrapper_path = (
        _PROJECT_ROOT / "heart-message-manager" / "static" / "preview"
        / "MessageBufferStore.py"
    )
    assert not wrapper_path.exists(), (
        "MessageBufferStore.py must be deleted — it wrapped the "
        "IndexedDB shim that this change removes."
    )


def test_no_indexeddb_in_pyconfig_toml():
    """The PyScript config must not declare any IDB-backed shims."""
    src_path = _PROJECT_ROOT / "heart-message-manager" / "py-config.toml"
    if not src_path.exists():
        pytest.skip("py-config.toml not present")
    src = src_path.read_text(encoding="utf-8")
    # Comment-only mentions are OK; explicit `indexedDB` /
    # `MessageBufferStore` / `IndexedDBEventLog` mappings are not.
    assert "MessageBufferStore" not in src, (
        "py-config.toml must not map MessageBufferStore.py — that "
        "wrapper was the IDB facade and is removed."
    )