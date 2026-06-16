# Issue #39 — Use MessageManager in the browser preview; drop request_message from EffectsCoordinator

## Goal

Move the browser preview off `EffectsCoordinator`'s internal `request_message` / `_recent` deque and onto the existing `MessageManager`. The browser runs Python via PyScript, so this is a Python-only refactor — no JS ports of `lib_shared/` classes.

The browser's `MessageManager` becomes app-scoped (a singleton loaded from `base.html`), so its in-memory state survives SPA navigations. The `EffectsCoordinator`'s state is also app-scoped; the canvas + rAF loop stay per-page; the coord's render layer is bound per-page. The browser and the Pi use the **same** `MessageManager` + `InMemoryMessages` + `EffectsCoordinator` Python classes. The only difference is `is_browser=True` for the seed-fetch runtime (`js.fetch` vs `requests`), which is the existing behavior.

Out of scope (defer):
- The "preview always visible" goal. Canvas stays on preview pages only. `app_main.py` lives in `base.html` so the bootstrap is ready when the canvas moves there.
- Any change to `heart-message-manager/main.py` (Flask server). The server is unaffected; `MessageManager` works as today for Flask-side use.
- Logout flow: if the user logs out mid-session, the in-memory state isn't automatically wiped. The next login's `seed()` will clear + repopulate, so this is benign for now. If we want logout to clear the buffer explicitly, that's a follow-up.

## Architecture

```
   WS-MQTT broker
        │  raw envelope
        ▼
  mqtt_ws_client.js   (JS shim in static/)
        │  raw string
        ▼
  app.js  ─►  window._message_manager.dispatch(raw)
                            │  (Python, PyScript, app-scoped)
                            ▼
                  MessageManager  (lib_shared/message_manager.py)
                            │
                            ▼
                    InMemoryMessages
                  (lib_shared/messages.py)
                            │
                ┌───────────┴────────────┐
                ▼                        ▼
        EffectsCoordinator       (Pi: same coord, recent_provider
        (recent_provider          reads from the manager via lambda)
         reads from manager)
                │
                │  on preview pages:
                │  page-local WebCanvas, WebDisplay, PreviewScroller, effects, heart
                │  bound via coord.bind(display, scroller, effects, heart)
                ▼
        rAF loop (preview.js)
```

In-memory state survives SPA navigation because the `MessageManager` is a singleton in `app_main.py`. The login flow's `wipe()` + `manager.seed()` is the only path that populates the state on a fresh page load. Live MQTT dispatches fill it after that. A full page reload mid-session leaves the buffer empty until the next login or live updates — by design.

## Files

### Changed: `lib_shared/effects_coordinator.py`

- Drop `request_message(text)` method and the `_recent` deque.
- Make `display`, `scroller`, `effects`, `heart` constructor args optional (default `None`).
- Add `bind(self, display, scroller, effects, heart)` — sets all four. Single-call API; the previous constructor-set shape stays for the Pi (which has all four at construction time).
- `tick()` is a no-op if `self.display is None`. State advances only when the coord is bound, so it stays consistent with what's rendered.
- `recent_provider` stays. The browser's `app_main.py` passes `recent_provider=lambda: manager.get_messages(limit=5)`. The Pi keeps `recent_provider=lambda: _message_mgr.get_messages(limit=5)`.
- The state machine itself (`mode`, `idx`, `phase_start`, `last_shown_text`, `pending_text`, `fade_*`) is unchanged.

`MessageManager` itself doesn't change — it still uses `InMemoryMessages` in both runtimes. The `is_browser` parameter stays for the seed-fetch runtime (`js.fetch` vs `requests`), which is the existing behavior.

### New: `heart-message-manager/app_main.py`

PyScript entry, loaded from `base.html`. Owns the app-global instances. No canvas, no rAF loop.

```python
# top-level await for PyScript 2024.9.x
from pyodide_js import loadPackage
await loadPackage(["micropip"])  # numpy/Pillow are preview-page-specific

# Standard imports
from lib_shared.message_manager import MessageManager
from lib_shared.effects_coordinator import EffectsCoordinator
# (MqttWsClient wrapper, sign-of-life pieces)

# Build the global manager. In-memory state starts empty; the auth-aware
# app.js init flow's manager.seed() is the only path that populates it.
manager = MessageManager(
    messages_api_url=...,
    config_api_url=...,
    api_key=...,
    is_browser=True,
)

# Build the global coord (render layer not bound yet)
coordinator = EffectsCoordinator(
    display=None, scroller=None, effects=None, heart=None,
    recent_provider=lambda: manager.get_messages(limit=5),
)

# MqttWsClient (Python wrapper around the JS shim) wires on_envelope=manager.dispatch
mqtt = MqttWsClient(ws_url=..., username=..., password=..., topic=..., on_envelope=manager.dispatch)
mqtt.start()

# Expose to JS
import js
js.window._message_manager = manager
js.window._coordinator = coordinator
```

### Changed: `heart-message-manager/preview_main.py`

