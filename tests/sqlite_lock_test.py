"""Tests for the sqlite init_db + rebuild_from_s3 concurrency guards.

Gunicorn boots every worker via a separate ``import main`` call,
so two (or more) workers can race on the schema-build / S3-rebuild
path. SQLite only permits one writer; the second worker either sees
a file mid-creation ("attempt to write a readonly database") or
hits journal contention ("disk I/O error"). Both observations came
from the 2026-07-10 v137 deploy crash.

These tests pin the fix: an ``fcntl.flock`` advisory lock on a
sidecar file (``<db>.init.lock``) serializes the unlink-and-rebuild
phase. Workers that don't get the lock wait briefly for the DB to
exist, then no-op.

The tests are host-only — no Heroku. /tmp is always writable here.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _import_sqlite():
    """Import ``heart_message_manager.sqlite`` fresh, registering the
    synthetic package on first call. Returns the module."""
    pkg_name = "heart_message_manager"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(_PROJECT_ROOT / "heart-message-manager")]
        sys.modules[pkg_name] = pkg
    s3_dir = str(_PROJECT_ROOT / "heart-message-manager")
    if s3_dir not in sys.path:
        sys.path.insert(0, s3_dir)
    return importlib.import_module(f"{pkg_name}.sqlite")


def test_init_db_creates_lock_sidecar(tmp_path, monkeypatch):
    """After `init_db()`, the sidecar ``.init.lock`` file exists on disk."""
    monkeypatch.setenv("HEROKU_APP_NAME", "1")  # force the /tmp DB path
    sqlite = _import_sqlite()
    db_path = tmp_path / "lindsay50.db.sqlite"
    monkeypatch.setattr(sqlite, "_db_path", lambda: db_path)
    sqlite.init_db()
    assert db_path.exists(), "sqlite DB not created"
    assert (tmp_path / "lindsay50.db.init.lock").exists(), "sidecar lock file missing"


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """Calling `init_db()` twice in a row doesn't raise. The second call
    sees CREATE TABLE IF NOT EXISTS as a no-op and returns silently.
    (The 2026-07-10 second-worker failure was hitting SQLite, not Python —
    we want to make sure the no-op path stays no-op.)"""
    monkeypatch.setenv("HEROKU_APP_NAME", "1")
    sqlite = _import_sqlite()
    db_path = tmp_path / "lindsay50.db.sqlite"
    monkeypatch.setattr(sqlite, "_db_path", lambda: db_path)
    sqlite.init_db()
    sqlite.init_db()  # must not raise
    # Schema still intact — insert + read should round-trip.
    from lib_shared.models import Message

    sqlite.put_message(
        Message(
            id="m1",
            sender="+15551234567",
            body="hello",
            received_at="2026-07-10T00:00:00Z",
        )
    )
    got = sqlite.get_message("m1")
    assert got is not None
    assert got.body == "hello"


def test_init_db_skips_lock_when_unopenable(tmp_path, monkeypatch, caplog):
    """If the lock file can't be opened (no permission, parent dir
    gone, etc.), `init_db()` falls through to the unlocked path
    rather than raising — degraded concurrency is preferable to a
    boot crash. Lock failures are logged at WARNING."""
    monkeypatch.setenv("HEROKU_APP_NAME", "1")
    sqlite = _import_sqlite()
    db_path = tmp_path / "lindsay50.db.sqlite"
    monkeypatch.setattr(sqlite, "_db_path", lambda: db_path)
    # Force os.open to fail for the sidecar path.
    _real_open = os.open

    def _maybe_fail(path, *args, **kwargs):
        if str(path).endswith(".init.lock"):
            raise OSError("simulated lock failure")
        return _real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _maybe_fail)
    with caplog.at_level("WARNING"):
        sqlite.init_db()
    # Lock failure was logged AND the DB still got created.
    assert db_path.exists(), "DB not created when lock failed"
    assert any(
        "could not open lock file" in rec.message for rec in caplog.records
    ), "expected WARNING about lock failure; got nothing"


def test_init_db_concurrent_workers_serialize(tmp_path, monkeypatch):
    """Two threads calling `init_db()` concurrently — one wins the
    flock, the other waits for the file to appear then no-ops.
    Both calls return; the schema is intact; no exception is raised.

    This is the regression test for the v137 boot crash."""
    import threading

    monkeypatch.setenv("HEROKU_APP_NAME", "1")
    sqlite = _import_sqlite()
    db_path = tmp_path / "lindsay50.db.sqlite"
    monkeypatch.setattr(sqlite, "_db_path", lambda: db_path)

    errors: list[BaseException] = []

    def _worker():
        try:
            sqlite.init_db()
        except BaseException as exc:  # noqa: BLE001 — capture everything
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"workers raised: {errors!r}"
    assert db_path.exists()
    # Schema is usable.
    conn = sqlite.sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    table_names = {r[0] for r in rows}
    assert {"messages", "config"}.issubset(table_names)
