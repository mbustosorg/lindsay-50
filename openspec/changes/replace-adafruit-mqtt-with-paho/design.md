## Context

The repo has two Python MQTT clients: `lib_shared/adafruit_mqtt_client.py` (the Adafruit_IO.MQTTClient wrapper, used by Flask on Heroku) and `lib_shared/paho_mqtt_client.py` (a paho-mqtt adapter, used by the Pi device and by Flask for local dev). The factory in `lib_shared/mqtt_factory.py` selects between them at runtime based on a `MQTT_CLIENT` config key.

The browser is independent of both — it speaks raw MQTT 3.1.1 over WebSocket (`heart-message-manager/static/mqtt_ws_client.js`), and just fixed its own `feeds/<feed>` topic bug by hand-building the wire-format topic. The reason the browser didn't have the same bug Flask and the Pi have historically dodged: those two clients use the AIO library, which transparently translates feed names like `"lindsay50"` into the wire format `"{username}/feeds/lindsay50"`. The AIO library's translation is invisible to the caller — and that's exactly the problem. When the wire format is wrong, we have no way to inspect or override it from the Python side.

The local-dev path is already paho. The Heroku path is the last holdout. The paho adapter (`lib_shared/paho_mqtt_client.py`) already subscribes to the full topic string from `MQTT_TOPIC` (which the operator sets to whatever the broker expects — for Adafruit IO, that means the full `{username}/feeds/lindsay50` path). The paho client does no AIO-specific translation. The factory's `paho` branch already works against `io.adafruit.com:8883` (TLS) — it's used in CI and local dev today.

After this change, the only Python MQTT client in the repo is `paho-mqtt`. The AIO wrapper goes away. The factory becomes a thin constructor. The `MQTT_CLIENT` config key is removed.

## Goals / Non-Goals

**Goals:**

- A single Python MQTT client (`paho-mqtt`) is used by every Python entrypoint (Flask, the Pi). The Adafruit IO wrapper is removed.
- The factory in `lib_shared/mqtt_factory.py` is removed; both entrypoints import `PahoMqttClient` from `lib_shared.paho_mqtt_client` directly. There is no platform branching anywhere — the choice is encoded by which constructor each entrypoint calls (and both call the same one).
- The AIO library is no longer a runtime dependency. `requirements.txt` no longer pins it.
- The browser is unaffected. The Flask-side `_derive_mqtt_ws_url()` is simplified to a single derivation path.
- The operator-facing `settings.toml` example no longer mentions `MQTT_CLIENT` or the `adafruit` option.

**Non-Goals:**

- Switching brokers. The broker is still Adafruit IO (`io.adafruit.com`); only the client library changes.
- Changing the wire format. We still subscribe to the full `{username}/feeds/{topic}` path (operator configures `MQTT_TOPIC` accordingly).
- Refactoring the paho client itself (`lib_shared/paho_mqtt_client.py`). It's a thin wrapper and the change is purely about deleting the AIO path.
- Adding per-platform MQTT features. The paho client already supports both publish (Flask) and subscribe (Flask + Pi) and the auto-reconnect loop in a daemon thread.
- Changing the message envelope shape (`MessageEnvelope`).
- Removing the WebSocket broker endpoint. The browser still needs the WS URL.

## Decisions

### 1. Delete `lib_shared/adafruit_mqtt_client.py`

**Decision:** Delete the file. No other module in the repo imports it after the factory is simplified.

**Rationale:** The AIO wrapper exists only to support the `MQTT_CLIENT = "adafruit"` branch. With that branch gone, the file has no callers. Leaving it as a dead module invites future imports ("oh, look, an MQTT client") that re-introduce the AIO dependency path. Deleting is the only clean end state.

**Alternatives considered:**