Becomes a per-page PyScript shim. Reads the global coord from `window`, creates page-local canvas/scroller/effects/heart, binds them, and exposes the existing `window.tick` / `get_frame_rgba` / `get_current_text` / `get_current_effect_name` surface.

```python
# Preview-specific (numpy/Pillow are only used by the canvas)
from pyodide_js import loadPackage
await loadPackage(["numpy", "Pillow"])

from preview_display import WebCanvas, WebDisplay
from preview_scroller import PreviewScroller
from lib_shared.patterns.fireworks import Fireworks
# ... etc.

import js
coord = js.window._coordinator

# Page-local render layer
web_canvas = WebCanvas(64, 64)
display = WebDisplay(web_canvas)
scroller = PreviewScroller(display)
effects = [Fireworks(display), Flame(display), NightSky(display), Honeycomb(display), Hyperspace(display)]
heart = Heartbeat(display)

# Bind to the global coord
coord.bind(display, scroller, effects, heart)

# Expose the JS surface (same names as today — preview.js rAF loop is unchanged)
js.window.tick = lambda: coord.tick()
js.window.get_frame_rgba = lambda: web_canvas.to_imagedata()
js.window.get_current_text = lambda: coord.current_text
js.window.get_current_effect_name = lambda: coord.current_effect_name
```

`coord.start(None)` (the boot-splash kick) moves to here, on first bind, not in the constructor. The first navigation to a preview page kicks the boot splash; subsequent navigations rebind without restarting it.

### Changed: `heart-matrix-controller/main.py`

Drop the `on_message=lambda msg: coordinator.request_message(msg.body)` line. The Pi's `tick()` discovers new messages on the next pass via `recent_provider` (the same way it already pulls for the random recent pick). No other Pi changes.

### Templates

`base.html` (or `base-playful.html`) gets a `<py-script src=".../app_main.py">` block so the bootstrap runs on every page. The existing preview-page `<py-script src=".../preview_main.py">` blocks stay, but their job is now just the per-page canvas binding.

### `heart-message-manager/static/app.js` — auth-aware seed trigger

`app.js` already runs on every page and is the natural place to detect auth state (via `document.cookie`, a page-level element, or an `/api/auth/status` probe). It gains one new responsibility on the **first logged-in page load** of a session:

- Detect that the user is logged in.
- Call `await window._message_manager.seed()`.

`seed()` does `self._messages.clear()` + `add_many()` from REST internally, so this single call is the canonical "wipe + repopulate" — no separate `MessageBufferStore.wipe()` call needed.

This single trigger covers both cases that need seeding:
- **Login flow** — after the user authenticates, `app.js` calls `seed()` (replacing the existing JS-side `wipe()` + `seed()` pair that went through `MessageBufferStore`).
- **Full page reload mid-session** — the user was already logged in, the page reloaded, `app.js` detects the auth state on first load and calls `seed()`. The in-memory state was lost (page reload), so this is the re-boot path, analogous to the Pi calling `seed()` at startup.

SPA navigations (no full reload) need no trigger — the `MessageManager` is a singleton and its in-memory state persists.

**Timing:** `app.js` runs in parallel with the `<py-script src=".../app_main.py">` bootstrap. It needs `window._message_manager` to exist before it can call `seed()`. Implementation can use `pyscript.ready` (PyScript 2024.9.x has this), a custom event dispatched by `app_main.py` when ready, or a simple poll for `window._message_manager` — any of these is fine.

### Deleted: `heart-message-manager/static/message_buffer_store.py` and `message_buffer_store.js`

`MessageBufferStore` is fully dead after this change — the `MessageManager` (via `seed()`) owns the wipe + repopulate flow, and live MQTT dispatches own the in-session persistence. Delete both the Python wrapper and the JS shim. Remove every reference in the codebase (search for `MessageBufferStore`, `message_buffer_store`, `createMessageBufferStore`, and any `put_message` / `put_config` / `wipe` / `hydrate` calls in `app.js`).

Note: `SignConfig` persistence moves entirely server-side (S3, SQLite — unchanged). The browser no longer caches the config in IDB; it re-reads via `seed()` after page load.

## Tests

- `tests/effects_coordinator_test.py` (changed): remove all `test_request_message_*` tests; add tests for `bind`, the no-op-when-unbound `tick()`, and the "render layer can be swapped mid-life" path (bind a fake display, tick, unbind, tick — verify state advances only when bound).
- `tests/message_manager_test.py` (unchanged): the manager is still `is_browser=False` + `InMemoryMessages` in tests; no IDB involvement. Existing tests stay green.
- `tests/test_sign_runtime_config.py` and other existing tests: unchanged.

## Out of scope (defer)

- The "preview is always visible" goal. Canvas stays on preview pages only. `app_main.py` lives in `base.html` so the bootstrap is ready when the canvas moves there.
- Any change to `heart-message-manager/main.py` (Flask server). The server is unaffected; `MessageManager` works as today for Flask-side use.
- The "newly arrived message" discovery in `EffectsCoordinator.tick()`: works the same as the Pi's random-recent path — `tick()` polls `recent_provider`, the newest unshown message becomes `pending_text` implicitly. No explicit `_last_consumed_id` tracking is needed.
