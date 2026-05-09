## ADDED Requirements

### Requirement: Messages are stored with full metadata

The system SHALL store every inbound SMS with a UUID v4 identifier, sender phone number (E.164), message body, and ISO 8601 timestamp.

#### Scenario: Message stored on webhook
- **WHEN** Twilio POSTs to `/api/messages`
- **THEN** the system generates a UUID v4, stores `{id, sender, body, received_at}` in SQLite, and returns TwiML

#### Scenario: Messages are retrievable by time
- **WHEN** `storage.get_messages_since(timestamp)` is called
- **THEN** it returns all messages with `received_at` strictly after the given timestamp, ordered by `received_at` descending (most recent first)

#### Scenario: Individual message is retrievable
- **WHEN** `storage.get_message(id)` is called
- **THEN** it returns the single message with that id, or None

### Requirement: Message storage is logged to S3

The system SHALL append every inbound message to an S3 log as a durable backup. S3 log entries contain: `id`, `sender_number`, `sender_name` (if in allowed_senders), `body`, `received_at`.

#### Scenario: Message logged to S3 on webhook
- **WHEN** Twilio POSTs a valid SMS to `/api/messages`
- **THEN** the message is appended to the S3 log before the TwiML response is returned

#### Scenario: S3 log entry format
- **WHEN** a message is logged to S3
- **THEN** the entry contains `{id, sender_number, sender_name, body, received_at}` as JSON

#### Scenario: Flask rebuilds from S3 on restart
- **WHEN** Flask starts (after crash or deploy)
- **THEN** it reads the S3 log and repopulates SQLite with all logged messages

### Requirement: Config is snapshotted to S3 on change

The system SHALL save a timestamped config snapshot to S3 on every config change. Old snapshots are pruned (keep most recent 10).

#### Scenario: Config snapshot saved on change
- **WHEN** config is updated via the admin UI
- **THEN** a timestamped config snapshot is saved to S3 (e.g., `config/config-2026-05-08T120000.json`)

#### Scenario: Flask loads config from S3 on startup
- **WHEN** Flask starts
- **THEN** it finds the most recent config snapshot in S3 and loads it into SQLite

#### Scenario: Config snapshots are pruned
- **WHEN** more than 10 config snapshots exist in S3
- **THEN** the oldest snapshots are deleted (keep most recent 10)

### Requirement: ESP32 stores messages in memory

The ESP32 SHALL store every received message in an in-memory dict with UUID deduplication.

#### Scenario: ESP32 inserts received message
- **WHEN** the ESP32 receives a message via Adafruit IO MQTT
- **THEN** it inserts `{id, sender, body, received_at}` into its in-memory dict (keyed by UUID)

#### Scenario: ESP32 deduplicates by UUID
- **WHEN** a message with an existing UUID is received
- **THEN** the message is ignored (not added to the dict)

#### Scenario: ESP32 fetches message history on boot
- **WHEN** the ESP32 boots and connects to WiFi
- **THEN** it fetches message history from Adafruit IO HTTP and populates its in-memory dict
