## ADDED Requirements

### Requirement: MessageEnvelope supports the command type
The `MessageEnvelope` class MUST accept `type="command"` with a payload dict. The `from_json` and `to_json` constructors MUST round-trip the new type without modification (the type field is already a free-form string).

#### Scenario: Command envelope serializes and deserializes
- **WHEN** code constructs `MessageEnvelope("command", {"action": "reboot"})` and calls `to_json`
- **THEN** output is `{"type":"command","payload":{"action":"reboot"}}` and `from_json` round-trips it identically

### Requirement: MessageManager dispatches command envelopes
`MessageManager.dispatch` MUST route envelopes with `type="command"` to a command handler. Unknown command actions MUST be logged and dropped without side effects.

#### Scenario: Valid reboot command
- **WHEN** dispatcher receives `{"type":"command","payload":{"action":"reboot"}}`
- **THEN** command handler runs `os.system("sudo reboot")` and the Pi reboots within ~5 seconds

#### Scenario: Unknown command action
- **WHEN** dispatcher receives `{"type":"command","payload":{"action":"dance"}}`
- **THEN** command handler logs a warning and takes no action

#### Scenario: Missing or malformed payload
- **WHEN** dispatcher receives `{"type":"command","payload":null}` or `{"type":"command"}`
- **THEN** command handler logs a warning and takes no action

### Requirement: Existing envelope types continue to work
Adding the `type=command` branch MUST NOT change the dispatch behavior for `type=message` or `type=config`.

#### Scenario: Message envelope still routes to message handler
- **WHEN** dispatcher receives `{"type":"message","payload":{...}}`
- **THEN** it routes to the existing message handler (unchanged behavior)

#### Scenario: Config envelope still routes to config handler
- **WHEN** dispatcher receives `{"type":"config","payload":{...}}`
- **THEN** it routes to the existing config handler (unchanged behavior)