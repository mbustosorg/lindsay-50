## ADDED Requirements

> **Note:** This capability covers the FilterRule mechanics (per-rule status, allowed types, migration). The interaction between FilterRule evaluation and the senders list (the master `text_settings.enforcement_enabled` toggle and per-entry `allowed` flag) is covered by the `senders-status` capability. FilterRule evaluation is independent of `cfg.text_settings.enforcement_enabled` — the rules run regardless of enforcement, and a rule that matches still suppresses a message even when enforcement is off.

### Requirement: Every FilterRule carries a status field with "enabled" default

`FilterRule` SHALL carry a `status` field on the wire and in memory. The status is the LIFECYCLE axis — whether the rule is "on" right now or muted without being deleted. The valid values SHALL be the literal strings `"enabled"` or `"disabled"`. The default for a new rule SHALL be `"enabled"`. A stored rule that lacks the `status` field on read SHALL be treated as `"enabled"` (the migration backfills the field; `from_dict` SHALL also accept the missing field silently so partial / legacy payloads still load).

The `status` field uses an enum rather than a boolean to keep the door open for future soft-delete states (e.g. `"archived"`) without breaking the wire.

#### Scenario: A new rule defaults to enabled
- **WHEN** the operator adds a keyword rule via the `/settings` page
- **THEN** `cfg.filters[<i>].status` SHALL equal `"enabled"`

#### Scenario: A stored rule without status loads as enabled
- **WHEN** `from_dict({"filters": [{"type": "keyword", "pattern": "spam"}], ...})` is called (no `status` key)
- **THEN** the parsed rule SHALL have `status == "enabled"` (back-compat default)

#### Scenario: The wire shape includes status
- **WHEN** `FilterRule.to_dict()` is called on a rule
- **THEN** the returned dict SHALL include the `status` field

#### Scenario: from_dict accepts both enabled and disabled statuses
- **WHEN** `FilterRule.from_dict({"type": "keyword", "pattern": "spam", "status": "disabled"})` is called
- **THEN** `rule.status` SHALL equal `"disabled"` (parsed from the wire, not the default)

### Requirement: FilterRule.action is suppress-only in v1

`FilterRule.action` SHALL be the literal string `"suppress"` as the only v1 value. Every rule, regardless of `type`, SHALL produce a suppression outcome when matched. `SignConfig.from_dict` SHALL reject any value other than `"suppress"` for the `action` field (raise `ValueError`).

This is the v1 simplification: rules are always "suppress this kind of message." An `action="allow"` rule would conflict with the implicit-allow semantics of `SignConfig.senders` entries (which are the only allow mechanism), so it's deferred to a future change if a use case emerges.

#### Scenario: A rule's action is always suppress
- **WHEN** `FilterRule(type="keyword", pattern="spam").action` is queried
- **THEN** it SHALL equal `"suppress"`

#### Scenario: from_dict rejects an unrecognized action
- **WHEN** `FilterRule.from_dict({"type": "keyword", "pattern": "spam", "action": "allow"})` is called
- **THEN** the call SHALL raise `ValueError` (action="allow" is not in v1)

### Requirement: FilterRule.type is restricted to keyword, regex, message — sender type is REMOVED

`FilterRule.type` SHALL accept only the literal strings `"keyword"`, `"regex"`, `"message"` in v3. The `"sender"` type SHALL be REMOVED from the wire entirely — `SignConfig.from_dict` SHALL reject a rule with `type="sender"` (raise `ValueError`). Sender matching is the responsibility of `SignConfig.senders` (the single source of truth), not `FilterRule`.

Stored v2 configs with `type=sender` rules are migrated during the v2 → v3 upgrade (see the migration requirement below): each such rule is converted to an entry in `SignConfig.senders` and dropped from `filters`. After the migration, no `type=sender` rules exist in stored configs.

#### Scenario: A keyword rule is accepted
- **WHEN** `FilterRule.from_dict({"type": "keyword", "pattern": "spam"})` is called
- **THEN** the call SHALL succeed and `rule.type` SHALL equal `"keyword"`

