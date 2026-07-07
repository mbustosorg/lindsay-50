## Context

The current `SignConfig` model in `lib_shared/models.py` carries a `senders: dict[str, str]` field that exists for one purpose: resolving a phone number to a human-readable display name when `FilteredMessages._enrich_messages` builds a `MessageView`. The phone-to-name map has no gating power — every message passes through `_enrich_messages`, gets a `sender_name` resolved from the dict (or `None` if absent), and emerges with `entry.suppressed = False` unless one of the configured `FilterRule`s matches.

The settings page (`heart-message-manager/templates/settings.html`) has an "Allowed Senders" panel that iterates `cfg.allowed_senders`, an attribute that **does not exist on `SignConfig`** — `SignConfig.__init__` carries a deprecated `allowed_senders: list[str] | None = None` parameter that's marked "Deprecated, ignored (kept for backward compat with tests)" in its docstring and silently discarded by the rest of the class. The panel renders an empty list, the "info only" caption is technically accurate, and the panel's wire path (the `sender_name` / `sender_phone` form fields parsed in `main.py:702-710`) actually populates `cfg.senders` — the working model — not `cfg.allowed_senders`. Two parallel storage paths exist and only one of them is alive.

The Filter Rules panel offers four rule types — `keyword`, `regex`, `sender`, `message`. The `sender` rule type's match logic in `FilteredMessages._matches` (line 88) is `msg.sender == rule.pattern` — an EXACT string match with no normalization. A rule created with `pattern = "+1 (555) 123-4567"` will not match a routed sender like `+15551234567`, and vice-versa. The issue's "Relax the number filter to ignore the routing codes, ie. just match the last 10 digits, remove -'s, etc." applies here.

The use case the simplified design addresses: an operator who has previously added Alice to the senders list wants to stop Alice's messages from showing on the display. Today the only way to do that is to delete Alice's entry, which loses her display-name metadata. The operator also wants to "disable it vs. delete it" for rules — the same affordance for FilterRule. The design introduces a clean two-axis taxonomy to cover both:

