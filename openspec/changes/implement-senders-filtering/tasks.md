## 1. phone_utils.py — last-10-digits normalization helper

- [ ] 1.1 Create `lib_shared/phone_utils.py` with one public function `normalize_phone(s: str) -> str`. The function SHALL strip non-digit characters, then return `"+1" + last_10_digits` if exactly 10 or 11 digits remain (11 only if leading digit is `"1"`); otherwise return the original input verbatim (passthrough for malformed values). No external dependencies; stdlib only
- [ ] 1.2 Add `tests/phone_utils_test.py` with the full truth table: E.164 (`+15551234567` → self), 10-digit (`5551234567` → `+15551234567`), with parens/dashes (`+1 (555) 123-4567` → `+15551234567`), with dots/spaces (`555.123.4567` → `+15551234567`), 11-digit starting with `1` (`15551234567` → `+15551234567`), empty string (`` → ``), non-numeric (`"not-a-phone"` → `"not-a-phone"`), and shorter-than-10 (`"12345"` → `"12345"`)

## 2. SignConfig structural refactor: nest fields into settings blocks + bump CURRENT_VERSION

- [ ] 2.1 In `lib_shared/models.py`, **rename `SignConfig.sign` → `SignConfig.sign_settings`** (the type stays `SignSettings`; only the attribute name changes for naming consistency with `effects_settings` / `text_settings`). Update `SignConfig.__init__` to accept `sign_settings` instead of `sign`. Update `SignConfig.from_dict`, `to_dict`, `update`, and `update_from_dict` to read/write the new attribute name. Drop the top-level `sign` key from `to_dict` and the wire.
- [ ] 2.2 In `lib_shared/models.py`, **rename `SignSettings.name` → `SignSettings.sign_name`** (for clarity — matches the HTML form field name and disambiguates "the sign's name" from generic "name"). Update `SignSettings.__init__`, `from_dict`, `to_dict`, and any callers in `lib_shared/` / `heart-message-manager/` / `heart-matrix-controller/` that read `cfg.sign.name` to read `cfg.sign_settings.sign_name` instead. The default value (`"Lindsay's Heart"`) is unchanged.
- [ ] 2.3 In `lib_shared/models.py`, **add `SignSettings.timezone: str = "US/Pacific"`** as a new field on `SignSettings`. Update `SignSettings.__init__`, `from_dict`, `to_dict` to read/write it. The wire shape for `sign_settings` becomes `{"sign_name": str, "timezone": str}`. The default timezone is `"US/Pacific"`.
- [ ] 2.4 In `lib_shared/models.py`, **add `TextSettings.enforcement_enabled: bool = True`** as a new field on `TextSettings`. Update `TextSettings.__init__`, `from_dict`, `to_dict` to read/write it. The wire shape for `text_settings` gains the new field; existing fields (`speed`, `color`, `text_effect`) are unchanged.
- [ ] 2.5 In `lib_shared/models.py`, **add `EffectsSettings.name_display_format: str = "first_initial_if_duplicates"`** as a new field on `EffectsSettings`. Update `EffectsSettings.__init__`, `from_dict`, `to_dict` to read/write it. The wire shape for `effects_settings` gains the new field; existing fields (`effects`, `fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`, `recent_count`) are unchanged. `from_dict` SHALL reject unknown `name_display_format` values with `ValueError` (only `"full"`, `"first_initial"`, `"first"`, `"first_initial_if_duplicates"` are accepted; missing field defaults to `"first_initial_if_duplicates"`).
- [ ] 2.6 In `lib_shared/models.py`, **remove top-level `timezone` parameter from `SignConfig.__init__`** (it now lives at `cfg.sign_settings.timezone`). **Remove top-level `enforcement_enabled` parameter** (it now lives at `cfg.text_settings.enforcement_enabled`). **Remove top-level `name_display_format` parameter** (it now lives at `cfg.effects_settings.name_display_format`). The constructor no longer accepts these as kwargs; callers that pass them get `TypeError`.
- [ ] 2.7 In `lib_shared/models.py`, **bump `SignConfig.CURRENT_VERSION` from `2` to `3`**. The `version` argument default in `SignConfig.__init__` becomes `3`.
- [ ] 2.8 Update `SignConfig.from_dict` and `update_from_dict` to read the new nested field locations: `cfg.sign_settings = SignSettings.from_dict(data.get("sign_settings") or {})` (NOT `data.get("sign")`); `cfg.text_settings.enforcement_enabled = data.get("text_settings", {}).get("enforcement_enabled", True)` (read inside `TextSettings.from_dict`); `cfg.effects_settings.name_display_format = data.get("effects_settings", {}).get("name_display_format", "first_initial_if_duplicates")` (read inside `EffectsSettings.from_dict`). The `to_dict` output uses the new nested keys.
- [ ] 2.9 Update `SignConfig.to_dict` to emit the new shape: `{"version": 3, "senders": [...], "filters": [...], "sign_settings": {...}, "effects_settings": {...}, "text_settings": {...}}`. No top-level `sign`, `timezone`, `enforcement_enabled`, or `name_display_format` keys.
- [ ] 2.10 Add tests in `tests/sign_settings_test.py` asserting:
  - `SignSettings().sign_name == "Lindsay's Heart"` and `SignSettings().timezone == "US/Pacific"` (constructor defaults)
  - `SignSettings(sign_name="Custom", timezone="UTC").sign_name == "Custom"` and `.timezone == "UTC"`
  - `SignSettings.from_dict({"sign_name": "Alice's Sign", "timezone": "Europe/Paris"})` produces the matching in-memory object
  - `SignSettings.from_dict({})` produces defaults (back-compat for partial payloads)
  - `SignSettings.from_dict(None)` produces defaults (back-compat for absent payloads)
  - `to_dict()` on a SignSettings returns `{"sign_name": ..., "timezone": ...}` (both keys always present)
  - `from_dict(to_dict(s))` round-trips losslessly
  - `SignConfig(sign_settings=SignSettings(...))` works; `SignConfig(sign=...)` raises `TypeError` (attribute renamed)
  - `SignConfig(timezone=...)` raises `TypeError` (top-level parameter removed)
  - `SignConfig(enforcement_enabled=...)` raises `TypeError` (top-level parameter removed)
  - `SignConfig(name_display_format=...)` raises `TypeError` (top-level parameter removed)
  - `to_dict()` on a SignConfig emits `sign_settings: {"sign_name": ..., "timezone": ...}` and DOES NOT emit `sign`, `timezone`, `enforcement_enabled`, or `name_display_format` at the top level

