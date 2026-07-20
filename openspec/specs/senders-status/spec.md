# senders-status Specification

## Purpose
TBD - created by archiving change implement-senders-filtering. Update Purpose after archive.
## Requirements
### Requirement: Each senders entry carries an `allowed` flag and a `name`

`SignConfig.senders` SHALL be a `dict[str, dict]` in the running code. The dict SHALL map a NORMALIZED phone string (last-10-digits with leading `+1`, via `phone_utils.normalize_phone`) to a value object with three keys:

- `name` — the operator-supplied display name (string, may be empty). Used for the rendered "From: <name>" in the admin UI and (when formatted) on the sign display — see the `name-display-format` capability.
- `allowed` — a boolean: is this sender on the allowlist? `True` means "this sender's messages render when enforcement is on"; `False` means "this sender's messages are blocked when enforcement is on, but the name is preserved for display." Default for new entries: `True`.
- `phone` — the operator-supplied phone string from the form (the ORIGINAL phone, before normalization, preserved for round-trip display in the admin UI).

The wire shape SHALL be a list of objects: `[{"phone": str, "name": str, "allowed": bool}, ...]`. On `from_dict`, each wire entry SHALL be normalized and stored under its normalized key. On `to_dict`, each entry SHALL be emitted with its stored `phone` (the original, not the normalized key).

A stored entry that lacks the `allowed` field SHALL be treated as `True` (back-compat: every pre-existing senders entry was implicitly on the allowlist). The migration backfills `allowed=True`; `from_dict` SHALL also accept a missing field silently so partial / legacy payloads still load.

The default for an absent `senders` list (no entries at all) SHALL be an empty dict — combined with `enforcement_enabled=True`, an empty list means NOTHING renders (the allowlist is empty, so no sender is allowed through). With `enforcement_enabled=False`, an empty list means EVERYTHING renders.

**There is NO per-entry `status: "enabled"|"disabled"` lifecycle field.** The "disable without delete" affordance the issue asks for is provided by the `text_settings.enforcement_enabled` master toggle (see the decision rule below) — flipping the master toggle off preserves every entry in the config while bypassing the filter entirely. A per-entry lifecycle would only matter if individual entries could be muted while the master toggle stayed on, which is not requested and would add a second axis of complexity on top of `allowed`.

#### Scenario: A new entry defaults to allowed
- **WHEN** the operator adds a row with `sender_name = "Alice"` and `sender_phone = "+15551234567"` and saves
- **THEN** `cfg.senders[normalize_phone("+15551234567")]` SHALL equal `{"name": "Alice", "allowed": True, "phone": "+15551234567"}`

#### Scenario: A stored entry without allowed loads as True
- **WHEN** `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice"}], ...})` is called (no `allowed` key)
- **THEN** the parsed entry SHALL have `allowed == True` (back-compat default — every pre-existing sender was implicitly on the allowlist)

#### Scenario: The wire shape includes allowed
- **WHEN** `to_dict()` is called on a `SignConfig` with two entries — one `allowed=True`, one `allowed=False`
- **THEN** the returned dict's `senders` list SHALL include both entries with their `allowed` fields present

#### Scenario: Round-trip preserves original phone format
- **WHEN** the operator adds `sender_phone = "+1 (555) 123-4567"` and saves, then reloads
- **THEN** the entry's stored `phone` SHALL equal `"+1 (555) 123-4567"` (original, not the normalized `+15551234567`); the dict key SHALL be the normalized form for lookup

#### Scenario: A stored entry has no status field
- **WHEN** `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "allowed": true}], ...})` is called
- **THEN** the parsed entry SHALL NOT have a `status` key (the field does not exist on senders entries — only `allowed` is the per-entry axis; lifecycle is the master `text_settings.enforcement_enabled` toggle)

### Requirement: The decision rule for a sender entry depends only on the master enforcement toggle and the per-entry allowed flag

The sign SHALL display a message from sender `S` iff both of these hold:

1. `cfg.text_settings.enforcement_enabled == True` (the master toggle is on).
2. `cfg.senders[<normalize_phone(S)>]["allowed"] is True` — the sender is in the list AND explicitly on the allowlist.