- *Keep the file with a `# DEPRECATED` comment.* Dead code with a warning is still dead code. New contributors won't read the comment and will use it.
- *Keep it as a stub that raises `NotImplementedError` on construction.* A guard rail, but it adds a module to maintain for no production benefit. The "no AIO import" property is enforced by the spec's `rg` scenarios, not by the factory; we don't need a runtime guard on top of that.

### 2. Delete `lib_shared/mqtt_factory.py`; import `PahoMqttClient` directly

**Decision:** Delete the factory. Both `heart-message-manager/main.py` and `heart-matrix-controller/main.py` import `PahoMqttClient` from `lib_shared.paho_mqtt_client` directly and construct it with `_message_mgr.dispatch` (Pi) or `_noop_dispatch` (Flask) inline.

**Rationale:** With one client to choose from, the factory no longer has a choice to make. A one-line wrapper that just re-exports the constructor adds an indirection layer with no logic. The constructor takes a single positional argument; calling it at the two import sites is no less readable than calling `make_mqtt_client(dispatch_callback)`. A 2-line module that exists only to rename a class on import is a code smell — it's a place to "add config" later, and that "later" is the path back to the AIO selector we just removed.

**Alternatives considered:**

- *Keep the factory as a one-liner.* Smaller diff (no import changes at the call sites), but the factory exists to make a choice; with no choice to make, the file's only purpose is to be a "future home" for choices we don't want to make. Delete it now so the next contributor has to consciously re-introduce the indirection.
- *Keep the factory as a multi-client selector with a hardcoded `"paho"` constant.* The `if which == "adafruit"` branch is dead; preserving it as a constant in the body is dead-code-by-construction. Doesn't reduce maintenance surface.

### 3. Remove `adafruit-io` from `requirements.txt`

**Decision:** Delete the `# Adafruit IO MQTT — Heroku prod (MQTT_CLIENT="adafruit")` line. The `paho-mqtt` line stays.

**Rationale:** The `adafruit-io` package transitively pulls in `adafruit_minimqtt` and a few other AIO dependencies. With the wrapper gone, the package is unused. Heroku installs from this file (`pip install -r requirements.txt` is in the standard Python buildpack), so removing the line shrinks the slug by the AIO dep tree and removes a class of "version conflict on Heroku" failures.

**Alternatives considered:**

- *Keep `adafruit-io` as a "just in case" dependency.* Adds ~1MB to the Heroku slug for no caller. If a future contributor needs it back, they can re-add it with a commit message explaining why.
- *Move `adafruit-io` to a comment in `requirements.txt` listing why it was removed.* Comments in `requirements.txt` are read by humans, not the build system. A git history `git log -p -- requirements.txt` already records why.

### 4. Simplify `_derive_mqtt_ws_url()` in `heart-message-manager/main.py`

**Decision:** Drop the `if mqtt_client == "adafruit": return "wss://io.adafruit.com/mqtt"` branch. The function becomes:

```python
def _derive_mqtt_ws_url() -> str:
    explicit = _cfg.if_exists("MQTT_WS_URL")
    if explicit:
        return explicit
    host = _cfg.if_exists("MQTT_HOST") or "127.0.0.1"
    return f"wss://{host}/mqtt"  # or ws://, see below
```

Actually, the safer move is to preserve the existing local-vs-prod heuristic: if `MQTT_HOST` is `127.0.0.1` or `localhost`, use `ws://`; otherwise `wss://`. The current `_derive_mqtt_ws_url` already encodes the `wss://io.adafruit.com/mqtt` for Heroku via the `MQTT_CLIENT == "adafruit"` branch. After the change, the heuristic is just "is `MQTT_HOST` loopback? → `ws://` else → `wss://`". This is a one-liner check on the host string.

**Rationale:** The browser-side CSP needs the right scheme. Local dev runs a Mosquitto container with `ws://` on port 9002. Heroku runs against Adafruit IO with `wss://` on 443. We need both code paths to keep working. The `MQTT_CLIENT` selector was the old way to disambiguate; with it gone, the host string is the disambiguator.

