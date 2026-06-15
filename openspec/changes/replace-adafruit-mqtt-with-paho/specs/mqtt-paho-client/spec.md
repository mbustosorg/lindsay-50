## MODIFIED Requirements

### Requirement: Python MQTT clients use paho-mqtt exclusively
The system SHALL use `paho-mqtt` (via `lib_shared/paho_mqtt_client.PahoMqttClient`) as the only Python MQTT client. No module in the repo SHALL import from `Adafruit_IO` or `lib_shared.adafruit_mqtt_client` at runtime. The `adafruit-io` package SHALL NOT be a runtime dependency.

#### Scenario: No module imports the Adafruit IO library
- **WHEN** `rg -n "from Adafruit_IO|import Adafruit_IO|from lib_shared.adafruit_mqtt_client|import lib_shared.adafruit_mqtt_client" .` is executed (excluding `openspec/` and `.venv/`)
- **THEN** no matches are returned

#### Scenario: adafruit-io is not a runtime dependency
- **WHEN** `rg -n "adafruit-io|adafruit_io|adafruit_minimqtt" requirements.txt heart-matrix-controller/requirements.txt` is executed
- **THEN** no matches are returned (the package is no longer pinned)

#### Scenario: Both entrypoints import PahoMqttClient directly
- **WHEN** `rg -n "from lib_shared.paho_mqtt_client import PahoMqttClient" heart-message-manager/main.py heart-matrix-controller/main.py` is executed
- **THEN** exactly one match is returned per entrypoint file (Flask and the Pi both import the client directly, with no factory indirection)

### Requirement: MQTT factory module is deleted
The file `lib_shared/mqtt_factory.py` SHALL NOT exist in the repo. No module SHALL import `make_mqtt_client` or reference `lib_shared.mqtt_factory`. Both `heart-message-manager/main.py` and `heart-matrix-controller/main.py` SHALL construct `PahoMqttClient` directly, without going through a factory.

#### Scenario: mqtt_factory.py is removed
- **WHEN** `ls lib_shared/mqtt_factory.py` is executed
- **THEN** the file does not exist

#### Scenario: No surviving import of the factory
- **WHEN** `rg -n "mqtt_factory|make_mqtt_client" lib_shared/ heart-message-manager/ heart-matrix-controller/` is executed
- **THEN** no matches are returned (the module is gone and nothing imports it)

#### Scenario: Entry points construct PahoMqttClient inline
- **WHEN** `heart-message-manager/main.py` and `heart-matrix-controller/main.py` are read
- **THEN** each file contains exactly one `PahoMqttClient(...)` constructor call bound to a `dispatch_callback` (`_noop_dispatch` in Flask, `_message_mgr.dispatch` on the Pi) — no factory wrapper, no `make_mqtt_client` call

### Requirement: Adafruit IO wrapper is deleted
The file `lib_shared/adafruit_mqtt_client.py` SHALL NOT exist in the repo. No module SHALL re-export symbols from it.

#### Scenario: adafruit_mqtt_client.py is removed
- **WHEN** `ls lib_shared/adafruit_mqtt_client.py` is executed
- **THEN** the file does not exist

#### Scenario: No surviving import of the wrapper
- **WHEN** `rg -n "adafruit_mqtt_client" lib_shared/ heart-message-manager/ heart-matrix-controller/` is executed
- **THEN** no matches are returned (the file is gone and nothing imports it)

### Requirement: The MQTT_CLIENT config key is removed
No code path in the repo SHALL read the `MQTT_CLIENT` config key. The operator-facing `settings.toml.example` SHALL NOT contain the key or a comment block introducing the `adafruit` option.

#### Scenario: MQTT_CLIENT is not read anywhere
- **WHEN** `rg -n "MQTT_CLIENT" lib_shared/ heart-message-manager/main.py heart-matrix-controller/main.py` is executed
- **THEN** no matches are returned (the config key is removed from all readers)

#### Scenario: MQTT_CLIENT is not in the example
- **WHEN** `rg -n "MQTT_CLIENT" heart-message-manager/settings.toml.example` is executed
- **THEN** no matches are returned

### Requirement: Flask MQTT WebSocket URL derivation has no AIO branch
`heart-message-manager/main.py`'s `_derive_mqtt_ws_url()` SHALL derive the URL from `MQTT_WS_URL` (operator override) or from `MQTT_HOST` (with a `ws://` vs `wss://` heuristic). The function SHALL NOT branch on `MQTT_CLIENT == "adafruit"`. The default URL for the prod broker (Adafruit IO) SHALL remain `wss://io.adafruit.com/mqtt`; the default for local dev (loopback host) SHALL remain `ws://127.0.0.1:9002/mqtt`.

#### Scenario: _derive_mqtt_ws_url has no AIO branch
- **WHEN** `rg -n "mqtt_client == .adafruit." heart-message-manager/main.py` is executed
- **THEN** no matches are returned

#### Scenario: Explicit MQTT_WS_URL is respected
- **WHEN** `MQTT_WS_URL` is set in the operator's `settings.toml`
- **THEN** `_derive_mqtt_ws_url()` returns that exact string, regardless of `MQTT_HOST`

#### Scenario: Loopback host defaults to ws://
- **WHEN** `MQTT_HOST` is `127.0.0.1` or `localhost` and `MQTT_WS_URL` is unset
- **THEN** `_derive_mqtt_ws_url()` returns a `ws://` URL with port 9002

#### Scenario: Non-loopback host defaults to wss://
- **WHEN** `MQTT_HOST` is a non-loopback value (e.g., `io.adafruit.com`) and `MQTT_WS_URL` is unset
- **THEN** `_derive_mqtt_ws_url()` returns a `wss://` URL with no explicit port (broker default 443)

### Requirement: settings.toml.example does not mention the removed MQTT_CLIENT key
`heart-message-manager/settings.toml.example` SHALL NOT contain a `MQTT_CLIENT` key or a "MQTT client" comment block referencing the `adafruit` option. The `MQTT_WS_URL` comment SHALL describe the URL override and the host-derived default, but SHALL NOT reference `MQTT_CLIENT`.

#### Scenario: MQTT_WS_URL comment does not reference MQTT_CLIENT
- **WHEN** `rg -n "MQTT_CLIENT" heart-message-manager/settings.toml.example` is executed
- **THEN** no matches are returned; the `MQTT_WS_URL` comment stands on its own

### Requirement: Pi main.py comment reflects paho-only client
The comment in `heart-matrix-controller/main.py` describing the MQTT client SHALL be updated to reflect that paho is the only client across all platforms.

#### Scenario: Pi comment no longer references the adafruit option
- **WHEN** `rg -n "adafruit available via MQTT_CLIENT" heart-matrix-controller/main.py` is executed
- **THEN** no matches are returned

### Requirement: Existing test suite remains green
After this change, all pre-existing pytest files in `tests/` SHALL continue to pass without modification. No new test file SHALL be added (the change is a static removal; the "no AIO import" and "PahoMqttClient is the only client" properties are enforced by the `rg` scenarios in this spec, not by a runtime test).

#### Scenario: Full pytest suite passes
- **WHEN** `PYTHONPATH=. pytest tests/ -v` is executed
- **THEN** all pre-existing test files pass unchanged; no new test file is collected
