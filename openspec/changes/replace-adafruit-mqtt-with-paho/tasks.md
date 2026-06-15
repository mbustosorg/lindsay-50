## 1. Delete the MQTT factory and update call sites

- [x] 1.1 `git rm lib_shared/mqtt_factory.py`. The file is the entire factory (the `make_mqtt_client` function and the module docstring). After this change, the only Python MQTT client in the repo is `PahoMqttClient` from `lib_shared.paho_mqtt_client`.
- [x] 1.2 In `heart-message-manager/main.py` (line 101): replace `from lib_shared.mqtt_factory import make_mqtt_client` with `from lib_shared.paho_mqtt_client import PahoMqttClient`. Update line 108 from `_mqtt_client = make_mqtt_client(_noop_dispatch)` to `_mqtt_client = PahoMqttClient(_noop_dispatch)`.
- [x] 1.3 In `heart-matrix-controller/main.py` (line 40): replace `from lib_shared.mqtt_factory import make_mqtt_client` with `from lib_shared.paho_mqtt_client import PahoMqttClient`. Update line 78 from `_mqtt_client = make_mqtt_client(_message_mgr.dispatch)` to `_mqtt_client = PahoMqttClient(_message_mgr.dispatch)`.
- [x] 1.4 Verify no surviving reference to the factory: `rg -n "mqtt_factory|make_mqtt_client" lib_shared/ heart-message-manager/ heart-matrix-controller/` returns no matches.

## 2. Delete the Adafruit IO wrapper

- [x] 2.1 `git rm lib_shared/adafruit_mqtt_client.py`. The file is the entire AIO wrapper (the `AdafruitMqttClient` class) and has no other callers once the factory stops selecting it.
- [x] 2.2 Verify no module imports the deleted file: `rg -n "adafruit_mqtt_client" lib_shared/ heart-message-manager/ heart-matrix-controller/` returns no matches.

## 3. Remove `adafruit-io` from requirements

- [x] 3.1 Delete the `adafruit-io       # Adafruit IO MQTT — Heroku prod (MQTT_CLIENT="adafruit")` line from `requirements.txt`. Leave `paho-mqtt` (line 14) in place.
- [x] 3.2 Verify no other requirements file pins AIO: `rg -n "adafruit-io|adafruit_io|adafruit_minimqtt" requirements.txt heart-matrix-controller/requirements.txt` returns no matches. (`heart-matrix-controller/requirements.txt` already only pins paho.)

## 4. Simplify `_derive_mqtt_ws_url()`

- [x] 4.1 Edit `heart-message-manager/main.py`'s `_derive_mqtt_ws_url()`:
  - Drop the `mqtt_client = (_cfg.if_exists("MQTT_CLIENT") or "").lower()` line.
  - Drop the `if mqtt_client == "adafruit": return "wss://io.adafruit.com/mqtt"` branch.
  - Replace the host-derived default with a `ws://` vs `wss://` heuristic on `MQTT_HOST`: if the host is `127.0.0.1` or `localhost`, return `f"ws://{host}:9002/mqtt"`; else return `f"wss://{host}/mqtt"`.
  - Update the function's docstring to drop the "Adafruit IO" and "MQTT_CLIENT" references; describe the host-derived heuristic and the `MQTT_WS_URL` override.
- [x] 4.2 Update the `# Platform MQTT client (adafruit on Heroku, paho for local dev)` comment in `heart-message-manager/main.py` (line 98) to read `# Platform MQTT client (paho on every platform) — used only as a publisher (no Flask-side subscriber). The device and the browser both subscribe to the broker on their own.`
- [x] 4.3 Verify the CSP setup (lines 588-612) still works with the simplified URL: the `_set_preview_csp` after-request handler reads `_derive_mqtt_ws_url()` and uses its origin for `connect-src`. The heuristic change is internal to the URL; the CSP splice point is unchanged.

## 5. Update `heart-message-manager/settings.toml.example`