**Alternatives considered:**

- *Force operators to set `MQTT_WS_URL` explicitly.* Document the change in the migration and remove the heuristic. Brittle — operators who don't read the migration notes get a broken preview page.
- *Always use `MQTT_WS_URL` if set, else default to `wss://io.adafruit.com/mqtt` (the prod case).* Removes the local-dev happy path. The Pi local-dev workflow (running Flask + Mosquitto locally) breaks unless operators override.
- *Keep the `MQTT_CLIENT = "adafruit"` branch as a no-op.* A no-op that returns the right URL is a hidden no-op — future contributors will think the branch is still load-bearing. Either it's load-bearing and stays, or it goes.

### 5. Update `heart-message-manager/settings.toml.example`

**Decision:** Remove the `MQTT_CLIENT = "adafruit"` line and the three-line comment block that introduces it ("# MQTT client: 'adafruit' (Adafruit IO MQTT with TLS) or 'paho' (raw paho-mqtt, for local dev)"). The `MQTT_HOST` / `MQTT_PORT` / `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_TOPIC` keys stay. The `MQTT_WS_URL` comment is updated to drop the "Adafruit IO: `wss://io.adafruit.com/mqtt`" reference and just say "operator-overridable; default derived from `MQTT_HOST`".

**Rationale:** Operator-facing config should not mention removed options. A future operator reading the example should not be confused into setting `MQTT_CLIENT = "adafruit"` and getting an `IfExistsError` on the now-unused key.

**Alternatives considered:**

- *Leave `MQTT_CLIENT` in the example as `# (removed; paho is the only client)`.* The `settings.toml` example is a copy-paste template. Keeping removed keys as comments is noise.
- *Move the docstring to `CLAUDE.md`.* The "removed config keys" list is implementation history, not operator-facing.

### 6. Update `heart-matrix-controller/main.py` comment

**Decision:** The comment `# Platform MQTT client (paho on the Pi; adafruit available via MQTT_CLIENT)` becomes `# Platform MQTT client (paho on every platform)`.

**Rationale:** Single-line comment update for consistency. The Pi's `main.py` is the only place that comments on the cross-platform split. The comment is otherwise unchanged — paho was already the Pi's client.

**Alternatives considered:**

- *Delete the comment entirely.* The next reader can `git blame` to find the history. Marginal — comments are cheap and a one-liner explaining "paho, every platform" is informative for new contributors.

### 7. No dedicated test for the paho client or the import surface

**Decision:** Don't add a new test file for this change. The "no AIO import" property is enforced by the `rg` checks in tasks 8.1 (every scenario in `specs/mqtt-paho-client/spec.md` is either a `rg` command or an `ls` command — no test framework required). The `PahoMqttClient` constructor is a 1-line wrapper around the paho library; testing it would be testing paho.

**Rationale:** The original design included `tests/mqtt_factory_test.py` to assert two contracts of the factory ("returns paho" and "doesn't pull in adafruit"). With the factory removed, both contracts are static properties of the import graph — `from lib_shared.paho_mqtt_client import PahoMqttClient` either succeeds or it doesn't, and a `rg` check on `Adafruit_IO` either matches or it doesn't. A pytest that calls `PahoMqttClient(lambda x: None)` would assert that paho is installed and the import resolves — neither of which is a meaningful test for our codebase.

**Alternatives considered:**

- *Keep `tests/mqtt_factory_test.py` as a smoke test for the paho import path.* Renamed to `tests/mqtt_paho_client_test.py`. The test would assert `from lib_shared.paho_mqtt_client import PahoMqttClient` succeeds. This is testing Python's import machinery, not our code. Skip.
- *Add a test that mocks paho and asserts `PahoMqttClient` calls into it correctly.* Mock-heavy tests of a thin third-party wrapper assert that the wrapper passes arguments through. The behavior is one line of glue code; the test would be longer than the code under test. Skip.

## Risks / Trade-offs

