## ADDED Requirements

### Requirement: Each senders entry carries an action field and a status field with defaults

`SignConfig.senders` SHALL be a `dict[str, dict]` in the running code. The dict SHALL map a NORMALIZED phone string (last-10-digits with leading `+1`, via `phone_utils.normalize_phone`) to a value object with four keys:

- `name` — the operator-supplied display name (string, may be empty)
- `action` — one of the literal strings `"allow"` or `"suppress"` (the EFFECT axis: what happens when this sender matches)
- `status` — one of the literal strings `"enabled"` or `"disabled"` (the LIFECYCLE axis: is this entry "on" right now?)
- `phone` — the operator-supplied phone string from the form (the ORIGINAL phone, before normalization, preserved for round-trip display in the admin UI)

The wire shape SHALL be a list of objects: `[{"phone": str, "name": str, "action": str, "status": str}, ...]`. On `from_dict`, each wire entry SHALL be normalized and stored under its normalized key. On `to_dict`, each entry SHALL be emitted with its stored `phone` (the original, not the normalized key).

The defaults for a new entry SHALL be `action="allow"` and `status="enabled"`. A stored entry that lacks the `action` field SHALL be treated as `"allow"`; a stored entry that lacks the `status` field SHALL be treated as `"enabled"` (the migration backfills both fields; `from_dict` SHALL also accept missing fields silently so partial / legacy payloads still load).

The default for an absent `senders` list (no entries at all) SHALL be an empty dict — no entries, nothing rendered.

#### Scenario: A new entry defaults to allow + enabled
- **WHEN** the operator adds a row with `sender_name = "Alice"` and `sender_phone = "+15551234567"` and saves
- **THEN** `cfg.senders[normalize_phone("+15551234567")]` SHALL equal `{"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}`

#### Scenario: A stored entry without action loads as allow
- **WHEN** `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice"}], ...})` is called (no `action` key)
- **THEN** the parsed entry SHALL have `action == "allow"` (back-compat default)

#### Scenario: A stored entry without status loads as enabled
- **WHEN** `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice"}], ...})` is called (no `status` key)
- **THEN** the parsed entry SHALL have `status == "enabled"` (back-compat default)

#### Scenario: The wire shape includes both action and status
- **WHEN** `to_dict()` is called on a `SignConfig` with two entries — one allow+enabled, one suppress+disabled
- **THEN** the returned dict's `senders` list SHALL include both entries with their `action` and `status` fields present

#### Scenario: Round-trip preserves original phone format
- **WHEN** the operator adds `sender_phone = "+1 (555) 123-4567"` and saves, then reloads
- **THEN** the entry's stored `phone` SHALL equal `"+1 (555) 123-4567"` (original, not the normalized `+15551234567`); the dict key SHALL be the normalized form for lookup

### Requirement: Only senders with action="allow" AND status="enabled" render their messages

The sign SHALL display only messages from senders whose entry in `cfg.senders` has BOTH `action == "allow"` AND `status == "enabled"`. The decision rule, applied inside `FilteredMessages._enrich_messages`, SHALL be:

- Normalize `entry.message.sender` via `phone_utils.normalize_phone`.
- Look up the normalized sender in `cfg.senders`.
- If the sender is NOT in the dict → suppress (the sender has not been added to the list — implicit suppress).
- If the sender IS in the dict with `action == "allow"` AND `status == "enabled"` → render.
- If the sender IS in the dict with `action == "suppress"` → suppress (regardless of `status`).
- If the sender IS in the dict with `status == "disabled"` → suppress (regardless of `action`).

The decision is hard-coded — there is no mode flag, no master enable/disable. The `senders` dict is the only mechanism. The two fields are independent: `action` controls effect, `status` controls lifecycle — flipping either independently re-classifies the sender.

#### Scenario: A message from an allowed+enabled sender renders
- **WHEN** `cfg.senders` contains an entry for `+15551234567` with `action == "allow"` AND `status == "enabled"`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `False` and `get_messages(suppress=True)` SHALL include it

#### Scenario: A message from an allowed+disabled sender is suppressed
- **WHEN** `cfg.senders` contains an entry for `+15551234567` with `action == "allow"` AND `status == "disabled"`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `True` (the lifecycle flag is "off"; the entry is muted without being deleted)

