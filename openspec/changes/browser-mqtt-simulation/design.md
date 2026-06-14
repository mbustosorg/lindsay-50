## Context

`add-sign-preview-rendering` proved that PyScript can run the device's effect and scroller code in the browser. The preview works, but its connection to the broker is mediated by the Flask server: every preview tab polls `/api/live-messages?limit=1&suppress=true` every 3 s, and Flask's server-side `MessageManager` exists only to keep that endpoint fresh — the Flask process subscribes to the same MQTT feed the device does, dispatches envelopes into its own in-memory ring buffer, and the polled endpoint reads that buffer.

Paho and Adafruit IO both expose MQTT-over-WebSocket on the same broker the device already uses (the broker translates WS frames to native MQTT on the server side). That makes the broker the natural rendezvous point: the browser can connect to the broker directly over a single WebSocket, subscribe to the same topic as the device, and run its own `MessageManager` instance in WASM Python. The ring buffer then lives in the browser, `/api/live-messages` polling is no longer the source of truth for the preview, and the browser holds state that survives page reloads and SPA route changes.

The relevant pieces in the codebase:

- `lib_shared/message_manager.py` — the device/Flask `MessageManager`; today imports `requests` (for the seed path) and reads `lib_shared.config_reader` (which reads `settings.toml` + env). The seed path makes a server-only assumption (REST + API key + timeout) that does not translate to the browser.
- `lib_shared/messages.py` — `InMemoryMessages` (the ring buffer); pure data, no server dependency, fine for the browser.
- `lib_shared/models.py` — `MessageEnvelope`, `Message`, `SignConfig`, `FilterRule`; pure data, fine for the browser.
- `lib_shared/mqtt_factory.py` — picks `adafruit` or `paho` client on the server. Not used in the browser — the browser connects via WS, not a TCP socket.
- `lib_shared/adafruit_mqtt_client.py` / `lib_shared/paho_mqtt_client.py` — server-side MQTT clients. Not used in the browser.
- `heart-message-manager/main.py` — runs `_message_mgr = MessageManager()` at boot (no `on_message` callback), passes `dispatch` to `make_mqtt_client()`. The server's `MessageManager` is what backs the existing `/api/live-messages` polled endpoint. The change does **not** remove the server's `MessageManager` — the device still needs the REST endpoints and the `seed()` path.
- `heart-message-manager/static/preview.js` — runs the 3 s `setInterval` polling loop that this change replaces.
- `heart-message-manager/templates/preview.html` — hosts the `<py-script>` block; will host the new MQTT bootstrap and remove the polling.

The browser already loads Pyodide + Pillow + numpy to run the effects; the change adds the MQTT-WS client and the IndexedDB-backed persistence on top of the same runtime.

## Goals / Non-Goals

**Goals:**

- Browser connects to the broker over WebSocket and runs its own `MessageManager` instance, eliminating `/api/live-messages` polling as the preview's source of new-message signals
- The in-browser ring buffer and `SignConfig` survive page reloads and SPA route changes via browser-side storage
- The existing `coordinator.request_message(body)` API on `PreviewCoordinator` stays unchanged; the in-browser `MessageManager`'s `_on_message` callback drives it
- The browser's MQTT-WS connection uses the same broker, the same topic, and the same credentials the device uses; no new broker config
- The Flask process's `MessageManager` instance, its MQTT subscription, and the `/api/live-messages`, `/api/live-messages/seed`, and `/api/live-config` endpoints are removed
- The `lib_shared/message_manager.py` class itself stays — the device imports it directly; only the Flask's instance and the live endpoints are removed, not the class
- The `/api/messages` (GET and POST) and `/api/config` (GET and PUT) REST endpoints stay — the Pi's `MessageManager.seed()` calls the non-live `/api/messages` and `/api/config` at boot
- The Twilio ingress path (`POST /api/messages`), SQLite + S3 storage, and the broker publish path are unchanged
- The browser connects to the broker directly over WebSocket; no new Flask WebSocket endpoint
- Tests that hit the removed endpoints are replaced with equivalent coverage of the new browser-internal functions (`BrowserMessageManager.dispatch`, `MqttWsClient` wrapper, `MessageBufferStore` wrapper)
- The `templates/testing.html` page is reworked to use the browser's MQTT-WS client for roundtrip verification instead of polling the Flask's live endpoints
- The device (`heart-matrix-controller/`) is unaffected; the change only touches the Flask process, the preview path, and the testing page

**Non-Goals:**

- Adding a WebSocket endpoint in Flask (the broker handles the WS transport, not Flask)
- Replicating the device's exact frame timing or RNG state in the browser (already documented in `add-sign-preview-rendering`'s design as an accepted divergence; the in-browser `MessageManager` and the device's `MessageManager` are independent processes that happen to share a contract)
- Real-time message rotation across the full history (this change replaces the polling transport; the message-rotation algorithm itself is still a separate concern)
- Cross-tab synchronization between two open preview tabs (each tab is its own browser-side `MessageManager`; this is documented as accepted)
- Removing the `/api/messages` (GET) or `/api/config` (GET) REST endpoints — the Pi's boot seed path still depends on them

## Decisions

### Decision 1: MQTT-over-WebSocket via the broker (not a Flask WebSocket)

The browser opens a WebSocket directly to the broker's MQTT-WS endpoint, not to Flask:

