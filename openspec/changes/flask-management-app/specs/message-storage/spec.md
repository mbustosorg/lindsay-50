## ADDED Requirements

### Requirement: Messages are stored with full metadata

The system SHALL store every inbound SMS with a UUID v4 identifier, sender phone number (E.164), message body, and ISO 8601 timestamp.

#### Scenario: Message stored on webhook
- **WHEN** Twilio POSTs to `/api/messages`
- **THEN** the system generates a UUID v4, stores `{id, sender, body, received_at}` in SQLite, and returns TwiML

#### Scenario: Messages are retrievable by time
- **WHEN** `storage.get_messages_since(timestamp)` is called
- **THEN** it returns all messages with `received_at` strictly after the given timestamp, ordered by `received_at` ascending

#### Scenario: Individual message is retrievable
- **WHEN** `storage.get_message(id)` is called
- **THEN** it returns the single message with that id, or None

### Requirement: Message storage is replicated to R2

The system SHALL use Litestream to continuously replicate SQLite WAL to Cloudflare R2. On restart, Litestream SHALL restore from R2 before Flask starts.

#### Scenario: Litestream restores on startup
- **WHEN** Flask starts (after crash or deploy)
- **THEN** Litestream restores the SQLite database from R2 before the web process begins

#### Scenario: WAL replicated during operation
- **WHEN** Flask writes a message to SQLite
- **THEN** Litestream streams the WAL change to R2 within the configured sync interval

### Requirement: ESP32 can store messages locally

The ESP32 SHALL append every received message to a local SQLite database with the same schema as Flask.

#### Scenario: ESP32 inserts received message
- **WHEN** the ESP32 receives a message via its communication layer
- **THEN** it inserts `{id, sender, body, received_at}` into its local SQLite database

#### Scenario: ESP32 queries messages by time
- **WHEN** `storage.get_messages_since(timestamp)` is called on ESP32
- **THEN** it returns all messages with `received_at` after the timestamp, ordered ascending
