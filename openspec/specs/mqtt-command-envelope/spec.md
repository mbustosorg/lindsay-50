# mqtt-command-envelope Specification

## Purpose
TBD - created by archiving change self-upgrading-matrix-controller. Update Purpose after archive.
## Requirements
### Requirement: MessageEnvelope supports the command type
The `MessageEnvelope` class MUST accept `type="command"` with a payload dict. The `from_json` and `to_json` constructors MUST round-trip the new type without modification (the type field is already a free-form string).

#### Scenario: Command envelope serializes and deserializes
- **WHEN** code constructs `MessageEnvelope("command", {"action": "check-for-update"})` and calls `to_json`
- **THEN** output is `{"type":"command","payload":{"action":"check-for-update"}}` and `from_json` round-trips it identically

### Requirement: MessageManager dispatches command envelopes to registered handlers
`MessageManager.dispatch` MUST route envelopes with `type="command"` to a handler looked up from the `command_handlers` mapping by the payload's `action` field. Unknown command actions and missing handlers MUST be logged and dropped without side effects.

The `command_handlers` mapping is supplied at construction time (and may be extended via `register_command_handler`); it MUST default to an empty mapping. The mapping returned by `MessageManager.command_handlers` MUST be a copy of the internal state (callers cannot mutate it).

#### Scenario: Valid check-for-update command
- **WHEN** dispatcher receives `{"type":"command","payload":{"action":"check-for-update"}}` and a `check-for-update` handler is registered
- **THEN** dispatcher invokes the registered handler with the envelope's payload

#### Scenario: Unknown command action
- **WHEN** dispatcher receives `{"type":"command","payload":{"action":"dance"}}` and no `dance` handler is registered
- **THEN** dispatcher logs a warning and takes no action

#### Scenario: Missing or malformed payload
- **WHEN** dispatcher receives `{"type":"command","payload":null}` or `{"type":"command"}`
- **THEN** dispatcher logs a warning and takes no action

#### Scenario: Handler raises an exception
- **WHEN** the registered handler raises (any exception) while being invoked
- **THEN** dispatcher logs the error and continues running (does NOT crash, does NOT propagate to MQTT loop)

### Requirement: Existing envelope types continue to work
Adding the `type=command` branch MUST NOT change the dispatch behavior for `type=message` or `type=config`.

#### Scenario: Message envelope still routes to message handler
- **WHEN** dispatcher receives `{"type":"message","payload":{...}}`
- **THEN** it routes to the existing message handler (unchanged behavior)

#### Scenario: Config envelope still routes to config handler
- **WHEN** dispatcher receives `{"type":"config","payload":{...}}`
- **THEN** it routes to the existing config handler (unchanged behavior)

### Requirement: Force-upgrade, restart, and shutdown are routed through the existing command dispatcher

`MessageManager.dispatch` MUST recognize three additional `action` values:

- `force-upgrade`
- `restart`
- `shutdown`

Each value MUST be looked up in `command_handlers` by exact match. Unknown actions and missing handlers MUST continue to be logged and dropped without side effects (matching the existing requirement for `dance`-style unknowns). The envelope's overall shape and JSON contract MUST NOT change — `{"type":"command","payload":{"action":"<name>"}}` is the only supported shape.

Handlers for these three new actions MUST be registered at Pi boot in `heart-matrix-controller/main.py:register_command_handlers`. The default handler mapping MUST continue to be empty (Flask and Pi each register what they need at startup; no shared registry).

The existing `check-for-update` action registration is RETAINED in this change (kept on the Pi alongside the three new ones). It will be removed in a follow-up change once all deployed Pis have rolled forward to the new `GET /api/sign/settings` boot-fetch path. This parallel registration allows the new code to upgrade an old Pi (the old Pi still understands `check-for-update`), and allows the old Pi's last-known-good behavior to continue operating in parallel with the new flow.

#### Scenario: Pi boots with the three new handlers registered, check-for-update also registered

- **WHEN** `heart-matrix-controller/main.py` runs its startup
- **THEN** `command_handlers` contains entries for `force-upgrade`, `restart`, `shutdown`, AND `check-for-update` (the latter retained for transitional compatibility)

#### Scenario: Flask publishes a force-upgrade envelope and the dispatcher routes it

- **WHEN** Flask publishes `{"type":"command","payload":{"action":"force-upgrade"}}` and the Pi's `MessageManager` receives it
- **THEN** the dispatcher routes the envelope to the registered `force-upgrade` handler and no other handler is invoked

#### Scenario: Flask publishes a restart envelope and the dispatcher routes it

- **WHEN** Flask publishes `{"type":"command","payload":{"action":"restart"}}`
- **THEN** the dispatcher routes the envelope to the registered `restart` handler

