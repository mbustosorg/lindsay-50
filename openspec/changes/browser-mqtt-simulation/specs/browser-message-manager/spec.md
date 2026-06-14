## ADDED Requirements

### Requirement: MessageManager takes config and an `is_browser` flag as constructor parameters (no `config_reader` dependency)

The `MessageManager` class in `lib_shared/message_manager.py` SHALL be refactored to take its configuration as constructor parameters rather than reading it at module import time, and SHALL own both I/O implementations internally rather than receiving an injected transport callable:

- The module SHALL NOT import `lib_shared.config_reader` (no `from lib_shared.config_reader import get_config`)
- The module SHALL NOT call `cfg = get_config()` at module top
- The module SHALL NOT `import requests` at module top — the `requests` import SHALL be lazy and live inside the seed-fetch I/O path (only triggered when the device/server path runs)
- The module SHALL NOT `import js` at module top — the `js.fetch` import SHALL be lazy and live inside the seed-fetch I/O path (only triggered when the browser path runs)
- The constructor SHALL accept `messages_api_url: str`, `config_api_url: str`, `api_key: str`, `is_browser: bool = False`, and an optional `on_message: Callable[[Message], None]` callback. The `is_browser` flag defaults to `False` (the device's value); the device's call site does not need to pass it; the browser's call site always passes `is_browser=True` as a hardcoded literal in PyScript (no `isBrowser` is rendered into `APP_CONFIG` — the browser call site knows it is the browser from the surrounding PyScript code)
- The class SHALL internally own an `InMemoryMessages(SignConfig(), maxlen=100)` ring buffer (the same as today)
- The `dispatch(raw: str) -> None` method SHALL be unchanged in behavior: parse a `MessageEnvelope` from JSON, route to the ring buffer (for `type: "message"`) or `SignConfig` (for `type: "config"`), and invoke the `on_message` callback for non-suppressed messages
- The `seed() -> Awaitable[None]` method SHALL be `async` and SHALL internally call an `async def _fetch(self, url: str) -> dict` method. The `_fetch` method SHALL:
  - When `is_browser=True`: lazily import `js.fetch` and call `await js.fetch(url, method="GET", headers={"X-API-Key": self._api_key})`; raise `RuntimeError` on non-`ok` responses; return `await response.json()`
  - When `is_browser=False`: lazily import `requests` and call `requests.get(url, headers={"X-API-Key": self._api_key}, timeout=5)` from an `asyncio.to_thread(...)` worker; raise on HTTP errors; return the parsed JSON
- The `get_messages(limit: int = 100, suppress: bool = True)` method SHALL be unchanged
- The public surface SHALL expose `messages: InMemoryMessages` and `config: SignConfig`

The same refactored class SHALL be used by the device (`heart-matrix-controller/main.py`, which passes `is_browser=False`) and by the browser (the PyScript code in the base template, which passes `is_browser=True`). The dispatch / ring-buffer / filter / config-update logic is shared; only the I/O path inside `_fetch` differs.

#### Scenario: lib_shared/message_manager.py no longer imports config_reader at module top

- **WHEN** the change is deployed
- **THEN** `lib_shared/message_manager.py` SHALL NOT contain `from lib_shared.config_reader import get_config` at module top; introspecting the module's top-level AST (e.g. via `ast.parse`) SHALL confirm the absence of that import; the module SHALL NOT call `cfg = get_config()` at module top

#### Scenario: lib_shared/message_manager.py has no top-level `import requests` and no top-level `import js`

- **WHEN** the change is deployed
- **THEN** `lib_shared/message_manager.py` SHALL NOT contain `import requests` or `import js` (or `from js import ...`) at module top; the `requests` import SHALL appear only inside the server-path branch of `_fetch` (or inside a lazy-loader helper); the `js` import SHALL appear only inside the browser-path branch of `_fetch` (or inside a lazy-loader helper); introspecting the module's top-level AST SHALL confirm the absence of both

#### Scenario: Constructor accepts URLs, api_key, and is_browser

- **WHEN** `MessageManager(messages_api_url, config_api_url, api_key, is_browser=False, on_message=None)` is called
- **THEN** the instance SHALL be usable immediately; the public methods (`dispatch`, `seed`, `get_messages`) SHALL be available; the ring buffer SHALL be initialized with `InMemoryMessages(SignConfig(), maxlen=100)`; the `is_browser` flag SHALL be stored and SHALL drive the I/O path inside `_fetch`

#### Scenario: seed uses the internal _fetch method (server path)

- **WHEN** `await mgr.seed()` is called on a `MessageManager` constructed with `is_browser=False`
- **THEN** `_fetch` SHALL lazily import `requests` and SHALL call `requests.get(url, headers={"X-API-Key": self._api_key}, timeout=5)` from an `asyncio.to_thread(...)` worker; the method SHALL `await self._fetch(self._messages_api_url)` to obtain the message list (a list of `Message` dicts) and SHALL `await self._fetch(self._config_api_url)` to obtain the `SignConfig` dict; HTTP errors SHALL raise into the caller; the ring buffer and `SignConfig` SHALL be populated from the results; no `js` import SHALL be triggered; no `requests` import SHALL appear at module top

#### Scenario: seed uses the internal _fetch method (browser path)

- **WHEN** `await mgr.seed()` is called on a `MessageManager` constructed with `is_browser=True`
- **THEN** `_fetch` SHALL lazily import `js.fetch` and SHALL call `await js.fetch(url, method="GET", headers={"X-API-Key": self._api_key})`; a non-`ok` response SHALL raise `RuntimeError`; the method SHALL `await self._fetch(self._messages_api_url)` and `await self._fetch(self._config_api_url)`; no `requests` import SHALL be triggered; no module-top imports SHALL be evaluated

#### Scenario: The device call site does not need to pass is_browser

- **WHEN** `heart-matrix-controller/main.py` constructs the `MessageManager`
- **THEN** the call site SHALL pass `messages_api_url` and `config_api_url` resolved from the device's `settings.toml`, the `api_key` from the device's `settings.toml`, and `on_message=...`; the call site SHALL NOT need to pass `is_browser` (the default is `False`); the call site SHALL NOT pass a `seed_transport` callable; the device's caller SHALL `await` or `asyncio.run(...)` the seed from its boot thread; the device's runtime behavior SHALL be functionally equivalent to the previous implementation

#### Scenario: The browser call site hardcodes is_browser=True

- **WHEN** the base template's `app.js` (or the `/preview` page's PyScript init hook) constructs the `MessageManager`
- **THEN** the call site SHALL pass `messages_api_url` and `config_api_url` from `window.APP_CONFIG`, `api_key=APP_CONFIG.apiKey`, `is_browser=True` (a hardcoded literal in the PyScript code — the call site knows it is in the browser because the surrounding code is PyScript), and `on_message=...`; the call site SHALL NOT pass a `seed_transport` callable; the caller `await`s the seed from PyScript

