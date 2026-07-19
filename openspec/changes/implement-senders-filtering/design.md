## Context

The current `SignConfig` model in `lib_shared/models.py` carries a `senders: dict[str, str]` field that exists for one purpose: resolving a phone number to a human-readable display name when `FilteredMessages._enrich_messages` builds a `MessageView`. The phone-to-name map has no gating power — every message passes through `_enrich_messages`, gets a `sender_name` resolved from the dict (or `None` if absent), and emerges with `entry.suppressed = False` unless one of the configured `FilterRule`s matches.

The settings page (`heart-message-manager/templates/settings.html`) has an "Allowed Senders" panel that iterates `cfg.allowed_senders`, an attribute that **does not exist on `SignConfig`** — `SignConfig.__init__` carries a deprecated `allowed_senders: list[str] | None = None` parameter that's marked "Deprecated, ignored (kept for backward compat with tests)" in its docstring and silently discarded by the rest of the class. The panel renders an empty list, the "info only" caption is technically accurate, and the panel's wire path (the `sender_name` / `sender_phone` form fields parsed in `main.py:702-710`) actually populates `cfg.senders` — the working model — not `cfg.allowed_senders`. Two parallel storage paths exist and only one of them is alive.

The Filter Rules panel offers four rule types — `keyword`, `regex`, `sender`, `message`. The `sender` rule type's match logic in `FilteredMessages._matches` is `msg.sender == rule.pattern` — an EXACT string match with no normalization. A rule created with `pattern = "+1 (555) 123-4567"` will not match a routed sender like `+15551234567`, and vice-versa. The issue's "Relax the number filter to ignore the routing codes, ie. just match the last 10 digits, remove -'s, etc." applies here.

The current top-level `SignConfig` shape is mixed: `senders`, `filters`, `version`, `sign` (a nested `SignSettings` block), `timezone` (bare top-level string), `effects_settings` (nested `EffectsSettings` block), and `text_settings` (nested `TextSettings` block). The mix is the result of incremental growth — `sign` was nested early on as a single-field block, then `effects_settings` and `text_settings` followed the nested pattern, but `timezone` and the operator-tunable fields for the upcoming senders filter (`enforcement_enabled`, `name_display_format`) never got folded into the right blocks. The change is the right moment to consolidate: every field belongs to one of four groups — `sign_settings` (identity + operational), `effects_settings` (presentation), `text_settings` (selection + rendering), and the top-level `senders` / `filters` lists (which are the allowlist + rule lists themselves, not configuration about them).

The use cases the design addresses (issue 6 and the linked issue 58):