#### Scenario: Flask publishes a shutdown envelope and the dispatcher routes it

- **WHEN** Flask publishes `{"type":"command","payload":{"action":"shutdown"}}`
- **THEN** the dispatcher routes the envelope to the registered `shutdown` handler

#### Scenario: Flask publishes a future-unknown command envelope

- **WHEN** Flask publishes `{"type":"command","payload":{"action":"future-unknown"}}` and no handler is registered
- **THEN** the dispatcher logs a warning and takes no action (matching the existing unknown-action contract)

### Requirement: Command handler exceptions are isolated and logged

Each of the three new handlers MUST be allowed to raise without crashing the MQTT loop or the render loop. The existing handler-isolation contract from the `self-upgrading-matrix-controller` spec MUST apply to all three new actions:

- The dispatcher MUST catch and log any exception raised by `force-upgrade`, `restart`, or `shutdown` handlers.
- The dispatcher MUST NOT propagate the exception to the paho MQTT loop.
- The Pi MUST continue running its render loop unaffected.

#### Scenario: Force-upgrade handler raises an exception

- **WHEN** `force-upgrade` handler raises (e.g. `OSError` from `os.execvpe`)
- **THEN** the dispatcher logs the error and continues; the MQTT loop and render loop are unaffected

#### Scenario: Restart handler raises an exception (e.g. missing sudo permission)

- **WHEN** `restart` handler's `subprocess.run(["sudo", "reboot"], check=False)` returns non-zero or raises
- **THEN** the dispatcher logs the error, the Pi's render loop continues, and the operator sees no pill change on the dashboard

#### Scenario: Shutdown handler raises an exception

- **WHEN** `shutdown` handler's `subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)` raises
- **THEN** the dispatcher logs the error; render loop continues; Pi remains online

### Requirement: Existing message and config envelope types are unchanged

The addition of three new command actions MUST NOT modify the dispatch behavior for `type=message` or `type=config`. Both MUST continue to route to their existing handlers via the existing dispatch contract. The `type=config` envelope is now also the carrier for the `sign.target_version` field (per `pi-upgrade-settings` spec), so the config handler MUST be extended to accept and cache that field — but its routing and dispatch contract are unchanged.

#### Scenario: Message envelope still routes to message handler

- **WHEN** dispatcher receives `{"type":"message","payload":{...}}` after the new handlers are registered
- **THEN** it routes to the existing message handler (unchanged behavior)

#### Scenario: Config envelope still routes to config handler

- **WHEN** dispatcher receives `{"type":"config","payload":{...}}` after the new handlers are registered
- **THEN** it routes to the existing config handler (unchanged behavior); the config handler reads the new `sign.target_version` field if present (else falls through to Flask's running SHA) and caches it for the loader's next check

### Requirement: Loader reads resolved target from GET /api/sign/settings on boot

The Pi's loader MUST call `GET /api/sign/settings` (auth via `X-API-Key`, 5-second timeout) on boot, parse the response's `target_version` field (a 7-character short SHA, always concrete on the wire), and use it as the upgrade target. The local-vs-target comparison MUST be `short_sha(local_git_rev_parse_head) == target_version` (was `local == expected_sha`). On any failure (network error, HTTP 5xx, malformed JSON, missing `target_version` field), the loader MUST fall through to running the existing `current/.../main.py` without upgrade.

The existing `GET /api/sign/boot-config` endpoint MUST remain unchanged and continue to serve legacy Pis (it returns `{expected_sha, short_sha}`); new code uses `/api/sign/settings` exclusively.

#### Scenario: Loader boots with GET /api/sign/settings succeeding

- **WHEN** the Pi boots and the endpoint returns `{"target_version": "abc1234", "timezone": "US/Pacific"}`
- **THEN** the loader parses `target_version = "abc1234"`, computes `local = short_sha("git rev-parse HEAD")` (7-char truncation), compares `local == "abc1234"`; if they differ, the loader stages from `abc1234` and probes the swap; if they match, the loader logs "already at target" and execs `current/.../main.py`

#### Scenario: Loader boots with GET /api/sign/settings failing (network/HTTP error)

- **WHEN** the Pi boots and the endpoint request times out, returns 5xx, or returns malformed JSON
- **THEN** the loader logs the failure and falls through to running `current/.../main.py` — no upgrade is attempted, the Pi continues rendering on its currently-active version (safe default; matches today's `boot-config` failure behavior)

#### Scenario: Legacy Pi calls the existing /api/sign/boot-config endpoint

- **WHEN** a pre-this-change Pi boots and calls `GET /api/sign/boot-config`
- **THEN** the existing endpoint returns `{"expected_sha": "<flask-sha>", "short_sha": "<short>"}` unchanged; the legacy Pi upgrades using its existing logic (no regression)