```
   ┌────────────────── Browser tab ──────────────────┐
   │                                                 │
   │  ┌── PyScript (WASM Python) ──┐                 │
   │  │  MqttWsClient              │                 │
   │  │   (broker WS frames)       │                 │
   │  │         │ envelope         │                 │
   │  │         ▼                  │                 │
   │  │  MessageManager            │                 │
   │  │   InMemoryMessages (ring)  │                 │
   │  │   SignConfig               │                 │
   │  │   FilterRule               │                 │
   │  │         │ on_message       │                 │
   │  │         ▼                  │                 │
   │  │  PreviewCoordinator        │                 │
   │  │   request_message(body)    │                 │
   │  └────────────────────────────┘                 │
   │                                                 │
   │  ┌── message_buffer_store.py ─────────┐         │
   │  │  IndexedDB-backed ring + config    │         │
   │  └────────────────────────────────────┘         │
   │                                                 │
   │  ┌── <canvas id="sign-canvas"> ──────────┐      │
   │  │  requestAnimationFrame loop           │      │
   │  └───────────────────────────────────────┘      │
   └─────────────────────────────────────────────────┘
                       │
                       │ WebSocket (MQTT-over-WS)
                       │
                ┌──────┴──────┐
                │  MQTT       │
                │  broker     │──── same broker
                │  (Paho or   │     the device uses
                │  Adafruit)  │
                └─────────────┘
```

**Why direct to the broker, not through Flask:**

