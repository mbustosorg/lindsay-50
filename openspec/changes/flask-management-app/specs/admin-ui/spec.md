## ADDED Requirements

### Requirement: Dashboard shows recent activity

`GET /` SHALL display the 20 most recent messages, total message count, and per-filter suppression counts.

#### Scenario: Dashboard loads
- **WHEN** a user visits `/`
- **THEN** the page shows a list of the 20 most recent messages with sender, body preview, and timestamp

### Requirement: Message list is paginated

`GET /messages` SHALL display messages in pages of 50. Query params `?page=N` control which page.

#### Scenario: Pagination navigation
- **WHEN** a user visits `/messages?page=2`
- **THEN** messages 51–100 are displayed with navigation to page 1 and 3

### Requirement: Individual messages can be suppressed

`POST /api/messages/{id}/suppress` SHALL add a `type=message` filter rule for the given message UUID. `POST /api/messages/{id}/unsuppress` SHALL remove it.

#### Scenario: Suppress a message
- **WHEN** a user POSTs to `/api/messages/abc-123/suppress`
- **THEN** a filter rule `{type: "message", pattern: "abc-123", action: "suppress"}` is added to the config and the message is marked suppressed

#### Scenario: Unsuppress a message
- **WHEN** a user POSTs to `/api/messages/abc-123/unsuppress`
- **THEN** any existing `type=message` filter rule with pattern "abc-123" is removed from the config

### Requirement: Filter rules can be managed

`GET /filters` SHALL list all filter rules. `POST /filters` SHALL add a rule. `DELETE /filters/{index}` SHALL remove a rule by array index.

#### Scenario: Add keyword filter
- **WHEN** a user submits `{type: "keyword", pattern: "spam", action: "suppress"}` to `POST /filters`
- **THEN** the rule is appended to `config.filters` and persisted

#### Scenario: Delete filter rule
- **WHEN** a user POSTs to `DELETE /filters/2`
- **THEN** the rule at index 2 is removed from `config.filters`

### Requirement: Settings page allows config editing

`GET /settings` SHALL display forms for allowed_senders, rendering defaults, and sign name. `PUT /api/config` SHALL update the config.

#### Scenario: Update sign name
- **WHEN** a user sets sign name to "Lindsay's Heart" and submits
- **THEN** `config.sign.name` is updated in SQLite

#### Scenario: Add allowed sender
- **WHEN** a user adds sender `{name: "Alice", phone: "+15551234567"}` and submits
- **THEN** the entry is added to `config.allowed_senders`

### Requirement: Preview page shows filtered display list

`GET /preview` SHALL call `filters.display_list()` with all messages and the current config, and display the result.

#### Scenario: Preview matches ESP32 output
- **WHEN** a user visits `/preview`
- **THEN** the displayed message list matches exactly what `filters.display_list(storage.get_all_messages(), storage.get_config())` returns

### Requirement: Config changes are published

When config is changed via the admin UI (settings, filters, suppress), the system SHALL publish the updated config JSON to the ESP32 communication layer.

#### Scenario: Filter change triggers publish
- **WHEN** a user adds a filter rule via `/filters`
- **THEN** the new config JSON is published to the ESP32 (via MQTT or HTTP, depending on the communication decision)