## 3. SignConfig.senders shape change + FilterRule taxonomy

- [ ] 3.1 In `lib_shared/models.py`, change `SignConfig.senders` from `dict[str, str]` (phone → name) to `dict[str, dict]` (normalized_phone → `{"name": str, "allowed": bool, "phone": str}`). Update `from_dict` to: for each wire entry in `data.get("senders", [])`, normalize the phone via `phone_utils.normalize_phone` and store under the normalized key with `name=entry["name"]`, `allowed=entry.get("allowed", True)` (back-compat: every pre-existing sender was implicitly on the allowlist), `phone=entry["phone"]`. Update `to_dict` to: emit each value as a wire entry with `phone=value["phone"]` (the original, not the normalized key), `name=value["name"]`, `allowed=value["allowed"]`. Sort by phone for deterministic output. There is NO `status` field on senders entries — lifecycle is the LIST-level `text_settings.enforcement_enabled` toggle
- [ ] 3.2 Remove the deprecated `allowed_senders: list[str] | None = None` parameter from `SignConfig.__init__`. The parameter is gone — the constructor raises `TypeError` if called with it. Update any tests that used it (per the docstring, it was only kept for test back-compat)
- [ ] 3.3 In `lib_shared/models.py`, change `FilterRule.enabled: bool` to `FilterRule.status: "enabled" | "disabled"`. Update `FilterRule.from_dict` to accept `status` as an optional key defaulting to `"enabled"`, and to reject any `action` value other than `"suppress"` with `ValueError`. Update `FilterRule.to_dict` to always include `status` in the output. Restrict `FilterRule.type` to the set `{"keyword", "regex", "message"}` — reject any other value (including `"sender"`, which is REMOVED from the wire) with `ValueError`. (FilterRule.status is a SEPARATE per-RULE lifecycle — distinct from the senders list which has no per-entry lifecycle.)
- [ ] 3.4 Add tests in `tests/senders_status_test.py` asserting:
  - `SignConfig().senders == {}` (default empty dict-of-dict)
  - `SignConfig().sign_settings.sign_name == "Lindsay's Heart"` and `SignConfig().sign_settings.timezone == "US/Pacific"` (constructor defaults — SignConfig default)
  - `SignConfig().text_settings.enforcement_enabled == True` (default — enforcement on)
  - `SignConfig().effects_settings.name_display_format == "first_initial_if_duplicates"` (default)
  - `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "allowed": true}]})` produces `cfg.senders["+15551234567"] == {"name": "Alice", "allowed": True, "phone": "+15551234567"}` (key is normalized, value preserves original)
  - `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice"}]})` (no `allowed`) produces `cfg.senders["+15551234567"]["allowed"] == True` (back-compat default)
  - `from_dict({"senders": [...]})` does NOT add a `status` field to any entry — there is no per-entry lifecycle in v3
  - `to_dict()` on a config with two senders entries emits a list of two `{"phone": ..., "name": ..., "allowed": ...}` dicts, sorted by phone; the top-level dict ALSO includes `sign_settings`, `effects_settings` (with `name_display_format`), and `text_settings` (with `enforcement_enabled`)
  - `from_dict(to_dict(cfg))` round-trips losslessly (original phone format preserved, dict keys normalized, allowed/enforcement_enabled/name_display_format preserved)
  - `SignConfig(allowed_senders=[...])` raises `TypeError` (parameter removed)
  - `from_dict({"effects_settings": {"name_display_format": "last_only"}})` raises `ValueError` (unknown format rejected)
  - `from_dict({"effects_settings": {"name_display_format": "first"}})` succeeds with `cfg.effects_settings.name_display_format == "first"`