#### Scenario: A message from a suppress+enabled sender is suppressed
- **WHEN** `cfg.senders` contains an entry for `+15551234567` with `action == "suppress"` AND `status == "enabled"`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `True` (the operator wants this sender's messages suppressed even though the entry is "on")

#### Scenario: A message from a suppress+disabled sender is suppressed
- **WHEN** `cfg.senders` contains an entry for `+15551234567` with `action == "suppress"` AND `status == "disabled"`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `True` (both axes say suppress; the entry is doubly muted)

#### Scenario: A message from a sender NOT in the list is suppressed
- **WHEN** `cfg.senders` does NOT contain an entry for `+15551234567` (the operator has not added this sender), and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `True` and `get_messages(suppress=True)` SHALL exclude it

#### Scenario: Display name resolves regardless of action/status
- **WHEN** `cfg.senders` contains `{"name": "Alice", "action": "suppress", "status": "enabled", "phone": "+15551234567"}` and a message arrives from `+15551234567`
- **THEN** `entry.sender_name` SHALL equal `"Alice"` even though the message is suppressed (the display name lookup works for muted/blocked senders — the operator can still see "From: Alice (blocked)" in the admin UI)

### Requirement: Phone numbers are normalized for the senders lookup

A `normalize_phone(s)` helper SHALL live in `lib_shared/phone_utils.py`. The helper SHALL:

- Strip all non-digit characters from the input.
- If exactly 10 digits remain, return `"+1" + digits`.
- If exactly 11 digits remain AND the first digit is `"1"`, return `"+1" + digits[1:]`.
- Otherwise (fewer than 10 digits, more than 11 digits, no digits at all, etc.), return the original input string verbatim — passthrough behavior for malformed inputs.

The helper SHALL be used by:

- `cfg.senders` key generation (on `from_dict` and on form save): the dict key is always the normalized form.
- `FilteredMessages._enrich_messages` lookup: the incoming sender is normalized before lookup so formatting differences match.

#### Scenario: An E.164 number normalizes to itself
- **WHEN** `normalize_phone("+15551234567")` is called
- **THEN** it SHALL return `"+15551234567"`

#### Scenario: A US 10-digit number gets a +1 prefix
- **WHEN** `normalize_phone("5551234567")` is called
- **THEN** it SHALL return `"+15551234567"`

#### Scenario: A number with formatting normalizes to its last 10 digits
- **WHEN** `normalize_phone("+1 (555) 123-4567")` is called
- **THEN** it SHALL return `"+15551234567"`

#### Scenario: A non-numeric string passes through unchanged
- **WHEN** `normalize_phone("not-a-phone")` is called
- **THEN** it SHALL return `"not-a-phone"`

#### Scenario: A formatted incoming sender matches a differently-formatted entry in the dict
- **WHEN** `cfg.senders` contains an entry with key `+15551234567` (from a stored phone `"+1 (555) 123-4567"`)
- **THEN** an incoming `Message(sender="555.123.4567")` SHALL resolve to the same dict entry (both normalize to `+15551234567`)

### Requirement: Filtering happens at egress only — every inbound SMS is stored regardless of senders list state

The Twilio webhook handler (`/api/messages` in `heart-message-manager/main.py`) SHALL NOT consult `cfg.senders` before persisting a message. Every delivery from Twilio SHALL be stored to SQLite, snapshotted to S3, and published over MQTT as a `type="message"` envelope, regardless of whether the sender is in the list or what their `action`/`status` is. The decision to render or suppress happens only at display-read time inside `MessageManager.get_messages(suppress=True)`.

This is the "disable without deleting" affordance's enabling guarantee: an operator can add a previously-unlisted sender to the list (or flip a suppressed sender back to allow, or flip a disabled sender back to enabled), and the previously-received-but-suppressed messages become visible on the next config update without re-ingestion from Twilio.

#### Scenario: An SMS from an unlisted sender is stored on ingress
- **WHEN** the sender is not in `cfg.senders` (or has `action="suppress"` or `status="disabled"`), and an SMS arrives
- **THEN** the message SHALL be persisted to SQLite, snapshotted to S3, and a `type="message"` envelope SHALL be published to MQTT; the suppression decision happens only at read time

#### Scenario: get_messages(suppress=True) excludes senders-action-suppressed messages
- **WHEN** the ring buffer contains messages from an allowed+enabled sender, a suppressed+enabled sender, an allowed+disabled sender, and an unlisted sender
- **THEN** `get_messages(suppress=True)` SHALL return only the allowed+enabled sender's messages; `get_messages(suppress=False)` SHALL return all four

### Requirement: Config update re-enriches the buffer and flips previously-suppressed messages

When a `type="config"` envelope arrives at `MessageManager._handle_config` and the new config has a different `senders` dict (entry added, removed, or `action`/`status` flipped), the device SHALL re-enrich the in-memory ring buffer. `MessageView.entry.suppressed` SHALL be re-evaluated for every buffered message using the new senders dict, and a previously-suppressed message SHALL become visible (and vice-versa) without re-ingestion.

#### Scenario: Adding an unlisted sender flips their previously-suppressed message to visible
- **WHEN** the buffer contains a message from `+15551234567` with `entry.suppressed == True` (sender was unlisted), and a new config arrives with `cfg.senders` now containing `{"+15551234567": {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}}`
- **THEN** after the config update the same message's `entry.suppressed` SHALL be `False`, and `get_messages(suppress=True)` SHALL include it

#### Scenario: Flipping an allowed+enabled sender to allowed+disabled suppresses their previously-visible messages
- **WHEN** the buffer contains visible messages from `+15551234567`, and a new config arrives with the same entry but `status = "disabled"` (and `action="allow"` unchanged)
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `True`, and `get_messages(suppress=True)` SHALL exclude them

#### Scenario: Flipping an allowed+enabled sender to suppress+enabled suppresses their previously-visible messages
- **WHEN** the buffer contains visible messages from `+15551234567`, and a new config arrives with the same entry but `action = "suppress"` (and `status="enabled"` unchanged)
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `True`

#### Scenario: Flipping a suppressed sender back to allow+enabled restores visibility
- **WHEN** the buffer contains suppressed messages from `+15551234567`, and a new config arrives with the same entry but `action = "allow"` AND `status = "enabled"`
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `False`

#### Scenario: Removing a sender from the list suppresses their messages
- **WHEN** the buffer contains visible messages from `+15551234567`, and a new config arrives with the entry removed from `cfg.senders`
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `True`

### Requirement: Sender suppression carries a synthetic "sender_action" marker on MessageView.rules

When `FilteredMessages._enrich_messages` suppresses a message because of `cfg.senders` (sender not in dict, OR `action="suppress"`, OR `status="disabled"`) AND no FilterRule matched, the entry's `rules` list SHALL contain exactly one synthetic rule: `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}`. When a FilterRule ALSO matched, the real rule(s) SHALL be kept (the synthetic marker is omitted — the real rule wins for display).

The synthetic marker lets the admin UI render a "Suppressed by sender action" badge in the messages list without adding a new field on the `MessageView` model. The marker type name is `"sender_action"` (not `"sender_status"`) because the synthetic marker signals that the senders list made a suppression decision based on either `action` OR `status` — the two fields are equivalent from a "this message is hidden" perspective, and the marker name reflects the upstream taxonomy.

#### Scenario: Sender-action-only suppression adds a synthetic marker
- **WHEN** the senders list suppresses a message (any reason — unlisted, action="suppress", or status="disabled") and no FilterRule matched
- **THEN** `entry.suppressed` SHALL be `True` and `entry.rules` SHALL contain exactly `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}`

#### Scenario: A sender-action + FilterRule suppression uses the real rule
- **WHEN** the senders list suppresses a message AND a FilterRule (e.g. keyword) also matched
- **THEN** `entry.suppressed` SHALL be `True` and `entry.rules` SHALL contain the matching FilterRule(s) but NOT the synthetic sender_action marker

### Requirement: Settings page renders a "Senders" section with per-row Action dropdown and Status checkbox

The admin UI's `/settings` page SHALL render the senders controls under a section titled **"Senders"** (replacing the current section title "Allowed Senders" — the new title describes the data the operator is editing, not the policy it implements; the policy is implicit and constant).

The section SHALL render a table with five columns: `Name` (text input), `Phone (E.164)` (text input), `Action` (dropdown: `Allow` / `Suppress`), `Status` (checkbox), and `Remove` (button). The Action column SHALL expose a per-row dropdown with two options: `Allow` and `Suppress`. The Status column SHALL expose a per-row checkbox; the box SHALL be checked iff `cfg.senders[<normalized_phone>]["status"] == "enabled"`. The default for new rows SHALL be `Allow` (dropdown) + `Enabled` (checkbox checked).

The page SHALL iterate `cfg.senders.items()` (replacing the broken `cfg.allowed_senders` iteration). Each row SHALL render the `name`, the `phone` (the **NORMALIZED** phone — see below), the action dropdown, and the status checkbox. A `Remove` button per row SHALL delete the entry from the dict on save.

**Phone display format:** the rendered Phone field SHALL show the NORMALIZED phone (the dict key, e.g. `+15551234567`), NOT the operator's original input (which might have been `+1 (555) 123-4567`, `555.123.4567`, etc.). Normalized display is consistent across all rows regardless of how each sender was originally typed — easier for the operator to scan a list of senders and recognize duplicates. The operator's original input is still preserved in `cfg.senders[<key>]["phone"]` for round-trip wire fidelity (an "edit" affordance could surface it if a future change wants to), but the default display is normalized.

A short helper line SHALL appear above the table: "Phone numbers are normalized to +1XXXXXXXXXX." (mirrors the column header "Phone (E.164)").

The form posts parallel lists `sender_name`, `sender_phone`, `sender_action` (dropdown value per row: `"allow"` or `"suppress"`), and `sender_status` (a checkbox list — each checked checkbox's value is its row index, e.g. `sender_status="0"`, `sender_status="1"`; unchecked rows are absent from the form data and treated as `disabled`). The handler uses the standard HTML "checkbox list with index values" pattern: a row's `status` is `"enabled"` iff `str(i)` is in the parsed `sender_status` list, else `"disabled"`.

The page SHALL NOT render a mode radio (no Off / Allowlist / Blocklist selector). The page SHALL NOT render a master `Enabled` toggle. The senders list is the only mechanism — entries with `action="allow"` + `status="enabled"` pass, everything else is suppressed.

#### Scenario: A page render shows the senders table with Action dropdown and Status checkbox
- **WHEN** `cfg.senders` contains `{"+15551234567": {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}, "+15558888888": {"name": "Bob", "action": "suppress", "status": "disabled", "phone": "+15558888888"}}`
- **THEN** the rendered table SHALL have two rows: Alice's row with Action=Allow selected AND Status checkbox checked; Bob's row with Action=Suppress selected AND Status checkbox unchecked

#### Scenario: A muted entry shows the normalized phone format
- **WHEN** `cfg.senders["+15551234567"]["phone"] == "+1 (555) 123-4567"` (operator typed formatted input; dict key is the normalized form)
- **THEN** the rendered Phone field SHALL show `+15551234567` (the normalized dict key), NOT the original `+1 (555) 123-4567` — display is normalized for visual consistency across rows

#### Scenario: The section title is "Senders", not "Allowed Senders"
- **WHEN** the operator views the `/settings` page
- **THEN** the senders section SHALL be titled "Senders" (replacing the old "Allowed Senders" title — the new title is neutral about the policy it implements)

#### Scenario: A helper line explains phone normalization
- **WHEN** the operator views the `/settings` page
- **THEN** a short helper line "Phone numbers are normalized to +1XXXXXXXXXX." SHALL appear above the senders table

#### Scenario: A new empty row defaults to Allow + Enabled
- **WHEN** the operator clicks "+ Add Entry"
- **THEN** the new row's Action dropdown SHALL default to Allow AND the Status checkbox SHALL default to checked (the most permissive values; the operator can flip either after filling in Name + Phone)

#### Scenario: The page does not iterate cfg.allowed_senders
- **WHEN** the template renders
- **THEN** the template SHALL NOT reference `cfg.allowed_senders` anywhere (the broken iteration is fully replaced)

### Requirement: /settings POST handler parses per-row Action dropdown and Status checkbox list and persists the new shape

The `/settings` POST handler in `heart-message-manager/main.py` SHALL be extended to read parallel lists `sender_name`, `sender_phone`, `sender_action` (dropdown value per row: `"allow"` or `"suppress"`), and `sender_status` (a checkbox list indexed by row position — only checked rows appear in the form data, with value equal to the row's index). For each row, the handler SHALL:

- Strip `name` and `phone`.
- Skip rows where `phone` is empty (empty phone = unfilled row, preserve operator intent).
- Determine `status` from the checkbox list: `status="enabled"` iff `str(row_index)` is in the parsed `sender_status` list, else `status="disabled"`.
- Build a new `cfg.senders` dict mapping `normalize_phone(phone)` to `{"name": name or phone, "action": action or "allow", "status": status, "phone": phone}` (the original phone preserved for round-trip).
- Default `action` to `"allow"` when the form field is missing or carries an unrecognized value (defensive against partial / legacy form posts).

The handler SHALL call `_save_and_publish(cfg)` after the rebuild. A POST with zero entries SHALL NOT wipe the existing `cfg.senders` (defensive: same partial-form preservation as the existing sign_name / timezone handling).

#### Scenario: A POST with one allow+enabled row persists correctly
- **WHEN** the operator POSTs `sender_name=Alice`, `sender_phone=+15551234567`, `sender_action=allow`, `sender_status=0` (row 0's checkbox is checked)
- **THEN** `cfg.senders["+15551234567"]` SHALL equal `{"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}` after save

#### Scenario: A POST with one suppress+disabled row persists correctly
- **WHEN** the operator POSTs `sender_name=Bob`, `sender_phone=+15558888888`, `sender_action=suppress` (no `sender_status` value — checkbox unchecked for this row)
- **THEN** `cfg.senders["+15558888888"]` SHALL equal `{"name": "Bob", "action": "suppress", "status": "disabled", "phone": "+15558888888"}` after save

#### Scenario: A row with an empty phone is dropped
- **WHEN** the operator POSTs `sender_name=Test`, `sender_phone=`, `sender_action=allow`, `sender_status=0`
- **THEN** the row SHALL NOT appear in `cfg.senders` after save

#### Scenario: A POST with zero senders rows preserves existing entries
- **WHEN** the operator POSTs the settings form with no `sender_name` / `sender_phone` values
- **THEN** `cfg.senders` SHALL equal the previous value (no wipe)

#### Scenario: A POST with formatted phone normalizes the dict key but preserves the original
- **WHEN** the operator POSTs `sender_phone=+1 (555) 123-4567`, `sender_status=0`
- **THEN** `cfg.senders["+15551234567"]["phone"]` SHALL equal `"+1 (555) 123-4567"` (original) and the dict key SHALL be the normalized form; `cfg.senders["+15551234567"]["status"]` SHALL equal `"enabled"` (checkbox checked)

#### Scenario: A POST with missing action defaults to allow
- **WHEN** the operator POSTs `sender_name=Alice`, `sender_phone=+15551234567`, `sender_status=0` (no `sender_action` field)
- **THEN** `cfg.senders["+15551234567"]["action"]` SHALL equal `"allow"` (defensive default — back-compat for legacy form posts missing the new field)

#### Scenario: A POST with multiple rows preserves each row's checkbox state
- **WHEN** the operator POSTs three rows where `sender_status=0` and `sender_status=2` are present (row 0 checked, row 1 unchecked, row 2 checked)
- **THEN** row 0's `status` SHALL equal `"enabled"`, row 1's `status` SHALL equal `"disabled"`, row 2's `status` SHALL equal `"enabled"` — each row's checkbox state is independent

### Requirement: The senders field round-trips through config storage and the wire

`SignConfig.to_dict()` SHALL include the `senders` key as a list of `{"phone": str, "name": str, "action": str, "status": str}` objects, sorted by phone for deterministic output. `SignConfig.from_dict()` SHALL accept the list shape and parse each entry into the new dict-of-dict internal shape. `SignConfig.update_from_dict()` SHALL replace the in-memory `cfg.senders` with the parsed value (full replacement, not merge).

The wire shape (sent over MQTT as a `type="config"` envelope and persisted in SQLite + S3) SHALL include `senders` at the top level alongside `filters`, `sign`, `timezone`, `version`, `effects_settings`, `text_settings`.

#### Scenario: to_dict emits the list shape with action and status
- **WHEN** `cfg.senders` has two entries — one allow+enabled, one suppress+disabled
- **THEN** `cfg.to_dict()["senders"]` SHALL be a list of two `{"phone": ..., "name": ..., "action": ..., "status": ...}` dicts, one with `action: "allow"` + `status: "enabled"` and one with `action: "suppress"` + `status: "disabled"`

#### Scenario: from_dict parses the list shape into the dict shape
- **WHEN** `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "action": "allow", "status": "enabled"}], ...})` is called
- **THEN** the returned `SignConfig`'s `senders` SHALL equal `{"+15551234567": {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}}`

#### Scenario: update_from_dict replaces the in-memory senders
- **WHEN** `cfg.update_from_dict({"senders": [...]})` is called with a new list
- **THEN** `cfg.senders` SHALL be the new value (full replacement, not merged with the old dict)

#### Scenario: A round-trip preserves the original phone format
- **WHEN** an entry with `phone = "+1 (555) 123-4567"` is added and the config is serialized via `to_dict` then re-parsed via `from_dict`
- **THEN** the re-parsed entry's `phone` SHALL equal `"+1 (555) 123-4567"` (original preserved, not normalized away)

### Requirement: v2 → v3 migration renames senders.status → senders.action and adds senders.status lifecycle

The `_v2_to_v3` migration in `lib_shared/config_migrations.MIGRATIONS` SHALL transform a v2 senders entry into a v3 senders entry:

- For each entry in `data["senders"]` (wire shape: list of dicts), the migration SHALL rename the `status` field (which carried the v2 vocabulary `"allowed"` or `"blocked"`) to the new `action` field, with a value rename: `"allowed"` → `"allow"`, `"blocked"` → `"suppress"`. The migration SHALL add a new `status` lifecycle field with the default value `"enabled"` (every pre-existing sender was implicitly "on" — the new lifecycle field is purely additive on the wire).

The migration SHALL be a SHALLOW COPY of the input (does not mutate the caller's dict). The migration SHALL preserve `filters`, `sign`, `timezone`, `effects_settings`, `text_settings` unchanged. The `filters` array transformation is described in the `filter-rule-status` capability (renaming `enabled: bool` → `status: "enabled"|"disabled"`; converting `type=sender` rules into senders list entries).

The `migrate_on_startup` flow (already wired in `heart-message-manager/main.py` from the prior `runtime-sign-config` change) SHALL invoke `_v2_to_v3` automatically when the stored config is at v2. Devices running the new code SHALL also run the migration defensively in `SignConfig.from_dict` / `update_from_dict` at the top of the function.

#### Scenario: A v2 payload with senders migrates to v3 with action+status
- **WHEN** `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3` and the `senders` entry SHALL have `action: "allow"` (renamed from `status: "allowed"`) AND `status: "enabled"` (new lifecycle field backfilled)

#### Scenario: A v2 payload with blocked sender migrates to v3 with suppress action
- **WHEN** `migrate({"version": 2, "senders": [{"phone": "+15558888888", "name": "Bob", "status": "blocked"}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3` and the `senders` entry SHALL have `action: "suppress"` (renamed from `status: "blocked"`) AND `status: "enabled"`

#### Scenario: A v3 payload is unchanged by the migration
- **WHEN** `migrate({"version": 3, "senders": [...], "filters": [...]}, current_version=3)` is called
- **THEN** the returned dict SHALL equal the input (idempotent)

#### Scenario: A v2 payload with a senders dict shape migrates correctly
- **WHEN** `migrate({"version": 2, "senders": {"+15551234567": "Alice"}, ...}, current_version=3)` is called (the OLD v1-style dict shape — possible for very old stored configs)
- **THEN** the returned dict SHALL normalize the dict into the list shape with `{"phone": "+15551234567", "name": "Alice", "action": "allow", "status": "enabled"}` (the migration handles both list and dict legacy shapes)

#### Scenario: The migration does not mutate the input dict
- **WHEN** `migrate({"version": 2, ...}, current_version=3)` is called
- **THEN** the caller's original dict SHALL retain its `version: 2` and original `senders` shape (the migration returns a new dict, never mutates the input)

#### Scenario: The server's startup migration writes the v3 config back to S3 and publishes via MQTT
- **WHEN** the server starts and the S3-stored config is at v2
- **THEN** `migrate_on_startup` SHALL run `_v2_to_v3`, write the migrated v3 config to S3, update the local SQLite cache to v3, and publish a `type="config"` envelope to MQTT with the migrated payload