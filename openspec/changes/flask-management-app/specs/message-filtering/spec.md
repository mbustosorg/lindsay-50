## ADDED Requirements

### Requirement: Filter rules are evaluated in order

The system SHALL evaluate filter rules against a message in the order they appear in `config.filters`. The first matching rule with `action = "suppress"` suppresses the message. If no rule matches, the message is displayed.

#### Scenario: Keyword suppress
- **WHEN** `filters.apply(message, config)` is called with a message whose body contains "badword" and config has `{type: "keyword", pattern: "badword", action: "suppress"}`
- **THEN** the message is marked suppressed

#### Scenario: Keyword is case-insensitive
- **WHEN** the rule is `keyword / "BADWORD"` and the message body is "badword"
- **THEN** the message is suppressed (case-insensitive match)

#### Scenario: Regex suppress
- **WHEN** `filters.apply(message, config)` is called with a message body of "   " and config has `{type: "regex", pattern: "^\\s*$", action: "suppress"}`
- **THEN** the message is suppressed

#### Scenario: Sender suppress
- **WHEN** `filters.apply(message, config)` is called with sender "+15550001111" and config has `{type: "sender", pattern: "+15550001111", action: "suppress"}`
- **THEN** the message is suppressed

#### Scenario: Message UUID suppress
- **WHEN** `filters.apply(message, config)` is called with message id "abc-123" and config has `{type: "message", pattern: "abc-123", action: "suppress"}`
- **THEN** the message is suppressed

#### Scenario: No matching rule
- **WHEN** `filters.apply(message, config)` is called and no filter rule matches
- **THEN** the message is NOT suppressed

### Requirement: display_list returns filtered messages in order

`filters.display_list(messages, config)` SHALL return all messages where `apply()` returned False, ordered by `received_at` ascending.

#### Scenario: Only non-suppressed returned
- **WHEN** `filters.display_list([msg1, msg2, msg3], config)` is called where msg2 is suppressed
- **THEN** the result is `[msg1, msg3]`

#### Scenario: Ordered by received_at ascending
- **WHEN** messages arrive out of order
- **THEN** `display_list` returns them sorted by `received_at` ascending

### Requirement: Filter logic is identical on Flask and ESP32

The `lib/filters.py` module SHALL produce the same suppression decisions on Flask (using Python `re`) and on ESP32 (using CircuitPython `ure`).

#### Scenario: Python regex engine
- **WHEN** `filters.apply(msg, config)` runs on Flask with a regex rule
- **THEN** it uses Python's `re` module for pattern matching

#### Scenario: CircuitPython regex engine
- **WHEN** `filters.apply(msg, config)` runs on ESP32 with a regex rule
- **THEN** it uses CircuitPython's `ure` module for pattern matching

#### Scenario: Identical results
- **WHEN** the same message and config are evaluated on Flask and ESP32
- **THEN** both return the same suppression result