- **No new server endpoint.** The broker already speaks MQTT-over-WS; the same credentials the device uses (Adafruit IO `MQTT_USERNAME`/`MQTT_PASSWORD`; or Paho's username/password) authenticate the browser. No Flask route, no `Flask-Sock`, no Heroku router caveats.
- **One broker connection per tab, not per client.** The browser's WS connection is the only new client the broker sees per tab; the existing server-side subscription is unchanged.
- **Symmetric with the device.** The browser subscribes to the same topic the device does, so an envelope published by the Flask Twilio handler is seen by both the device and the browser in parallel — neither has to wait for the other to relay.

**Broker URLs:** the same broker the device uses. For Adafruit IO, the WS endpoint is `wss://io.adafruit.com/mqtt` (path-based, query-string auth). For a local Paho broker, `ws://<host>:<ws-port>/mqtt` (default `ws-port = 9001`). The `MQTT_WS_URL` config key is added to the PyScript config so the operator can copy the same broker values from the device's `settings.toml` (the device already has `MQTT_HOST`/`MQTT_PORT`/etc. — the WS URL is derived from the host + a different port).

### Decision 2: `MessageManager` owns both I/O implementations; `is_browser: bool` selects the path

The existing `lib_shared/message_manager.py` imports `requests` and pulls config via `cfg = get_config()` at module top. Both of those are server-side assumptions. The cleanest factoring is to **refactor `MessageManager` itself** to take the configuration it needs as constructor parameters, removing the `from lib_shared.config_reader import get_config` at module top and removing the module-level `cfg = get_config()` call.

The seed path is preserved — both the device and the browser use it — and **`MessageManager` owns both I/O implementations internally**: a `requests`-based path for the device and a `js.fetch`-based path for the browser. The two paths are selected by an `is_browser: bool` constructor flag. Callers do not pass a transport; they pass the flag, the URLs, and the `X-API-Key`.

```python
# lib_shared/message_manager.py (refactored)
import asyncio
from lib_shared.models import MessageEnvelope, Message, SignConfig
from lib_shared.messages import InMemoryMessages


# Lazy module references: requests is server-only; js.fetch is browser-only.
# Resolved at first use inside _fetch, so the module is importable in both runtimes.
_requests = None
_js_fetch = None


def _ensure_browser_runtime():
    global _js_fetch
    if _js_fetch is None:
        from js import fetch as _js_fetch  # noqa: F811
    return _js_fetch


def _ensure_server_runtime():
    global _requests
    if _requests is None:
        import requests as _requests  # noqa: F811
    return _requests


class MessageManager:
    """MessageManager — config is injected, I/O implementations are internal.

    The device constructs with `is_browser=False` and the class uses `requests`
    for the seed fetch. The browser constructs with `is_browser=True` and the
    class uses the browser's native `fetch` for the seed fetch. The dispatch /
    ring-buffer / config-update logic is identical in both environments.
    """

    def __init__(self, messages_api_url, config_api_url, api_key,
                 is_browser=False, on_message=None):
        """Create MessageManager with explicit URLs and an `is_browser` flag.

        Args:
            messages_api_url: URL of the messages REST endpoint (e.g. /api/messages).
            config_api_url: URL of the config REST endpoint (e.g. /api/config).
            api_key: the X-API-Key the device uses; the same value is used
                     for the seed fetch in both environments.
            is_browser: True when running in the browser (PyScript / Pyodide);
                        defaults to False (the device path). The device's call
                        site does not pass this kwarg; the browser's call site
                        always passes is_browser=True.
            on_message: callback(msg: Message) — invoked when a "message"
                        envelope arrives over MQTT.
        """
        self._messages = InMemoryMessages(SignConfig(), maxlen=100)
        self._messages_api_url = messages_api_url
        self._config_api_url = config_api_url
        self._api_key = api_key
        self._is_browser = is_browser
        self._on_message = on_message

    @property
    def config(self) -> SignConfig: return self._messages._config
    @property
    def messages(self) -> InMemoryMessages: return self._messages

    def dispatch(self, raw: str) -> None: ...     # unchanged

    async def _fetch(self, url: str) -> dict:
        """One HTTP GET to a JSON endpoint, returning the parsed dict.

        Server: requests.get in a worker thread (sync lib, async-friendly).
        Browser: js.fetch with the X-API-Key header (already async).
        """
        if self._is_browser:
            js_fetch = _ensure_browser_runtime()
            response = await js_fetch(url, method="GET",
                                      headers={"X-API-Key": self._api_key})
            if not response.ok:
                raise RuntimeError(f"seed fetch {url} returned HTTP {response.status}")
            return await response.json()
        else:
            requests = _ensure_server_runtime()
            def _sync():
                r = requests.get(url, headers={"X-API-Key": self._api_key}, timeout=5)
                r.raise_for_status()
                return r.json()
            return await asyncio.to_thread(_sync)

    async def seed(self) -> None:
        msgs = await self._fetch(self._messages_api_url)
        cfg = await self._fetch(self._config_api_url)
        # populate ring + config from msgs / cfg  (unchanged from before)

    def get_messages(self, limit, suppress): ...  # unchanged
```

**Why internal branching with an `is_browser` flag (not transport injection, not auto-detection):**

- **Simpler call sites — no template plumbing required.** The `is_browser` flag is a constructor kwarg that defaults to `False` (the device's value). The device's call site passes nothing extra; the browser's call site hardcodes `is_browser=True` in PyScript. Because the Pi's `main.py` is unambiguously the device and the browser's PyScript is unambiguously the browser, neither call site has to consult runtime config to know which environment it's in — the call site already knows. No `isBrowser` field in `APP_CONFIG`; no Jinja rendering of the flag; no template-side plumbing.
- **Auth is a first-class constructor parameter, not a closure capture.** `api_key` is passed in once, at construction time. The same value is used for both the messages and config fetches. The browser's call site reads it from `APP_CONFIG.apiKey`; the device's call site reads it from `settings.toml`. There is no per-fetch closure to compose.
- **Explicit, testable choice over fragile auto-detection.** Auto-detecting the runtime (e.g. `try: from js import fetch`) runs the detection at module import time, which is a side effect that fails unpredictably in tests, and confuses test setups that need to mock either path. The `is_browser: bool` flag is one extra constructor kwarg with a sensible default; the choice is visible at the call site, explicit in the constructor signature, and easy to mock in tests (a test constructs with the flag it wants and monkey-patches `mgr._fetch`).
- **The trade-off is acknowledged.** `MessageManager` no longer is environment-agnostic — it knows about `requests` and `js.fetch`. The `requests` import is lazy (only triggered when `_fetch` runs the server path) so the browser import graph does not pull `requests` in. The `js.fetch` import is similarly lazy. The class is still the single source of truth for dispatch / ring / filter / config-update; only the I/O call shape is environment-specific, and it is gated explicitly by the flag.

**Auth: X-API-Key in both environments.** The X-API-Key is the same value in both environments — the device's configured key. The browser reads it from `APP_CONFIG.apiKey` (rendered into the base template's inline `<script>` block, gated by `@login_required` so only authenticated users see it). The `MessageManager._fetch` method uses it as an `X-API-Key` header on both the server and the browser paths. The Flask process authenticates the seed fetch via the same X-API-Key check the device's transport hits. **No session cookie is involved in the seed fetch** — the auth model for the seed is consistent across device and browser. The page-level `@login_required` is a separate layer (it gates visibility of the inline JS, which contains the key).

**Why seed from the REST API in the browser (not skip the seed path):**

The user's design feedback is explicit: we cannot skip the seed path. The browser needs the same historical backfill the device does. Going from "poll 1 message every 3 s" to "seed 100 messages from `/api/messages` on load" is the right move — the WS connection then keeps the buffer up-to-date, and there is no recurring polling. The seed is a one-shot operation on app start, not a polling path.

**Consistent X-API-Key auth (mirrors the device):**

The X-API-Key check is the same code path the device exercises. When the operator runs the Flask locally and points the browser at it, the browser's seed hits the exact same auth check the device does. If the device's key is misconfigured, the browser surfaces it in dev. This is the value of consistent auth: dev exercises the real device code path, not a parallel one.

### Decision 3: MQTT-WS client in PyScript — native JS shim, after Pyodide package survey

Pyodide package survey: Pyodide's curated packages do not include `paho-mqtt`, any MQTT client, or a websockets wrapper. The `packages/` directory in the pyodide repo holds 34 core packages (cffi, numpy, pytest, micropip, etc.); none of MQTT, websocket, or websockets appear. `paho-mqtt` exists on PyPI but its network layer uses `socket`/`ssl`, which do not work in the browser sandbox — even if installed via `micropip.install`, the import-time calls would fail.

The browser does ship a native `WebSocket` global, accessible from Pyodide via the `js` module proxy (`js.WebSocket`); the same is true for `window.indexedDB`. So the working options for MQTT-over-WS from Pyodide are:

1. **A Python wrapper that uses `js.WebSocket` directly.** Pyodide proxies the browser's `WebSocket` constructor; `onmessage` / `onopen` / `onclose` / `onerror` are `EventHandler` objects whose `setter` accepts Python callables. This works but is verbose from Python; the WebSocket API was designed for JS, and the event-handler semantics are awkward to wrap.
2. **A thin native JS shim that owns the WebSocket lifecycle and the MQTT 3.1.1 framing, called from PyScript via `create_proxy`.** The shim is small (a few hundred lines, no extra download), exposes a clean callback-based surface (`onEnvelope`, `onStatus`), and gives full control over reconnect, backoff, and visibility-pause. PyScript calls into it via `create_proxy` for the callbacks.

**Choice: native JS shim** (confirms the prior design direction after the package survey). Rationale:

- No new Pyodide package dependency; the broker WS framing is small enough to write directly.
- Full control over reconnect / backoff / pause-on-hidden in one place.
- The shim exposes a tiny surface: `createMqttWsClient({ url, username, password, topic, onEnvelope, onStatus })`. PyScript wraps it in a Python class (`MqttWsClient`) that calls the JS shim via `create_proxy`.

```js
// static/mqtt_ws_client.js — runs in the browser, called from PyScript
export function createMqttWsClient({ url, username, password, topic, onEnvelope, onStatus }) {
  // WS → MQTT CONNECT (MQTT 3.1.1)
  // SUBSCRIBE on the configured topic
  // PUBLISH payloads decoded as UTF-8 → JSON → onEnvelope(rawString)
  // Auto-reconnect with exponential backoff
  // onStatus('connected' | 'reconnecting' | 'paused' | 'error', detail)
}
```

```python
# heart-message-manager/static/mqtt_ws_client.py — PyScript wrapper
import json
from js import createMqttWsClient  # from mqtt_ws_client.js

class MqttWsClient:
    def __init__(self, ws_url, username, password, topic, on_envelope):
        self._client = createMqttWsClient({
            "url": ws_url,
            "username": username,
            "password": password,
            "topic": topic,
            "onEnvelope": self._on_envelope_js,
            "onStatus": self._on_status_js,
        })
        self._on_envelope = on_envelope

    def _on_envelope_js(self, raw):
        self._on_envelope(raw)
```

The PyScript interop cost is one function call per inbound envelope, which is negligible.

### Decision 4: Message buffer lives at the app level, not the `/preview` page; wipe + re-seed on app start

The current Flask implementation subscribes to MQTT at boot, regardless of who is looking at the admin UI. The browser equivalent is to **load the WS connection + IndexedDB hydration on every admin page load** (the base template), not just on `/preview`. The buffer is kept up-to-date even when the operator is on `/messages`, `/settings`, `/filters`, or `/`. The `/preview` page reads from the same in-memory state.

```
   ┌────────────────── Browser tab ──────────────────┐
   │                                                 │
   │  ┌── base.html (every admin page) ──┐           │
   │  │  app.js                          │           │
   │  │   ↓                              │           │
   │  │  MessageBufferStore (hydrate)    │           │
   │  │   ↓                              │           │
   │  │  MessageManager (refactored,     │           │
   │  │   shared with the device)        │           │
   │  │   ↑                              │           │
   │  │  MqttWsClient (WS → broker)      │           │
   │  │   ↑                              │           │
   │  │  Seed via fetch(/api/messages)   │           │
   │  │  on app start                    │           │
   │  └──────────────────────────────────┘           │
   │                                                 │
   │  ┌── /preview page ──────────────────┐         │
   │  │  preview.js (reads shared state,  │         │
   │  │  PyScript coordinator, canvas)    │         │
   │  └───────────────────────────────────┘         │
   │                                                 │
   │  ┌── /messages, /settings, /filters, /  ──┐     │
   │  │  Same shared state; same WS client;   │     │
   │  │  IndexedDB is the persistence floor.  │     │
   │  └────────────────────────────────────────┘     │
   └─────────────────────────────────────────────────┘
```

**Why at the app level, not `/preview`:** the operator's mental model is "I have one inbox across the whole app." If the buffer only updates on `/preview`, navigating away means the rest of the app sees stale state, and a fresh `/messages` page load is a re-fetch from SQLite (slow) instead of reading the in-memory ring. Putting the WS + IndexedDB hydrate in the base template (`templates/base.html` or a new `templates/_app_bootstrap.html` partial) means the buffer is current regardless of which page the operator is on.

**Wipe + re-seed on app start.** To prevent the IndexedDB from drifting away from the broker's actual state, the browser's bootstrap performs a **wipe + re-seed** on app start:

1. **On app start** (first page load of an authenticated session): clear the `messages` object store in IndexedDB; call `fetch('/api/messages', { headers: { 'X-API-Key': apiKey } })` (X-API-Key auth, same as the device) to repopulate it with the canonical message list from Flask; load the config from `fetch('/api/config', { headers: { 'X-API-Key': apiKey } })`; open the WS connection; from this point on, new envelopes arrive via the WS and are appended to the buffer.
2. **On every page navigation within the app** (e.g. `/messages` → `/settings`): the in-memory state is rebuilt from the IndexedDB hydrate; the WS reconnects; new envelopes are appended. No wipe (the IndexedDB is recent).
3. **On login**: the IndexedDB is wiped (any data from a prior session is gone) and re-seeded from the REST API.
4. **On reconnect after a long disconnect** (configurable threshold, default 5 minutes): the IndexedDB is wiped and re-seeded; the WS resumes; new envelopes are appended. This is a recovery from missed messages during the disconnect window.

**Why wipe + re-seed is the right shape:** the broker doesn't keep a long-term message log (Adafruit IO has no retained messages on a regular topic; local Paho typically doesn't either). The Flask process does (SQLite + S3), and `/api/messages` returns the canonical list. Wipe + re-seed at app start guarantees the browser's IndexedDB matches the canonical list at session start; the WS appends new messages from there. The wipe is one IndexedDB `clear()` call (microseconds); the re-seed is one fetch (hundreds of ms over local network, single round trip).

