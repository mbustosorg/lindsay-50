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