### Requirement: Seed fetch uses X-API-Key in both environments (consistent auth)

The `MessageManager._fetch` method SHALL use the `X-API-Key` header to authenticate the `/api/messages` and `/api/config` GET requests, in both the device and the browser. The `X-API-Key` is the same value the device uses (read from `settings.toml` and rendered into the base template's inline `APP_CONFIG` block for the browser).

The existing `@login_required` decorator on the admin pages SHALL continue to gate the page (so only authenticated users see the inline JS, which contains the key). The X-API-Key is a separate auth layer for the seed fetch itself; the page being visible is not equivalent to the seed being authorized.

This is consistent with the device's existing auth path. The benefit: the browser exercises the device's exact code path in dev — if the device's X-API-Key is misconfigured, the browser surfaces it. Bugs in the auth flow that would affect the device also affect the browser.

#### Scenario: Seed fetch carries X-API-Key in the browser

- **WHEN** the browser's `await mgr.seed()` calls `_fetch`
- **THEN** the underlying `js.fetch` call SHALL include `headers: { "X-API-Key": "<value from APP_CONFIG.apiKey>" }`; the server SHALL authenticate via the X-API-Key check (the same check the device's transport hits); the response SHALL be 200 with the canonical message / config list

#### Scenario: Seed fetch carries X-API-Key on the device

- **WHEN** the device's `await mgr.seed()` calls `_fetch`
- **THEN** the underlying `requests.get` call SHALL include `headers={"X-API-Key": "..."}` with the device's configured key; the server SHALL authenticate via the same X-API-Key check; the response SHALL be 200 with the canonical message / config list

#### Scenario: Wrong X-API-Key is rejected in the browser

- **WHEN** the operator's `settings.toml` has an incorrect or rotated `API_KEY`
- **THEN** the `_fetch` call to `/api/messages` SHALL return a non-`ok` response; `_fetch` SHALL raise `RuntimeError`; `seed()` SHALL propagate the error; the base template SHALL surface a clear "API key rejected — check settings.toml" error in the connection status block

#### Scenario: Inline JS contains the API key (no isBrowser field needed)

- **WHEN** the base template renders the inline `<script>window.APP_CONFIG = { ... }</script>` block
- **THEN** the rendered script SHALL contain `MQTT_WS_URL`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC`, `MESSAGES_API_URL`, `CONFIG_API_URL`, `apiKey` (the X-API-Key), and `mqttLongDisconnectMs`; the rendered script SHALL NOT contain an `isBrowser` field (the browser call site hardcodes `is_browser=True` directly in PyScript — no need to render the flag into the inline JS); the page SHALL be gated by `@login_required` so only authenticated users see the inline config

### Requirement: Filter rules are evaluated before the on_message callback fires

The `MessageManager` SHALL evaluate the configured `FilterRule` set against each inbound `Message` before adding it to the ring buffer and before invoking the `on_message` callback. Filtered-out messages SHALL be marked as suppressed in the ring buffer and SHALL NOT trigger the `on_message` callback. This is unchanged from the previous behavior.

#### Scenario: A filtered message is suppressed and does not trigger on_message

- **WHEN** a `type: "message"` envelope arrives whose sender / body matches an active `FilterRule` exclusion
- **THEN** the message SHALL be added to the ring buffer with `suppressed=True`; the `on_message` callback SHALL NOT be invoked

#### Scenario: A non-filtered message triggers on_message

- **WHEN** a `type: "message"` envelope arrives that does NOT match any active `FilterRule` exclusion
- **THEN** the message SHALL be added to the ring buffer with `suppressed=False`; the `on_message` callback SHALL be invoked exactly once with the `Message`

#### Scenario: get_messages with suppress=True returns non-suppressed only

- **WHEN** `get_messages(limit, suppress=True)` is called on a ring containing both suppressed and non-suppressed messages
- **THEN** the returned list SHALL contain only messages with `suppressed=False`, newest first, up to `limit` entries

### Requirement: Ring buffer caps at 100 messages

The `InMemoryMessages` instance owned by `MessageManager` SHALL have `maxlen=100`. When a 101st message is added, the oldest message SHALL be evicted. This is unchanged from the previous behavior.

#### Scenario: 101st message evicts the oldest

- **WHEN** 100 messages are present in the ring buffer and a 101st message is added via `dispatch`
- **THEN** the oldest message (smallest `received_at`) SHALL be evicted; `get_messages(200, suppress=False)` SHALL return exactly 100 messages, the most recent 100

### Requirement: The device and the browser use the same MessageManager class

The `MessageManager` class SHALL be a single class shared by the device and the browser. The device's `heart-matrix-controller/main.py` and the browser's PyScript code SHALL import the same `lib_shared.message_manager.MessageManager` symbol. There SHALL be no `BrowserMessageManager` sibling class.

#### Scenario: One class, two I/O paths

- **WHEN** the change is deployed
- **THEN** `lib_shared/` SHALL contain exactly one `MessageManager` class, defined in `lib_shared/message_manager.py`; there SHALL NOT be a `lib_shared/browser_message_manager.py` module; the device and the browser SHALL both import `from lib_shared.message_manager import MessageManager`

#### Scenario: The device's MessageManager import is unchanged

- **WHEN** `heart-matrix-controller/main.py` is read
- **THEN** it SHALL still import `from lib_shared.message_manager import MessageManager`; the call site SHALL pass the URLs, the `api_key`, and `is_browser=False`; the device's runtime behavior (boot seed + MQTT subscription + display updates) SHALL be unchanged

#### Scenario: The browser imports the same class

- **WHEN** the base template's `app.js` (or the `/preview` page's PyScript init hook) loads the `MessageManager`
- **THEN** it SHALL resolve to the same `lib_shared.message_manager.MessageManager` class that the device uses; the browser's import graph SHALL include the refactored class with no top-level `import requests` and no top-level `import js`