**The "missing messages on hidden tab" concern is solved by the status state machine, not by an explicit visibility listener.** The WS client tracks its own `lastConnectedAt` timestamp; when the disconnect duration exceeds the configured threshold (default 5 min), it emits a `paused` status event. On the next `connected` event after a `paused` window, the base template's `app.js` triggers the wipe + re-seed sequence. The OS's natural background-throttling of WebSockets (especially on mobile) is one of the ways a long disconnect can happen; the `paused` state fires regardless of cause (visibility, network drop, broker crash, laptop sleep). We do not add an explicit `visibilitychange` listener — the OS pauses us when it wants to, and the `paused` state covers all recovery paths.

### Decision 5: IndexedDB persistence for the ring buffer and `SignConfig`

The ring buffer must survive page reloads and SPA route changes. Browser-side storage options:

- **`localStorage`** — synchronous, ~5 MB cap, fine for 100 small messages but blocks the main thread and is small. Not great.
- **`IndexedDB`** — async, large quota (typically 50%+ of disk), perfect for a 100-message ring buffer + a small `SignConfig` JSON blob. The standard for browser-side persistence of structured data.
- **`OPFS` / `IDBFS` (Pyodide)** — exposes a filesystem on top of IndexedDB. Convenient for pickling but heavier than we need.

**Choice: IndexedDB via a small JS shim, called from the base template.** Same pattern as the MQTT-WS shim. The shim is plain JS loaded by the base template's `<script>` tag (not PyScript), so it runs on every page regardless of whether PyScript has finished loading.

**Schema:**

```
db: lindsay-50-browser
  store: messages
    keyPath: id
    indexes: by-received_at (received_at, descending)
  store: config
    keyPath: key  (single record, key="current")
```

**Lifecycle:**

