"""Browser-side IndexedDB-backed message buffer store (PyScript wrapper).

Calls the native JS shim in `message_buffer_store.js` via Pyodide's
`create_proxy`. The store persists the in-browser `MessageManager`'s
ring buffer (most recent 100 messages) and the live `SignConfig` to
IndexedDB so the buffer survives page reloads and SPA route changes.

Wipe + re-seed is the recovery shape: on app start, login, and after
a long-disconnect reconnect, the base template's `app.js` calls
`wipe()` followed by `MessageManager.seed()`.
"""

from js import createMessageBufferStore  # type: ignore[import-not-found]  # noqa: F401


class MessageBufferStore:
    """Python wrapper around the native JS IndexedDB shim.

    Args:
        db_name: The IndexedDB database name. Defaults to
                 "lindsay-50-browser".
    """

    def __init__(self, db_name: str = "lindsay-50-browser") -> None:
        """Create the wrapper. The underlying JS store is not opened
        until the first call (lazy open via IndexedDB.open)."""
        self._store = createMessageBufferStore({"dbName": db_name})

    def hydrate(self):
        """Return `{ messages: [...], config: {...} | null }` from
        IndexedDB. Called on every admin page load before the first
        `requestAnimationFrame`.

        `messages` is the most recent 100 entries, newest first by
        `received_at`. `config` is the `SignConfig` dict under the
        `current` key, or `None` if no record exists.

        Returns `(messages, config)` for convenience to Python callers
        (the JS shim returns an object; Pyodide unwraps it).
        """
        result = self._store.hydrate()
        # The JS shim returns { messages: [...], config: {...} | null }.
        # Pyodide unwraps the JsProxy; for dict access, use `result.dict()`
        # or coerce via `dict(result)` on a JsProxy.
        try:
            d = result.dict() if hasattr(result, "dict") else dict(result)
        except Exception:
            d = {"messages": [], "config": None}
        return d.get("messages", []), d.get("config", None)

    def wipe(self) -> dict:
        """Clear the `messages` and `config` object stores.

        Returns `{ ok: True }` on success, `{ ok: False, error: str }` on
        failure. Never throws across the JS bridge.
        """
        return self._store.wipe()

    def put_message(self, msg: dict) -> dict:
        """Write a single message to the `messages` store, with
        atomic trim to the most recent 100 entries by `received_at`.

        `msg` is a dict with at least `id` and `received_at`.

        Returns `{ ok: True }` on success, `{ ok: False, error: str }`
        on failure. Fire-and-forget from the caller's perspective.
        """
        return self._store.putMessage(msg)

    def put_config(self, cfg: dict) -> dict:
        """Replace the `current` key in the `config` object store.

        Returns `{ ok: True }` on success, `{ ok: False, error: str }`
        on failure.
        """
        return self._store.putConfig(cfg)