#### Scenario: A regex rule is accepted
- **WHEN** `FilterRule.from_dict({"type": "regex", "pattern": "^spam$"})` is called
- **THEN** the call SHALL succeed and `rule.type` SHALL equal `"regex"`

#### Scenario: A message rule is accepted
- **WHEN** `FilterRule.from_dict({"type": "message", "pattern": "msg-12345"})` is called
- **THEN** the call SHALL succeed and `rule.type` SHALL equal `"message"`

#### Scenario: A sender rule is rejected
- **WHEN** `FilterRule.from_dict({"type": "sender", "pattern": "+15551234567"})` is called
- **THEN** the call SHALL raise `ValueError` (the type is REMOVED from the wire — sender matching is the senders list's job)

### Requirement: Disabled rules are skipped at apply time

`FilteredMessages._apply_filter` SHALL skip any rule where `rule.status == "disabled"` (the rule is treated as absent — it does NOT contribute to the suppressing list). The match logic for `type == "keyword"`, `type == "regex"`, and `type == "message"` is unchanged otherwise. There is no `type == "sender"` branch in `_matches` (sender matching moved to the senders list).

The "disabled" status is the "disable it vs. delete it" affordance the issue asks for: the operator can mute a rule without losing its definition.

#### Scenario: A disabled keyword rule does not suppress matching messages
- **WHEN** `cfg.filters` contains `{"type": "keyword", "pattern": "spam", "status": "disabled"}` and a message arrives containing `"spam"`
- **THEN** `entry.suppressed` SHALL be `False` (the disabled rule is skipped at apply time)

#### Scenario: A disabled keyword rule is skipped at apply time
- **WHEN** `_apply_filter` runs against a rule with `status="disabled"`
- **THEN** the rule SHALL NOT be returned in the suppressing list (it is treated as absent)

#### Scenario: An enabled keyword rule suppresses matching messages
- **WHEN** `cfg.filters` contains `{"type": "keyword", "pattern": "spam", "status": "enabled"}` and a message arrives containing `"spam"`
- **THEN** `entry.suppressed` SHALL be `True` (the rule fires normally)

### Requirement: Settings page renders the Filter Rules table with a per-row Status checkbox

The admin UI's `/settings` page SHALL render the existing Filter Rules table with a new `Status` column. The column SHALL appear between `Pattern` and `Action` (the current ordering is `Type | Pattern | Action | Delete`). The new ordering is `Type | Pattern | Status | Action | Delete`.

The `Status` column SHALL render as a **checkbox** for every row — NOT a dropdown, NOT a select, NOT a tri-state control. The checkbox SHALL be checked iff `cfg.filters[i].status == "enabled"` and unchecked iff `cfg.filters[i].status == "disabled"`. The control is binary because `FilterRule.status` only supports `"enabled"` and `"disabled"` right now — when (if) a future value like `"archived"` lands on the wire, the UI for that state will be a separate change. The default for new rows SHALL be `Enabled` (checkbox checked).

The "Add Rule" form SHALL also include a `Status` **checkbox** (NOT a dropdown) defaulting to checked. The form posts `filter_status` (checked value `"on"`) when the box is checked, and the field is absent when unchecked. The handler reads `request.form.get("filter_status") == "on"` to determine the new rule's status — checked → `"enabled"`, absent → `"disabled"`.

The Add Rule `Type` selector SHALL be the only dropdown in this form (it offers `keyword`, `regex`, `message` — a 3-way choice that can't be a checkbox). The `Action` selector in the Add Rule form SHALL be removed in v1 since `FilterRule.action` is hardcoded to `"suppress"` (the only valid v1 value) — there's nothing for the operator to choose, so the field is omitted from the form. The `Status` checkbox and the `Type` dropdown are the only controls the operator fills in for a new rule, plus the `Pattern` text input.

#### Scenario: A page render shows the Filter Rules table with Status checkboxes
- **WHEN** `cfg.filters` contains two rules — one with `status="enabled"`, one with `status="disabled"`
- **THEN** the rendered table SHALL have two rows: the first row's Status checkbox SHALL be checked, the second row's Status checkbox SHALL be unchecked. The Status column SHALL be a checkbox control, NOT a dropdown

#### Scenario: The Status column is rendered as a checkbox control
- **WHEN** the `/settings` page renders
- **THEN** the Filter Rules table SHALL NOT contain any `<select>` element inside the `Status` column of any row (Status is a checkbox, not a dropdown)

#### Scenario: The Add Rule Type selector is the only dropdown
- **WHEN** the operator views the `/settings` page's Add Rule section
- **THEN** the only `<select>` element in the Add Rule form SHALL be the `Type` selector (offering `keyword`, `regex`, `message`); the `Status` field SHALL be a checkbox and the `Action` field SHALL be absent

#### Scenario: A new rule defaults to Enabled
- **WHEN** the operator submits the Add Rule form
- **THEN** the new rule SHALL have `status == "enabled"` (the most permissive default — the checkbox defaults to checked)

#### Scenario: An unchecked Add Rule checkbox produces a disabled rule
- **WHEN** the operator submits the Add Rule form with the Status checkbox unchecked (no `filter_status` field posted)
- **THEN** the new rule SHALL have `status == "disabled"`

### Requirement: v2 → v3 migration renames enabled bool to status enum and converts type=sender rules

The `_v2_to_v3` migration in `lib_shared/config_migrations.MIGRATIONS` SHALL transform a v2 `filters` array into a v3 `filters` array AND transform `type=sender` rules into senders list entries.

For each rule in `data["filters"]`:

- If the rule has `type=sender`: convert to a senders list entry with `allowed=False`, `name=rule.pattern`, `phone=rule.pattern` (best-effort: the rule's pattern becomes both the display name and the phone; `allowed=False` because the v2 sender-type rule was always a suppression rule, so the migrated entry inherits the suppressed classification). Append the new entry to `data["senders"]` (creating the list if absent). The migrated entry SHALL NOT contain a `status` field — there is no per-entry lifecycle in v3. DROP the rule from `data["filters"]` — sender matching is the senders list's job going forward.
- Otherwise (the rule has `type` in `keyword`/`regex`/`message`): rename the `enabled` (bool) field to `status` (enum). `enabled=True` → `status="enabled"`; `enabled=False` → `status="disabled"`. Rules that already have `status` (the migration is idempotent) are left unchanged.

The migration SHALL NOT mutate the caller's input dict (returns a shallow copy). The migration SHALL preserve `sign_settings` (with its `sign_name` and `timezone` fields), `effects_settings`, `text_settings`, and `filters` (after the `type=sender` rules have been removed) unchanged. The structural moves (top-level `timezone` → `sign_settings.timezone`, `sign` rename → `sign_settings` with `name` → `sign_name`, top-level `enforcement_enabled` → `text_settings.enforcement_enabled`, top-level `name_display_format` → `effects_settings.name_display_format`) are described in the overall v2 → v3 migration; the `filter-rule-status` migration step is just the FilterRule-level rename and type removal. The migration's transformation of the `senders` array itself is described in the `senders-status` capability (legacy `status="allowed"|"blocked"` → `allowed: bool`).

#### Scenario: A v2 payload with an enabled keyword rule migrates to v3
- **WHEN** `migrate({"version": 2, "filters": [{"type": "keyword", "pattern": "spam", "enabled": true}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3` and the filter SHALL have `status: "enabled"` (renamed from `enabled: true`)

#### Scenario: A v2 payload with a disabled keyword rule migrates to v3
- **WHEN** `migrate({"version": 2, "filters": [{"type": "keyword", "pattern": "spam", "enabled": false}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3` and the filter SHALL have `status: "disabled"` (renamed from `enabled: false`)

#### Scenario: A v2 payload with a type=sender rule migrates the rule into a senders entry
- **WHEN** `migrate({"version": 2, "filters": [{"type": "sender", "pattern": "+15551234567"}], "senders": []}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3`, the `filters` array SHALL be empty (the rule was dropped), AND the `senders` array SHALL contain a new entry `{"phone": "+15551234567", "name": "+15551234567", "allowed": false}` (the rule's pattern became both the display name and the phone; `allowed=false` because the original rule was a sender-suppression rule; no `status` field — there is no per-entry lifecycle in v3)

#### Scenario: A v2 payload with a type=sender rule appends to an existing senders list
- **WHEN** `migrate({"version": 2, "filters": [{"type": "sender", "pattern": "+15559999999"}], "senders": [{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3`, the `filters` array SHALL be empty, AND the `senders` array SHALL contain TWO entries (the original Alice entry migrated to `allowed: true` with no `status` field AND the new sender rule converted to `allowed: false` with no `status` field)

#### Scenario: A v2 payload with a filter missing the enabled field gets enabled default
- **WHEN** `migrate({"version": 2, "filters": [{"type": "keyword", "pattern": "spam"}]}, current_version=3)` is called (no `enabled` key)
- **THEN** the returned dict SHALL have `version == 3` and the filter SHALL have `status: "enabled"` backfilled (back-compat default)

#### Scenario: A v3 payload is unchanged by the migration
- **WHEN** `migrate({"version": 3, "filters": [...], "senders": [...]}, current_version=3)` is called
- **THEN** the returned dict SHALL equal the input (idempotent)

#### Scenario: The migration does not mutate the input dict
- **WHEN** `migrate({"version": 2, ...}, current_version=3)` is called
- **THEN** the caller's original dict SHALL retain its `version: 2` and original `filters`/`senders` shapes (the migration returns a new dict, never mutates the input)

### Requirement: The filters array round-trips through config storage and the wire

`SignConfig.to_dict()` SHALL include the `filters` key as a list of dict objects, each with `type`, `pattern`, `action`, and `status` fields. `SignConfig.from_dict()` SHALL accept the list shape and parse each entry into the new `FilterRule` instances. `SignConfig.update_from_dict()` SHALL replace the in-memory `cfg.filters` with the parsed value (full replacement, not merge).

The wire shape (sent over MQTT as a `type="config"` envelope and persisted in SQLite + S3) SHALL include `filters` at the top level alongside `senders`, `sign_settings` (containing `sign_name` and `timezone`), `version`, `effects_settings` (containing the effects list, pacing fields, and `name_display_format`), and `text_settings` (containing `speed`, `color`, `text_effect`, and `enforcement_enabled`). There is NO top-level `sign`, `timezone`, `enforcement_enabled`, or `name_display_format` key — they all live inside their respective nested settings blocks.

#### Scenario: to_dict emits the list shape with status
- **WHEN** `cfg.filters` has two rules — one enabled, one disabled
- **THEN** `cfg.to_dict()["filters"]` SHALL be a list of two dicts, each with `status` reflecting the rule's status

#### Scenario: from_dict rejects a type=sender rule
- **WHEN** `from_dict({"filters": [{"type": "sender", "pattern": "+15551234567"}], ...})` is called
- **THEN** the call SHALL raise `ValueError` (the type is REMOVED from the wire; the migration converts existing stored type=sender rules before they reach `from_dict`)

#### Scenario: update_from_dict replaces the in-memory filters
- **WHEN** `cfg.update_from_dict({"filters": [...]})` is called with a new list
- **THEN** `cfg.filters` SHALL be the new value (full replacement, not merged with the old list)

#### Scenario: A round-trip preserves rule status
- **WHEN** a rule with `status="disabled"` is added and the config is serialized via `to_dict` then re-parsed via `from_dict`
- **THEN** the re-parsed rule SHALL have `status == "disabled"` (status preserved end-to-end)