- [ ] 3.5 Add tests in `tests/filter_rule_status_test.py` asserting:
  - `FilterRule(type="keyword", pattern="spam").status == "enabled"` (default)
  - `FilterRule.from_dict({"type": "keyword", "pattern": "spam"})` (no `status` key) → `rule.status == "enabled"`
  - `FilterRule.from_dict({"type": "keyword", "pattern": "spam", "status": "disabled"})` → `rule.status == "disabled"`
  - `FilterRule.from_dict({"type": "keyword", "pattern": "spam", "action": "allow"})` raises `ValueError` (action="allow" is not in v1)
  - `FilterRule.from_dict({"type": "sender", "pattern": "+15551234567"})` raises `ValueError` (type="sender" is REMOVED from the wire)
  - `rule.to_dict()` always includes the `status` key

## 4. _v2_to_v3 migration in lib_shared/config_migrations

- [ ] 4.1 In `lib_shared/config_migrations.py`, add a `_v2_to_v3(d)` migration function and register it in the `MIGRATIONS` dict as `{1: _v1_to_v2, 2: _v2_to_v3}`. The function SHALL:
  - Return a shallow copy of `d` (do not mutate the caller's dict — matches the v1 → v2 migration's contract)
  - **Structural moves (top-level → nested):**
    - Create `sign_settings` if not present: `out_sign = d.get("sign_settings") or d.get("sign") or {}`. Migrate `sign.name` → `sign_settings.sign_name` (default `"Lindsay's Heart"` if absent). Migrate top-level `timezone` → `sign_settings.timezone` (default `"US/Pacific"` if absent). Set `out["sign_settings"] = out_sign`. Drop the original top-level `sign` and `timezone` keys.
    - Create `text_settings` if not present: `out_text = d.get("text_settings") or {}`. Migrate top-level `enforcement_enabled` → `text_settings.enforcement_enabled` (default `True` if absent). Set `out["text_settings"] = out_text`. Drop the original top-level `enforcement_enabled` key.
    - Create `effects_settings` if not present: `out_effects = d.get("effects_settings") or {}`. Migrate top-level `name_display_format` → `effects_settings.name_display_format` (default `"first_initial_if_duplicates"` if absent). Set `out["effects_settings"] = out_effects`. Drop the original top-level `name_display_format` key.
  - **Senders entry migration:**
    - If `senders` is a dict (legacy v1 shape `{phone: name}`), convert it to the list shape `[{"phone": p, "name": n, "allowed": True} for (p, n) in d["senders"].items()]`
    - If `senders` is a list, for each entry: if a legacy `status` field is present, map `status="allowed"` → `allowed=True`, `status="blocked"` → `allowed=False`; if no legacy `status` field, backfill `allowed=True`. The migrated entry SHALL NOT contain a `status` field — there is no per-entry lifecycle in v3
  - **FilterRule migration:**
    - For each rule in `d.get("filters", [])`:
      - If the rule has `type=sender`: convert to a senders list entry with `allowed=False`, `name=rule.pattern`, `phone=rule.pattern` (the v2 sender rule was always a suppression rule; `allowed=False` carries that semantic under the v3 allowlist-only model). The migrated entry SHALL NOT contain a `status` field. Append to `senders` (creating the list if absent, deduplicating by normalized phone — if the entry already exists in `senders`, leave it alone — the pre-existing entry wins). DROP the rule from `filters`.
      - Otherwise: rename `enabled` (bool) → `status` (enum). `enabled=True` → `status="enabled"`; `enabled=False` → `status="disabled"`. If `enabled` is missing, set `status="enabled"`. (This is the per-RULE lifecycle, separate from the senders-list's LIST-level `text_settings.enforcement_enabled` toggle.)
  - Set `version` to `3`
  - Preserve `sign_settings`, `text_settings`, `effects_settings`, `filters`, `senders` after the migration runs
- [ ] 4.2 Add tests in `tests/config_migrations_test.py` (extending the existing file) asserting:
  - **Structural moves — `sign_settings`:**
    - `migrate({"version": 2, "sign": {"name": "Alice's Sign"}, "timezone": "US/Eastern"}, current_version=3)` returns `sign_settings: {"sign_name": "Alice's Sign", "timezone": "US/Eastern"}`, with NO top-level `sign` or `timezone` keys
    - `migrate({"version": 2, "sign": {"name": "Alice's Sign"}}, current_version=3)` returns `sign_settings: {"sign_name": "Alice's Sign", "timezone": "US/Pacific"}` (default timezone backfilled)
    - `migrate({"version": 2, "timezone": "US/Eastern"}, current_version=3)` (no `sign`) returns `sign_settings: {"sign_name": "Lindsay's Heart", "timezone": "US/Eastern"}` (sign_settings block created with sign_name default)
    - `migrate({"version": 2, "sign_settings": {"sign_name": "Pre-v3 Layout"}, "sign": {"name": "Override"}}, current_version=3)` returns `sign_settings: {"sign_name": "Pre-v3 Layout"}` (the `sign_settings` block wins; the legacy `sign` block is dropped)
  - **Structural moves — `text_settings`:**
    - `migrate({"version": 2, "enforcement_enabled": False}, current_version=3)` returns `text_settings: {"enforcement_enabled": False}` (the new field folds into the existing block, which is created with empty defaults for speed/color/text_effect)
    - `migrate({"version": 2}, current_version=3)` (no `text_settings`, no `enforcement_enabled`) returns `text_settings: {"enforcement_enabled": True}` (default enforcement backfilled)
    - `migrate({"version": 2, "text_settings": {"speed": 5}}, current_version=3)` returns `text_settings: {"speed": 5, "enforcement_enabled": True}` (existing fields preserved, new field added with default)
  - **Structural moves — `effects_settings`:**
    - `migrate({"version": 2, "name_display_format": "full"}, current_version=3)` returns `effects_settings: {"name_display_format": "full"}` (the new field folds into the existing block, which is created empty for the effects list)
    - `migrate({"version": 2, "effects_settings": {"fade_seconds": 2.5}}, current_version=3)` returns `effects_settings: {"fade_seconds": 2.5, "name_display_format": "first_initial_if_duplicates"}` (existing fields preserved, new field added with default)
  - **Senders migration:**
    - `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice"}], "filters": [{"type": "keyword", "pattern": "spam"}]}, current_version=3)` returns a v3 dict with the senders entry having `allowed=True` (no `status` field) and no `status` lifecycle on the entry, the filter having `status="enabled"`, the nested `text_settings.enforcement_enabled=True`, the nested `effects_settings.name_display_format="first_initial_if_duplicates"`, the nested `sign_settings.sign_name="Lindsay's Heart"` + `sign_settings.timezone="US/Pacific"`, and `version: 3`
    - `migrate({"version": 2, "senders": {"+15551234567": "Alice"}}, current_version=3)` (legacy dict shape) returns a v3 dict with senders in the list shape `[{phone, name, allowed: True}]` — no `status` field
    - `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]}, current_version=3)` (legacy `status="allowed"`) returns the senders entry with `allowed=True` (mapped from the legacy field); the migrated entry SHALL NOT contain a `status` field
    - `migrate({"version": 2, "senders": [{"phone": "+15558888888", "name": "Bob", "status": "blocked"}]}, current_version=3)` (legacy `status="blocked"`) returns the senders entry with `allowed=False`; the migrated entry SHALL NOT contain a `status` field
  - **FilterRule migration:**
    - `migrate({"version": 2, "filters": [{"type": "keyword", "pattern": "spam", "enabled": False}]}, current_version=3)` preserves the existing `enabled=False` (renamed to `status="disabled"`; the migration is idempotent on rules that already have `status`)
    - `migrate({"version": 2, "filters": [{"type": "sender", "pattern": "+15551234567"}], "senders": []}, current_version=3)` returns a v3 dict with `filters=[]` AND `senders=[{"phone": "+15551234567", "name": "+15551234567", "allowed": False}]` — no `status` field on the migrated entry
    - `migrate({"version": 2, "filters": [{"type": "sender", "pattern": "+15559999999"}], "senders": [{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]}, current_version=3)` returns a v3 dict with `filters=[]` AND `senders` containing TWO entries (the original Alice entry migrated to `allowed: True` with no `status` field AND the new sender rule converted to `allowed: False` with no `status` field)
    - `migrate({"version": 2, "filters": [{"type": "keyword", "pattern": "spam"}]}, current_version=3)` (no `enabled` key) returns `filters=[{type: "keyword", pattern: "spam", status: "enabled"}]` (back-compat default)
  - **Idempotency + immutability:**
    - `migrate({"version": 3, "senders": [...]}, current_version=3)` is idempotent (input returned unchanged)
    - The migration does NOT mutate the input dict (the caller's original dict retains its `version: 2` and original shape)
    - `migrate({"version": 1}, current_version=3)` runs BOTH v1 → v2 AND v2 → v3 in sequence (end-to-end chain still works)
  - **Non-mutation of nested blocks:**
    - A v2 input with `text_settings: {"speed": 5}` and a top-level `enforcement_enabled: True` produces a v3 with `text_settings: {"speed": 5, "enforcement_enabled": True}` (the existing fields are NOT overwritten when the new field is added — the merge is additive)
    - A v2 input with `effects_settings: {"fade_seconds": 2.5}` and a top-level `name_display_format: "full"` produces a v3 with `effects_settings: {"fade_seconds": 2.5, "name_display_format": "full"}` (same additive merge)

## 5. FilteredMessages: senders check + FilterRule.status skip

- [ ] 5.1 In `lib_shared/messages.py`, add a module-level helper `should_render_sender(sender: str, senders: dict, enforcement_enabled: bool = True) -> bool` (or a method on `SignConfig` — pick the simpler form). The function SHALL:
  - If `not enforcement_enabled`, return `True` immediately (the master toggle is off — every message renders, names still resolve)
  - Normalize `sender` via `phone_utils.normalize_phone`
  - Look up `senders.get(normalized)` — if absent, return `False` (sender is not in the list → suppressed; allowlist is exclusive when enforcement is on)
  - If present and `entry["allowed"] is True`, return `True`
  - Otherwise (`allowed=False`), return `False`
- [ ] 5.2 In the same file, modify `FilteredMessages._enrich_messages` to call `should_render_sender(entry.message.sender, self._config.senders, self._config.text_settings.enforcement_enabled)` AFTER the existing `_apply_filter` loop. If the function returns `False` AND no FilterRule matched, append a synthetic rule dict `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}` to `entry.rules` and set `entry.suppressed = True`. If the function returns `False` AND FilterRules already matched, set `entry.suppressed = True` (the real rules win for `entry.rules` display — no synthetic marker added in that case). When `text_settings.enforcement_enabled` is `False`, no synthetic marker is added (the function returns `True` immediately — no suppression decision was made)
- [ ] 5.3 In the same file, update the display-name lookup from `entry.sender_name = self._config.senders.get(entry.message.sender)` to `entry.sender_name = format_display_name((self._config.senders.get(normalize_phone(entry.message.sender)) or {}).get("name", ""), self._config.effects_settings.name_display_format, all_first_names)`. The lookup works regardless of `allowed` — display names are always resolved (the operator sees "From: Alice" even for disallowed senders). `all_first_names` is precomputed once per `_enrich_messages` call from `cfg.senders.items()`
- [ ] 5.4 In the same file, modify `FilteredMessages._apply_filter` to skip any rule where `rule.status == "disabled"` (the rule is treated as absent — it does NOT contribute to the suppressing list). The existing rule-match logic for `type == "keyword"`, `type == "regex"`, and `type == "message"` is unchanged. REMOVE the `type == "sender"` branch from `FilteredMessages._matches` (sender matching moved to the senders list)
- [ ] 5.5 Add tests in `tests/senders_status_test.py` (extending the existing file) asserting:
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}}, True)` returns `True`
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "allowed": False, "phone": "+15551234567"}}, True)` returns `False` (allowed=False suppresses)
  - `should_render_sender("+15551234567", {}, True)` returns `False` (sender not in dict)
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "allowed": False, "phone": "+15551234567"}}, False)` returns `True` (master toggle off → render regardless of allowed)
  - `should_render_sender("+15551234567", {}, False)` returns `True` (master toggle off → render regardless of presence)
  - `should_render_sender("+1 (555) 123-4567", {"+15551234567": {"name": "Alice", "allowed": True, "phone": "+15551234567"}}, True)` returns `True` (incoming sender normalized before lookup matches the normalized dict key)
  - End-to-end through `MessageManager`: with Alice allowed, an SMS from Alice is added to the ring buffer and `get_messages(suppress=True)` includes it
  - End-to-end: with Alice not-allowed, an SMS from Alice is added to the ring buffer but `get_messages(suppress=True)` excludes it
  - End-to-end: with Alice not in the list and `text_settings.enforcement_enabled=True`, an SMS from Alice is added to the ring buffer but `get_messages(suppress=True)` excludes it
  - End-to-end: with Alice not in the list and `text_settings.enforcement_enabled=False`, an SMS from Alice is added to the ring buffer AND `get_messages(suppress=True)` includes it (master toggle bypasses filtering)
  - End-to-end: a config update that adds Alice to the list with `allowed=True` re-enriches the buffer and Alice's previously-suppressed message becomes visible (the egress-not-ingress guarantee — no Twilio re-fetch needed)
  - End-to-end: a config update that flips Alice from allowed to not-allowed re-enriches and her previously-visible message becomes suppressed
  - End-to-end: a config update that removes Alice from the senders dict re-enriches and her previously-visible message becomes suppressed
  - End-to-end: a config update that flips `text_settings.enforcement_enabled` from `True` to `False` re-enriches and previously-suppressed messages become visible (master toggle off)
  - End-to-end: a config update that flips `text_settings.enforcement_enabled` from `False` to `True` re-enriches and previously-visible messages from unlisted senders become suppressed again
  - End-to-end: the `entry.rules` list contains a synthetic `{"type": "sender_action", ...}` marker when senders list suppressed a message AND no FilterRule matched
  - End-to-end: the `entry.rules` list does NOT contain the synthetic marker when a FilterRule also matched (the real rule wins for display)
  - End-to-end: the `entry.rules` list does NOT contain the synthetic marker when `text_settings.enforcement_enabled` is `False` (master toggle bypasses the filter — no suppression decision was made)
  - End-to-end: `MessageView.sender_name` is populated from `cfg.senders[<normalized_phone>]["name"]` regardless of `allowed` (display-name lookup works even when blocked)
  - End-to-end: `MessageView.sender_name` is populated from `cfg.senders[<normalized_phone>]["name"]` even when `text_settings.enforcement_enabled` is `False` (display-name lookup works regardless of enforcement)
- [ ] 5.6 Add tests in `tests/filter_rule_status_test.py` (extending the existing file) asserting:
  - `_apply_filter` returns an empty list when ALL rules have `status="disabled"`
  - `_apply_filter` returns the rule when it has `status="enabled"` and matches the message
  - `_apply_filter` skips a rule with `status="disabled"` even when its pattern matches (the disabled rule is treated as absent)
  - `_apply_filter` returns matching rules regardless of `cfg.text_settings.enforcement_enabled` (FilterRule evaluation is independent of the master enforcement toggle)

## 6. /settings POST handler: parse per-row Allowed checkbox list + nested enforcement + nested name_display_format

- [ ] 6.1 In `heart-message-manager/main.py`, replace the existing `sender_name` / `sender_phone` POST handler block with one that reads:
  - `request.form.getlist("sender_name")` (parallel list, one entry per row)
  - `request.form.getlist("sender_phone")` (parallel list, one entry per row)
  - `request.form.getlist("sender_allowed")` (checkbox list — each entry's value is the row index of a checked box; unchecked rows are absent)
  Build a new `cfg.senders` dict by iterating the lists: for each row, strip name and phone, skip if phone is empty, otherwise determine `allowed=True` iff `str(row_index)` is in the parsed `sender_allowed` list else `False`. Store under `normalize_phone(phone)` with `{"name": name or phone, "allowed": allowed, "phone": phone}` (the original phone preserved for round-trip). If the entries list is empty (zero rows posted), DO NOT wipe the existing `cfg.senders` (defensive partial-post handling). There is NO per-row `sender_status` field — lifecycle is the LIST-level `text_settings.enforcement_enabled` toggle
- [ ] 6.2 In the same file, parse `request.form.get("enforcement_enabled") == "1"` for `cfg.text_settings.enforcement_enabled` (default `True` when the field is absent). Also parse `request.form.get("name_display_format")` for `cfg.effects_settings.name_display_format` (default `"first_initial_if_duplicates"` when the field is absent or unrecognized). There is NO `sender_mode` field — the allowlist interpretation is the only behavior
- [ ] 6.3 In the same file, update the existing `sign_name` / `timezone` POST handler to write to the nested locations: `cfg.sign_settings.sign_name = sign_name` (was `cfg.sign.name`) and `cfg.sign_settings.timezone = timezone` (was top-level `cfg.timezone`). The HTML form field NAMES are unchanged (`sign_name` and `timezone`) — only the assignment targets move into the nested block
- [ ] 6.4 In the same file, update the Filter Rules POST handler to also parse `request.form.get("filter_status") == "on"` (or whatever the new field is) to set the new rule's `status` to `"enabled"` or `"disabled"`. Existing rules updated via the table form should preserve their checkbox state across POSTs (the handler reads the per-row `filter_status_<i>` checkbox values, mirroring the senders pattern). (FilterRule.status is a SEPARATE per-RULE lifecycle, distinct from the senders list which has no per-entry lifecycle.)
- [ ] 6.5 Add tests in `tests/settings_post_handler_test.py` (or extend an existing file) asserting:
  - A POST with one row (`sender_name=Alice`, `sender_phone=+15551234567`, `sender_allowed=0`) results in `cfg.senders["+15551234567"] == {"name": "Alice", "allowed": True, "phone": "+15551234567"}` after save
  - A POST with one row (`sender_name=Bob`, `sender_phone=+15558888888`, no `sender_allowed` value for Bob's row — the Allowed checkbox is unchecked) results in `cfg.senders[<normalized>]["allowed"] == False` after save
  - A POST with three rows where `sender_allowed=0` and `sender_allowed=2` are present results in row 0 (allowed=True), row 1 (allowed=False), row 2 (allowed=True) — each row's checkbox state is independent
  - A POST with `enforcement_enabled=0` results in `cfg.text_settings.enforcement_enabled == False`
  - A POST without the `enforcement_enabled` field results in `cfg.text_settings.enforcement_enabled == True` (defensive default)
  - A POST with `enforcement_enabled=1` and a sender not in the list still renders the sender's message after applying the config (master toggle bypass; test the round-trip via a `MessageManager` fixture or a `_handle_config` simulation)
  - A POST with `name_display_format=first` results in `cfg.effects_settings.name_display_format == "first"`
  - A POST with no `name_display_format` field results in `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` (defensive default)
  - A POST with `name_display_format=last_only` (not a valid value) results in `cfg.effects_settings.name_display_format == "first_initial_if_duplicates"` (defensive fallback — the operator's partial form doesn't corrupt the config)
  - A POST with `sign_name=My Sign` results in `cfg.sign_settings.sign_name == "My Sign"` (not `cfg.sign.name`)
  - A POST with `timezone=US/Eastern` results in `cfg.sign_settings.timezone == "US/Eastern"` (not top-level `cfg.timezone`)
  - A POST with a row with empty `sender_phone` drops that row from the saved entries
  - A POST with zero senders rows preserves the previous `cfg.senders`
  - A POST with a row with formatted phone (`+1 (555) 123-4567`) stores under the normalized key `+15551234567` and preserves the original in `cfg.senders[<key>]["phone"]`
  - There is NO `sender_mode` field — a POST that includes `sender_mode=blocklist` is silently ignored (the field is not used; the allowlist interpretation is the only behavior)
  - There is NO per-row `sender_status` field — a POST that includes `sender_status=0` is silently ignored (per-entry lifecycle is not in the v3 wire shape)

## 7. /settings template: fix broken iteration, add Allowed checkbox, add Filter Rules Status checkbox, add enforcement checkbox + name display format dropdown, update sign_settings reads

- [ ] 7.1 In `heart-message-manager/templates/settings.html`, REPLACE the existing "Allowed Senders" panel (which iterates `cfg.allowed_senders`, an attribute that does not exist on `SignConfig`) with a proper iteration over `cfg.senders.items()`. The new panel SHALL contain:
  - A **Senders** section header (replacing "Allowed Senders")
  - At the top of the section: a single **Enforce senders filter** checkbox (its `name` attribute SHALL be `enforcement_enabled` and its `value` SHALL be `"1"`; pre-checked iff `cfg.text_settings.enforcement_enabled == True`) AND a **Name display format** dropdown (its `name` attribute SHALL be `name_display_format` and its options SHALL pre-select the value matching `cfg.effects_settings.name_display_format`). There is NO mode radio.
  - A short helper line above the table: "Phone numbers are normalized to +1XXXXXXXXXX."
  - A table with four columns: `Name` (text input), `Phone (E.164)` (text input), `Allowed` (checkbox), and `Remove` (button). Pre-populate one row per entry in `cfg.senders.items()` (key = normalized_phone, value = `{"name", "allowed", "phone"}`). The Name input's `value` is `entry["name"]`, the Phone input's `value` is the **normalized dict key** (e.g. `+15551234567`, NOT the original `entry["phone"]` like `+1 (555) 123-4567`), the Allowed checkbox is `checked` iff `entry["allowed"] is True` (its `name` attribute SHALL be `sender_allowed` and its `value` SHALL be the row index). The table SHALL NOT have a Status column — lifecycle is the LIST-level `text_settings.enforcement_enabled` toggle
  - An `+ Add Entry` button that appends a new empty row via JS (Allowed checkbox defaults to checked)
  - A `Remove` button per row that deletes the row from the form via JS
  - The form posts parallel lists `sender_name`, `sender_phone`, and `sender_allowed` (checkbox list indexed by row; only checked rows appear in the form data, with value equal to their row index). It ALSO posts `enforcement_enabled=1` when checked (absent when unchecked) and `name_display_format=<value>` always. There is NO per-row `sender_status` field
- [ ] 7.2 In the same template, update the existing **Sign Identity** panel reads to use the new nested locations:
  - The `sign_name` input reads `value="{{ cfg.sign_settings.sign_name }}"` (was `cfg.sign.name`)
  - The `timezone` dropdown reads `cfg.sign_settings.timezone` for its selected option (was top-level `cfg.timezone`)
  - The HTML form field NAMES are unchanged (`sign_name` and `timezone`) — only the source attribute paths change
- [ ] 7.3 In the same template, modify the existing **Filter Rules** panel:
  - Add a `Status` column between `Pattern` and `Action`. Each row SHALL render a checkbox `checked` iff `cfg.filters[i].status == "enabled"`. The checkbox's `name` attribute SHALL be `filter_status_<row_index>` (per-row indexed name) so the POST handler can read each row's state independently. (This is the per-RULE lifecycle — separate from the senders list which has no per-entry lifecycle.)
  - Remove the `sender` option from the Add Rule `Type` dropdown (keep `keyword`, `regex`, `message`)
  - Add an `Enabled` checkbox to the Add Rule form, checked by default — the form posts `filter_status=on` for new rules when checked (the new rule is created with `status="enabled"`); an unchecked box produces `status="disabled"`
- [ ] 7.4 Add a test in `tests/settings_template_test.py` (or extend an existing template test) asserting:
  - The template iterates `cfg.senders.items()` (not `cfg.allowed_senders`) — grep the rendered template string for `allowed_senders`, no hits
  - The rendered section title is "Senders" (not "Allowed Senders")
  - An "Enforce senders filter" checkbox is rendered at the top of the Senders section with the correct state (checked when `cfg.text_settings.enforcement_enabled == True`, unchecked when `False`)
  - A "Name display format" dropdown is rendered at the top of the Senders section with the correct selection (matching `cfg.effects_settings.name_display_format`)
  - A helper line "Phone numbers are normalized to +1XXXXXXXXXX." appears above the table
  - The template renders the Allowed column with a checkbox per row (NOT a dropdown); the checkbox's `name` attribute is `sender_allowed` and its `value` is the row index
  - The Allowed checkbox is `checked` when the entry's `allowed is True` and unchecked when `allowed is False`
  - The template does NOT render a Status column on the senders table — exactly four columns (Name / Phone (E.164) / Allowed / Remove)
  - The template does NOT render any `sender_status` input field
  - The template's Phone input shows the normalized phone format (`+15551234567`), not the original (`+1 (555) 123-4567`) — even when `entry["phone"]` carries the original
  - The template does NOT render any `sender_mode` input — there is no mode radio, no blocklist interpretation
  - The template's Filter Rules table renders the new `Status` column with a checkbox per row (NOT a dropdown)
  - The template's Add Rule dropdown offers exactly `keyword`, `regex`, `message` (no `sender` option)
  - The template's Sign Identity panel reads `cfg.sign_settings.sign_name` for the sign_name input's value (not `cfg.sign.name`)
  - The template's Sign Identity panel reads `cfg.sign_settings.timezone` for the timezone dropdown's selected option (not top-level `cfg.timezone`)

## 8. End-to-end regression: existing tests still pass

- [ ] 8.1 Run the full test suite: `PYTHONPATH=. pytest tests/ -v`. Confirm no regressions. Fix any test that breaks because it depended on:
  - The deprecated `SignConfig(allowed_senders=...)` parameter (update those tests to use the `senders` dict with `allowed`)
  - The removed top-level `timezone` parameter (update to `sign_settings=SignSettings(sign_name=..., timezone=...)`)
  - The removed `sign` parameter (update to `sign_settings=...`)
  - The removed top-level `enforcement_enabled` parameter (update to `text_settings=TextSettings(enforcement_enabled=...)`)
  - The removed top-level `name_display_format` parameter (update to `effects_settings=EffectsSettings(name_display_format=...)`)
  - `cfg.sign.name` reads (update to `cfg.sign_settings.sign_name`)
  - `cfg.timezone` reads (update to `cfg.sign_settings.timezone`)
  - `cfg.enforcement_enabled` reads (update to `cfg.text_settings.enforcement_enabled`)
  - `cfg.name_display_format` reads (update to `cfg.effects_settings.name_display_format`)
- [ ] 8.2 Manually verify the egress-not-ingress guarantee by walking through the message flow on paper: an SMS arrives at `/api/messages`, gets persisted to SQLite + S3 + MQTT (no senders check), arrives at the Pi's `MessageManager._handle_message`, populates the ring buffer, gets enriched with the current `cfg.senders` decision AND `cfg.text_settings.enforcement_enabled`, and either appears or is suppressed on the next `get_messages(suppress=True)` read. The Pi's `MessageManager._handle_config` re-enriches the buffer on every config change so a sender added later flips a previously-suppressed message to visible
- [ ] 8.3 Verify the v2 → v3 migration end-to-end: take a v2 config with `senders=[{"phone": "+15551234567", "name": "Alice"}]` (no legacy `status`), `filters=[{"type": "keyword", "pattern": "spam"}, {"type": "sender", "pattern": "+15559999999"}]`, top-level `sign={"name": "My Sign"}`, top-level `timezone="US/Eastern"`, top-level `enforcement_enabled=False`, and top-level `name_display_format="full"`, run it through `SignConfig.from_dict(...)`, and confirm the result has:
  - `sign_settings: {"sign_name": "My Sign", "timezone": "US/Eastern"}` (top-level `sign` and `timezone` gone; renamed block; renamed field)
  - `text_settings: {"speed": 3, "color": 16711680, "text_effect": "scroll", "enforcement_enabled": False}` (new field added; existing fields default-filled because the v2 input didn't have them)
  - `effects_settings: {..., "name_display_format": "full"}` (new field added; existing fields default-filled because the v2 input didn't have them)
  - The senders entry with `allowed=True` (backfilled for v2 entries without a legacy status field)
  - The keyword filter with `status="enabled"` backfilled
  - The sender rule converted to a new senders entry with `allowed=False` + `name="+15559999999"` + `phone="+15559999999"` (no `status` field on either migrated entry)
  - The `filters` list contains only the keyword rule
  - Round-trip back through `to_dict()` and confirm `from_dict(to_dict(...))` is idempotent (no further migration runs)
- [ ] 8.4 Document the behavior change in the operator-facing release notes: after the upgrade, unlisted senders are suppressed; the operator must either add their known senders with `Allowed=checked` after the upgrade OR uncheck "Enforce senders filter" to bypass filtering entirely (every message renders, names still resolve via the display-name lookup). Stored `type=sender` rules are migrated to senders list entries (allowed=False, no status field) — the operator should review the migrated entries and either delete them or flip their `allowed` checkbox to True. Note that the configuration layout has changed: the sign's name and timezone now live in `sign_settings`, the enforcement toggle in `text_settings`, and the name display format in `effects_settings` — the migration brings stored configs forward automatically, but any external scripts or tests that read these as top-level attributes need updating.
