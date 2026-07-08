## ADDED Requirements

### Requirement: Force-upgrade, restart, and shutdown are routed through the existing command dispatcher

`MessageManager.dispatch` MUST recognize three additional `action` values alongside the existing `check-for-update`:

- `force-upgrade`
- `restart`
- `shutdown`

Each value MUST be looked up in `command_handlers` by exact match. Unknown actions and missing handlers MUST continue to be logged and dropped without side effects (matching the existing requirement for `dance`-style unknowns). The envelope's overall shape and JSON contract MUST NOT change — `{"type":"command","payload":{"action":"<name>"}}` is the only supported shape.

Handlers for these three new actions MUST be registered at Pi boot in `heart-matrix-controller/main.py:register_command_handlers`. The default handler mapping MUST continue to be empty (Flask and Pi each register what they need at startup; no shared registry).

#### Scenario: Pi boots with all four handlers registered
- **WHEN** `heart-matrix-controller/main.py` runs its startup
- **THEN** `command_handlers` contains entries for `check-for-update`, `force-upgrade`, `restart`, and `shutdown`

#### Scenario: Flask publishes a force-upgrade envelope and the dispatcher routes it
- **WHEN** Flask publishes `{"type":"command","payload":{"action":"force-upgrade"}}` and the Pi's `MessageManager` receives it
- **THEN** the dispatcher routes the envelope to the registered `force-upgrade` handler and the existing `check-for-update` handler is unaffected

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

The addition of three new command actions MUST NOT modify the dispatch behavior for `type=message` or `type=config`. Both MUST continue to route to their existing handlers via the existing dispatch contract.

#### Scenario: Message envelope still routes to message handler
- **WHEN** dispatcher receives `{"type":"message","payload":{...}}` after the new handlers are registered
- **THEN** it routes to the existing message handler (unchanged behavior)

#### Scenario: Config envelope still routes to config handler
- **WHEN** dispatcher receives `{"type":"config","payload":{...}}` after the new handlers are registered
- **THEN** it routes to the existing config handler (unchanged behavior)