- **`action`** (the effect axis): what happens when a sender matches / a rule matches. Values: `"allow"` (render the message) or `"suppress"` (don't render). On `SignConfig.senders` entries, `action` replaces an earlier draft's `status` field — it's about effect, not lifecycle.
- **`status`** (the lifecycle axis): is this entry/rule "on" right now, or muted without being deleted? Values: `"enabled"` or `"disabled"`. Extensible — future soft-delete states (e.g. `"archived"`) can land without breaking the wire. Applied uniformly to both `SignConfig.senders` entries AND `FilterRule`s.

The taxonomy also resolves a redundancy: `FilterRule.type=sender` overlaps with `SignConfig.senders` (both match senders), and `FilterRule.type=sender`'s default `action="suppress"` conflicts with the implicit `allow` semantics of being in the senders list. With the unified taxonomy, the cleanest decision is to **remove `FilterRule.type=sender` entirely** — `SignConfig.senders` is the single source of truth for sender-level matching, with richer metadata (display name, lifecycle, action). `FilterRule` then has a clearer purpose: keyword/regex (content) and message-ID (specific message) suppression.

The existing re-enrichment machinery (`MessageManager._handle_config` calls `_enrich_messages` on the whole buffer after a config update) is the natural extension point — an entry added or a status flipped fires `_handle_config`, which re-classifies every buffered `MessageView`. No new event channel is needed for the operator's actions to take effect on previously-received messages.

`SignConfig` is already wired end-to-end: SQLite stores it, S3 snapshots it, MQTT publishes it as a `type="config"` `MessageEnvelope`, the Pi's `MessageManager._handle_config` applies it via `update_from_dict`. The `MIGRATIONS` registry in `lib_shared/config_migrations.py` brings older versions forward on read AND on server startup. Adding the per-entry `action`/`status` fields, removing `FilterRule.type=sender`, and renaming `FilterRule.enabled` to `FilterRule.status` are wire-shape changes that fit cleanly into this existing migration path — the previous change (`runtime-sign-config`) added the registry for the same purpose.

## Goals / Non-Goals

**Goals:**

- `SignConfig.senders` entries each carry TWO new fields:
  - `action: "allow" | "suppress"` — the effect when the sender matches. Default `"allow"` (back-compat: every pre-existing senders entry was implicitly allowing their messages to render).
  - `status: "enabled" | "disabled"` — the lifecycle flag (mute without delete). Default `"enabled"`.
  - A sender renders iff their entry has `action="allow"` AND `status="enabled"`. Senders with `action="suppress"` OR `status="disabled"` OR not in the list are suppressed.
- The behavior is hard-coded: there is no mode flag, no master enabled toggle. The senders list is the only mechanism.
- `FilterRule` gains `status: "enabled" | "disabled"` (replacing `enabled: bool` — the enum is extensible to future soft-delete states). `_apply_filter` skips rules where `status != "enabled"`. The `FilterRule.action` field stays `"suppress"` as the only v1 value (action=allow is a future extension if a use case emerges — for now, every rule suppresses when matched, and senders list entries with `action="allow"` are the only allow mechanism).
- `FilterRule.type="sender"` is REMOVED from the wire. Stored v2 configs with `type=sender` rules are migrated to entries in `SignConfig.senders` (the single source of truth) during the v2 → v3 migration; after migration, no `type=sender` rules exist. New rules cannot be created with `type=sender` from the UI (the dropdown omits the option).
- Phone-number normalization is centralized in `lib_shared/phone_utils.py` so the new senders lookup uses the same last-10-digits rule.
- Filtering happens at egress only. Every Twilio delivery still lands in SQLite + S3 and arrives at the device's `MessageManager` ring buffer. `get_messages(suppress=True)` drops action/status-suppressed entries; `get_messages(suppress=False)` returns them. A subsequent config update (entry added or action/status flipped) re-enriches the buffer and reclassifies previously-suppressed messages without re-ingestion.
- The settings page's broken `cfg.allowed_senders` iteration (which renders an empty list because the attribute doesn't exist) is replaced with a proper iteration over `cfg.senders.items()` under a new section title **"Senders"**. The new table adds an Action dropdown (Allow/Suppress) AND a Status checkbox alongside the existing Name / Phone / Remove columns. The Filter Rules table gets a per-row `Status` checkbox.
- `SignConfig.version` is bumped to 3; the existing `MIGRATIONS` registry brings stored v2 configs forward to v3 on read AND on server startup. The migration handles: senders.status → senders.action rename with value rename; FilterRule.type=sender → senders entry conversion; FilterRule.enabled → FilterRule.status rename.

**Non-Goals:**

- No ingress filtering. Twilio still delivers every SMS to the Flask server. The change is purely an egress decision.
- No mode tri-state (off / allowlist / blocklist). The simplified design hard-codes the allowlist behavior — every senders entry is either action=allow or action=suppress, and unlisted senders are implicitly suppressed.
- No master enabled toggle. The senders list is the only mechanism; to "turn off" filtering the operator deletes entries (a less ambiguous affordance than a global toggle that the operator might forget about).
- No FilterRule.action=allow. Suppress-only v1.
- No new Flask routes. The existing `/settings` POST handler is extended.
- No MQTT wire change for messages. Only the config envelope changes (senders entry fields renamed; `FilterRule.enabled` → `status`; `FilterRule.type=sender` removed; version bump).
- No new database schema. The config is a single JSON blob in SQLite + S3; the migration registry handles the upgrade.
- No changes to `lib_shared/effects_coordinator.py` / `heart-matrix-controller/`. The device reads the new config via the existing `_handle_config` path; the effects rotation is unaffected.
- The deprecated `SignConfig.__init__(allowed_senders=...)` parameter is REMOVED — it was only ever referenced in test fixtures (per the docstring), and breaking those tests is the intended outcome (they should use the `senders` dict with `action`+`status` instead).
- The Twilio webhook (`/api/messages` in `heart-message-manager/main.py`) is untouched.

## Decisions

### D1. `senders` value type changes from `dict[str, str]` to `dict[str, dict]`

**Decision:** `cfg.senders` is `dict[str, dict]` in the running code. The dict key is the NORMALIZED phone (last-10-digits with leading `+1`, via `phone_utils.normalize_phone`). The value is `{"name": str, "action": "allow" | "suppress", "status": "enabled" | "disabled", "phone": str}` — `phone` stores the operator-supplied original (for round-trip display), `action` and `status` are the new taxonomy fields, `name` is the display name.

**Alternatives considered:**