1. **On app start** (per the wipe+re-seed sequence in Decision 4): the IndexedDB `messages` and `config` stores are cleared; the ring and `SignConfig` are repopulated from the REST API.
2. **On every `dispatch()` that accepts a `message` envelope** (after filter): the new message is written to IndexedDB. `add()` calls `store.put(message)` and trims the store to the last 100 keys.
3. **On every `dispatch()` that accepts a `config` envelope**: the `SignConfig` is serialized and written to the `config` store.
4. **On every page navigation within the app**: the in-memory state is rebuilt from the IndexedDB hydrate; the WS reconnects; new envelopes are appended.
5. **On tab close**: the last write is the last state. No teardown needed.

**Why not `localStorage`:** the 5 MB cap is fine for 100 messages today, but the limit is per-origin and synchronous, and `SignConfig` updates can be JSON blobs that we'd rather not stringify on every keystroke. IndexedDB is the conventional answer.

**Storage writes are async, not in the hot path.** `dispatch()` writes to IndexedDB after accepting the in-memory state, so an IndexedDB write failure (quota, private mode) does not break the in-memory ring — the in-browser preview keeps working, it just won't survive a reload. We log the write failure and surface it in the connection status block as a non-fatal warning.

### Decision 6: Bootstrap order — on app load, not `/preview` page load

The bootstrap sequence runs once per page load in the base template. It is the same on every admin page (`/`, `/messages`, `/settings`, `/filters`, `/preview`, `/testing`, etc.) — the `/preview` page adds the PyScript coordinator and canvas on top, but the WS + IndexedDB + ring hydrate are common across all pages:

1. **JS bootstrap** (`static/app.js`, loaded from the base template): set up the `MessageBufferStore` (IndexedDB shim), the `MqttWsClient` (MQTT-WS shim), and the refactored `MessageManager` as module-scope singletons; show the connection-status block (Live / Reconnecting / Paused / Error).
2. **Wipe IndexedDB** (once per session start): `store.clear()` on the `messages` and `config` object stores. Cheap; runs in parallel with the next step.
3. **Re-seed from REST** (once per session start): `await mgr.seed()` (where `mgr` is a `MessageManager(..., is_browser=True, ...)` constructed in PyScript) populates the ring via the canonical `/api/messages` and `/api/config` endpoints; the class's internal `_fetch` method issues the `js.fetch` calls with `X-API-Key` headers. Both fetches use the same X-API-Key auth the device uses; both hit the existing endpoints (no new routes).
4. **Connect to broker over WS**: `MqttWsClient.connect(...)` with the broker URL and credentials from the base-template config; on `connected` and `subscribe-ack`, the status block updates to "Live".
5. **Wire `MessageManager`'s `on_message` callback to per-page handlers**: any registered page (e.g. `/preview` via `coordinator.request_message(body)`) receives the new message; the in-memory ring buffer is the source of truth. Other pages (e.g. `/messages`) read from the same in-memory state for their listing.
6. **Animation loop** (only on `/preview`): `requestAnimationFrame` at ≥ 30 FPS; the canvas blits the frame. Other pages do not run the animation loop.
7. **No polling against `/api/live-messages`.** The Flask process no longer hosts that endpoint or the ring buffer it served. The only network activity from any admin page is the single WS connection and (on app start) the two fetch calls for re-seeding.

**First-load message:** "Connecting…" is shown until step 4 completes. The buffer is current as soon as step 3 (the re-seed) finishes; the WS connection in step 4 starts appending new envelopes from there. The "Live" indicator turns on in step 4.

### Decision 7: Reuse the existing `coordinator.request_message(body)` API on the `/preview` page

The previous change established `PreviewCoordinator.request_message(body)` as the bridge from "new message" to "show it on the canvas." This change preserves that API. The wiring at the `/preview` page is:

```python
# in /preview page bootstrap (PyScript)
def on_message(msg: Message) -> None:
    # the MessageManager (refactored) callback
    py_coordinator.request_message(msg.body)  # via create_proxy

mgr = MessageManager(
    messages_api_url=APP_CONFIG.messagesApiUrl,
    config_api_url=APP_CONFIG.configApiUrl,
    api_key=APP_CONFIG.apiKey,
    is_browser=True,  # we are in the browser (PyScript knows this)
    on_message=on_message,
)
await mgr.seed()  # async: re-seed from REST (uses js.fetch internally)
```

`PreviewCoordinator` is unchanged. The polling-vs-MQTT swap is entirely above the coordinator.

### Decision 8: Config surface for the base template

The browser's MQTT-WS connection needs `MQTT_WS_URL`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC`. The `MessageManager` seed path needs `MESSAGES_API_URL`, `CONFIG_API_URL`, and the `X-API-Key` for the seed fetch (same auth the device uses). These come from environment values the base template injects at page render. The naming matches the device's `settings.toml` keys so the operator can copy values directly.

**The base template** (`templates/base.html` or a new `templates/_app_bootstrap.html` partial) renders a small inline `<script>` block with the values resolved from the Flask process's `settings.toml` (server-side, at template render time):

```html
<script>
  window.APP_CONFIG = {
    mqttWsUrl:      "{{ mqtt_ws.MQTT_WS_URL }}",
    mqttUsername:   "{{ mqtt.MQTT_USERNAME }}",
    mqttPassword:   "{{ mqtt.MQTT_PASSWORD }}",
    mqttTopic:      "{{ mqtt.MQTT_TOPIC }}",
    messagesApiUrl: "{{ config.MESSAGES_API_URL }}",
    configApiUrl:   "{{ config.CONFIG_API_URL }}",
    apiKey:         "{{ auth.API_KEY }}",
    mqttLongDisconnectMs: {{ mqtt_ws.MQTT_LONG_DISCONNECT_MS | default(300000) }}
  };
</script>
<script src="{{ url_for('static', filename='app.js') }}"></script>
```

