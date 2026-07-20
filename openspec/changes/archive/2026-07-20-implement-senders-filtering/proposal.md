## Why

The sign's "Allowed Senders" panel on the `/settings` page (`heart-message-manager/templates/settings.html`) currently iterates `cfg.allowed_senders` — an attribute that does NOT exist on `SignConfig`. The panel renders an empty list and is labeled "informational only" because it has no gating power. The underlying `cfg.senders` field is a phone-to-name dict used purely for display-name resolution on `MessageView.sender_name` — there's no way for the operator to suppress a specific sender without deleting their entry from the list and losing the display-name metadata. And the panel doesn't expose ANY of the affordances the operator needs: no master on/off switch for enforcement, no control over how the display name renders, and no per-sender flag distinguishing "this person is on the allowlist" from "I have their name but I want them suppressed."

The use cases the change addresses (from issue 6 and the linked issue 58, marked as a duplicate):

- An operator who has previously added Alice to the senders list (so her messages display) wants to stop Alice's messages from showing without losing Alice's name in the admin UI. Today the only way to do that is to delete Alice's entry, which loses her display-name metadata.
- The operator wants to disable rules without deleting them ("disable it vs. delete it" — the issue's exact phrasing) — and disable the entire senders filter ("Add an 'enabled' checkbox whether to enforce the list. We don't want to have to delete the entries if we want to turn it off. If off, the names will still be used for displaying texts.").
- The operator wants a per-sender flag for whether they're on the "allowed" list (so the name→number mapping can be maintained even when enforcement is off).
- The operator wants to choose how the display name renders: full name, first + last initial, first only, or first with last initial only when there are duplicate first names (the issue's default).

The behavior change is also a wire-shape breaking change — `SignConfig.CURRENT_VERSION` bumps from 2 to 3, and the migration registry needs to bring stored configs forward on read AND on server startup. While we're touching the wire shape, three top-level fields move into nested settings blocks to keep related concerns grouped:

- **`sign_name` + `timezone`** → moved from the top level (where `sign_name` lives at `cfg.sign.name` and `timezone` is a bare top-level string) into a single `sign_settings` block. Both are sign-identity / operational metadata, not message rendering.
- **`enforcement_enabled`** → moved from the top level into `text_settings`. The text-rendering pipeline is where the message selection algorithm lives, so the master enforcement toggle belongs there.
- **`name_display_format`** → moved from the top level into `effects_settings`. The display format is a presentation knob (how names render on the display and admin UI), and effects settings already groups presentation concerns.

This collapses the operator's mental model: every field belongs to one of four groups — `sign_settings` (identity + operational), `effects_settings` (presentation), `text_settings` (selection + rendering), and the top-level `senders` / `filters` lists (which are the allowlist + rule lists themselves, not configuration about them).

This change introduces a clean taxonomy that covers all of the affordances above:

- **`allowed`** (per-entry on senders entries): is this sender on the allowlist? A boolean. `True` means "this sender's messages render when enforcement is on"; `False` means "this sender's messages are blocked when enforcement is on, but the name is preserved for display." Default for new entries: `True` (every pre-existing sender was implicitly on the allowlist).
- **`text_settings.enforcement_enabled`**: the master on/off switch for the entire senders filter. When `False`, the senders filter is bypassed entirely (every message renders regardless of any per-entry state); names still resolve for display. Picked via a single checkbox. Default: `True`.
- **`effects_settings.name_display_format`**: how the operator's stored names render on the display and in the admin UI. Values: `"full"`, `"first_initial"`, `"first"`, or `"first_initial_if_duplicates"` (default). See the `name-display-format` capability for the full semantics.

**There is NO per-entry `status: "enabled"|"disabled"` lifecycle field on senders entries.** The issue's "let's still leave the filters in the config, just with an additional status attribute" sentence refers to the GLOBAL `enforcement_enabled` toggle on the LIST itself, not a per-entry lifecycle on each entry. The master toggle is the issue's "disable without delete at the list level" affordance: flip the toggle off, every entry stays in the config but the filter is bypassed. Flipping it back on restores filtering without re-typing entries. A per-entry lifecycle would only matter if individual entries could be muted while the master toggle stayed on — that's not requested and would add a second axis of complexity on top of `allowed`.

The behavior is hard-coded allowlist-only — there is no mode radio (the issue does not ask for blocklist support, and adding blocklist semantics on top of an allowlist-only model is a future extension if a use case emerges). The senders list IS the allowlist: when enforcement is on, only entries with `allowed=True` render; everyone else is suppressed.

The per-entry `allowed` field resolves a redundancy the draft had: `FilterRule.type=sender` overlaps with `SignConfig.senders` (both match senders). With the unified taxonomy, the cleanest decision is to **remove `FilterRule.type=sender` entirely** — `SignConfig.senders` is the single source of truth for sender-level matching, with richer metadata (display name, allowed flag). `FilterRule` then has a clearer purpose: keyword/regex (content) and message-ID (specific message) suppression.

The behavior is configurable per nested field. A sender renders iff:

1. `cfg.text_settings.enforcement_enabled == True` (the master switch is on), AND
2. `cfg.senders[<normalized sender>]["allowed"] is True`.

Filtering still happens at egress (no change to ingress — every Twilio delivery is stored; a config update can flip a previously-suppressed message back to visible without re-ingestion).

While we're touching this code:

- The broken `cfg.allowed_senders` template iteration is replaced with the correct iteration over `cfg.senders` (now `dict[str, dict]` after the entry-shape change). The template's existing Name / Phone / Remove columns stay; an **Allowed** checkbox column is added (default `True`). There is NO per-row Status column — the master `enforcement_enabled` toggle is the only lifecycle control.
- The "Enforce senders filter" checkbox is added at the top of the Senders section (above the table). The "Name display format" dropdown is added beside the enforcement checkbox. There is no Allowlist / Blocklist selector — the allowlist interpretation is the only mode.
- `FilterRule.type=sender` is REMOVED from the wire (not just the UI). The Filter Rules UI shows only `keyword`, `regex`, `message` — and existing stored configs with `type=sender` rules are migrated to entries in `SignConfig.senders` (the single source of truth), with `allowed=False` (since the original rule was always a suppression rule, `allowed=False` carries the same semantic under the v3 allowlist-only model).
- The surviving FilterRule types (`keyword`, `regex`, `message`) get a per-row `Status` checkbox (`Enabled` checked / `Disabled` unchecked) — the new lifecycle affordance for rules (separate from the senders list, where lifecycle lives at the LIST level via `text_settings.enforcement_enabled`).
- Phone matching is normalized to last-10-digits via `phone_utils.normalize_phone` so formatting differences (`+1 (555) 123-4567` vs `5551234567`) match correctly.
- The settings page gains an **enforcement toggle** (in the Senders section) and a **name display format dropdown** (in the Effects section, since that's where the field lives after the move). There is no mode radio.
- `SignConfig.sign` (the existing `SignSettings` block) is renamed to `SignConfig.sign_settings` for naming consistency with `effects_settings` and `text_settings`. Its `name` field is renamed to `sign_name` (matching the form field name and clarifying what "name" means in context). `timezone` joins `sign_name` inside the new `sign_settings` block.

## What Changes

### Top-level vs nested layout (v3)

The wire shape (and the in-memory `SignConfig` layout) becomes:

```python
SignConfig(
    version=3,
    filters=[...],                   # top-level — the rule list itself
    senders={...},                   # top-level — the allowlist itself (now dict[str, dict])
    sign_settings=SignSettings(      # renamed from `sign`; gains `timezone`
        sign_name="Lindsay's Heart", # renamed from `name`
        timezone="US/Pacific",
    ),
    effects_settings=EffectsSettings(
        effects=[...],
        fade_seconds=...,
        hold_seconds=...,
        intro_seconds=...,
        idle_seconds=...,
        recent_count=...,
        name_display_format="first_initial_if_duplicates",  # NEW (moved from top-level)
    ),
    text_settings=TextSettings(
        speed=3,
        color=0xFF0000,
        text_effect="scroll",
        enforcement_enabled=True,     # NEW (moved from top-level)
    ),
)
```

### Senders entry shape (the v3 wire-format changes)

- `SignConfig.senders` field changes shape. The current internal shape is `dict[str, str]` (phone → name). The new shape is `dict[str, dict]` mapping NORMALIZED phone (last-10-digits via `phone_utils.normalize_phone`) to a value object `{"name": str, "allowed": bool, "phone": str}`. The dict key is the normalized form (for O(1) lookup after normalizing the incoming sender); the value's `phone` field preserves the operator-supplied original (for round-trip display in the admin UI). Default value: `allowed=True` (back-compat: every pre-existing sender was implicitly on the allowlist).
- The wire shape for `senders` changes from `[{"phone": str, "name": str}]` (v2) to `[{"phone": str, "name": str, "allowed": bool}]` (v3). Each entry carries `allowed` explicitly on the wire. There is no `status` field on senders entries — lifecycle is the LIST-level `text_settings.enforcement_enabled` toggle, not a per-entry field.

### New and renamed top-level fields (the structural refactor)

- `sign_settings` (NEW block at the top level, replacing the prior `sign` attribute) — contains:
  - `sign_name: str` — display name. Renamed from `SignSettings.name` for clarity (matches the existing HTML form field name and disambiguates "the sign's name" from generic "name").
  - `timezone: str` — IANA timezone string. Moved from top-level `cfg.timezone` into `cfg.sign_settings.timezone`.
- `effects_settings` (existing block, extended):
  - `name_display_format: "full" | "first_initial" | "first" | "first_initial_if_duplicates"` — NEW. Moved from top-level into `effects_settings.name_display_format`. See the `name-display-format` capability for the full semantics.
- `text_settings` (existing block, extended):
  - `enforcement_enabled: bool` — NEW. Moved from top-level into `text_settings.enforcement_enabled`. See the `senders-status` capability. Default: `True`.
- There is NO `mode` field anywhere. The allowlist interpretation is the only behavior.

### FilterRule changes

- `FilterRule` gains `status: "enabled" | "disabled"` (replacing the previous draft's `enabled: bool` — a more expressive enum that's extensible to future soft-delete states). Default: `"enabled"` (back-compat for v2 stored configs that had `enabled=True`). The `action` field stays `"suppress"` as the only v1 value (action=allow is a future extension if a use case emerges — for now, every rule suppresses when matched, and senders list entries with `allowed=True` are the only allow mechanism). The FilterRule.status lifecycle is independent of the senders list — FilterRule.status is for "disable a rule vs delete a rule"; senders lifecycle is the LIST-level `text_settings.enforcement_enabled` toggle.
- `FilterRule.type="sender"` is REMOVED from the wire. The Filter Rules UI shows only `keyword`, `regex`, `message`. Stored v2 configs with `type="sender"` rules are migrated: each such rule is converted to an entry in `SignConfig.senders` with `allowed=False`, `name=<rule.pattern>` (the rule's pattern becomes the senders entry's display name as a best-effort; the phone is the rule's pattern), and the rule itself is dropped from `filters`. After the migration, no `type=sender` rules exist in stored configs.

### New helper modules

- A new `lib_shared/phone_utils.py` module houses `normalize_phone(s) -> str` — strips non-digit characters, returns `"+1" + last_10_digits` when 10 or 11 digits remain (11 only if leading digit is `"1"`); passthrough for malformed input. Used by: senders dict key generation (on `from_dict` and on form save) and the `_enrich_messages` lookup.
- A new `lib_shared/name_utils.py` module houses two helpers — `parse_name(s) -> (first, last)` (splits the operator-supplied name on whitespace, returning first/last parts; handles single-word, multi-word-last, and whitespace-tolerance edge cases) and `format_display_name(name, fmt, all_first_names=None) -> str` (applies the chosen format — see the `name-display-format` capability for the four format options). Used by `FilteredMessages._enrich_messages` to compute `entry.sender_name`.

### Filtering logic

- `FilteredMessages._enrich_messages` gains a step that runs after the existing `_apply_filter` loop: normalize `entry.message.sender`, consult `cfg.text_settings.enforcement_enabled`, then look up the normalized sender in `cfg.senders`. The full decision rule is in the `senders-status` capability — but the in-message step is: when the senders list suppresses the message AND no FilterRule matched, append a synthetic rule `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}` to `entry.rules` and set `entry.suppressed = True`. When `cfg.text_settings.enforcement_enabled == False`, the senders list makes no suppression decision at all (no synthetic marker). The display-name lookup site updates from `self._config.senders.get(sender)` (returns str) to `format_display_name(self._config.senders.get(normalize_phone(sender), {}).get("name", ""), self._config.effects_settings.name_display_format, all_first_names)` — display names resolve via the configured format and respect the duplicate-first-name logic.
- `FilteredMessages._apply_filter` skips rules where `rule.status == "disabled"`. (The previous draft's `rule.enabled is False` check is renamed to `rule.status != "enabled"`.)

### Settings page UI

- The settings page (`heart-message-manager/templates/settings.html`) replaces the broken "Allowed Senders" panel (which iterates the non-existent `cfg.allowed_senders`) with a proper iteration over `cfg.senders.items()` under a new section title **"Senders"**. The new panel:
  - At the top: a single **Enforce senders filter** checkbox (the master toggle for `cfg.text_settings.enforcement_enabled`) and a **Name display format** dropdown (see `name-display-format` for the full UI specs). There is NO mode radio.
  - A short helper line above the table: "Phone numbers are normalized to +1XXXXXXXXXX."
  - The table keeps the existing Name / Phone / Remove columns and adds ONE new column: **Allowed** (checkbox). The Allowed checkbox is checked iff the entry's `allowed is True`. The Phone input shows the NORMALIZED phone (the dict key, e.g. `+15551234567`) for visual consistency across rows; the operator's original input is preserved in `cfg.senders[<key>]["phone"]` for round-trip wire fidelity.
  - Default for new rows: `Allowed=checked`.
  - The form posts `enforcement_enabled` (checkbox, value `"1"` when checked), `name_display_format` (dropdown), and the per-row parallel lists `sender_name`, `sender_phone`, and `sender_allowed` (checkbox list indexed by row; checked → `allowed=True`, absent → `allowed=False`).
- The settings page's Filter Rules panel gets a per-row `Status` checkbox column (checked iff `cfg.filters[i].status == "enabled"`). The Add Rule form's `Type` dropdown offers `keyword`, `regex`, `message` only (the `sender` option is removed — existing stored `type=sender` rules were migrated to senders list entries during the upgrade). The Add Rule form includes an `Enabled` checkbox defaulting to checked.
- The settings page's Sign Identity panel reads `sign_name` from `cfg.sign_settings.sign_name` (was `cfg.sign.name`) and `timezone` from `cfg.sign_settings.timezone` (was top-level `cfg.timezone`). The form posts `sign_name` and `timezone` (no field rename needed — the HTML form field is already called `sign_name`; the attribute rename is in the Python model only).

### Versioning & migration

- `SignConfig.CURRENT_VERSION` bumps from `2` to `3`. A `_v2_to_v3` migration in `lib_shared/config_migrations.MIGRATIONS`:
  - **Structural moves** (top-level → nested):
    - Move top-level `timezone` (string) → `sign_settings.timezone`. Create `sign_settings` if absent (with `sign_name` defaulted to `"Lindsay's Heart"`).
    - Move `sign` block (v2 shape: `{"name": str}`) → `sign_settings` block (v3 shape: `{"sign_name": str, "timezone": str}`). The `name` field is renamed to `sign_name`. The `timezone` field is filled from the top-level value (or defaulted to `"US/Pacific"`). Drop the now-empty `sign` top-level key.
    - Move top-level `enforcement_enabled` → `text_settings.enforcement_enabled`. Create `text_settings` if absent (with `speed=3, color=0xFF0000, text_effect="scroll"` defaults).
    - Move top-level `name_display_format` → `effects_settings.name_display_format`. Create `effects_settings` if absent (with loader-driven defaults for `effects` and `None` for pacing fields that fall through to the loader).
  - **Senders entry migration** (the existing v3 changes):
    - For each entry in `data["senders"]` (wire shape: list of dicts): map a v2 `status` field (legacy/draft values `"allowed"|"blocked"`) to the new `allowed` boolean field (`"allowed"` → `True`, `"blocked"` → `False`). If the v2 entry has no `status` field (the actual current v2 wire shape), backfill `allowed=True`. The migrated entry SHALL NOT contain a `status` field — there is no per-entry lifecycle in v3.
  - **FilterRule migration** (the existing v3 changes):
    - For each rule in `data["filters"]` with `type=sender`: convert to a `SignConfig.senders` entry (with `allowed=False`, `name=rule.pattern`, `phone=rule.pattern` — no status field). Drop the rule from `filters`. (This is the migration for the now-removed `type=sender` rule type — sender matching moves to the senders list.)
    - For each remaining rule (non-sender) in `data["filters"]`: rename `enabled` (bool) to `status` ("enabled"|"disabled"). `enabled=True` → `status="enabled"`; `enabled=False` → `status="disabled"`. (This is a separate, FilterRule-level lifecycle, NOT a per-sender field.)
    - Handle the legacy v1 senders dict shape (`{"+15551234567": "Alice"}`) by converting to the list shape on read.
  - **Version bump**: Set `version` to `3`.
  - The existing `migrate_on_startup` flow (added in the prior `runtime-sign-config` change) picks this up automatically; no caller changes needed.

## Migration & Versioning

This is a wire-shape change that combines the v3 senders-filtering changes (already covered) with a structural refactor that moves four top-level fields into nested settings blocks:

- `version` is bumped to 3 in `SignConfig.CURRENT_VERSION`.
- A new `_v2_to_v3` migration function is added to `lib_shared.config_migrations.MIGRATIONS`. It:
  - Returns a shallow copy of the input (does not mutate the caller's dict).
  - **Structural moves**: extracts `sign` (rename `name` → `sign_name`, move `timezone` from top-level into the new block, rename the block key from `sign` to `sign_settings`), extracts `enforcement_enabled` into `text_settings.enforcement_enabled` (creating `text_settings` if absent), extracts `name_display_format` into `effects_settings.name_display_format` (creating `effects_settings` if absent).
  - **Senders migration**: legacy `status` field (`"allowed"|"blocked"`) → `allowed` boolean (`"allowed"` → `True`, `"blocked"` → `False`). v2 entries without a legacy status field backfill `allowed=True`. The migrated entry SHALL NOT contain a `status` field.
  - **FilterRule migration**: `type=sender` rules → senders entries (allowed=False, name=pattern, phone=pattern; no status field). Drop the rule. Non-sender rules: rename `enabled` (bool) → `status` (enum).
  - Set `version` to `3`.
- The existing `migrate_on_startup` flow picks this up automatically. Stored v2 configs in SQLite + S3 are brought forward to v3 on server startup, and the migrated config is published to MQTT so connected devices re-read it on their next envelope.
- On the device side, `MessageManager._handle_config` calls `update_from_dict`, which runs the migration at the top — a v2 envelope arriving over MQTT is transparently upgraded to v3 in the device's in-memory config.

## Capabilities

### New Capabilities

- `senders-status`: `SignConfig.senders` entries each carry a single field:
  - `allowed`: bool — is this sender on the allowlist? Default `True` (back-compat: every pre-existing senders entry was implicitly on the allowlist).
  - A sender renders under the allowlist-only rule in this capability (`cfg.text_settings.enforcement_enabled` AND entry present AND `allowed=True`). Filtering happens at egress. The dict key is the NORMALIZED phone; the value's `phone` field preserves the operator's original input for round-trip display fidelity. The `cfg.text_settings.enforcement_enabled` toggle bypasses filtering entirely when off (every message renders; names still resolve). There is NO per-entry lifecycle `status` field — lifecycle is the LIST-level toggle, which is the issue's explicit "disable without delete at the list level" affordance.
- `name-display-format`: `EffectsSettings.name_display_format` carries the display format with four valid values:
  - `"full"` — Full name (first + last). Example: "Alice Smith".
  - `"first_initial"` — First name + initial of last name. Example: "Alice S."
  - `"first"` — First name only. Example: "Alice".
  - `"first_initial_if_duplicates"` — First name only by default; first + last initial when the first name appears in two or more entries. Default value (the issue's default).
  - The format governs how `MessageView.sender_name` is computed from the stored `name` field. The stored name is the operator-supplied full string; the format applies at read time without mutating the stored value.
- `filter-rule-status`: every `FilterRule` carries a `status: "enabled" | "disabled"` field (replacing the previous draft's `enabled: bool` — the enum is extensible to future soft-delete states). `_apply_filter` skips rules where `status != "enabled"`. The FilterRule `action` field stays `"suppress"` as the only v1 value (action=allow is a future extension). `FilterRule.type` is restricted to `"keyword"`, `"regex"`, `"message"` — the `sender` type is REMOVED from the wire (sender matching moved to `SignConfig.senders`). FilterRule evaluation is independent of `cfg.text_settings.enforcement_enabled` — even when enforcement is off, enabled FilterRules still suppress matching messages. (FilterRule.status is a SEPARATE lifecycle affordance from the senders-list `enforcement_enabled` toggle; they serve different purposes.)

### Modified Capabilities

- `sign-runtime-config` (in `openspec/changes/runtime-sign-config/specs/sign-runtime-config`): the v2 `SignConfig` wire shape (top-level `sign: {name}`, top-level `timezone: str`, top-level `enforcement_enabled: bool`, top-level `name_display_format: str`) becomes the v3 shape (nested `sign_settings: {sign_name, timezone}`, nested `text_settings.enforcement_enabled`, nested `effects_settings.name_display_format`). The new `allowed` field on each `senders` entry lives inside the existing top-level `senders` block.
- `admin-ui` (in `openspec/changes/flask-management-app/specs/admin-ui`): the broken "Allowed Senders" panel is REPLACED with a proper iteration over `cfg.senders.items()` under a new section title **"Senders"**. The panel gains two NEW top-level controls (enforcement checkbox, name display format dropdown) AND a per-row Allowed checkbox. The Phone field shows the normalized phone (`+15551234567`); a helper line explains the normalization. The Filter Rules panel gets a per-row `Status` checkbox and the `sender` type is removed from the Add Rule dropdown (now also removed from the wire). The Sign Identity panel reads `sign_name` and `timezone` from the new nested `cfg.sign_settings` block.
- `config-storage` (in `openspec/changes/flask-management-app/specs/config-storage`): `SignConfig` wire shape undergoes the structural refactor described above — `sign` renamed to `sign_settings` with `sign_name`/`timezone` fields, `enforcement_enabled` moves into `text_settings`, `name_display_format` moves into `effects_settings`. The senders wire shape changes (each entry gains `allowed` boolean field — replacing the v2 `status` field with a value rename, or backfilled for v2 entries without a status field). `FilterRule` wire shape changes (`enabled` bool → `status` enum; `type="sender"` removed). `version` bumps to 3. A `_v2_to_v3` migration brings stored configs forward on read AND on server startup. The migration handles structural moves (top-level → nested), field renames (legacy senders.status → senders.allowed with value rename + no new status lifecycle), filter rule type removal (type=sender → senders entry with allowed=False), and field type changes (FilterRule.enabled bool → status enum).
- `twilio-webhook` (in `openspec/changes/flask-management-app/specs/twilio-webhook`): ingress behavior is UNCHANGED. Every Twilio delivery still lands in SQLite + S3 + the MQTT envelope — the senders list and `text_settings.enforcement_enabled` do NOT gate ingress. The change is purely an egress decision at `MessageManager.get_messages(suppress=True)` time.
- `message-storage` (in `openspec/changes/flask-management-app/specs/message-storage`): the `MessageView.rules` list can now contain a synthetic `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}` entry when the senders list suppressed a message and no `FilterRule` matched. The wire shape of `MessageView.to_dict()` is unchanged otherwise. `MessageView.sender_name` is now computed via `format_display_name` (the configured `effects_settings.name_display_format` is applied).

## Impact

- **New files:**
  - `lib_shared/phone_utils.py` — `normalize_phone(s) -> str` (last-10-digits, leading `+1` when available, passthrough when no 10-digit suffix).
  - `lib_shared/name_utils.py` — `parse_name(s) -> (first, last)` and `format_display_name(name, fmt, all_first_names=None) -> str` (handles all four `name_display_format` options, including the duplicate-first-name logic).
  - `tests/phone_utils_test.py` — direct unit tests for `normalize_phone`.
  - `tests/name_utils_test.py` — direct unit tests for `parse_name` and `format_display_name` covering all four format options + edge cases (single-word names, multi-word last names, whitespace tolerance, duplicate detection).
  - `tests/senders_status_test.py` — per-entry allowed logic; end-to-end through `MessageManager` for allowed / disallowed / unlisted; enforcement toggle bypass; egress-not-ingress guarantee; config-update re-enrich flips state; wire round-trip preserves original phone format; the type=sender-to-senders-entry migration; no per-entry status lifecycle (only `allowed`); the `cfg.text_settings.enforcement_enabled` location.
  - `tests/filter_rule_status_test.py` — `_apply_filter` skips `status != "enabled"` rules; wire-shape round-trip with `status` enum; `type=sender` rules are no longer accepted at `from_dict`.
  - `tests/senders_status_ui_test.py` — Flask POST handler accepts the new per-row Allowed checkbox field (indexed by row); the broken `cfg.allowed_senders` template iteration is gone; Filter Rules UI has no `sender` option; enforcement toggle + name display format dropdown are parsed and persisted; there is no per-row Status column or per-row sender_status field.
  - `tests/sign_settings_test.py` — `SignSettings.sign_name` and `SignSettings.timezone` round-trip through `to_dict`/`from_dict`; defaults are sensible; nested-on-SignConfig layout is correct.
  - `tests/nested_settings_migration_test.py` — v2 → v3 migration moves `sign` → `sign_settings` (with `name` → `sign_name` rename), moves top-level `timezone` into `sign_settings.timezone`, moves `enforcement_enabled` into `text_settings.enforcement_enabled`, moves `name_display_format` into `effects_settings.name_display_format`; v2 inputs that lack `text_settings` / `effects_settings` get them created with sensible defaults; v2 inputs that already have those blocks get the new fields merged in (not overwritten).
- **Modified files:**
  - `lib_shared/models.py` — `SignSettings` class: rename `name` → `sign_name` and ADD `timezone` field. `TextSettings` class: ADD `enforcement_enabled` field. `EffectsSettings` class: ADD `name_display_format` field. `SignConfig`: rename attribute `sign` → `sign_settings` (the type also changes from `SignSettings` to the same class — the rename is purely on the attribute); remove top-level `timezone` parameter; remove top-level `enforcement_enabled` parameter; remove top-level `name_display_format` parameter; bump `CURRENT_VERSION` to 3. Change `SignConfig.senders` to `dict[str, dict]` (normalized_phone → `{"name", "allowed", "phone"}`). Change `FilterRule` to have `status: "enabled" | "disabled"` (replacing `enabled: bool`); remove `FilterRule.type="sender"` from the valid type set. There is no `mode` field anywhere — the allowlist interpretation is the only behavior. There is no per-sender `status` field — lifecycle is the LIST-level `text_settings.enforcement_enabled` toggle.
  - `lib_shared/config_migrations.py` — add `_v2_to_v3` migration + registry entry. The migration handles: (a) structural moves: top-level `timezone` → `sign_settings.timezone`; `sign` block rename to `sign_settings` and `name` → `sign_name` rename; top-level `enforcement_enabled` → `text_settings.enforcement_enabled` (creating `text_settings` if absent); top-level `name_display_format` → `effects_settings.name_display_format` (creating `effects_settings` if absent); (b) senders migration: legacy `status="allowed"|"blocked"` → `allowed: bool`, with `allowed=True` backfilled for v2 entries without the legacy status field; (c) FilterRule migration: `type=sender` → senders entry (allowed=False; no status field); (d) FilterRule.enabled bool → FilterRule.status enum rename.
  - `lib_shared/messages.py` — add the allowlist-only senders check inside `_enrich_messages` (after the existing `_apply_filter` loop); update the display-name lookup to use `format_display_name` with the configured `effects_settings.name_display_format` and the precomputed `all_first_names` list; remove the `type == "sender"` branch from `_matches` (since sender matching moved to the senders list); skip `status != "enabled"` rules in `_apply_filter`. Reads `enforcement_enabled` from `cfg.text_settings.enforcement_enabled` (not from a top-level field).
  - `heart-message-manager/main.py` — extend the `/settings` POST handler to parse `cfg.text_settings.enforcement_enabled` (checkbox) and `cfg.effects_settings.name_display_format` (dropdown) AND the per-row `sender_allowed` checkbox list (indexed by row); rebuild `cfg.senders` from the form's rows preserving per-row allowed. Update `cfg.sign_settings.sign_name` (was `cfg.sign.name`) and `cfg.sign_settings.timezone` (was top-level `cfg.timezone`). The existing `sign_name` / `timezone` / `filter_action` handling is unchanged in field NAMES — the HTML form fields are already called `sign_name` and `timezone`, so the form parsing is unchanged; the assignment targets move into the nested block. There is NO `sender_mode` field, and NO per-row `sender_status` field.
  - `heart-message-manager/templates/settings.html` — REPLACE the broken `cfg.allowed_senders` iteration with `cfg.senders.items()`; ADD an enforcement checkbox + name display format dropdown at the top of the Senders section (NO mode radio); ADD an Allowed checkbox column per row (NO Status column); REMOVE the `sender` option from the Filter Rules Add Rule dropdown; ADD a Status checkbox column to the Filter Rules table (this is for the FilterRule lifecycle — a separate feature from the senders list). Update Sign Identity reads to `cfg.sign_settings.sign_name` and `cfg.sign_settings.timezone`.
  - `heart-matrix-controller/patterns/browser_media_overlay.py`, `lib_shared/message_manager.py`, `lib_shared/effects_coordinator.py` — any consumer that read `cfg.timezone` directly needs to be updated to `cfg.sign_settings.timezone`. (`cfg.timezone` was previously a top-level attribute; the consumer code lives in the broader codebase and is out-of-scope for these tests but mentioned in the impact list for awareness.)
  - `heart-message-manager/sqlite.py` — no shape change; the `SignConfig.to_dict()` round-trip already picks up the new fields. The startup migration path (added in the prior change) handles the v2 → v3 upgrade automatically.
- **No new dependencies.** `re` and stdlib are already in use.
- **No MQTT wire-shape change for messages** — only the config envelope changes (senders entry gains `allowed` boolean; `FilterRule.enabled` → `status`; `FilterRule.type=sender` removed; `sign` block renamed to `sign_settings` with `sign_name`/`timezone`; `enforcement_enabled` moves into `text_settings`; `name_display_format` moves into `effects_settings`; version bump).
- **No new Flask route** — the existing `/settings` POST handler is extended; the page already lives at `/settings`.
- **Behavior change worth flagging:** the senders filter is now allowlist-only with enforcement enabled by default — the same end-state as the previous "hardcoded allowlist" draft, with the addition of a master on/off switch. Under the default, senders NOT in the list are suppressed (previously they passed through with no display name). Operators who upgrade and don't add their known senders will see all their unlisted senders disappear from the display. The v2 → v3 migration backfills `text_settings.enforcement_enabled=True`, so operators who want to bypass filtering entirely (to avoid the manual re-adding step) can flip the master toggle off via the new `/settings` checkbox. There is no blocklist interpretation — operators who wanted blocklist semantics would need a separate future change.
- **Structural change worth flagging:** four fields that were top-level in v2 (`timezone`, `sign.name`, `enforcement_enabled`, `name_display_format`) move into nested settings blocks. Any code (test, script, or external consumer) that read these as top-level attributes will need to update to the new nested locations. The migration brings stored configs forward transparently; the only impact is on code that imports `SignConfig` directly and reads these attributes.