- *Keep `senders` as `dict[str, str]` and add a parallel `blocked_senders: list[str]` field.* Rejected because parallel storage paths for the same data are exactly the bug the change is fixing. Two fields = two places to update on form save, two places to consult in `_enrich_messages`, two chances for them to drift.
- *Keep `senders` as `dict[str, str]` and add a separate `senders_status: dict[str, str]` (phone → status).* Rejected for the same reason — parallel paths again. The "status on each entry" affordance the user asked for is precisely that: status lives on the entry, not in a parallel field.
- *Change `senders` to `list[dict]` (no dict key, just a list).* Rejected because the dict key (normalized phone) gives O(1) lookup after normalization. With a list, every `_enrich_messages` call would iterate the list and normalize each entry on each call — O(n) per message, multiplied by the buffer size.
- *Use a `SenderEntry` class with `phone` / `name` / `action` / `status` attributes.* Rejected because the field surface is small (four fields) and a class doesn't add value over a dict literal — the existing pattern for `FilterRule` and the new `EffectsSettings` / `TextSettings` keeps things in `models.py` for consistency, but those have validation logic that benefits from being a class. `senders` entries don't.

**Rationale:** Dict-of-dict with the normalized key gives O(1) lookup and naturally keeps the per-entry fields together. The internal dict shape is an implementation detail; the wire shape is the simpler list-of-dict format the operator sees on the page.

### D2. Filtering is egress-only, decided at `_enrich_messages` time

**Decision:** The senders action/status check runs inside `FilteredMessages._enrich_messages` (in `lib_shared/messages.py`), after the existing `_apply_filter` loop. No code in the ingest path (`/api/messages` → `sqlite.add_message` → S3 → MQTT publish) consults `cfg.senders`.

**Alternatives considered:**

- *Gate ingress in `/api/messages` before `sqlite.add_message`.* Rejected because the issue explicitly says "Filtering is not on ingress now, the idea is to filter on egress. That way, messages aren't lost and can be added once the sender is added or unblocked." A sender who joins the allowlist later must see their already-received messages without re-ingestion.
- *Gate ingress in the MQTT publish.* Rejected for the same reason — the message would be lost from S3 / SQLite / the device's buffer.

**Rationale:** Egress filtering composes cleanly with the existing `_enrich_messages` machinery. The operator's config change fires `MessageManager._handle_config` (already on the device and the browser via the MQTT config envelope), which calls `_enrich_messages` on the whole buffer, which reclassifies every entry's `entry.suppressed` flag. No new event channel needed.

### D3. Phone normalization is last-10-digits with a leading `+1` prefix

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

### D4. Behavior is hard-coded allowlist-only — no mode or master toggle

**Decision:** The senders list is the only mechanism. There is no mode field (`off` / `allowlist` / `blocklist`) and no master `enabled` toggle. The decision rule is the same in all configurations: a sender renders iff their `cfg.senders` entry has `action="allow"` AND `status="enabled"`.

**Alternatives considered:**

- *Add a `mode` field with values `off` / `allowlist` / `blocklist` and a master `enabled` toggle (the original draft).* Rejected by the user's simplification request. The two booleans gave the operator a 5-state grid (`off` + any, `allowlist` + disabled, `allowlist` + enabled, `blocklist` + disabled, `blocklist` + enabled) when 3 of the 5 states were redundant. The simplification collapses it to one state: per-entry `status` (lifecycle) and per-entry `action` (effect).
- *Add a single `mode` field with values `disabled` / `enabled` (effectively the master toggle alone).* Rejected for the same reason — the operator can already achieve "disabled" by deleting all entries, and the master toggle added cognitive overhead (a forgotten toggle could disable filtering without the operator realizing).
- *Default `mode = "off"` so the first run shows everything.* Rejected because the user explicitly said "we can just hard-code that only 'allowed' senders messages will show up" — the intent is the allowlist behavior from the start, with no opt-in step.

**Rationale:** The simplification collapses the operator's mental model to "each entry has an action and a status; allowed + enabled shows, everything else is suppressed." No global flags to remember. The behavior change for senders NOT in the list is the documented cost — operators add their known senders after the upgrade (the migration does NOT auto-add senders from the message history).

### D5. Two-axis taxonomy: `action` (effect) vs `status` (lifecycle)

**Decision:** The change introduces a clean two-axis taxonomy across `SignConfig.senders` entries and `FilterRule`s:

