## Why

The Flask server (Heroku) and the Raspberry Pi display are both Python processes, but they speak MQTT through two different libraries: the Flask server uses `Adafruit_IO.MQTTClient` (a thin wrapper over `adafruit_minimqtt` that does AIO-specific topic translation like `{user}/feeds/{feed}`), while the Pi uses `paho-mqtt` directly. The browser already speaks raw MQTT 3.1.1 over WebSocket and hand-builds the topic — it never touched the AIO library because AIO is Python-only.

`Adafruit_IO.MQTTClient` is a thin wrapper that does **two things** for us: it hand-builds the `{username}/feeds/{topic}` wire-format topic, and it runs an MQTT-over-TLS loop. Both of those are easy to do in paho. The AIO wrapper's only "convenience" (the topic translation) was the source of the browser-side `feeds/<feed>` bug — the AIO library's topic handling is opaque, and we have no way to inspect or override it. Standardizing on paho gives us one client for both Python entrypoints, full visibility into the wire format, and a single dependency to maintain.

The local-dev path already runs on paho (the `MQTT_CLIENT = "paho"` branch is what we use in development today). The Heroku path is the last holdout. After this change, the only Python MQTT client in the codebase is `paho-mqtt`.

## What Changes

- **Delete `lib_shared/adafruit_mqtt_client.py`** — the AIO wrapper is unused once the factory stops selecting it. No other module imports it.
- **Delete `lib_shared/mqtt_factory.py`** — with one client to choose from, the factory no longer has a choice to make. Both `heart-message-manager/main.py` and `heart-matrix-controller/main.py` import `PahoMqttClient` from `lib_shared.paho_mqtt_client` directly. The single-arg constructor `PahoMqttClient(dispatch_callback=…)` is short enough to call inline at the two import sites.
- **Remove `adafruit-io` from `requirements.txt`** — the only AIO dependency in the repo. `paho-mqtt` is already there.
- **Update `heart-message-manager/main.py`** — drop the `if mqtt_client == "adafruit"` branch in `_derive_mqtt_ws_url()`. The WebSocket URL is now derived purely from `MQTT_HOST` (and `MQTT_WS_URL` override). The module comment on line 98 ("adafruit on Heroku, paho for local dev") is updated to "paho on every platform".
- **Update `heart-message-manager/settings.toml.example`** — remove the `MQTT_CLIENT = "adafruit"` line and the "MQTT client" comment block that introduces it. Update the `MQTT_WS_URL` comment to drop the "Adafruit IO: `wss://io.adafruit.com/mqtt`" branch. The Flask-side `MQTT_WS_URL` default is the same `wss://io.adafruit.com/mqtt` (the broker is still Adafruit IO's; only the client library changes).
- **Update `heart-matrix-controller/main.py`** — the `# Platform MQTT client (paho on the Pi; adafruit available via MQTT_CLIENT)` comment becomes "# Platform MQTT client (paho on every platform)".
- **Update `heart-matrix-controller/settings.toml.example`** — the MQTT_HOST comment still says "io.adafruit.com (Adafruit IO cloud with TLS on port 8883)" — leave it, since the broker is still Adafruit IO, only the client changes. No key change needed in the Pi's settings.
- **Tests** — no new test file. The "no AIO import" property and the "PahoMqttClient is the only Python MQTT client" property are enforced by the `rg` checks in tasks 8.1 (every spec scenario is either a `rg` or an `ls` command). The `PahoMqttClient` constructor is a thin wrapper around the paho library; its end-to-end behavior is covered by the manual webhook test in `CLAUDE.md` and by the Heroku prod environment.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `flask-server-cleanup` (from `openspec/specs/` — assumed to exist; verify on archive): the Flask server no longer depends on `adafruit-io`. The `MQTT_CLIENT` config key is removed. The `_derive_mqtt_ws_url` helper drops the AIO branch.
- `sign-preview-rendering`: the `/preview` page's MQTT-WS connection now resolves to a single URL derivation path (paho broker topology) — no behavior change visible to the browser, but the `connect-src` CSP and the JS client both rely on the same URL, so the change is internal to `_derive_mqtt_ws_url`.

## Impact

- **Deleted files**: `lib_shared/adafruit_mqtt_client.py`, `lib_shared/mqtt_factory.py`.
- **Modified files**: `heart-message-manager/main.py` (drop the `make_mqtt_client` import; import `PahoMqttClient` from `lib_shared.paho_mqtt_client` directly; `_derive_mqtt_ws_url` simplified; comment updated), `heart-message-manager/settings.toml.example` (`MQTT_CLIENT` line + comment block removed, `MQTT_WS_URL` comment simplified), `heart-matrix-controller/main.py` (drop the `make_mqtt_client` import; import `PahoMqttClient` from `lib_shared.paho_mqtt_client` directly; comment only), `requirements.txt` (`adafruit-io` line removed).
- **Heroku config**: the `MQTT_CLIENT` config var (if set to `"adafruit"` in the Heroku dashboard) becomes a no-op — paho is used regardless. Document this in the commit message; no code path reads `MQTT_CLIENT` after this change.
- **Browser**: zero change. The browser still opens a WebSocket to `MQTT_WS_URL` (still Adafruit IO's `wss://io.adafruit.com/mqtt` by default). The Flask-side `connect-src` CSP still allows the same origin. No new dependencies in the JS bundle.
- **Pi device**: zero change. The Pi already runs paho (the factory's default branch). The `MQTT_CLIENT` env var on the Pi is unset.
- **Out of scope**: changing the broker (we still use Adafruit IO — just the client library is different), changing the wire format (still `{username}/feeds/{topic}` — paho is configured to subscribe to the full path), changing the message envelope shape, removing the AIO broker entirely, switching the browser from WebSocket to long-polling.
