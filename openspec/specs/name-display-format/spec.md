# name-display-format Specification

## Purpose
TBD - created by archiving change implement-senders-filtering. Update Purpose after archive.
## Requirements
### Requirement: The EffectsSettings config carries a name_display_format setting

`EffectsSettings` SHALL carry a `name_display_format` field on the wire and in memory. The field lives inside the existing `effects_settings` block on `SignConfig` (not at the top level) — the display format is a presentation knob, and presentation knobs group inside `effects_settings` (alongside the effects list and pacing fields). The field SHALL be one of the literal strings:

- `"full"` — Full name (first and last). Example: "Alice Smith".
- `"first_initial"` — First name + initial of last name. Example: "Alice S." (the period after the initial is included).
- `"first"` — First name only. Example: "Alice".
- `"first_initial_if_duplicates"` — First name only by default; first name + last initial when the first name appears in two or more entries in `cfg.senders`. Example: "Alice" if no other sender is also named Alice, otherwise "Alice S." (the period after the initial is included).

The default for a new (or migrated) config SHALL be `"first_initial_if_duplicates"` (the issue specifies this as the default).

The format governs how `MessageView.sender_name` is computed from the sender's stored `name` field. The stored `name` is the operator-supplied full name (whatever the operator typed — typically "First Last", but possibly a single-word name, a nickname, or a multi-word last name like "Alice Smith Jones"). The display-format layer applies the chosen transformation at read time. The stored `name` field is NOT mutated by the format choice — the operator can flip the format back and forth without retyping names.

The wire shape: the field appears inside the top-level `effects_settings` dict as `{"effects_settings": {"name_display_format": <value>, ...}}`. The top-level `cfg.name_display_format` does NOT exist on v3 SignConfigs — the value lives only inside `effects_settings`.

#### Scenario: The default format is first_initial_if_duplicates
- **WHEN** `SignConfig()` is constructed with no arguments (and so `EffectsSettings()` defaults apply)
- **THEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"`

#### Scenario: The wire shape carries the name_display_format field
- **WHEN** `SignConfig.to_dict()` is called
- **THEN** the returned dict SHALL include an `effects_settings` block whose `name_display_format` key has the current value; the top-level dict SHALL NOT have a `name_display_format` key

#### Scenario: from_dict parses the name_display_format field at the nested location
- **WHEN** `SignConfig.from_dict({"effects_settings": {"name_display_format": "first"}, ...})` is called
- **THEN** `cfg.effects_settings.name_display_format == "first"`

#### Scenario: from_dict defaults to first_initial_if_duplicates when the field is missing
- **WHEN** `SignConfig.from_dict({"effects_settings": {}})` is called (no `name_display_format` key inside effects_settings)
- **THEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` (back-compat default for partial / legacy payloads)

#### Scenario: from_dict creates effects_settings when the block is missing
- **WHEN** `SignConfig.from_dict({"senders": [...]})` is called with no `effects_settings` block at all
- **THEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` (an empty `effects_settings` block is created with the default value)

#### Scenario: from_dict rejects an unrecognized format value
- **WHEN** `SignConfig.from_dict({"effects_settings": {"name_display_format": "last_only"}})` is called
- **THEN** the call SHALL raise `ValueError` (only the four documented values are accepted)

### Requirement: Name parsing splits the stored name into first and last

A `parse_name(name) -> (first, last)` helper SHALL live in `lib_shared/name_utils.py`. The helper SHALL:

- Strip leading/trailing whitespace from the input.
- Split the input on whitespace runs (one or more whitespace characters).
- If the result has zero tokens, return `("", "")`.
- If the result has one token, return `(token, "")` — the token is the first name; there is no last name.
- If the result has two or more tokens, return `(tokens[0], " ".join(tokens[1:]))` — the first token is the first name; the rest (joined with single spaces) is the last name. This handles multi-word last names like "Alice Smith Jones" → `("Alice", "Smith Jones")`.

The helper SHALL be used by `format_display_name(...)` to derive the first/last components from the stored `name` field before applying the format. The helper SHALL be tolerant of names with irregular whitespace (multiple spaces, tabs, leading/trailing whitespace).