- [x] 5.1 Delete the `MQTT_CLIENT = "adafruit"` line.
- [x] 5.2 Delete the three-line comment block immediately above it that introduces the `adafruit` / `paho` choice ("# MQTT client: ...").
- [x] 5.3 Update the `MQTT_WS_URL` comment block to drop the "Adafruit IO: `wss://io.adafruit.com/mqtt`" line and the "Local Paho: `ws://<host>:9002/mqtt`" wording. The new comment says: "# MQTT-over-WebSocket URL for the browser-side `mqtt_ws_client.js`. Default is derived from `MQTT_HOST`: loopback → `ws://<host>:9002/mqtt`; non-loopback → `wss://<host>/mqtt`. Override here if your broker is on a different port or scheme." Keep the existing default value (`MQTT_WS_URL = "wss://io.adafruit.com/mqtt"`) as-is.

## 6. Update `heart-matrix-controller/main.py` comment

- [x] 6.1 Edit the comment on line 77 from `# Platform MQTT client (paho on the Pi; adafruit available via MQTT_CLIENT)` to `# Platform MQTT client (paho on every platform)`.

## 7. No new test file

- [x] 7.1 No new test file is created. The "no AIO import" property is enforced by the `rg` checks in tasks 8.1, and the "PahoMqttClient is the only Python MQTT client" property is verified by the direct import in tasks 1.2 and 1.3 plus the `rg` checks in 8.1. Adding a pytest for either property would test Python's import machinery, not our code.

## 8. Final verification

- [x] 8.1 Run the spec scenario checks from `specs/mqtt-paho-client/spec.md`:
  - `rg -n "from Adafruit_IO|import Adafruit_IO|from lib_shared.adafruit_mqtt_client|import lib_shared.adafruit_mqtt_client" .` (excluding `openspec/` and `.venv/`) returns no matches.
  - `rg -n "adafruit-io|adafruit_io|adafruit_minimqtt" requirements.txt heart-matrix-controller/requirements.txt` returns no matches.
  - `rg -n "adafruit_mqtt_client" lib_shared/ heart-message-manager/ heart-matrix-controller/` returns no matches.
  - `rg -n "mqtt_factory|make_mqtt_client" lib_shared/ heart-message-manager/ heart-matrix-controller/` returns no matches.
  - `rg -n "MQTT_CLIENT" lib_shared/ heart-message-manager/main.py heart-matrix-controller/main.py heart-message-manager/settings.toml.example` returns no matches.
  - `ls lib_shared/adafruit_mqtt_client.py` fails (file does not exist).
  - `ls lib_shared/mqtt_factory.py` fails (file does not exist).
  - `ls lib_shared/paho_mqtt_client.py` succeeds (file still exists).
- [x] 8.2 Run the full pytest suite: `PYTHONPATH=. pytest tests/ -v` — all pre-existing test files pass unchanged. No new test file is added.
- [x] 8.3 Local smoke test (Flask): `python heart-message-manager/main.py` boots without `ImportError` (no `Adafruit_IO` import path is hit). `curl -X POST http://localhost:5000/api/messages -d "From=%2B15551234567&Body=hello+world&To=%2B15559999999"` publishes a message — check the Flask log for `PahoMqttClient published envelope to <topic>`.
- [x] 8.4 Local smoke test (Pi): in a venv with `requirements.txt` minus the `adafruit-io` line and the `rgbmatrix` C extension (which can't build on macOS), `python -c "from lib_shared.paho_mqtt_client import PahoMqttClient; c = PahoMqttClient(lambda x: None); assert isinstance(c, PahoMqttClient)"` exits 0. (Full Pi boot requires the actual hardware — out of scope for local CI.)
- [x] 8.5 Heroku deploy: push to a Heroku staging app, send a test SMS, confirm the message appears in the admin UI and is delivered to a test MQTT subscriber. Remove the `MQTT_CLIENT` config var from the Heroku dashboard if it was set.