- **Heroku `MQTT_CLIENT` config var becomes a no-op.** If the operator has it set to `"adafruit"` in the Heroku dashboard, the new code ignores it. The operator's preview will still work (the `_derive_mqtt_ws_url` change still defaults to the right URL for prod). Document the removal in the commit message and update the Heroku config docs if any exist. No code change is needed for backward compat.
- **The AIO library's topic translation goes away.** This is a feature, not a bug — but it does mean `MQTT_TOPIC` must be the full wire-format path (`{username}/feeds/{topic}`) on the operator's side. The local-dev path already works this way (operators set `MQTT_TOPIC` to the full path), and the Heroku `settings.toml` example already shows the full path. No operator-facing change.
- **The "is it `ws://` or `wss://`?" heuristic in `_derive_mqtt_ws_url` is a string check on `MQTT_HOST`.** Operators using a non-Adafruit-IO broker with a non-loopback host (e.g., a public IP) will get `wss://<host>/mqtt`, which is the right default for TLS. Operators using a public broker with `ws://` (unusual) need to set `MQTT_WS_URL` explicitly. This is no worse than the current `MQTT_CLIENT = "adafruit"` selector, which also required the operator to set the right key.
- **The "no AIO import" property is enforced by `rg` checks, not by a runtime test.** This is a one-line regression net ("run `rg 'from Adafruit_IO' .` and confirm zero matches"), but it's the right tool — a test that asserts the same property would only catch regressions after a test run, not at edit time, and would add a pytest that exists only to assert static properties of the import graph.
- **AIO-specific debug logs go away.** The `AdafruitMqttClient` class logs AIO-specific connection events ("AdafruitMqttClient connected, subscribing to {username}/{feed}"). The paho client logs the same events under different names. Operators tailing logs to debug a connection failure will see slightly different output. The `PahoMqttClient` log line already includes the topic and username, so the disambiguation is preserved.
- **The browser doesn't change.** This is intentional and called out in the proposal and the design. The browser was never the issue.

## Migration Plan

1. Land `lib_shared/adafruit_mqtt_client.py` (deleted) + `lib_shared/mqtt_factory.py` (deleted) + `requirements.txt` (`adafruit-io` removed) + `heart-message-manager/main.py` (drop `make_mqtt_client` import, import `PahoMqttClient` directly; `_derive_mqtt_ws_url` simplified; comment updated) + `heart-message-manager/settings.toml.example` (`MQTT_CLIENT` removed) + `heart-matrix-controller/main.py` (drop `make_mqtt_client` import, import `PahoMqttClient` directly; comment only) in a single commit.
2. Verify locally: `python heart-message-manager/main.py` boots, Twilio webhook test (`curl -X POST .../api/messages -d "..."`) publishes a message and the Pi (or a local `paho-mqtt` subscriber script) receives it.
3. Deploy to Heroku. The Heroku config var `MQTT_CLIENT` (if set) becomes a no-op; remove it from the Heroku dashboard manually.
4. Verify on Heroku: send a test SMS, confirm the message appears in the admin UI and the live MQTT topic.
5. Verify the Pi: `sudo python3 heart-matrix-controller/main.py` boots, subscribes, and scrolls a seeded message.
6. Verify the browser: open `/preview` (or any admin page), confirm the WebSocket connects (look for "Connected" in the dev console status indicator).

Rollback is a single `git revert` of the commit, plus restoring the `adafruit-io` line in `requirements.txt` and the `MQTT_CLIENT` line in the Heroku config.

## Open Questions

None blocking. The one judgment call is the `ws://` vs `wss://` heuristic in `_derive_mqtt_ws_url` — the design defaults to "loopback → `ws://`, else `wss://`", which matches the current behavior for both Heroku (non-loopback → `wss://`) and local dev (loopback → `ws://`). Operators with an unusual broker can set `MQTT_WS_URL` explicitly, which has always been supported.