If `cfg.text_settings.enforcement_enabled == False`, every sender renders regardless of any per-entry state (the master toggle bypasses the allowlist entirely — this is the issue's "Add an 'enabled' checkbox whether to enforce the list" requirement; names still resolve for display).

If `cfg.text_settings.enforcement_enabled == True` but the sender is NOT in the list → suppress (the operator has not added them; the allowlist is exclusive).

If `cfg.text_settings.enforcement_enabled == True` and the sender IS in the list with `allowed=False` → suppress (the operator added them for display-name purposes but explicitly marked them as NOT on the allowlist).

The decision rule, applied inside `FilteredMessages._enrich_messages`, SHALL be:

```
def should_render_sender(sender, senders, enforcement_enabled):
    if not enforcement_enabled:
        return True  # master toggle off → no filtering, every message renders

    normalized = normalize_phone(sender)
    entry = senders.get(normalized)
    if entry is None:
        return False  # not on the allowlist
    return entry["allowed"]  # True → on the allowlist; False → off
```

The behavior is hard-coded allowlist-only — there is no mode field, no blocklist interpretation, and no per-entry lifecycle. The senders list is the allowlist; everything else is implicitly blocked when enforcement is on.

#### Scenario: An allowed sender renders when enforcement is on
- **WHEN** `cfg.text_settings.enforcement_enabled == True`, `cfg.senders["+15551234567"]["allowed"] is True`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `False` and `get_messages(suppress=True)` SHALL include it

#### Scenario: A disallowed sender is suppressed
- **WHEN** `cfg.text_settings.enforcement_enabled == True`, `cfg.senders["+15551234567"]["allowed"] is False`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `True` (the operator added them for display-name purposes but explicitly marked them as off the allowlist)

#### Scenario: An unlisted sender is suppressed
- **WHEN** `cfg.text_settings.enforcement_enabled == True`, `cfg.senders` does NOT contain `+15551234567`, and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `True` (the operator has not added this sender — implicit block in allowlist mode)

#### Scenario: Enforcement disabled bypasses the filter
- **WHEN** `cfg.text_settings.enforcement_enabled == False` (regardless of any entry state), and a message arrives from `+15551234567`
- **THEN** `entry.suppressed` SHALL be `False` (the master toggle is off — every message renders; names still resolve for display)

#### Scenario: Enforcement disabled preserves all entries
- **WHEN** the operator unchecks "Enforce senders filter" and saves
- **THEN** `cfg.senders` SHALL be unchanged — every previously-added entry (with whatever `allowed` value) stays in the config; the operator can re-check the toggle at any time to re-enable filtering without re-typing entries (this is the issue's "disable without delete" affordance at the LIST level — there is no per-entry lifecycle because the LIST-level toggle is sufficient)

#### Scenario: Display name resolves regardless of allowed
- **WHEN** `cfg.senders` contains `{"name": "Alice", "allowed": False, "phone": "+15551234567"}` and a message arrives from `+15551234567`
- **THEN** `entry.sender_name` SHALL equal a formatted version of `"Alice"` (per the `name-display-format` capability) even though the message is suppressed (the display name lookup works for disallowed senders — the operator can still see "From: Alice" in the admin UI)

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

The Twilio webhook handler (`/api/messages` in `heart-message-manager/main.py`) SHALL NOT consult `cfg.senders` or `cfg.text_settings.enforcement_enabled` before persisting a message. Every delivery from Twilio SHALL be stored to SQLite, snapshotted to S3, and published over MQTT as a `type="message"` envelope, regardless of whether the sender is in the list, what their `allowed` value is, or whether enforcement is enabled. The decision to render or suppress happens only at display-read time inside `MessageManager.get_messages(suppress=True)`.

This is the "disable without deleting" affordance's enabling guarantee: an operator can add a previously-unlisted sender to the list, flip a disallowed sender back to allowed, or toggle enforcement on/off — and the previously-received-but-suppressed messages become visible on the next config update without re-ingestion from Twilio.

#### Scenario: An SMS from an unlisted sender is stored on ingress
- **WHEN** the sender is not in `cfg.senders` (or has `allowed=False`), and an SMS arrives
- **THEN** the message SHALL be persisted to SQLite, snapshotted to S3, and a `type="message"` envelope SHALL be published to MQTT; the suppression decision happens only at read time

#### Scenario: get_messages(suppress=True) excludes disallowed and unlisted senders when enforcement is on
- **WHEN** the ring buffer contains messages from an allowed sender, a disallowed sender, and an unlisted sender (with `cfg.text_settings.enforcement_enabled == True`)
- **THEN** `get_messages(suppress=True)` SHALL return only the allowed sender's messages; `get_messages(suppress=False)` SHALL return all three

#### Scenario: get_messages(suppress=True) renders everything when enforcement is off
- **WHEN** the ring buffer contains messages from senders of various allowed combinations AND `cfg.text_settings.enforcement_enabled == False`
- **THEN** `get_messages(suppress=True)` SHALL return ALL messages (the master toggle bypasses filtering — names still resolve via the display-name lookup)

### Requirement: Config update re-enriches the buffer and flips previously-suppressed messages

When a `type="config"` envelope arrives at `MessageManager._handle_config` and the new config has a different `senders` dict (entry added, removed, or `allowed` flipped) or `enforcement_enabled`, the device SHALL re-enrich the in-memory ring buffer. `MessageView.entry.suppressed` SHALL be re-evaluated for every buffered message using the new enforcement + senders dict, and a previously-suppressed message SHALL become visible (and vice-versa) without re-ingestion.

#### Scenario: Adding an unlisted sender flips their previously-suppressed message to visible
- **WHEN** the buffer contains a message from `+15551234567` with `entry.suppressed == True` (sender was unlisted), and a new config arrives with `cfg.senders` now containing `{"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}}`
- **THEN** after the config update the same message's `entry.suppressed` SHALL be `False`, and `get_messages(suppress=True)` SHALL include it

#### Scenario: Flipping an allowed sender to disallowed suppresses their previously-visible messages
- **WHEN** the buffer contains visible messages from `+15551234567`, and a new config arrives with the same entry but `allowed = False`
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `True`, and `get_messages(suppress=True)` SHALL exclude them

#### Scenario: Flipping a disallowed sender back to allowed restores visibility
- **WHEN** the buffer contains suppressed messages from `+15551234567`, and a new config arrives with the same entry but `allowed = True`
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `False`

#### Scenario: Removing a sender from the list suppresses their messages
- **WHEN** the buffer contains visible messages from `+15551234567`, and a new config arrives with the entry removed from `cfg.senders`
- **THEN** after the config update the same messages' `entry.suppressed` SHALL be `True`

#### Scenario: Disabling enforcement flips blocked senders to visible
- **WHEN** the buffer contains messages from `+15551234567` with `entry.suppressed == True` (sender was unlisted), and a new config arrives with `cfg.text_settings.enforcement_enabled == False`
- **THEN** after the config update the same message's `entry.suppressed` SHALL be `False` (the master toggle bypasses filtering)

#### Scenario: Re-enabling enforcement re-suppresses the buffer
- **WHEN** the buffer contains a message from `+15551234567` with `entry.suppressed == False` (enforcement was off — everything rendered), and a new config arrives with `cfg.text_settings.enforcement_enabled == True` AND `+15551234567` is NOT in the senders dict
- **THEN** after the config update the same message's `entry.suppressed` SHALL be `True` (unlisted in allowlist mode with enforcement on)

### Requirement: Sender suppression carries a synthetic "sender_action" marker on MessageView.rules

When `FilteredMessages._enrich_messages` suppresses a message because of `cfg.senders` (sender not in dict, OR `allowed=False`) AND no FilterRule matched, the entry's `rules` list SHALL contain exactly one synthetic rule: `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}`. When a FilterRule ALSO matched, the real rule(s) SHALL be kept (the synthetic marker is omitted — the real rule wins for display).

The synthetic marker lets the admin UI render a "Suppressed by sender action" badge in the messages list without adding a new field on the `MessageView` model. The marker type name is `"sender_action"` because the synthetic marker signals that the senders list made a suppression decision based on the per-entry `allowed` flag (or absence from the dict). There is no separate lifecycle marker because there is no per-entry lifecycle field — the master `text_settings.enforcement_enabled` toggle is the only lifecycle axis, and it does not produce a per-message marker (when enforcement is off, no suppression decision is made at all).

When `cfg.text_settings.enforcement_enabled == False`, no synthetic marker is added (the suppression decision wasn't made by the senders filter — the master toggle is off).

#### Scenario: Sender-action-only suppression adds a synthetic marker
- **WHEN** the senders list suppresses a message (any reason — unlisted OR `allowed=False`) and no FilterRule matched
- **THEN** `entry.suppressed` SHALL be `True` and `entry.rules` SHALL contain exactly `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}`

#### Scenario: Enforcement disabled produces no synthetic marker
- **WHEN** `cfg.text_settings.enforcement_enabled == False` (every message renders — no suppression decision is made by the senders list)
- **THEN** `entry.suppressed` SHALL be `False` and `entry.rules` SHALL NOT contain a `sender_action` synthetic marker

#### Scenario: A sender-action + FilterRule suppression uses the real rule
- **WHEN** the senders list suppresses a message AND a FilterRule (e.g. keyword) also matched
- **THEN** `entry.suppressed` SHALL be `True` and `entry.rules` SHALL contain the matching FilterRule(s) but NOT the synthetic sender_action marker

### Requirement: Settings page renders a "Senders" section with a per-row Allowed checkbox

The admin UI's `/settings` page SHALL render the senders controls under a section titled **"Senders"** (replacing the current section title "Allowed Senders" — the new title describes the data the operator is editing, not the policy it implements; the policy is constant: allowlist, gated by the master enforcement toggle).

The section SHALL render:

- At the top: a single **Enforce senders filter** checkbox (the master toggle for `cfg.text_settings.enforcement_enabled`).
- Below that: the **name display format dropdown** (see `name-display-format` for full UI requirements).
- A short helper line above the table: "Phone numbers are normalized to +1XXXXXXXXXX." (mirrors the column header "Phone (E.164)").
- A table with four columns: `Name` (text input), `Phone (E.164)` (text input), `Allowed` (checkbox), and `Remove` (button). The `Allowed` column SHALL expose a per-row checkbox; the box SHALL be checked iff `cfg.senders[<normalized_phone>]["allowed"] is True`. The default for new rows SHALL be `Allowed=checked`.

The page SHALL iterate `cfg.senders.items()` (replacing the broken `cfg.allowed_senders` iteration). Each row SHALL render the `name`, the `phone` (the **NORMALIZED** phone — see below), and the allowed checkbox. A `Remove` button per row SHALL delete the entry from the dict on save.

**Phone display format:** the rendered Phone field SHALL show the NORMALIZED phone (the dict key, e.g. `+15551234567`), NOT the operator's original input (which might have been `+1 (555) 123-4567`, `555.123.4567`, etc.). Normalized display is consistent across all rows regardless of how each sender was originally typed — easier for the operator to scan a list of senders and recognize duplicates. The operator's original input is still preserved in `cfg.senders[<key>]["phone"]` for round-trip wire fidelity (an "edit" affordance could surface it if a future change wants to), but the default display is normalized.

The form posts parallel lists `sender_name`, `sender_phone`, and `sender_allowed` (a checkbox list — each checked checkbox's value is its row index, e.g. `sender_allowed="0"`, `sender_allowed="1"`; unchecked rows are absent from the form data and treated as `allowed=False`). The handler uses the standard HTML "checkbox list with index values" pattern: a row's `allowed` is `True` iff `str(row_index)` is in the parsed `sender_allowed` list, else `False`. The form ALSO posts the enforcement checkbox (`enforcement_enabled=1` when checked) and the name display format dropdown (`name_display_format`) — all parsed by the same POST handler.

There is NO per-row Status column and NO per-row Status checkbox — the master `text_settings.enforcement_enabled` toggle is the only lifecycle control. The page SHALL NOT render a mode radio (no Allowlist / Blocklist selector). The behavior is allowlist-only by design — the issue is explicit that only the allowed-senders list is supported.

#### Scenario: A page render shows the senders table with Allowed checkboxes
- **WHEN** `cfg.senders` contains `{"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}, "+15558888888": {"name": "Bob", "allowed": False, "phone": "+15558888888"}}`
- **THEN** the rendered table SHALL have two rows: Alice's row with the Allowed checkbox checked; Bob's row with the Allowed checkbox unchecked

#### Scenario: A muted entry shows the normalized phone format
- **WHEN** `cfg.senders["+15551234567"]["phone"] == "+1 (555) 123-4567"` (operator typed formatted input; dict key is the normalized form)
- **THEN** the rendered Phone field SHALL show `+15551234567` (the normalized dict key), NOT the original `+1 (555) 123-4567` — display is normalized for visual consistency across rows

#### Scenario: The section title is "Senders", not "Allowed Senders"
- **WHEN** the operator views the `/settings` page
- **THEN** the senders section SHALL be titled "Senders" (replacing the old "Allowed Senders" title — the new title is neutral about the data the operator maintains)

#### Scenario: A helper line explains phone normalization
- **WHEN** the operator views the `/settings` page
- **THEN** a short helper line "Phone numbers are normalized to +1XXXXXXXXXX." SHALL appear above the senders table

#### Scenario: A new empty row defaults to Allowed
- **WHEN** the operator clicks "+ Add Entry"
- **THEN** the new row's Allowed checkbox SHALL default to checked (the most permissive value; the operator can uncheck it after filling in Name + Phone)

#### Scenario: The page does not iterate cfg.allowed_senders
- **WHEN** the template renders
- **THEN** the template SHALL NOT reference `cfg.allowed_senders` anywhere (the broken iteration is fully replaced)

#### Scenario: The page does not render a mode radio
- **WHEN** the template renders
- **THEN** the template SHALL NOT render any Allowlist / Blocklist radio group — only the master "Enforce senders filter" checkbox governs whether filtering happens

#### Scenario: The page does not render a per-row Status column
- **WHEN** the template renders the senders table
- **THEN** the table SHALL have exactly four columns (Name / Phone (E.164) / Allowed / Remove) — no Status column, no per-row status checkbox

#### Scenario: The page shows the enforcement checkbox at the top of the Senders section
- **WHEN** `cfg.text_settings.enforcement_enabled == False` and the operator views the `/settings` page
- **THEN** the "Enforce senders filter" checkbox SHALL be unchecked at the top of the Senders section, above the senders table

### Requirement: /settings POST handler parses per-row Allowed checkbox list and persists the new shape

The `/settings` POST handler in `heart-message-manager/main.py` SHALL be extended to read parallel lists `sender_name`, `sender_phone`, and `sender_allowed` (checkbox list — each checked box's value is the row's index; unchecked rows are absent from the form data). For each row, the handler SHALL:

- Strip `name` and `phone`.
- Skip rows where `phone` is empty (empty phone = unfilled row, preserve operator intent).
- Determine `allowed` from the checkbox list: `allowed=True` iff `str(row_index)` is in the parsed `sender_allowed` list, else `allowed=False`.
- Build a new `cfg.senders` dict mapping `normalize_phone(phone)` to `{"name": name or phone, "allowed": allowed, "phone": phone}` (the original phone preserved for round-trip).

The handler SHALL call `_save_and_publish(cfg)` after the rebuild. A POST with zero entries SHALL NOT wipe the existing `cfg.senders` (defensive: same partial-form preservation as the existing sign_settings.sign_name / sign_settings.timezone handling). The handler SHALL also parse `request.form.get("enforcement_enabled") == "1"` (default `True` when the field is absent) for `cfg.text_settings.enforcement_enabled`, and `request.form.get("name_display_format")` (default `"first_initial_if_duplicates"`) for `cfg.effects_settings.name_display_format`.

The handler SHALL NOT read any per-row `sender_status` field — per-entry lifecycle is not in the v3 wire shape. The handler SHALL NOT read any `sender_mode` field — there is no mode (allowlist-only).

#### Scenario: A POST with one allowed row persists correctly
- **WHEN** the operator POSTs `sender_name=Alice`, `sender_phone=+15551234567`, `sender_allowed=0` (row 0's allowed checkbox is checked)
- **THEN** `cfg.senders["+15551234567"]` SHALL equal `{"name": "Alice", "allowed": True, "phone": "+15551234567"}` after save

#### Scenario: A POST with one disallowed row persists correctly
- **WHEN** the operator POSTs `sender_name=Bob`, `sender_phone=+15558888888` (no `sender_allowed` value for Bob's row — the Allowed checkbox is unchecked)
- **THEN** `cfg.senders["+15558888888"]` SHALL equal `{"name": "Bob", "allowed": False, "phone": "+15558888888"}` after save

#### Scenario: A POST persists the enforcement_enabled field
- **WHEN** the operator POSTs `enforcement_enabled=0` (the master toggle is unchecked)
- **THEN** `cfg.text_settings.enforcement_enabled == False` after save

#### Scenario: A POST without the enforcement_enabled field defaults to True
- **WHEN** the operator POSTs the form without an `enforcement_enabled` field
- **THEN** `cfg.text_settings.enforcement_enabled == True` after save (defensive default — unchecking produces the explicit `0` value)

#### Scenario: A POST persists the name_display_format field at the nested location
- **WHEN** the operator POSTs `name_display_format=first` (the dropdown selects "first")
- **THEN** `cfg.effects_settings.name_display_format == "first"` after save (the field lives inside `effects_settings`, not at the top level)

#### Scenario: A POST without the name_display_format field defaults to first_initial_if_duplicates
- **WHEN** the operator POSTs the form without a `name_display_format` field
- **THEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` after save (defensive default; the field lives at the nested location)

#### Scenario: A row with an empty phone is dropped
- **WHEN** the operator POSTs `sender_name=Test`, `sender_phone=`, `sender_allowed=0`
- **THEN** the row SHALL NOT appear in `cfg.senders` after save

#### Scenario: A POST with zero senders rows preserves existing entries
- **WHEN** the operator POSTs the settings form with no `sender_name` / `sender_phone` values
- **THEN** `cfg.senders` SHALL equal the previous value (no wipe)

#### Scenario: A POST with formatted phone normalizes the dict key but preserves the original
- **WHEN** the operator POSTs `sender_phone=+1 (555) 123-4567`, `sender_allowed=0`
- **THEN** `cfg.senders["+15551234567"]["phone"]` SHALL equal `"+1 (555) 123-4567"` (original) and the dict key SHALL be the normalized form; `cfg.senders["+15551234567"]["allowed"]` SHALL equal `True` (checkbox checked)

#### Scenario: A POST with multiple rows preserves each row's checkbox state independently
- **WHEN** the operator POSTs three rows where `sender_allowed=0` and `sender_allowed=2` are present (rows 0 and 2 are allowed)
- **THEN** row 0's `allowed` SHALL equal `True`; row 1's `allowed` SHALL equal `False`; row 2's `allowed` SHALL equal `True` — each row's checkbox state is independent

### Requirement: The senders field round-trips through config storage and the wire

`SignConfig.to_dict()` SHALL include the `senders` key as a list of `{"phone": str, "name": str, "allowed": bool}` objects, sorted by phone for deterministic output. `SignConfig.from_dict()` SHALL accept the list shape and parse each entry into the new dict-of-dict internal shape. `SignConfig.update_from_dict()` SHALL replace the in-memory `cfg.senders` with the parsed value (full replacement, not merge).

The wire shape (sent over MQTT as a `type="config"` envelope and persisted in SQLite + S3) SHALL include `senders` at the top level alongside `filters`, `sign`, `timezone`, `version`, `effects_settings`, `text_settings`.

#### Scenario: to_dict emits the list shape with allowed
- **WHEN** `cfg.senders` has two entries — one allowed, one disallowed
- **THEN** `cfg.to_dict()["senders"]` SHALL be a list of two `{"phone": ..., "name": ..., "allowed": ...}` dicts, one with `allowed: True` and one with `allowed: False`

#### Scenario: from_dict parses the list shape into the dict shape
- **WHEN** `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "allowed": True}], ...})` is called
- **THEN** the returned `SignConfig`'s `senders` SHALL equal `{"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}}`

#### Scenario: update_from_dict replaces the in-memory senders
- **WHEN** `cfg.update_from_dict({"senders": [...]})` is called with a new list
- **THEN** `cfg.senders` SHALL be the new value (full replacement, not merged with the old dict)

#### Scenario: A round-trip preserves the original phone format
- **WHEN** an entry with `phone = "+1 (555) 123-4567"` is added and the config is serialized via `to_dict` then re-parsed via `from_dict`
- **THEN** the re-parsed entry's `phone` SHALL equal `"+1 (555) 123-4567"` (original preserved, not normalized away)

### Requirement: v2 → v3 migration maps senders.status (allowed/blocked) → senders.allowed (bool)

The `_v2_to_v3` migration in `lib_shared/config_migrations.MIGRATIONS` SHALL transform a v2 senders entry into a v3 senders entry:

- For each entry in `data["senders"]` (wire shape: list of dicts), the migration SHALL map a v2 `status` field with values `"allowed"` or `"blocked"` (legacy/draft format) to the new `allowed` boolean field: `"allowed"` → `True`, `"blocked"` → `False`. If the v2 entry has no `status` field (the actual current v2 wire shape — `[{"phone", "name"}]` with no `status`), the migration SHALL backfill `allowed=True` (every pre-existing sender was implicitly on the allowlist). The migrated entry SHALL NOT contain a `status` field — there is no per-entry lifecycle in v3.

The migration SHALL be a SHALLOW COPY of the input (does not mutate the caller's dict). The migration SHALL preserve `filters`, `sign`, `timezone`, `effects_settings`, `text_settings` unchanged. The `filters` array transformation is described in the `filter-rule-status` capability (renaming `enabled: bool` → `status: "enabled"|"disabled"`; converting `type=sender` rules into senders list entries with `allowed=False`).

The `migrate_on_startup` flow (already wired in `heart-message-manager/main.py` from the prior `runtime-sign-config` change) SHALL invoke `_v2_to_v3` automatically when the stored config is at v2. Devices running the new code SHALL also run the migration defensively in `SignConfig.from_dict` / `update_from_dict` at the top of the function.

#### Scenario: A v2 payload with a legacy status="allowed" migrates to v3 with allowed=True
- **WHEN** `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3` and the `senders` entry SHALL have `allowed: True` (mapped from `status: "allowed"`); there SHALL be NO `status` field on the entry

#### Scenario: A v2 payload with a legacy status="blocked" migrates to v3 with allowed=False
- **WHEN** `migrate({"version": 2, "senders": [{"phone": "+15558888888", "name": "Bob", "status": "blocked"}]}, current_version=3)` is called
- **THEN** the returned dict SHALL have `version == 3` and the `senders` entry SHALL have `allowed: False` (mapped from `status: "blocked"`); there SHALL be NO `status` field on the entry

#### Scenario: A v2 payload without a status field migrates to v3 with allowed=True
- **WHEN** `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice"}]}, current_version=3)` is called (no `status` key — the actual current v2 wire shape)
- **THEN** the returned dict SHALL have `version == 3` and the `senders` entry SHALL have `allowed: True` (back-compat default — every pre-existing sender was implicitly on the allowlist)

#### Scenario: A v3 payload is unchanged by the migration
- **WHEN** `migrate({"version": 3, "senders": [...], "filters": [...]}, current_version=3)` is called
- **THEN** the returned dict SHALL equal the input (idempotent)

#### Scenario: A v2 payload with a senders dict shape migrates correctly
- **WHEN** `migrate({"version": 2, "senders": {"+15551234567": "Alice"}, ...}, current_version=3)` is called (the OLD v1-style dict shape — possible for very old stored configs)
- **THEN** the returned dict SHALL normalize the dict into the list shape with `{"phone": "+15551234567", "name": "Alice", "allowed": True}` (the migration handles both list and dict legacy shapes)

#### Scenario: The migration does not mutate the input dict
- **WHEN** `migrate({"version": 2, ...}, current_version=3)` is called
- **THEN** the caller's original dict SHALL retain its `version: 2` and original `senders` shape (the migration returns a new dict, never mutates the input)

#### Scenario: The server's startup migration writes the v3 config back to S3 and publishes via MQTT
- **WHEN** the server starts and the S3-stored config is at v2
- **THEN** `migrate_on_startup` SHALL run `_v2_to_v3`, write the migrated v3 config to S3, update the local SQLite cache to v3, and publish a `type="config"` envelope to MQTT with the migrated payload