**Why inline rather than in `py-config.toml`:** the `py-config.toml` is the PyScript config, which is loaded only when PyScript initializes (~5-10 s on first load). The base-template bootstrap needs the values immediately, on every page load, before PyScript is ready. The inline `<script>` block in the base template makes the config available to `app.js` synchronously, on every page. The values are still rendered server-side from `settings.toml`; the operator still fills in `settings.toml` once.

**Security: are these values safe in inline JS?** This is the user's question. The answer:

- The MQTT broker credentials (`MQTT_USERNAME` / `MQTT_PASSWORD`) are the same values the device uses; they are not new secrets. They live in `settings.toml` today.
- The seeded URLs (`MESSAGES_API_URL` / `CONFIG_API_URL`) point to the operator's own server; they are not secrets.
- The MQTT-WS auth on Adafruit IO uses the `aio_username` / `aio_key` as plain credentials in the WS URL or as basic auth — this is how the broker's MQTT-over-WS works; the secret is not protected from the client side. (Paho and Adafruit both expect the client to know its own credentials.)
- The browser does NOT use a session cookie for the seed fetch — it uses the same X-API-Key the device uses. The Twilio pattern (HMAC-signed request with a per-request signature from `TWILIO_AUTH_TOKEN`) is not the model here; the device's `X-API-Key` is a static shared header, used by both the device and the browser.
- All of these values are already implicitly trusted in the codebase: the existing `add-sign-preview-rendering` change renders the PyScript config (`py-config.toml`) which contains the operator's MQTT host/port, and the existing admin UI pages render API keys as `X-API-Key` headers in HTML (via the testing page's hidden inputs). The new inline config does not add a new trust boundary; it makes an existing trust boundary more explicit.
- The base template's `<script>` block is rendered by Jinja on the server and is therefore available only to authenticated users (the base template extends `login_required` via the auth blueprint's `before_app_request`). Unauthenticated users do not see the inline config.

**Public broker caveat (Adafruit IO):** the `aio_username` and `aio_key` are the WS auth values; they're the same values the device uses for native MQTT, so no new secret.

**Local Paho broker caveat:** the WebSocket transport is **disabled by default**. The operator must start the broker with `--ws-port` (or the equivalent in `mosquitto.conf`):

```bash
mosquitto -p 1883 --ws-port 9001
```

…and set `MQTT_WS_URL = ws://localhost:9001/mqtt` in `settings.toml`. This is documented in `README.md` and surfaced in the connection status block as a clear error if the WS port is closed.

### Decision 9: Flask's server-side `MessageManager`, its MQTT subscription, and the `/api/live-*` endpoints are removed