#### Scenario: A two-word name parses to first + last
- **WHEN** `parse_name("Alice Smith")` is called
- **THEN** it SHALL return `("Alice", "Smith")`

#### Scenario: A single-word name parses to first + empty last
- **WHEN** `parse_name("Madonna")` is called
- **THEN** it SHALL return `("Madonna", "")`

#### Scenario: A multi-word last name parses correctly
- **WHEN** `parse_name("Alice Smith Jones")` is called
- **THEN** it SHALL return `("Alice", "Smith Jones")`

#### Scenario: Whitespace is stripped and collapsed
- **WHEN** `parse_name("  Alice   Smith  ")` is called
- **THEN** it SHALL return `("Alice", "Smith")`

#### Scenario: An empty string parses to empty fields
- **WHEN** `parse_name("")` is called
- **THEN** it SHALL return `("", "")`

### Requirement: format_display_name applies the chosen format to a parsed name

A `format_display_name(name, fmt, all_first_names=None)` helper SHALL live in `lib_shared/name_utils.py`. The helper SHALL:

- Parse the input `name` via `parse_name(name)`.
- If `all_first_names` is `None`, treat it as `[first]` (the duplicate check is a no-op — only the current entry's first name is considered, so there are no "duplicates").
- If the format is `"full"`: return `f"{first} {last}".strip()` (the stored full name, normalized whitespace).
- If the format is `"first_initial"`: return `f"{first} {last[0]}."` if `last` is non-empty, else return `first`. (The period after the initial is part of the format.)
- If the format is `"first"`: return `first`.
- If the format is `"first_initial_if_duplicates"`: count occurrences of `first` in `all_first_names`. If the count is `>= 2` AND `last` is non-empty, return `f"{first} {last[0]}."`. Otherwise return `first`.

The helper SHALL be used in `lib_shared/messages.py` (inside `FilteredMessages._enrich_messages`) to compute `entry.sender_name` from `cfg.senders[<normalized>]["name"]` and `cfg.effects_settings.name_display_format`. The `all_first_names` argument SHALL be precomputed once per call to `_enrich_messages` as the list of `first` parts across ALL entries in `cfg.senders` (so the duplicate check sees the full picture, not just the current entry).

#### Scenario: full format returns the full name
- **WHEN** `format_display_name("Alice Smith", "full")` is called
- **THEN** it SHALL return `"Alice Smith"`

#### Scenario: first_initial format returns first + initial
- **WHEN** `format_display_name("Alice Smith", "first_initial")` is called
- **THEN** it SHALL return `"Alice S."`

#### Scenario: first format returns just the first name
- **WHEN** `format_display_name("Alice Smith", "first")` is called
- **THEN** it SHALL return `"Alice"`

#### Scenario: first_initial_if_duplicates with no duplicates returns just the first name
- **WHEN** `format_display_name("Alice Smith", "first_initial_if_duplicates", all_first_names=["Alice"])` is called
- **THEN** it SHALL return `"Alice"` (no duplicate Alice in the list)

#### Scenario: first_initial_if_duplicates with a duplicate returns first + initial
- **WHEN** `format_display_name("Alice Smith", "first_initial_if_duplicates", all_first_names=["Alice", "Alice"])` is called
- **THEN** it SHALL return `"Alice S."` (Alice appears twice — disambiguate with last initial)

#### Scenario: first_initial_if_duplicates with a single-word duplicate returns just the first name
- **WHEN** `format_display_name("Madonna", "first_initial_if_duplicates", all_first_names=["Madonna", "Madonna"])` is called
- **THEN** it SHALL return `"Madonna"` (no last name to take the initial from — can't disambiguate further)

#### Scenario: Single-word names never get an initial appended
- **WHEN** `format_display_name("Madonna", "first_initial")` is called
- **THEN** it SHALL return `"Madonna"` (no last name → no initial to add)

#### Scenario: first_initial format with multi-word last name uses the first letter of the multi-word last name
- **WHEN** `format_display_name("Alice Smith Jones", "first_initial")` is called
- **THEN** it SHALL return `"Alice S."` (the initial is from the start of the multi-word last name, not the second word)

### Requirement: MessageView.sender_name uses the configured display format

`FilteredMessages._enrich_messages` SHALL compute `entry.sender_name` from `cfg.senders[<normalized>]["name"]` and `cfg.effects_settings.name_display_format` via the `format_display_name` helper. The precomputed `all_first_names` list SHALL be built from `cfg.senders.items()` once per call to `_enrich_messages` (not per message) — the duplicate check needs the full sender set, but it's stable across the buffer's messages.

The `name` field on the senders entry is the operator-supplied string ("Alice Smith", "Madonna", "Alice Smith Jones", etc.). The format is applied at read time; the stored `name` is never mutated.

The display-name resolution SHALL continue to work regardless of `allowed` — the operator can see "From: Alice" (or whatever the format produces) for disallowed senders in the admin UI. The format is a pure function of the stored name; it does NOT consult the enforcement setting or any per-entry classification field.

#### Scenario: An allowed+enabled sender renders with the formatted name
- **WHEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` AND `cfg.senders["+15551234567"]["name"] == "Alice Smith"` AND the `all_first_names` list contains only one "Alice"
- **THEN** for a message from `+15551234567`, `entry.sender_name == "Alice"`

#### Scenario: A blocked sender still gets the formatted display name
- **WHEN** `cfg.senders["+15551234567"]["name"] == "Bob Jones"` AND `cfg.senders["+15551234567"]["allowed"] == false` AND `cfg.effects_settings.name_display_format == "first"`
- **THEN** for a message from `+15551234567`, `entry.sender_name == "Bob"` even though the message is suppressed (the operator can see "From: Bob" in the admin UI for blocked senders)

#### Scenario: The duplicate check is global across all senders
- **WHEN** `cfg.senders` contains TWO entries with first name "Alice" — `{"+15551111111": {"name": "Alice Smith", ...}, "+15552222222": {"name": "Alice Jones", ...}}` — AND `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"`
- **THEN** BOTH Alices SHALL render as `"Alice S."` and `"Alice J."` respectively (the duplicate check sees both entries; the second initial disambiguates them)

#### Scenario: An empty name renders as an empty string
- **WHEN** `cfg.senders["+15551234567"]["name"] == ""` (operator left the name blank)
- **THEN** `entry.sender_name == ""` (no name to format — the display is blank)

#### Scenario: A config update re-applies the format
- **WHEN** the operator flips `cfg.effects_settings.name_display_format` from `"full"` to `"first"` and saves
- **THEN** on the next config update the buffered messages SHALL re-compute `entry.sender_name` using the new format (e.g., "Alice Smith" → "Alice")

### Requirement: v2 → v3 migration moves top-level name_display_format into effects_settings

The `_v2_to_v3` migration in `lib_shared/config_migrations.MIGRATIONS` SHALL:

- If the v2 input has a top-level `name_display_format` key: extract that value, fold it into `effects_settings.name_display_format` (creating `effects_settings` if absent), and DROP the top-level `name_display_format` key.
- If the v2 input lacks `name_display_format` (the typical v2 case — v2 stored configs do not have this field): backfill `effects_settings.name_display_format = "first_initial_if_duplicates"` (creating `effects_settings` if absent).
- If the v2 input has `effects_settings` already with its own `name_display_format`: the existing value wins (the migration is additive — it doesn't overwrite a pre-existing block's value).
- If the v2 input has both a top-level `name_display_format` AND an existing `effects_settings` block without one: the top-level value folds into `effects_settings.name_display_format` (the migration is additive — it doesn't overwrite existing fields in the block).

#### Scenario: A v2 payload without name_display_format migrates with the default
- **WHEN** `migrate({"version": 2, "senders": [...]}, current_version=3)` is called (no `name_display_format` key at top level; no `effects_settings` block)
- **THEN** the returned dict SHALL have `effects_settings: {"name_display_format": "first_initial_if_duplicates", ...}` (effects_settings block created with the default)

#### Scenario: A v2 payload with top-level name_display_format migrates into effects_settings
- **WHEN** `migrate({"version": 2, "name_display_format": "full"}, current_version=3)` is called
- **THEN** the returned dict SHALL have `effects_settings: {"name_display_format": "full", ...}` and SHALL NOT have a top-level `name_display_format` key (the field moved into the nested block)

#### Scenario: A v2 payload with effects_settings existing merges the new field in
- **WHEN** `migrate({"version": 2, "effects_settings": {"fade_seconds": 2.5}}, current_version=3)` is called
- **THEN** the returned dict SHALL have `effects_settings: {"fade_seconds": 2.5, "name_display_format": "first_initial_if_duplicates"}` (existing fields preserved, new field added with default; the merge is additive)

### Requirement: Settings page exposes the name_display_format dropdown in the Senders section

The admin UI's `/settings` page SHALL render a **Name display format** dropdown in the **Senders** section (next to or below the **Enforce senders filter** checkbox). The dropdown SHALL offer four options:

- "Full name (First Last)" → value `"full"`
- "First name + last initial (Alice S.)" → value `"first_initial"`
- "First name only (Alice)" → value `"first"`
- "First name; last initial only if duplicate (default)" → value `"first_initial_if_duplicates"`

The dropdown SHALL pre-select the option matching `cfg.effects_settings.name_display_format`. The field's name SHALL be `name_display_format`. A short helper line above the dropdown explains the behavior: "How sender names appear on the display and in the admin UI. The default adds a last initial only when two or more senders share a first name."

The dropdown is rendered in the Senders section (alongside the enforcement checkbox) even though the value lives inside `effects_settings` — the operator-facing control is co-located with the senders table for UX coherence, but the underlying field path is `cfg.effects_settings.name_display_format`. This is intentional: the dropdown is the operator's input mechanism for the field; the storage location is a separate concern.

The POST handler SHALL parse `request.form.get("name_display_format")` (default `"first_initial_if_duplicates"`) and write it to `cfg.effects_settings.name_display_format`. An unrecognized value SHALL fall back to `"first_initial_if_duplicates"` (defensive against partial / legacy form posts).

#### Scenario: The settings page shows the format dropdown pre-selected
- **WHEN** `cfg.effects_settings.name_display_format == "first"` and the operator views the `/settings` page
- **THEN** the "First name only" option SHALL be selected and the other three options SHALL be unselected

#### Scenario: A POST with name_display_format=first persists the format
- **WHEN** the operator POSTs `name_display_format=first` along with the rest of the form
- **THEN** `cfg.effects_settings.name_display_format == "first"` after save (the field lives at the nested location, not at the top level)

#### Scenario: A POST without the name_display_format field defaults to first_initial_if_duplicates
- **WHEN** the operator POSTs the form without a `name_display_format` field
- **THEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` after save (defensive default)

#### Scenario: A POST with an unrecognized format falls back to the default
- **WHEN** the operator POSTs `name_display_format=last_only` (not a valid value)
- **THEN** `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` after save (defensive fallback — the operator's partial form doesn't corrupt the config)

### Requirement: The display format also applies to the admin UI messages list

The `/messages` admin page (and its playful-redesign variant) renders `MessageView.sender_name` for each row. The rendered name SHALL respect `cfg.effects_settings.name_display_format` — the same format applies to both the sign display and the admin UI. Operators see a consistent "from" identifier everywhere; flipping the format updates both views on the next page load.

#### Scenario: The messages list renders the formatted name
- **WHEN** `cfg.effects_settings.name_display_format == "first"` AND a buffered message has `entry.sender_name == "Alice"` (derived from stored name "Alice Smith")
- **THEN** the `/messages` page SHALL display "From: Alice" for that row

#### Scenario: The playful redesign renders the formatted name
- **WHEN** `cfg.effects_settings.name_display_format == "full"` AND the operator views `/playful/messages`
- **THEN** the playful redesign SHALL display the formatted full name for each row (same format as the original `/messages`)