| Concept | Field | Values | Meaning |
|---------|-------|--------|---------|
| Effect | `action` | `"allow"` \| `"suppress"` | What happens when this entry/rule matches. |
| Lifecycle | `status` | `"enabled"` \| `"disabled"` | Is this entry/rule "on" right now? |

**On `SignConfig.senders` entries**: both `action` and `status` are present. A sender renders iff `action="allow"` AND `status="enabled"`. The original draft used `status="allowed"|"blocked"` (lifecycle + effect conflated into a single field); the rename to `action="allow"|"suppress"` (effect-only) plus a separate `status="enabled"|"disabled"` (lifecycle-only) gives each field a single responsibility and matches the FilterRule taxonomy.

**On `FilterRule`s**: only `status` is being added in this change (replacing the original draft's `enabled: bool`). The `action` field stays `"suppress"` as the only v1 value — `action="allow"` for rules is a future extension. `FilterRule.action="allow"` would conflict with the implicit-allow semantics of being in the senders list, so it's deliberately deferred until there's a concrete use case.

**Alternatives considered:**

- *Keep `status="allowed"|"blocked"` as a single field on senders (effect + lifecycle conflated).* Rejected because it would conflict with `FilterRule.status="enabled"|"disabled"` (same field name, different semantics, different value vocabularies — a footgun for new readers). The user's clarification ("status and action are different things!") pointed at this exact problem.
- *Use a single `enabled: bool` field on senders (lifecycle only, drop the action axis).* Rejected because it loses the "block this sender without deleting them" affordance — flipping `enabled` to false suppresses the sender, but there's no way to express "this sender IS in the allowlist but I want them suppressed right now" differently from "I want to keep their name in the UI but not show their messages." The `action` axis gives operators the blocklist affordance the issue asks for.
- *Use a tri-state `status` enum on senders (`enabled` / `disabled` / `archived`).* Rejected because archiving is a future use case, not a present one; the enum stays open to it (extensible to `"archived"` later without breaking the wire) without committing to a value we don't use yet.
- *Use the same value vocabulary for both fields (`status="active"|"inactive"` everywhere).* Rejected because the action axis has a distinct vocabulary — `"allow"`/`"suppress"` is more descriptive of effect than `"active"`/`"inactive"` (which is closer to lifecycle anyway). Different axes → different vocabularies.

**Rationale:** The two-axis taxonomy gives each concept a distinct field with a distinct value vocabulary. Both axes are extensible (`status` could grow to `"archived"`; `action` could grow to `"quarantine"` for rules) without breaking the wire. The asymmetric treatment (senders get both fields, rules get only `status` in v1) reflects the asymmetric intent: senders are the primary allowlist mechanism, rules are an always-suppress mechanism.

### D6. `FilterRule.type == "sender"` is REMOVED from the wire

**Decision:** `FilterRule.type` is restricted to `"keyword"`, `"regex"`, `"message"`. The `sender` type is removed from the wire. The settings page's "Add Rule" `Type` dropdown offers only these three types. Stored v2 configs with `type=sender` rules are migrated during the v2 → v3 upgrade: each such rule is converted to an entry in `SignConfig.senders` with `action="suppress"`, `status="enabled"`, `name=rule.pattern`, `phone=rule.pattern` (best-effort: the rule's pattern becomes both the display name and the phone). The rule itself is dropped from `filters`. After the migration, no `type=sender` rules exist in stored configs. `FilterRule._matches` no longer has a `type == "sender"` branch (since sender matching is the senders list's job).

**Alternatives considered:**

- *Remove `type == "sender"` from the UI's Add Rule dropdown but keep it on the wire (original draft).* Rejected because the user identified this as a redundancy — `FilterRule.type=sender` overlaps with `SignConfig.senders` (both match senders), and the implicit `action="suppress"` conflicts with the implicit-allow semantics of being in the senders list. Two paths for sender matching = two chances to drift. Removing the redundancy gives `FilterRule` a clearer purpose (keyword/regex/message content matching).
- *Keep `type == "sender"` on the wire AND in the UI, but route it through the senders list at runtime.* Rejected because it would silently behave differently from a literal `action="suppress"` rule — the operator would see a `type=sender` rule and assume it works the same as a `type=keyword` rule, but instead it's a roundtrip through the senders list with a synthesized entry. Hidden behavior changes are worse than explicit removal.
- *Reject `type == "sender"` at `from_dict` time and force a migration that drops the rules.* Rejected because dropping the rules silently would lose operator-configured senders. The migration that converts to senders list entries preserves the operator's intent (block this sender's messages) in the most natural place.
- *Reject `type == "sender"` in the runtime matcher and let the rule persist as a no-op.* Rejected because silent no-op rules confuse operators ("I set this rule and nothing happened"). The migration eliminates them outright.