The Flask process previously ran its own `MessageManager` instance and subscribed to the broker just to keep a server-side ring buffer that the preview polled via `/api/live-messages`. With the browser now holding its own ring buffer (and the only other consumer of the Flask's ring being the testing page, which this change reworks), neither the Flask's `MessageManager` nor the broker subscription it backs has a remaining consumer. The change removes them.

Specifically, the change **removes**:

- The `_message_mgr = MessageManager()` instantiation in `heart-message-manager/main.py:101` and the `from lib_shared.message_manager import MessageManager` import in that file (it stays imported by `heart-matrix-controller/main.py:38`)
- The `threading.Thread(target=_message_mgr.seed, daemon=True).start()` block — the Flask was seeding its ring buffer from the same REST endpoints the Pi uses; with the Flask's ring gone, this seed is moot
- The `_mqtt_client = make_mqtt_client(_message_mgr.dispatch)` wiring and `_mqtt_client.start()` call — Flask no longer subscribes to the broker
- The `GET /api/live-messages` route (the polled snapshot of the Flask's ring)
- The `POST /api/live-messages/seed` route (the manual re-seed trigger)
- The `GET /api/live-config` route (the polled snapshot of the Flask's `SignConfig`)

The change **preserves**:

- `lib_shared/message_manager.py` itself — the class is imported directly by the Pi. The class, the `InMemoryMessages` ring buffer, and the `seed()` method (which the Pi calls at boot) are unchanged. The Pi is unaffected by removing the Flask's instance.
- `GET /api/messages` and `GET /api/config` — the Pi's `MessageManager.seed()` calls these (not the `live-` versions) at boot
- `POST /api/messages` — the Twilio ingress path
- `PUT /api/config` — the admin UI's config writes
- SQLite + S3 storage — these are independent of the Flask's `MessageManager`

**Why this is safe.** The Pi does not depend on any Flask-side state:

- The Pi instantiates its **own** `MessageManager(on_message=...)` (`heart-matrix-controller/main.py:62`) and seeds it via `_message_mgr.seed()` which calls `CONFIG_API_URL` / `MESSAGES_API_URL` (the non-live endpoints) at boot
- The Pi subscribes to the broker via its own `_mqtt_client = make_mqtt_client(_message_mgr.dispatch)` (`heart-matrix-controller/main.py:72`) — independent of the Flask's subscription
- The Flask's `_message_mgr.dispatch` was fed by the Flask's own `_mqtt_client`, not by the broker routing messages through the Flask. The Pi and the Flask were independent subscribers on the same topic; removing the Flask's subscription does not change the Pi's view

**Other consumers of the removed endpoints.** The change also reworks:

- `templates/testing.html` — replaces `fetch('/api/live-messages/seed')`, `fetch('/api/live-messages?suppress=...')`, and `fetch('/api/live-config')` with browser-side MQTT-WS verification: POST the test message via `/api/test-messages`, observe the envelope arriving in the browser's `MqttWsClient` → `BrowserMessageManager.dispatch`, and update the page from the in-browser ring buffer
- `tests/test_auth.py` — removes the `/api/live-messages` auth tests; the existing `/api/messages` and `/api/config` auth tests continue to cover the auth flow
- `tests/preview_poll_test.py` — deleted (it asserts the polling URL string, which no longer exists)
- `scripts/preview_server.py` and `scripts/verify_preview_browser.py` — drop the `/api/live-messages` references; the preview server still serves the static assets and the `/api/messages` POST path

## Risks / Trade-offs

- **[Risk] Two browser tabs each open their own WS connection to the broker** → Accepted. The broker handles N concurrent clients trivially. No cross-tab sync — each tab's ring buffer is independent. If the operator wants the same view in two tabs, both will receive the same envelopes from the broker; if one tab is older, its IndexedDB hydrate is independent.
- **[Risk] Missed envelopes on long disconnects** → Mitigated by the wipe + re-seed on reconnect after a long disconnect (Decision 4). The IndexedDB is wiped and re-fetched from `/api/messages`; the WS resumes and appends new envelopes from there. The "long disconnect" threshold is configurable, default 5 minutes. Below the threshold, missed envelopes during the disconnect are simply lost from the in-memory ring (the broker's at-most-once delivery semantics); the IndexedDB hydrate on the next page navigation does not replay them.
- **[Risk] IndexedDB quota / private-mode failure** → The in-memory ring keeps working; the persistence write is logged as a non-fatal warning. Reload reverts to whatever IndexedDB has. Acceptable.
- **[Risk] PyScript bundle size grows by the JS shim + IndexedDB shim** → The shims are small (a few hundred lines each, no new dependencies). First-load weight is dominated by Pyodide + Pillow + numpy, which are already loaded. Net delta is on the order of ~50 KB, not the MB scale of the Pyodide runtime.
- **[Risk] MQTT-WS auth on Adafruit IO uses the same `aio_key` as the device** → If that key is rotated, both the device and the browser must update. The change documents this. The `aio_key` is already a secret handled via `settings.toml`; no new secret shape.
- **[Trade-off] Local Paho broker needs explicit `--ws-port`** → Paho's MQTT-over-WS transport is **disabled by default**. Operators must start the broker with `--ws-port 9001` (or the equivalent in `mosquitto.conf`) and set `MQTT_WS_URL = ws://localhost:9001/mqtt` in `settings.toml`. This is documented in `README.md` and surfaced in the connection status block as a clear error if the WS port is closed. Adafruit IO exposes WS on `wss://io.adafruit.com/mqtt` by default; no extra configuration.
- **[Trade-off] WS reconnects on every page navigation** → The base template bootstraps a fresh WS connection on every page load. This is intentional: the alternative (a true persistent connection across navigations) requires a service worker or SPA, both of which are out of scope. The reconnect is fast (tens of ms), and the IndexedDB hydrate on each page load means the user sees a current buffer immediately, even before the WS reaches `connected`. The wipe + re-seed on app start is the only "expensive" part of the bootstrap; the per-page reconnect after that is cheap.
- **[Trade-off] Inline `<script>` exposes the X-API-Key to authenticated browsers** → The base template's `<script>` block inlines `MQTT_WS_URL` / `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_TOPIC` / `MESSAGES_API_URL` / `CONFIG_API_URL` / `apiKey` (the X-API-Key) / `mqttLongDisconnectMs`. The X-API-Key is the same key the device uses; it's not a new secret. The inline block is rendered only for authenticated users (the base template is gated by `@login_required` via the auth blueprint). This is the same trust model the existing `py-config.toml` and the testing page's hidden inputs use. The benefit: the browser exercises the device's exact auth code path in dev.
- **[Trade-off] The in-browser `MessageManager` and the device's `MessageManager` are independent processes that happen to subscribe to the same topic** → They can disagree on filter results (different filter rules, different timestamps). The preview shows "what the sign *would* display given the same envelope stream," not "exactly what the device is showing." Same trade-off documented in `add-sign-preview-rendering`.
- **[Trade-off] PyScript cold start (5–10 s first load) is unchanged** → The MQTT-WS / IndexedDB additions are tiny; the cold start is dominated by Pyodide + Pillow + numpy, all of which are already loaded. Mitigation: spinner + "Loading…" message; lazy-init only when the user opens `/preview`; browser cache makes subsequent loads 1–2 s. The base-template bootstrap (which runs on every page) does not depend on PyScript — it is plain JS, so it is fast on every page.
- **[Risk] Removing the Flask's MQTT subscription means the Flask can no longer act as a redundant subscriber** → Accepted. The Pi and the browser each open their own broker subscriptions; the Flask's subscription was only feeding its own ring buffer, which is being removed. The Twilio ingress path still publishes to the broker, so the Pi and browser see the same envelopes as before. If the operator wants a server-side log of all broker activity, that's a separate concern (S3 + SQLite already capture the publish-side; the broker-side capture is not in scope).
- **[Risk] The `/api/messages` (GET) and `/api/config` (GET) endpoints must stay accurate** → The Pi's boot seed AND the browser's per-session re-seed depend on them. The change keeps these endpoints and their SQLite-backed storage unchanged. If a future change touches them, it must keep the response shape (`Message` dict list for `/api/messages`, `SignConfig` dict for `/api/config`) both consumers parse.
- **[Risk] The `templates/testing.html` rework depends on the browser's MQTT-WS client** → If the operator opens `/testing` in a browser where the WS is blocked (corporate proxy, browser extension), the roundtrip verification silently fails. The page shows a clear "WebSocket blocked" error so the operator can fall back to the live device.
- **[Trade-off] Tests that asserted the polling URL string are removed, not migrated** → The previous `tests/preview_poll_test.py` and the `/api/live-messages` auth tests covered behavior that no longer exists. Their coverage is replaced by `tests/test_message_manager.py` (refactored class: dispatch, ring buffer, suppression, eviction, internal `_fetch` branching on `is_browser`) and the wrapper smoke tests. The auth flow itself is still covered by the remaining `/api/messages` and `/api/config` auth tests.

## Migration Plan

No data migration. Deployment steps:

1. **Add `lib_shared/browser_message_manager.py`** — the browser-compatible `MessageManager` (no `requests`, no `config_reader`).
2. **Add `heart-message-manager/static/mqtt_ws_client.js`** — native JS MQTT-over-WebSocket shim with auto-reconnect, pause-on-hidden, status callbacks.
3. **Add `heart-message-manager/static/mqtt_ws_client.py`** — PyScript wrapper that calls the JS shim via `create_proxy`.
4. **Add `heart-message-manager/static/message_buffer_store.js`** — native JS IndexedDB shim (small: `getMessages(limit)`, `putMessage(msg)`, `getConfig()`, `putConfig(dict)`, `trimToLast(n)`).
5. **Add `heart-message-manager/static/message_buffer_store.py`** — PyScript wrapper.
6. **Update `heart-message-manager/py-config.toml`** — declare the new env values (`MQTT_WS_URL`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC`).
7. **Update `heart-message-manager/main.py`** —
   - When serving `py-config.toml`, inject the env values from `settings.toml` `[mqtt_ws]` (or derived from `MQTT_HOST` + default WS port)
   - **Remove** the `from lib_shared.message_manager import MessageManager` import (no longer used by the Flask process)
   - **Remove** the `_message_mgr = MessageManager()` instantiation
   - **Remove** the `threading.Thread(target=_message_mgr.seed, daemon=True).start()` block
   - **Remove** the `from lib_shared.mqtt_factory import make_mqtt_client` import
   - **Remove** the `_mqtt_client = make_mqtt_client(_message_mgr.dispatch)` wiring and `_mqtt_client.start()` call
   - **Remove** the `GET /api/live-messages` route handler (`api_live_messages`)
   - **Remove** the `POST /api/live-messages/seed` route handler (`api_live_messages_seed`)
   - **Remove** the `GET /api/live-config` route handler (`api_live_config`)
   - Verify the `api_login_required` decorator still has other consumers; if `live-messages` was its only consumer, leave the decorator in place for the `/api/messages` and `/api/config` routes
8. **Update `heart-message-manager/static/preview.js`** — remove the 3 s `setInterval(pollLatestMessage, 3000)` polling loop; add the PyScript init hook that constructs `BrowserMessageManager` + `MqttWsClient` + `MessageBufferStore` and wires the `_on_message` callback to `coordinator.request_message(body)`.
9. **Update `heart-message-manager/templates/preview.html`** — surface the connection status (Live / Reconnecting / Paused) alongside the existing "currently active effect" / "now displaying" status block.
10. **Update `heart-message-manager/templates/testing.html`** — replace the `fetch('/api/live-messages/seed')`, `fetch('/api/live-messages?suppress=...')`, and `fetch('/api/live-config')` calls with browser-side MQTT-WS verification: POST to `/api/test-messages`, observe the envelope arriving in the browser's `MqttWsClient` → `BrowserMessageManager`, and update the page from the in-browser ring buffer
11. **Update `heart-message-manager/settings.toml.example`** — document the new `[mqtt_ws]` block and the `MQTT_WS_URL` derivation rule.
12. **Update `tests/test_auth.py`** — remove the `/api/live-messages` auth tests; the existing `/api/messages` and `/api/config` auth tests continue to cover the auth flow
13. **Delete `tests/preview_poll_test.py`** — the file asserts a polling URL string that no longer exists; coverage moves to `test_browser_message_manager.py`
14. **Update `scripts/preview_server.py` and `scripts/verify_preview_browser.py`** — drop the `/api/live-messages` references; the preview server still serves the static assets and the `/api/messages` POST path
15. **Add tests** in `tests/` for `BrowserMessageManager.dispatch` (parses both `message` and `config` envelopes, calls `_on_message` only on `message`, persists to `InMemoryMessages`, evicts at 100); for the `MqttWsClient` and `MessageBufferStore` Python wrapper signatures
16. **Verify locally**: open `/preview`, confirm WS connection is established, an inbound MQTT envelope is dispatched into the ring buffer, the canvas cycles to the new message, IndexedDB has the message on reload, a hard refresh re-hydrates the ring and shows the last message immediately; open `/testing`, POST a test message, confirm the browser's MQTT-WS client sees the envelope and the page updates from the in-browser ring; open `/messages` and `/settings` to confirm the admin UI still works without the Flask's `MessageManager`

Rollback: revert the `main.py` removals (re-add the Flask's `_message_mgr` and the live routes), revert `static/preview.js` to the polling version, revert `templates/testing.html` to the polling version, re-add the auth tests and `tests/preview_poll_test.py`, remove the new shim files and `browser_message_manager.py`, drop the new `[mqtt_ws]` block from `settings.toml.example`.

## Open Questions

- **Should the preview attempt broker-side replay on reconnect (clean session=false + retained messages)?** Most public brokers (Adafruit IO) default to clean sessions, and broker-side replay for a 100-message ring is overkill. Document the gap, leave the door open. A future change could add a server-side retained-message bridge if needed.
- **Cross-tab sync:** if the operator has two preview tabs open, each holds an independent ring. If we want them synchronized, the cleanest path is a `BroadcastChannel` between tabs (one tab is the "primary" that holds the WS connection, the others subscribe to its events). Not in v1; not on the critical path.
- **Should the `MQTT_WS_URL` be derived from `MQTT_HOST` automatically, or always explicit?** Today we plan to make it explicit in `settings.toml` (with a documented default of `wss://io.adafruit.com/mqtt` for Adafruit and `ws://<host>:9001/mqtt` for Paho). Operators who use a non-default port need to set it explicitly. If the auto-derivation is reliable enough, we can collapse it later.
- **What happens if the in-browser `MessageManager` receives a `config` envelope for a `FilterRule` the device doesn't know about?** The change makes the browser and device independent on the same contract; a config update from the broker is applied to the browser's `SignConfig` and the browser's `FilterRule` set. If the device's filter set and the browser's filter set disagree on the same envelope, the in-browser preview will show a different message than the device — same as the existing trade-off. Documented.
