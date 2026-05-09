## ADDED Requirements

### Requirement: Webhook receives SMS from Twilio

`POST /api/messages` SHALL accept Twilio's form-encoded webhook with `From`, `Body`, and `To` fields.

#### Scenario: Valid SMS received
- **WHEN** Twilio POSTs `From=+15551234567&Body=hello&To=+15559999999`
- **THEN** the system logs to S3, responds to Twilio with TwiML, then stores to SQLite and publishes to Adafruit IO

#### Scenario: Empty body is ignored
- **WHEN** Twilio POSTs with `Body=` (empty)
- **THEN** the system returns HTTP 204 No Content without storing

### Requirement: S3 is the source of truth for messages

The system SHALL append every inbound message to S3 before responding to Twilio. Flask rebuilds SQLite from S3 on restart.

#### Scenario: Message logged to S3 before response
- **WHEN** Twilio POSTs a valid SMS
- **THEN** the message is appended to the S3 log, then the TwiML response is returned

#### Scenario: Flask rebuilds SQLite from S3 on startup
- **WHEN** Flask starts
- **THEN** it reads all messages from S3 and repopulates SQLite

### Requirement: Webhook stores to SQLite and publishes to Adafruit after responding

The system SHALL store the message to SQLite and publish to Adafruit IO only after responding to Twilio (to minimize webhook response latency).

#### Scenario: Storage and publish after response
- **WHEN** Twilio POSTs a valid SMS
- **THEN** after returning TwiML, the message is stored to SQLite and published to Adafruit IO MQTT

### Requirement: All messages are stored regardless of sender

All inbound messages are stored to SQLite and S3. The allowed_senders config is used by the filter engine, not at storage time.

#### Scenario: Message from unknown sender is stored
- **WHEN** a message arrives from a phone number not in `config.allowed_senders`
- **THEN** the message is still stored to SQLite and S3

### Requirement: TwiML reply is personalized

The TwiML response SHALL include the sender's message body, HTML-escaped.

#### Scenario: Reply contains message
- **WHEN** Twilio POSTs `Body=Hello+World`
- **THEN** the TwiML response contains "Lindsay's Heart got your message: Hello World"
