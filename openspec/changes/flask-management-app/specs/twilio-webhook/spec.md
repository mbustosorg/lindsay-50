## ADDED Requirements

### Requirement: Webhook receives SMS from Twilio

`POST /api/messages` SHALL accept Twilio's form-encoded webhook with `From`, `Body`, and `To` fields.

#### Scenario: Valid SMS received
- **WHEN** Twilio POSTs `From=+15551234567&Body=hello&To=+15559999999`
- **THEN** the system stores the message and returns TwiML with a confirmation reply

#### Scenario: Empty body is ignored
- **WHEN** Twilio POSTs with `Body=` (empty)
- **THEN** the system returns HTTP 204 No Content without storing

### Requirement: Webhook stores before responding

The system SHALL store the message to SQLite before returning the TwiML response to Twilio.

#### Scenario: Storage completes before TwiML response
- **WHEN** Twilio POSTs a valid SMS
- **THEN** the message is committed to SQLite, then the TwiML response is returned (not the other way around)

### Requirement: Sender allowlist is enforced at storage time

If `config.allowed_senders` is non-empty, messages from phone numbers not in the list SHALL still be stored but marked as suppressed by a sender rule.

#### Scenario: Non-allowed sender
- **WHEN** `config.allowed_senders` is `[{phone: "+15551234567"}]` and a message from `+15550001111` arrives
- **THEN** the message is stored to SQLite AND a `type=sender` suppress rule for `+15550001111` is added to config

### Requirement: TwiML reply is personalized

The TwiML response SHALL include the sender's message body, HTML-escaped.

#### Scenario: Reply contains message
- **WHEN** Twilio POSTs `Body=Hello+World`
- **THEN** the TwiML response contains "Lindsay's Heart got your message: Hello World"