**Rationale:** Removing `type=sender` from the wire (not just the UI) makes `SignConfig.senders` the single source of truth for sender-level matching. The migration that converts stored `type=sender` rules into senders list entries preserves the operator's intent without dropping their work. After migration, the runtime is simpler (one path for sender matching) and the wire is cleaner (no deprecated type option).

### D7. Dict key is normalized; value preserves the original phone; UI displays normalized

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

### D8. Synthetic `sender_action` rule on the `MessageView.rules` list

**Decision:** When the senders action/status suppresses a message AND no `FilterRule` matched, the suppressing list appended to `entry.rules` contains a synthetic rule dict `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}`. When a `FilterRule` ALSO matched, the existing rule dicts are kept (the synthetic marker is omitted — the real rule wins for display).

**Alternatives considered:**

- *Add a new `MessageView.suppressed_by_sender: bool` field.* Rejected because the existing `entry.rules` list flows to the wire as part of `MessageView.to_dict()` and the admin UI already renders suppression reasons from it. Adding a parallel boolean doubles the surface for the same information.
- *Skip the synthetic marker entirely — just set `entry.suppressed = True`.* Rejected because the messages list UI uses `entry.rules` to show "Suppressed by: keyword 'spam'" / "Suppressed by: sender_action" badges; without the synthetic marker, sender-action-suppressed messages would render as "Suppressed by: <nothing>".

**Rationale:** A synthetic rule is the minimum change to the `MessageView` shape that carries the "why this message is suppressed" information to the wire. It composes with the existing `entry.rules` consumer path.

## Risks / Trade-offs

- **[Risk]** The behavior change for unlisted senders — they were previously shown (with no display name), now they are suppressed. An operator upgrading this change will see ALL their previously-shown but unlisted senders disappear from the display. → **Mitigation:** This is the documented behavior change (the proposal calls it out as a risk). The migration does NOT auto-add senders from the message history because that would be guessing the operator's intent — a sender who appears once might be spam, a sender who appears ten times might be a friend. The operator must explicitly add each sender they want to allow after the upgrade. This is a one-time data-entry cost; the alternative (auto-add all historical senders) is riskier because it would create a list of senders the operator didn't explicitly choose.

- **[Risk]** The settings page's existing broken `cfg.allowed_senders` template iteration might be relied on by an external scraper or admin UI variant. → **Mitigation:** The change replaces the iteration with a new senders table; if the playful-redesign variant (`*-playful.html`) also iterates `cfg.allowed_senders`, it gets the same fix as part of this change (a single template update covers both variants because they share the field surface). The deprecated `SignConfig.__init__(allowed_senders=...)` parameter is removed in the same change; the docstring says it's only kept for test back-compat, and any test that relies on it should be updated to use the `senders` dict with `action`+`status`.

- **[Risk]** Egress filtering means the operator's logs include every incoming SMS — including ones from senders they'll never display. → **Mitigation:** This is the intended behavior (the issue says "messages aren't lost"), and the suppression flag on `MessageView` makes the "this message is hidden" state visible on the admin UI. No action needed; this is the documented design.

- **[Risk]** A v2 → v3 migration that doesn't auto-add unlisted senders might leave the operator's sign looking empty if they don't manually add their known senders after the upgrade. → **Mitigation:** Documented in the proposal and the operator-facing change notes. The migration brings the existing `senders` dict forward (with `action="allow"` and `status="enabled"` backfilled), so any sender the operator had previously added stays allowed. The risk only affects senders who were never explicitly added — the previous behavior was to show them anyway (with no display name), and the new behavior is to suppress them. The operator's first action after the upgrade is to add their known senders.

