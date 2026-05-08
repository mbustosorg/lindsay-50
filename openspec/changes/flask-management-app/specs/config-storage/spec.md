## ADDED Requirements

### Requirement: Config is stored as JSON in SQLite

The system SHALL store config as a single JSON blob in a `config` SQLite table with columns `(key TEXT PRIMARY KEY, value TEXT NOT NULL)`. The active config has `key = "current"`.

#### Scenario: Config is retrievable
- **WHEN** `storage.get_config()` is called
- **THEN** it returns the parsed JSON from the row with `key = "current"`, or a default config if no row exists

#### Scenario: Config is updatable
- **WHEN** `storage.put_config(json_blob)` is called
- **THEN** it upserts the row `("current", <json_string>)` in the config table

### Requirement: Config schema is versioned

The config JSON SHALL contain a top-level integer `version` field. The value starts at 1.

#### Scenario: New config has version field
- **WHEN** a new config is created (no existing row)
- **THEN** the config JSON includes `"version": 1`

#### Scenario: Config version is preserved on save
- **WHEN** `storage.put_config()` is called with a config containing `"version": 1`
- **THEN** the stored JSON retains `"version": 1`

### Requirement: Config supports allowed_senders

The config JSON SHALL contain an `allowed_senders` array of objects with `name` (string) and `phone` (string, E.164) fields.

#### Scenario: Sender lookup by phone
- **WHEN** `config["allowed_senders"]` is a list of `{name, phone}` objects
- **THEN** the admin UI can display the sender name given a phone number

### Requirement: Config supports filters

The config JSON SHALL contain a `filters` array. Each entry has `type` (string), `pattern` (string), and `action` (string, always `"suppress"` in v1).

#### Scenario: Filter list is serializable
- **WHEN** `storage.put_config()` is called with a config containing filters
- **THEN** the filters array is preserved verbatim in SQLite

### Requirement: Config supports rendering settings

The config JSON SHALL contain a `rendering` object with `mode` (string), `speed` (float), and `color` (int, 0xRRGGBB).

#### Scenario: Rendering defaults are stored
- **WHEN** config is saved with `rendering.mode = "scroll"`, `rendering.speed = 0.04`, `rendering.color = 16711680`
- **THEN** those values are retrievable from storage

### Requirement: Config supports sign metadata

The config JSON SHALL contain a `sign` object with at least a `name` (string) field.

#### Scenario: Sign name is stored
- **WHEN** `config.sign.name` is set to `"Lindsay's Heart"`
- **THEN** it is preserved in SQLite and retrievable