- An operator who has previously added Alice to the senders list wants to stop Alice's messages from showing on the display without losing Alice's name in the admin UI. Today the only way to do that is to delete Alice's entry, which loses her display-name metadata. The design introduces a per-entry `allowed` flag so the operator can flip Alice to `allowed=False` while keeping the name (their messages are blocked when enforcement is on, but their display-name metadata is preserved).
- The operator wants to "disable it vs. delete it" for FilterRules — a separate lifecycle affordance, applied uniformly to rules. The design introduces a per-rule status axis (replacing the previous draft's `enabled: bool` with the enum `"enabled" | "disabled"`, extensible to future soft-delete states).
- The operator wants a master "Enforce senders filter" checkbox — issue 6 says "Add an 'enabled' checkbox whether to enforce the list. We don't want to have to delete the entries if we want to turn it off. If off, the names will still be used for displaying texts. let's still leave the filters in the config, just with an additional status attribute." The design adds `text_settings.enforcement_enabled` (the master on/off switch lives inside the text-rendering block because that's where the message selection algorithm is). When off, the filter is bypassed entirely (every message renders; names still resolve for display). The "additional status attribute" referenced in the issue is this MASTER toggle on the LIST itself, not a per-entry lifecycle.
- The operator wants a per-sender flag for whether they're on the "allowed" list — issue 6 says "We should also add a flag for each sender whether they're on the 'allowed' list." The design adds a per-entry `allowed` boolean.
- The operator wants to control how the display name renders — issue 6 lists four formats: full name, first + last initial, first only, first with last initial only when there are duplicate first names (default). The design adds `effects_settings.name_display_format`.

The design introduces a single per-entry field on senders AND three new fields added to existing nested settings blocks (no new top-level fields):

- **`allowed`** (the per-entry effect axis on senders entries): is this sender on the allowlist? A boolean. `True` means "this sender's messages render when enforcement is on"; `False` means "this sender's messages are blocked when enforcement is on, but the name is preserved for display." Default for new entries: `True`.
- **`text_settings.enforcement_enabled`** (master on/off switch): when `False`, the filter is bypassed entirely (every message renders regardless of per-entry state); names still resolve for display. Default: `True`. Lives inside `text_settings` because that's where the message selection algorithm is.
- **`effects_settings.name_display_format`** (presentation knob): how the stored names render on the display and admin UI. Lives inside `effects_settings` because that's where presentation knobs already group (pacing, effects list, recent_count).

**There is NO per-entry lifecycle field on senders entries.** The "disable without delete" affordance the issue asks for is provided by the `text_settings.enforcement_enabled` toggle (the issue's "additional status attribute"). A per-entry lifecycle would only matter if individual entries could be muted while the master toggle stayed on — that's not requested, and the master toggle covers the stated use case (operator can turn off the whole filter without losing any entries).

The behavior is hard-coded allowlist-only — there is no mode field (`off` / `allowlist` / `blocklist`). The senders list IS the allowlist: when enforcement is on, only entries with `allowed=True` render; everyone else is suppressed. Issue 6 only asks for the allowed-senders list — blocklist semantics (if ever needed) would be a separate future change.

The `FilterRule` model carries its own lifecycle (`status: "enabled"|"disabled"`) — but that's a SEPARATE feature for "disable a rule vs delete a rule", distinct from the senders list. Both features share the lifecycle vocabulary but apply to different objects: FilterRule.status is per-rule; `text_settings.enforcement_enabled` is the LIST-level master toggle on the senders filter. They're not the same field, and they don't share a single "status" namespace — `enforcement_enabled` is a boolean (on/off), `FilterRule.status` is an enum.

The taxonomy also resolves a redundancy: `FilterRule.type=sender` overlaps with `SignConfig.senders` (both match senders). With the unified taxonomy, the cleanest decision is to **remove `FilterRule.type=sender` entirely** — `SignConfig.senders` is the single source of truth for sender-level matching, with display-name metadata and the `allowed` flag. `FilterRule` then has a clearer purpose: keyword/regex (content) and message-ID (specific message) suppression.

The existing re-enrichment machinery (`MessageManager._handle_config` calls `_enrich_messages` on the whole buffer after a config update) is the natural extension point — an entry added, an `allowed` flag flipped, or the enforcement checkbox flipped all fire `_handle_config`, which re-classifies every buffered `MessageView`. No new event channel is needed for the operator's actions to take effect on previously-received messages.

`SignConfig` is already wired end-to-end: SQLite stores it, S3 snapshots it, MQTT publishes it as a `type="config"` `MessageEnvelope`, the Pi's `MessageManager._handle_config` applies it via `update_from_dict`. The `MIGRATIONS` registry in `lib_shared/config_migrations.py` brings older versions forward on read AND on server startup. Adding the per-entry `allowed` field, removing `FilterRule.type=sender`, renaming `FilterRule.enabled` to `FilterRule.status`, and the structural moves (top-level → nested blocks) are wire-shape changes that fit cleanly into this existing migration path — the previous change (`runtime-sign-config`) added the registry for the same purpose.

## Goals / Non-Goals

**Goals:**

- `SignConfig.senders` entries each carry ONE new field:
  - `allowed: bool` — the per-entry classification. Default `True` (back-compat: every pre-existing senders entry was implicitly on the allowlist).
  - A sender renders iff their entry has `allowed=True` AND `cfg.text_settings.enforcement_enabled` is on. Senders with `allowed=False` OR not in the list are suppressed when enforcement is on; everyone renders when enforcement is off.
  - **There is no per-entry lifecycle field.** The "disable without delete" affordance is the `text_settings.enforcement_enabled` toggle (see below).
- `text_settings.enforcement_enabled: bool` (the master on/off switch for the senders filter — the issue's "additional status attribute" referenced on the LIST). When `False`, the filter is bypassed entirely; names still resolve for display. Default `True`. Flipping this toggle off preserves every entry in the config; flipping it back on restores filtering without re-typing entries. Lives inside `text_settings` because the message selection algorithm lives there.
- `effects_settings.name_display_format` (presentation knob for the stored names — full / first_initial / first / first_initial_if_duplicates). Default `"first_initial_if_duplicates"`. Lives inside `effects_settings` because that's where presentation knobs group.
- `SignSettings.sign_name` + `SignSettings.timezone` (consolidated into the `sign_settings` block). `sign_name` is renamed from the existing `SignSettings.name` field for clarity (matches the HTML form field name and disambiguates "the sign's name" from generic "name"). `timezone` is moved from top-level into the `sign_settings` block. The `sign_settings` block is also renamed from the existing `SignConfig.sign` attribute to match the `effects_settings` / `text_settings` naming convention.
- The behavior is hard-coded allowlist-only: there is no mode field. The senders list IS the allowlist.
- `FilterRule` gains `status: "enabled" | "disabled"` (replacing `enabled: bool` — the enum is extensible to future soft-delete states). This is a SEPARATE per-RULE lifecycle, distinct from the senders-list's LIST-level `text_settings.enforcement_enabled` toggle. `_apply_filter` skips rules where `status != "enabled"`. The `FilterRule.action` field stays `"suppress"` as the only v1 value (action=allow is a future extension if a use case emerges — for now, every rule suppresses when matched, and senders list entries with `allowed=True` are the only allow mechanism).
- `FilterRule.type="sender"` is REMOVED from the wire. Stored v2 configs with `type=sender` rules are migrated to entries in `SignConfig.senders` (the single source of truth, with `allowed=False` and no per-entry status field) during the v2 → v3 migration; after migration, no `type=sender` rules exist. New rules cannot be created with `type=sender` from the UI (the dropdown omits the option).
- Phone-number normalization is centralized in `lib_shared/phone_utils.py` so the new senders lookup uses the same last-10-digits rule.
- Filtering happens at egress only. Every Twilio delivery still lands in SQLite + S3 and arrives at the device's `MessageManager` ring buffer. `get_messages(suppress=True)` drops senders-suppressed entries; `get_messages(suppress=False)` returns them. A subsequent config update (entry added, `allowed` flipped, or `text_settings.enforcement_enabled` flipped) re-enriches the buffer and reclassifies previously-suppressed messages without re-ingestion.
- The settings page's broken `cfg.allowed_senders` iteration (which renders an empty list because the attribute doesn't exist) is replaced with a proper iteration over `cfg.senders.items()` under a new section title **"Senders"**. The new table adds an Allowed checkbox alongside the existing Name / Phone / Remove columns, and an "Enforce senders filter" checkbox + "Name display format" dropdown sit at the top of the section. **There is no per-row Status column on the senders table** — the `text_settings.enforcement_enabled` toggle is the only lifecycle control. The Filter Rules table gets a per-row `Status` checkbox (for the per-rule lifecycle).
- `SignConfig.version` is bumped to 3; the existing `MIGRATIONS` registry brings stored v2 configs forward to v3 on read AND on server startup. The migration handles: structural moves (top-level → nested: `sign` → `sign_settings` with `name` → `sign_name` rename, `timezone` → `sign_settings.timezone`, `enforcement_enabled` → `text_settings.enforcement_enabled`, `name_display_format` → `effects_settings.name_display_format`); legacy senders.status → senders.allowed rename with value rename (with `allowed=True` backfilled for v2 entries that lack the legacy status field); FilterRule.type=sender → senders entry conversion (with `allowed=False` and no status field); FilterRule.enabled → FilterRule.status rename.

**Non-Goals:**

- No ingress filtering. Twilio still delivers every SMS to the Flask server. The change is purely an egress decision.
- No blocklist mode. The behavior is hard-coded allowlist-only — issue 6 only asks for the allowed-senders list. Blocklist semantics, if ever needed, are a separate future change.
- No FilterRule.action=allow. Suppress-only v1.
- No new Flask routes. The existing `/settings` POST handler is extended.
- No MQTT wire change for messages. Only the config envelope changes (senders entry fields renamed; `FilterRule.enabled` → `status`; `FilterRule.type=sender` removed; structural moves of top-level fields into nested blocks; version bump).
- No new database schema. The config is a single JSON blob in SQLite + S3; the migration registry handles the upgrade.
- No changes to `lib_shared/effects_coordinator.py` / `heart-matrix-controller/`. The device reads the new config via the existing `_handle_config` path; the effects rotation is unaffected.
- The deprecated `SignConfig.__init__(allowed_senders=...)` parameter is REMOVED — it was only ever referenced in test fixtures (per the docstring), and breaking those tests is the intended outcome (they should use the `senders` dict with `allowed` instead).
- The Twilio webhook (`/api/messages` in `heart-message-manager/main.py`) is untouched.

## Decisions

### D1. `senders` value type changes from `dict[str, str]` to `dict[str, dict]`

**Decision:** `cfg.senders` is `dict[str, dict]` in the running code. The dict key is the NORMALIZED phone (last-10-digits with leading `+1`, via `phone_utils.normalize_phone`). The value is `{"name": str, "allowed": bool, "phone": str}` — `phone` stores the operator-supplied original (for round-trip display), `allowed` is the per-entry classification, `name` is the display name. There is NO per-entry lifecycle field (no `status`).

**Alternatives considered:**

- *Keep `senders` as `dict[str, str]` and add a parallel `blocked_senders: list[str]` field.* Rejected because parallel storage paths for the same data are exactly the bug the change is fixing. Two fields = two places to update on form save, two places to consult in `_enrich_messages`, two chances for them to drift. Also rejected because the design is hard-coded allowlist-only — there is no "blocked" store because blocked senders are simply entries with `allowed=False`.
- *Keep `senders` as `dict[str, str]` and add a separate `senders_status: dict[str, str]` (phone → status).* Rejected for the same reason — parallel paths again. Per-entry lifecycle is not requested; the LIST-level `text_settings.enforcement_enabled` toggle covers the issue's stated use case ("disable without delete at the list level").
- *Change `senders` to `list[dict]` (no dict key, just a list).* Rejected because the dict key (normalized phone) gives O(1) lookup after normalization. With a list, every `_enrich_messages` call would iterate the list and normalize each entry on each call — O(n) per message, multiplied by the buffer size.
- *Use a `SenderEntry` class with `phone` / `name` / `allowed` attributes.* Rejected because the field surface is small (three fields) and a class doesn't add value over a dict literal — the existing pattern for `FilterRule` and the new `EffectsSettings` / `TextSettings` keeps things in `models.py` for consistency, but those have validation logic that benefits from being a class. `senders` entries don't.

**Rationale:** Dict-of-dict with the normalized key gives O(1) lookup and naturally keeps the per-entry fields together. The internal dict shape is an implementation detail; the wire shape is the simpler list-of-dict format the operator sees on the page. The single per-entry field (`allowed`) plus the LIST-level `text_settings.enforcement_enabled` toggle is sufficient — no per-entry lifecycle needed.

### D2. Four top-level fields move into nested settings blocks — `sign_settings` is renamed from `sign` for consistency

**Decision:** The wire shape undergoes a structural consolidation:

- The existing `sign` block (`{"name": str}`, with `name` defaulting to `"Lindsay's Heart"`) becomes `sign_settings` (`{"sign_name": str, "timezone": str}`). The block is renamed `sign` → `sign_settings` for naming consistency with `effects_settings` and `text_settings`. The `name` field is renamed `sign_name` for clarity (matches the HTML form field name and disambiguates "the sign's name" from generic "name"). The `timezone` field moves from top-level (`cfg.timezone`) into `cfg.sign_settings.timezone`. Default `timezone` is `"US/Pacific"`.
- `text_settings.enforcement_enabled` (NEW field on the existing `TextSettings` class): the master on/off switch for the senders filter. Default `True`. Lives inside `text_settings` because that's where the message selection algorithm is.
- `effects_settings.name_display_format` (NEW field on the existing `EffectsSettings` class): the presentation knob for the stored names. Default `"first_initial_if_duplicates"`. Lives inside `effects_settings` because that's where presentation knobs already group.
- `senders` and `filters` stay at the top level — they're the lists themselves, not configuration about them. Top-level placement keeps them discoverable and matches the operator's mental model ("here's the list of senders, here's the list of rules").

**Wire shape (v3):**

```python
{
    "version": 3,
    "senders": [...],                  # top-level — the allowlist itself
    "filters": [...],                  # top-level — the rule list itself
    "sign_settings": {                 # renamed from `sign`; gains `timezone`
        "sign_name": "Lindsay's Heart",  # renamed from `name`
        "timezone": "US/Pacific",
    },
    "effects_settings": {
        "effects": [...],
        "fade_seconds": ...,
        "hold_seconds": ...,
        "intro_seconds": ...,
        "idle_seconds": ...,
        "recent_count": ...,
        "name_display_format": "first_initial_if_duplicates",  # NEW
    },
    "text_settings": {
        "speed": 3,
        "color": 16711680,
        "text_effect": "scroll",
        "enforcement_enabled": True,    # NEW
    },
}
```

**Alternatives considered:**

- *Keep all four fields at the top level (no structural change).* Rejected because the current `sign`/`timezone` split is an obvious inconsistency — the sign's name lives inside `sign`, but its timezone lives at the top level, and the new enforcement / display-format fields would have to find a home too. Grouping by concern (identity / presentation / selection) is cleaner than scattering by historical accident.
- *Move `senders` and `filters` into nested groups too (e.g. `text_settings.senders` + `text_settings.filters`).* Rejected because they're the lists themselves, not configuration about them. The operator's mental model treats them as first-class objects on the page; nesting them inside `text_settings` would hide them. The structural move is for fields that don't otherwise have a home.
- *Create a separate `selection_settings` block for `enforcement_enabled` (and possibly `filters`).* Rejected because `text_settings` already groups text-rendering concerns, and the selection algorithm IS text rendering in this architecture (the message text either renders or it doesn't). Adding a new block for one field is over-engineering.
- *Rename `sign` to something other than `sign_settings` (e.g. `sign_identity`).* Rejected because the existing `sign` block already contains the sign's display name and timezone — those ARE identity fields, but the convention `sign_settings` matches `effects_settings` / `text_settings` for symmetric naming. The `_settings` suffix also signals "operator-tunable configuration" which is what these are.

**Rationale:** The four-block layout (`sign_settings`, `effects_settings`, `text_settings`, plus the top-level `senders`/`filters` lists) is the natural shape for this domain. Every field has a clear home; every home has a clear concern. The migration makes the rename transparent for stored configs and for code that consumes them via `update_from_dict` (the v3 `from_dict` parser reads from the new locations, the migration transforms v2 inputs into v3 shape before `from_dict` sees them).

### D3. Filtering is egress-only, decided at `_enrich_messages` time

**Decision:** The senders action/status check runs inside `FilteredMessages._enrich_messages` (in `lib_shared/messages.py`), after the existing `_apply_filter` loop. No code in the ingest path (`/api/messages` → `sqlite.add_message` → S3 → MQTT publish) consults `cfg.senders` or `cfg.text_settings.enforcement_enabled`.

**Alternatives considered:**

- *Gate ingress in `/api/messages` before `sqlite.add_message`.* Rejected because the issue explicitly says "Filtering is not on ingress now, the idea is to filter on egress. That way, messages aren't lost and can be added once the sender is added or unblocked." A sender who joins the allowlist later must see their already-received messages without re-ingestion.
- *Gate ingress in the MQTT publish.* Rejected for the same reason — the message would be lost from S3 / SQLite / the device's buffer.

**Rationale:** Egress filtering composes cleanly with the existing `_enrich_messages` machinery. The operator's config change fires `MessageManager._handle_config` (already on the device and the browser via the MQTT config envelope), which calls `_enrich_messages` on the whole buffer, which reclassifies every entry's `entry.suppressed` flag. No new event channel needed.

### D4. Phone normalization is last-10-digits with a leading `+1` prefix

**Decision:** `normalize_phone(s) -> str` in `lib_shared/phone_utils.py`:

- Strip everything except digits.
- If 10 digits remain, return `"+1" + digits`.
- If 11 digits remain starting with `1`, return `"+1" + last_10`.
- Otherwise (0 digits, fewer than 10 digits, more than 11 digits, etc.), return the original string verbatim.

**Alternatives considered:**

- *E.164 strictly — reject anything that doesn't match `^\+\d{10,15}$`.* Rejected because Twilio can route a number with the country code prefix (`+15551234567`) or without (`5551234567`); both must match.
- *Last-N digits configurable per country.* Rejected because the project is US-only (timezone selector in the settings page only offers US zones plus a few European/Asian examples for operators traveling) and the existing `senders` data is all E.164. The simpler rule covers every shape we've seen.
- *Use `phonenumbers` library.* Rejected because adding a 3rd-party dep for a 10-line normalization helper is overkill. The last-10-digits rule is what the issue asks for ("just match the last 10 digits").

**Rationale:** The last-10-digits rule matches US local numbers, US numbers with the country code, and US numbers with arbitrary formatting (parentheses, dashes, spaces, dots). It does NOT match international numbers from outside the US — that's an explicit non-goal (the project is a personal sign, not a global SMS gateway).

### D5. Behavior is hard-coded allowlist-only; ONE `text_settings.enforcement_enabled` master toggle

**Decision:** The senders list IS the allowlist. There is no mode field (`off` / `allowlist` / `blocklist`) — the allowlist interpretation is the only behavior (issue 6 only asks for an allowed-senders list). There IS a single master `text_settings.enforcement_enabled` boolean toggle (default `True`) that bypasses filtering entirely when off. When `True`, the decision rule is: a sender renders iff their `cfg.senders` entry has `allowed=True`. When `False`, the senders filter is bypassed entirely — every message renders regardless of any per-entry state, and names still resolve for display. The master toggle is the issue's "additional status attribute" referenced on the LIST itself. There is no per-entry lifecycle field — flipping the master toggle off preserves every entry in the config while bypassing filtering, which is exactly the "disable without delete" affordance the issue asks for.

**Alternatives considered:**

- *No master toggle (the original simplified draft).* Rejected by the user's clarification. The issue explicitly says "Add an 'enabled' checkbox whether to enforce the list. We don't want to have to delete the entries if we want to turn it off. If off, the names will still be used for displaying texts." The toggle is the issue's affordance for "I want to turn off enforcement without losing the entries."
- *Add a `mode` field with values `off` / `allowlist` / `blocklist` AND a master `enabled` toggle (the doubly-configurable first draft).* Rejected by the user's second clarification: "arg, I just realized it still said we would support both an allowlist and blocklist. we only want allowlist." Blocklist semantics are out of scope — the senders list IS the allowlist, no mode radio.
- *Default `text_settings.enforcement_enabled = False` so the first run shows everything (and the operator must opt in to filtering).* Rejected because the user explicitly said "we can just hard-code that only 'allowed' senders messages will show up" — the intent is the allowlist behavior from the start, with the toggle as an explicit override for operators who don't want to add senders. The default-on toggle also means the v2 → v3 migration doesn't silently change behavior for existing operators who never had an explicit choice.
- *Put the master toggle at the top level (the original draft).* Rejected because the selection algorithm lives inside `text_settings` (the message text either renders or it doesn't), so the toggle that controls whether the selection happens belongs inside `text_settings` next to the rest of the text-rendering knobs (`speed`, `color`, `text_effect`). Putting it at the top level splits related concerns across the wire.
- *Hide the master toggle behind a separate "advanced" admin section.* Rejected because the issue treats it as a first-class affordance — it sits at the top of the Senders section alongside the name-display-format dropdown.

**Rationale:** The simplification collapses the operator's mental model to "the senders list is the allowlist; the master toggle in text_settings lets you flip the whole filter off without losing entries; per-entry `allowed` gives you disable-without-delete at the entry level." The behavior change for senders NOT in the list — they go from "render with no display name" to "suppress" — is the documented cost; the master toggle gives operators a one-click escape hatch without losing their entries. Operators who don't want to add senders after the upgrade can simply uncheck "Enforce senders filter"; the migration does NOT auto-add senders from the message history because that would be guessing the operator's intent.

### D6. Single-axis taxonomy: senders carry only `allowed`; lifecycle lives at the LIST level on `text_settings.enforcement_enabled`

**Decision:** The change introduces a clean separation between sender-level effect and list-level lifecycle:

| Layer | Field | Values | Meaning | Applies to |
|-------|-------|--------|---------|-----------|
| Per-entry effect (senders) | `allowed` | `True` \| `False` | Is this sender on the allowlist? | `SignConfig.senders` entries |
| List-level lifecycle | `text_settings.enforcement_enabled` | `True` \| `False` | Is the whole senders filter active? | `SignConfig.text_settings` |
| Per-rule lifecycle | `status` | `"enabled"` \| `"disabled"` | Is this rule "on" right now? | `FilterRule` entries |

**On `SignConfig.senders` entries**: only `allowed` is the per-entry field. There is no per-entry lifecycle field — flipping `text_settings.enforcement_enabled` off preserves every entry in the config while bypassing the filter (the issue's "disable without delete at the list level" affordance). The original draft added a per-entry `status="enabled"|"disabled"` lifecycle, but the user clarified that the issue's "additional status attribute" referred to the GLOBAL toggle on the LIST, not a per-entry field. Per-entry lifecycle would only matter if individual entries could be muted while the master toggle stayed on — that's not requested.

**On `FilterRule`s**: `status` is the per-rule lifecycle (separate from the senders-list lifecycle — they apply to different objects). `_apply_filter` skips rules where `status != "enabled"`. The `FilterRule.action` field stays `"suppress"` as the only v1 value — `action="allow"` would conflict with the implicit-allow semantics of being in the senders list, so it's deliberately deferred until there's a concrete use case.

**On the LIST level**: `text_settings.enforcement_enabled` is the master on/off switch (default `True`). When `False`, the entire senders filter is bypassed — every message renders, names still resolve for display. This is the LIST-level "disable without delete" affordance the issue asks for. Lives inside `text_settings` because the message selection algorithm lives there.

**Alternatives considered:**

- *Add a per-entry `status="enabled"|"disabled"` lifecycle field on senders entries (the original draft).* Rejected by the user — the issue's "additional status attribute" referred to the GLOBAL toggle, not a per-entry field. Per-entry lifecycle would be a second axis of complexity on top of `allowed`, with no use case the issue actually requires. The master toggle covers the stated use case (operator can turn off the whole filter without losing entries).
- *Use the same value vocabulary everywhere (`status="active"|"inactive"`).* Rejected because the per-entry effect axis (`allowed`) is a boolean (true/false on the allowlist), the list-level lifecycle is a boolean (enforce/don't enforce), and the per-rule lifecycle is an enum (`"enabled"` / `"disabled"`, extensible). Each layer has its own type that fits its semantics — collapsing to a single vocabulary would force artificial uniformity.
- *Drop the FilterRule.status lifecycle and keep FilterRule as `enabled: bool`.* Rejected because the `enabled`/`status` rename is a separate concern from senders — the FilterRule.status lifecycle is for "disable a rule vs delete a rule", which the issue asks for separately. The enum form is more extensible than a bool (room for `"archived"` etc. without breaking the wire).

**Rationale:** Three distinct concepts deserve three distinct fields: the per-entry allowlist membership (senders.allowed), the LIST-level filter on/off (text_settings.enforcement_enabled), and the per-rule lifecycle (FilterRule.status). None of them are redundant; collapsing any pair would lose an affordance the issue or the existing code requires. The asymmetry — `FilterRule.status` is per-rule, `text_settings.enforcement_enabled` is list-level, senders.allowed is per-entry — reflects the natural separation: each lives at the layer that matches its scope.

### D7. `FilterRule.type == "sender"` is REMOVED from the wire

**Decision:** `FilterRule.type` is restricted to `"keyword"`, `"regex"`, `"message"`. The `sender` type is removed from the wire. The settings page's "Add Rule" `Type` dropdown offers only these three types. Stored v2 configs with `type=sender` rules are migrated during the v2 → v3 upgrade: each such rule is converted to an entry in `SignConfig.senders` with `allowed=False` (because the v2 sender-type rule was always a suppression rule, so the migrated entry inherits the blocked classification under the v3 allowlist-only model), `name=rule.pattern`, `phone=rule.pattern` (best-effort: the rule's pattern becomes both the display name and the phone). The migrated entry SHALL NOT have a `status` field — there is no per-entry lifecycle in v3. The rule itself is dropped from `filters`. After the migration, no `type=sender` rules exist in stored configs. `FilterRule._matches` no longer has a `type == "sender"` branch (since sender matching is the senders list's job).

**Alternatives considered:**

- *Remove `type == "sender"` from the UI's Add Rule dropdown but keep it on the wire (original draft).* Rejected because the user identified this as a redundancy — `FilterRule.type=sender` overlaps with `SignConfig.senders` (both match senders). Two paths for sender matching = two chances to drift. Removing the redundancy gives `FilterRule` a clearer purpose (keyword/regex/message content matching).
- *Keep `type == "sender"` on the wire AND in the UI, but route it through the senders list at runtime.* Rejected because it would silently behave differently from a literal `action="suppress"` rule — the operator would see a `type=sender` rule and assume it works the same as a `type=keyword` rule, but instead it's a roundtrip through the senders list with a synthesized entry. Hidden behavior changes are worse than explicit removal.
- *Reject `type == "sender"` at `from_dict` time and force a migration that drops the rules.* Rejected because dropping the rules silently would lose operator-configured senders. The migration that converts to senders list entries preserves the operator's intent (block this sender's messages) in the most natural place.
- *Reject `type == "sender"` in the runtime matcher and let the rule persist as a no-op.* Rejected because silent no-op rules confuse operators ("I set this rule and nothing happened"). The migration eliminates them outright.

**Rationale:** Removing `type=sender` from the wire (not just the UI) makes `SignConfig.senders` the single source of truth for sender-level matching. The migration that converts stored `type=sender` rules into senders list entries preserves the operator's intent (block this sender) by setting the new entry's `allowed=False` — under the v3 allowlist-only model, `allowed=False` is exactly the "block this sender's messages" semantic the original v2 rule carried. After migration, the runtime is simpler (one path for sender matching) and the wire is cleaner (no deprecated type option).

### D8. Dict key is normalized; value preserves the original phone; UI displays normalized

**Decision:** Three values, three roles:

- **Dict key** is the NORMALIZED phone (last-10-digits with leading `+1`, via `phone_utils.normalize_phone`). Used for O(1) lookup after normalizing the incoming sender in `_enrich_messages`.
- **Value's `phone` field** is the operator-supplied ORIGINAL phone string (for round-trip wire fidelity and any future "show me what I typed" affordance). Stored in `cfg.senders[<key>]["phone"]`; emitted on the wire as the entry's `phone`.
- **UI display** shows the NORMALIZED phone (the dict key), not the original. Consistency wins for an admin page where the operator scans a list of senders — every row shows in the same canonical format regardless of how each sender was originally typed. A short helper line "Phone numbers are normalized to +1XXXXXXXXXX." above the table sets expectations.

**Alternatives considered:**

- *Use the original phone as the dict key.* Rejected because lookup would require iterating the dict and normalizing each key on every `_enrich_messages` call — O(n) per message.
- *Use the normalized phone as both the key AND the wire entry's `phone` (drop the original).* Rejected because losing the operator's original input would be a visible UX regression in the wire dump (S3 / SQLite) and any external consumer that ingests the config would lose fidelity.
- *Display the original phone in the UI.* Rejected because mixed-format display (`+1 (555) 123-4567` next to `555.123.4567` next to `+15551234567`) makes the list harder to scan and harder to spot duplicates. The trade-off — the operator sees their input "cleaned up" — is mild; helper text acknowledges it.
- *Maintain a parallel normalized→original mapping alongside the original→normalized dict.* Rejected because two parallel dicts for the same data is the exact bug the change is fixing.

**Rationale:** The normalized key gives lookup efficiency; the original in the value gives wire-fidelity; the normalized display gives UI consistency. Each value has a single, distinct purpose — the asymmetry is intentional.

### D9. Synthetic `sender_action` rule on the `MessageView.rules` list

**Decision:** When the senders action/status suppresses a message AND no `FilterRule` matched, the suppressing list appended to `entry.rules` contains a synthetic rule dict `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}`. When a `FilterRule` ALSO matched, the existing rule dicts are kept (the synthetic marker is omitted — the real rule wins for display).

**Alternatives considered:**

- *Add a new `MessageView.suppressed_by_sender: bool` field.* Rejected because the existing `entry.rules` list flows to the wire as part of `MessageView.to_dict()` and the admin UI already renders suppression reasons from it. Adding a parallel boolean doubles the surface for the same information.
- *Skip the synthetic marker entirely — just set `entry.suppressed = True`.* Rejected because the messages list UI uses `entry.rules` to show "Suppressed by: keyword 'spam'" / "Suppressed by: sender_action" badges; without the synthetic marker, sender-action-suppressed messages would render as "Suppressed by: <nothing>".

**Rationale:** A synthetic rule is the minimum change to the `MessageView` shape that carries the "why this message is suppressed" information to the wire. It composes with the existing `entry.rules` consumer path.

## Risks / Trade-offs

- **[Risk]** The behavior change for unlisted senders — they were previously shown (with no display name), now they are suppressed. An operator upgrading this change will see ALL their previously-shown but unlisted senders disappear from the display. → **Mitigation:** This is the documented behavior change (the proposal calls it out as a risk). The migration does NOT auto-add senders from the message history because that would be guessing the operator's intent — a sender who appears once might be spam, a sender who appears ten times might be a friend. The operator can either explicitly add each sender they want to allow, OR flip the master `text_settings.enforcement_enabled` toggle off via the new "Enforce senders filter" checkbox to bypass filtering entirely (names still resolve for display). The toggle is the issue's explicit affordance for "we don't want to have to delete the entries if we want to turn it off."

- **[Risk]** The structural refactor moves four top-level fields into nested blocks (`sign.name` → `sign_settings.sign_name`, `timezone` → `sign_settings.timezone`, `enforcement_enabled` → `text_settings.enforcement_enabled`, `name_display_format` → `effects_settings.name_display_format`). Any external code (tests, scripts, third-party consumers) that read these as top-level attributes will break. → **Mitigation:** The migration brings stored configs forward transparently for the wire. For Python code, the v3 parser reads from the new nested locations, and the migration runs at the top of `from_dict`/`update_from_dict` so any v2 input shape is brought forward before the field-by-field read. Tests that construct `SignConfig` directly need updating — the constructor signature changes to match (remove top-level `timezone`, `enforcement_enabled`, `name_display_format` parameters; the `sign_settings` parameter replaces `sign`).

- **[Risk]** The settings page's existing broken `cfg.allowed_senders` template iteration might be relied on by an external scraper or admin UI variant. → **Mitigation:** The change replaces the iteration with a new senders table; if the playful-redesign variant (`*-playful.html`) also iterates `cfg.allowed_senders`, it gets the same fix as part of this change (a single template update covers both variants because they share the field surface). The deprecated `SignConfig.__init__(allowed_senders=...)` parameter is removed in the same change; the docstring says it's only kept for test back-compat, and any test that relies on it should be updated to use the `senders` dict with `allowed` instead.

- **[Risk]** Egress filtering means the operator's logs include every incoming SMS — including ones from senders they'll never display. → **Mitigation:** This is the intended behavior (the issue says "messages aren't lost"), and the suppression flag on `MessageView` makes the "this message is hidden" state visible on the admin UI. No action needed; this is the documented design.

- **[Risk]** A v2 → v3 migration that doesn't auto-add unlisted senders might leave the operator's sign looking empty if they don't manually add their known senders after the upgrade. → **Mitigation:** Documented in the proposal and the operator-facing change notes. The migration brings the existing `senders` dict forward (with `allowed=True` backfilled for v2 entries that lack the legacy `status` field), so any sender the operator had previously added stays allowed. The risk only affects senders who were never explicitly added — the previous behavior was to show them anyway (with no display name), and the new behavior is to suppress them. The operator's first action after the upgrade is to either add their known senders OR uncheck "Enforce senders filter" to bypass filtering.

- **[Risk]** Removing `FilterRule.type=sender` from the wire is a breaking change for stored configs. A stored v2 config with `type=sender` rules would not load on a v3 server (the `from_dict` parser doesn't accept the type). → **Mitigation:** The v2 → v3 migration runs BEFORE `from_dict` parses the rules — the migration converts each `type=sender` rule into a senders list entry and drops the rule, so by the time `from_dict` sees the rules list, there are no `type=sender` rules left. Stored configs survive the upgrade transparently. A v3 server receiving a v2 envelope over MQTT also runs the migration defensively at the top of `update_from_dict`, so the device path is also covered.

- **[Risk]** Per-entry `Status` on Filter Rules adds another column to the Filter Rules table, which is already wide. → **Mitigation:** The table gains one column (`Status` checkbox); the `Delete` button stays in its existing column. No new `Actions` column needed.

- **[Risk]** A v2 device receiving a v3 config envelope (after the server has migrated its stored config) will receive `senders` entries with `allowed` fields. The device's `MessageManager._handle_config` calls `update_from_dict`, which calls `migrate(...)` at the top — but the migration runs forward, not backward. A v2 device would silently drop the `allowed` field (its `from_dict` is the v2 parser, which expects the list shape without that field). → **Mitigation:** The server publishes the migrated config to MQTT after the startup migration runs, so any device that connects AFTER the server has migrated sees the v3 shape. A v2 device that was already connected before the migration would NOT see `allowed` (its in-memory config would treat the senders as before, with no suppression). This is acceptable — v2 devices are pre-migration Pi installs that the operator owns; once they reboot and re-fetch the config (a one-line change to call `seed()` on startup, which they already do), they pick up the v3 shape. No code path needs to support v2-to-v3 downgrade.

## Migration Plan

This is a wire-format change with a registry-driven upgrade path. The server normalizes v2 → v3 on startup; connected devices normalize v2 → v3 on every `update_from_dict` call.

1. **Pre-deploy:** No operator action needed. The stored config in SQLite + S3 is at version 2 (the version bumped by the previous `runtime-sign-config` change). The code is at version 3 (this change).
2. **Deploy:** Push the new server code. On startup, `migrate_on_startup` runs `_v2_to_v3` against the stored config. The migration:
   - **Structural moves (top-level → nested):**
     - Extracts top-level `timezone` (string) and folds it into the `sign` block (now `sign_settings`), creating `sign_settings = {"sign_name": ..., "timezone": ...}` if absent. The `sign` block key is renamed to `sign_settings`; the existing `sign.name` field is renamed to `sign_settings.sign_name`. Drops the original top-level `sign` and `timezone` keys.
     - Extracts top-level `enforcement_enabled` (NEW field, default `True` for v2 inputs that lack it) and folds it into `text_settings.enforcement_enabled`. Creates `text_settings` if absent (with `speed=3, color=0xFF0000, text_effect="scroll"` defaults). Drops the original top-level `enforcement_enabled` key.
     - Extracts top-level `name_display_format` (NEW field, default `"first_initial_if_duplicates"` for v2 inputs that lack it) and folds it into `effects_settings.name_display_format`. Creates `effects_settings` if absent (with loader-driven defaults for the effects list and `None` for pacing fields that fall through to the loader). Drops the original top-level `name_display_format` key.
   - **Senders entry migration (the existing v3 changes):**
     - For each entry in `senders` (wire shape: list of dicts), normalizes the field rename: legacy `status` field (`"allowed"|"blocked"`) → `allowed` field (`True`|`False`); v2 entries that have no legacy `status` field get `allowed=True` backfilled. The migrated entry SHALL NOT contain a `status` field — there is no per-entry lifecycle in v3.
   - **FilterRule migration (the existing v3 changes):**
     - For each rule in `filters` with `type=sender`: converts to a `senders` entry with `allowed=False`, `name=rule.pattern`, `phone=rule.pattern` (no `status` field — there is no per-entry lifecycle). Drops the rule from `filters`.
     - For each remaining rule (non-sender) in `filters`: renames `enabled` (bool) → `status` (enum). `enabled=True` → `status="enabled"`; `enabled=False` → `status="disabled"`.
     - Handles the legacy v1 dict shape (`{"+15551234567": "Alice"}`) by converting to the list shape.
   - **Version bump:** Sets `version` to `3`.
   - The migrated config is written back to S3, the SQLite cache is updated, and a `type="config"` envelope is published to MQTT.
3. **Devices:** The Pi's `MessageManager._handle_config` receives the v3 envelope. `update_from_dict` calls `migrate(...)` defensively at the top (no-op because the envelope is already v3), and the new field locations populate the device's in-memory config: `cfg.sign_settings.sign_name`, `cfg.sign_settings.timezone`, `cfg.text_settings.enforcement_enabled`, `cfg.effects_settings.name_display_format`. The device now filters at egress using `cfg.text_settings.enforcement_enabled`.
4. **Post-upgrade operator action:** After the upgrade, the operator visits `/settings` and either (a) adds each of their known senders to the senders table with `Allowed=checked`, OR (b) leaves the senders list as-is and unchecks "Enforce senders filter" to bypass filtering entirely (every previously-shown message renders, names still resolve). Any previously-shown-but-unlisted sender now shows `entry.suppressed=True` (because they're not in the list); once added, they're visible again on the next config update.
5. **Rollback:** If the change is rolled back, the previous code path (v2 schema) would refuse to parse a v3 envelope (`SignConfig.version == 3` is unexpected; the top-level `sign_settings`, `text_settings.enforcement_enabled`, `effects_settings.name_display_format` keys are all new). The rollback procedure is to revert the code AND revert the S3 config to its v2 shape (the previous deploy's S3 entry is still in the bucket under version history — the new deploy's S3 write replaces the old one). The rollback is operator-driven and uses the existing "rebuild-from-S3" path on the next server restart.

## Open Questions

None — the simplified design removes both the mode tri-state ambiguity (the doubly-configurable first draft had a mode tri-state + master toggle + per-entry action+status; the simplified design has 1 master toggle + per-entry allowed, all in an allowlist interpretation) AND the per-entry status lifecycle (the issue's "additional status attribute" referred to the LIST-level `text_settings.enforcement_enabled` toggle, not a per-entry field). The single per-entry field (`allowed`) plus the list-level `text_settings.enforcement_enabled` toggle covers both the "disable it vs. delete it" affordance at the LIST level (flip the toggle off, every entry preserved) AND the "block this sender's messages without deleting their entry" affordance at the entry level (flip `allowed=False`, name preserved for display). The `FilterRule.status` per-rule lifecycle is a separate feature for "disable a rule vs delete a rule" — independent of the senders list. The egress-only filtering composes with the existing `_enrich_messages` machinery without new event channels or new endpoints. Blocklist semantics, if a future use case emerges, would be a separate change with its own mode field and decision-rule update. The structural refactor (four top-level fields → nested blocks) is opportunistic — it happens because we're already bumping the version, and the wire shape changes are more cleanly grouped by concern than scattered by historical accident.