- **[Risk]** Removing `FilterRule.type=sender` from the wire is a breaking change for stored configs. A stored v2 config with `type=sender` rules would not load on a v3 server (the `from_dict` parser doesn't accept the type). → **Mitigation:** The v2 → v3 migration runs BEFORE `from_dict` parses the rules — the migration converts each `type=sender` rule into a senders list entry and drops the rule, so by the time `from_dict` sees the rules list, there are no `type=sender` rules left. Stored configs survive the upgrade transparently. A v3 server receiving a v2 envelope over MQTT also runs the migration defensively at the top of `update_from_dict`, so the device path is also covered.

- **[Risk]** Per-entry `Status` on Filter Rules adds another column to the Filter Rules table, which is already wide. → **Mitigation:** The table gains one column (`Status` checkbox); the `Delete` button stays in its existing column. No new `Actions` column needed.

- **[Risk]** A v2 device receiving a v3 config envelope (after the server has migrated its stored config) will receive `senders` entries with `action` and `status` fields. The device's `MessageManager._handle_config` calls `update_from_dict`, which calls `migrate(...)` at the top — but the migration runs forward, not backward. A v2 device would silently drop the `action`/`status` fields (its `from_dict` is the v2 parser, which expects the list shape without those fields). → **Mitigation:** The server publishes the migrated config to MQTT after the startup migration runs, so any device that connects AFTER the server has migrated sees the v3 shape. A v2 device that was already connected before the migration would NOT see `action`/`status` (its in-memory config would treat the senders as before, with no suppression). This is acceptable — v2 devices are pre-migration Pi installs that the operator owns; once they reboot and re-fetch the config (a one-line change to call `seed()` on startup, which they already do), they pick up the v3 shape. No code path needs to support v2-to-v3 downgrade.

## Migration Plan

This is a wire-format change with a registry-driven upgrade path. The server normalizes v2 → v3 on startup; connected devices normalize v2 → v3 on every `update_from_dict` call.

1. **Pre-deploy:** No operator action needed. The stored config in SQLite + S3 is at version 2 (the version bumped by the previous `runtime-sign-config` change). The code is at version 3 (this change).
2. **Deploy:** Push the new server code. On startup, `migrate_on_startup` runs `_v2_to_v3` against the stored config. The migration:
   - For each entry in `senders` (wire shape: list of dicts), normalizes the rename: `status` field (`"allowed"|"blocked"`) → `action` field (`"allow"|"suppress"`); adds `status="enabled"` lifecycle field.
   - For each rule in `filters` with `type=sender`: converts to a `senders` entry with `action="suppress"`, `status="enabled"`, `name=rule.pattern`, `phone=rule.pattern`. Drops the rule from `filters`.
   - For each remaining rule (non-sender) in `filters`: renames `enabled` (bool) → `status` (enum). `enabled=True` → `status="enabled"`; `enabled=False` → `status="disabled"`.
   - Handles the legacy v1 dict shape (`{"+15551234567": "Alice"}`) by converting to the list shape.
   - Sets `version` to `3`.
   - The migrated config is written back to S3, the SQLite cache is updated, and a `type="config"` envelope is published to MQTT.
3. **Devices:** The Pi's `MessageManager._handle_config` receives the v3 envelope. `update_from_dict` calls `migrate(...)` defensively at the top (no-op because the envelope is already v3), and the new `action`/`status` fields on each senders entry populate the device's in-memory config. The device now filters at egress.
4. **Post-upgrade operator action:** After the upgrade, the operator visits `/settings` and adds each of their known senders to the senders table with `Action=Allow` + `Status=Enabled`. Any previously-shown-but-unlisted sender now shows `entry.suppressed=True` (because they're not in the list); once added, they're visible again on the next config update.
5. **Rollback:** If the change is rolled back, the previous code path (v2 schema) would refuse to parse a v3 envelope (`SignConfig.version == 3` is unexpected). The rollback procedure is to revert the code AND revert the S3 config to its v2 shape (the previous deploy's S3 entry is still in the bucket under version history — the new deploy's S3 write replaces the old one). The rollback is operator-driven and uses the existing "rebuild-from-S3" path on the next server restart.

## Open Questions

None — the simplified design removes the mode tri-state ambiguity (the original draft had 5 states for 2 booleans; the simplified design has 1 state). The two-axis taxonomy (action+status) covers both the "disable it vs. delete it" affordance for rules AND the "block this sender's messages without deleting their entry" affordance for senders. The egress-only filtering composes with the existing `_enrich_messages` machinery without new event channels or new endpoints.