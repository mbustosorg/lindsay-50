## 1. phone_utils.py — last-10-digits normalization helper

- [ ] 1.1 Create `lib_shared/phone_utils.py` with one public function `normalize_phone(s: str) -> str`. The function SHALL strip non-digit characters, then return `"+1" + last_10_digits` if exactly 10 or 11 digits remain (11 only if leading digit is `"1"`); otherwise return the original input verbatim (passthrough for malformed values). No external dependencies; stdlib only
- [ ] 1.2 Add `tests/phone_utils_test.py` with the full truth table: E.164 (`+15551234567` → self), 10-digit (`5551234567` → `+15551234567`), with parens/dashes (`+1 (555) 123-4567` → `+15551234567`), with dots/spaces (`555.123.4567` → `+15551234567`), 11-digit starting with `1` (`15551234567` → `+15551234567`), empty string (`` → ``), non-numeric (`"not-a-phone"` → `"not-a-phone"`), and shorter-than-10 (`"12345"` → `"12345"`)

## 2. SignConfig.senders shape change + FilterRule taxonomy + CURRENT_VERSION bump

- [ ] 2.1 In `lib_shared/models.py`, change `SignConfig.senders` from `dict[str, str]` (phone → name) to `dict[str, dict]` (normalized_phone → `{"name": str, "action": "allow" | "suppress", "status": "enabled" | "disabled", "phone": str}`). Update `from_dict` to: for each wire entry in `data.get("senders", [])`, normalize the phone via `phone_utils.normalize_phone` and store under the normalized key with `name=entry["name"]`, `action=entry.get("action", "allow")`, `status=entry.get("status", "enabled")`, `phone=entry["phone"]`. Update `to_dict` to: emit each value as a wire entry with `phone=value["phone"]` (the original, not the normalized key), `name=value["name"]`, `action=value["action"]`, `status=value["status"]`. Sort by phone for deterministic output
- [ ] 2.2 Remove the deprecated `allowed_senders: list[str] | None = None` parameter from `SignConfig.__init__`. The parameter is gone — the constructor raises `TypeError` if called with it. Update any tests that used it (per the docstring, it was only kept for test back-compat)
- [ ] 2.3 In `lib_shared/models.py`, change `FilterRule.enabled: bool` to `FilterRule.status: "enabled" | "disabled"`. Update `FilterRule.from_dict` to accept `status` as an optional key defaulting to `"enabled"`, and to reject any `action` value other than `"suppress"` with `ValueError`. Update `FilterRule.to_dict` to always include `status` in the output. Restrict `FilterRule.type` to the set `{"keyword", "regex", "message"}` — reject any other value (including `"sender"`, which is REMOVED from the wire) with `ValueError`
- [ ] 2.4 Bump `SignConfig.CURRENT_VERSION` from `2` to `3`. The `version` argument default in `SignConfig.__init__` becomes `3`
- [ ] 2.5 Add tests in `tests/senders_status_test.py` asserting:
  - `SignConfig().senders == {}` (default empty dict-of-dict)
  - `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice", "action": "allow", "status": "enabled"}]})` produces `cfg.senders["+15551234567"] == {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}` (key is normalized, value preserves original)
  - `from_dict({"senders": [{"phone": "+15551234567", "name": "Alice"}]})` (no `action`/`status`) produces `cfg.senders["+15551234567"]["action"] == "allow"` AND `["status"] == "enabled"` (back-compat defaults)
  - `to_dict()` on a config with two senders entries emits a list of two `{"phone": ..., "name": ..., "action": ..., "status": ...}` dicts, sorted by phone
  - `from_dict(to_dict(cfg))` round-trips losslessly (original phone format preserved, dict keys normalized, action/status preserved)
  - `SignConfig(allowed_senders=[...])` raises `TypeError` (parameter removed)
- [ ] 2.6 Add tests in `tests/filter_rule_status_test.py` asserting:
  - `FilterRule(type="keyword", pattern="spam").status == "enabled"` (default)
  - `FilterRule.from_dict({"type": "keyword", "pattern": "spam"})` (no `status` key) → `rule.status == "enabled"`
  - `FilterRule.from_dict({"type": "keyword", "pattern": "spam", "status": "disabled"})` → `rule.status == "disabled"`
  - `FilterRule.from_dict({"type": "keyword", "pattern": "spam", "action": "allow"})` raises `ValueError` (action="allow" is not in v1)
  - `FilterRule.from_dict({"type": "sender", "pattern": "+15551234567"})` raises `ValueError` (type="sender" is REMOVED from the wire)
  - `rule.to_dict()` always includes the `status` key

## 3. _v2_to_v3 migration in lib_shared/config_migrations

- [ ] 3.1 In `lib_shared/config_migrations.py`, add a `_v2_to_v3(d)` migration function and register it in the `MIGRATIONS` dict as `{1: _v1_to_v2, 2: _v2_to_v3}`. The function SHALL:
  - Return a shallow copy of `d` (do not mutate the caller's dict — matches the v1 → v2 migration's contract)
  - If `senders` is a dict (legacy v1 shape `{phone: name}`), convert it to the list shape `[{"phone": p, "name": n, "action": "allow", "status": "enabled"} for (p, n) in d["senders"].items()]`
  - If `senders` is a list, for each entry: rename `status` field (values `"allowed"|"blocked"`) to `action` (values `"allow"|"suppress"`); add `status="enabled"` lifecycle field. Also add `action="allow"` if missing, and `status="enabled"` if missing (defensive backfills for partial payloads)
  - For each rule in `d.get("filters", [])`:
    - If the rule has `type=sender`: convert to a senders list entry with `action="suppress"`, `status="enabled"`, `name=rule.pattern`, `phone=rule.pattern`. Append to `senders` (creating the list if absent, deduplicating by normalized phone — if the entry already exists in `senders`, leave it alone — the pre-existing entry wins). DROP the rule from `filters`.
    - Otherwise: rename `enabled` (bool) → `status` (enum). `enabled=True` → `status="enabled"`; `enabled=False` → `status="disabled"`. If `enabled` is missing, set `status="enabled"`.
  - Set `version` to `3`
  - Preserve `filters`, `sign`, `timezone`, `effects_settings`, `text_settings` unchanged
- [ ] 3.2 Add tests in `tests/config_migrations_test.py` (extending the existing file) asserting:
  - `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice"}], "filters": [{"type": "keyword", "pattern": "spam"}]}, current_version=3)` returns a v3 dict with the senders entry having `action="allow"` + `status="enabled"`, the filter having `status="enabled"`, and `version: 3`
  - `migrate({"version": 2, "senders": {"+15551234567": "Alice"}}, current_version=3)` (legacy dict shape) returns a v3 dict with senders in the list shape `[{phone, name, action: "allow", status: "enabled"}]`
  - `migrate({"version": 2, "senders": [{"phone": "+15551234567", "name": "Alice", "status": "blocked"}]}, current_version=3)` returns the senders entry with `action="suppress"` (renamed from `status="blocked"`) AND `status="enabled"` (new lifecycle field backfilled)
  - `migrate({"version": 2, "filters": [{"type": "keyword", "pattern": "spam", "enabled": False}]}, current_version=3)` preserves the existing `enabled=False` (renamed to `status="disabled"`; the migration is idempotent on rules that already have `status`)
  - `migrate({"version": 2, "filters": [{"type": "sender", "pattern": "+15551234567"}], "senders": []}, current_version=3)` returns a v3 dict with `filters=[]` AND `senders=[{"phone": "+15551234567", "name": "+15551234567", "action": "suppress", "status": "enabled"}]` (the sender rule was converted to a senders entry)
  - `migrate({"version": 2, "filters": [{"type": "sender", "pattern": "+15559999999"}], "senders": [{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]}, current_version=3)` returns a v3 dict with `filters=[]` AND `senders` containing TWO entries (the original Alice entry migrated to `action="allow"` + `status="enabled"` AND the new sender rule converted to `action="suppress"` + `status="enabled"`)
  - `migrate({"version": 3, "senders": [...]}, current_version=3)` is idempotent (input returned unchanged)
  - The migration does NOT mutate the input dict (the caller's original dict retains its `version: 2` and original `senders` shape)
  - `migrate({"version": 1}, current_version=3)` runs BOTH v1 → v2 AND v2 → v3 in sequence (end-to-end chain still works)

## 4. FilteredMessages: senders-action check + FilterRule.status skip

- [ ] 4.1 In `lib_shared/messages.py`, add a module-level helper `should_render_sender(sender: str, senders: dict) -> bool` (or a method on `SignConfig` — pick the simpler form). The function SHALL:
  - Normalize `sender` via `phone_utils.normalize_phone`
  - Look up `senders.get(normalized)` — if absent, return `False` (sender is not in the list → suppressed)
  - If present and `entry["action"] == "allow"` AND `entry["status"] == "enabled"`, return `True`
  - Otherwise (action="suppress" OR status="disabled"), return `False`
- [ ] 4.2 In the same file, modify `FilteredMessages._enrich_messages` to call `should_render_sender(entry.message.sender, self._config.senders)` AFTER the existing `_apply_filter` loop. If the function returns `False` AND no FilterRule matched, append a synthetic rule dict `{"type": "sender_action", "pattern": "<normalized sender>", "action": "suppress"}` to `entry.rules` and set `entry.suppressed = True`. If the function returns `False` AND FilterRules already matched, set `entry.suppressed = True` (the real rules win for `entry.rules` display — no synthetic marker added in that case)
- [ ] 4.3 In the same file, update the display-name lookup from `entry.sender_name = self._config.senders.get(entry.message.sender)` to `entry.sender_name = (self._config.senders.get(normalize_phone(entry.message.sender)) or {}).get("name")`. The lookup works regardless of `action`/`status` — display names are always resolved (the operator sees "From: Alice" even for blocked/disabled senders)
- [ ] 4.4 In the same file, modify `FilteredMessages._apply_filter` to skip any rule where `rule.status == "disabled"` (the rule is treated as absent — it does NOT contribute to the suppressing list). The existing rule-match logic for `type == "keyword"`, `type == "regex"`, and `type == "message"` is unchanged. REMOVE the `type == "sender"` branch from `FilteredMessages._matches` (sender matching moved to the senders list)
- [ ] 4.5 Add tests in `tests/senders_status_test.py` (extending the existing file) asserting:
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}})` returns `True`
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "action": "allow", "status": "disabled", "phone": "+15551234567"}})` returns `False` (status=disabled suppresses)
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "action": "suppress", "status": "enabled", "phone": "+15551234567"}})` returns `False` (action=suppress suppresses)
  - `should_render_sender("+15551234567", {"+15551234567": {"name": "Alice", "action": "suppress", "status": "disabled", "phone": "+15551234567"}})` returns `False` (both axes suppress)
  - `should_render_sender("+15551234567", {})` returns `False` (sender not in dict)
  - `should_render_sender("+1 (555) 123-4567", {"+15551234567": {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}})` returns `True` (incoming sender normalized before lookup matches the normalized dict key)
  - End-to-end through `MessageManager`: with Alice allow+enabled, an SMS from Alice is added to the ring buffer and `get_messages(suppress=True)` includes it
  - End-to-end: with Alice allow+disabled, an SMS from Alice is added to the ring buffer but `get_messages(suppress=True)` excludes it
  - End-to-end: with Alice suppress+enabled, an SMS from Alice is added to the ring buffer but `get_messages(suppress=True)` excludes it
  - End-to-end: with Alice not in the list, an SMS from Alice is added to the ring buffer but `get_messages(suppress=True)` excludes it
  - End-to-end: a config update that adds Alice to the list with `action="allow"` + `status="enabled"` re-enriches the buffer and Alice's previously-suppressed message becomes visible (the egress-not-ingress guarantee — no Twilio re-fetch needed)
  - End-to-end: a config update that flips Alice from allow+enabled to allow+disabled re-enriches and her previously-visible message becomes suppressed
  - End-to-end: a config update that flips Alice from allow+enabled to suppress+enabled re-enriches and her previously-visible message becomes suppressed
  - End-to-end: the `entry.rules` list contains a synthetic `{"type": "sender_action", ...}` marker when senders list suppressed a message AND no FilterRule matched
  - End-to-end: the `entry.rules` list does NOT contain the synthetic marker when a FilterRule also matched (the real rule wins for display)
  - End-to-end: `MessageView.sender_name` is populated from `cfg.senders[<normalized_phone>]["name"]` regardless of `action`/`status` (display-name lookup works even when blocked)
- [ ] 4.6 Add tests in `tests/filter_rule_status_test.py` (extending the existing file) asserting:
  - `_apply_filter` returns an empty list when ALL rules have `status="disabled"`
  - `_apply_filter` returns the rule when it has `status="enabled"` and matches the message
  - `_apply_filter` skips a rule with `status="disabled"` even when its pattern matches (the disabled rule is treated as absent)

## 5. /settings POST handler: parse per-row Action dropdown and Status checkbox list

- [ ] 5.1 In `heart-message-manager/main.py`, replace the existing `sender_name` / `sender_phone` POST handler block with one that reads:
  - `request.form.getlist("sender_name")` (parallel list, one entry per row)
  - `request.form.getlist("sender_phone")` (parallel list, one entry per row)
  - `request.form.getlist("sender_action")` (parallel list, dropdown value per row: `"allow"` or `"suppress"`)
  - `request.form.getlist("sender_status")` (checkbox list — each entry's value is the row index of a checked box; unchecked rows are absent)
  Build a new `cfg.senders` dict by iterating the lists: for each row, strip name and phone, skip if phone is empty, otherwise determine `status="enabled"` iff `str(row_index)` is in the parsed `sender_status` list else `"disabled"`. Store under `normalize_phone(phone)` with `{"name": name or phone, "action": action or "allow", "status": status, "phone": phone}`. Default `action` to `"allow"` when the form field is missing or unrecognized (defensive against partial / legacy form posts). If the entries list is empty (zero rows posted), DO NOT wipe the existing `cfg.senders` (defensive partial-post handling)
- [ ] 5.2 In the same file, update the Filter Rules POST handler to also parse `request.form.get("filter_status") == "on"` (or whatever the new field is) to set the new rule's `status` to `"enabled"` or `"disabled"`. Existing rules updated via the table form should preserve their checkbox state across POSTs (the handler reads the per-row `filter_status_<i>` checkbox values, mirroring the senders pattern)
- [ ] 5.3 Add tests in `tests/settings_post_handler_test.py` (or extend an existing file) asserting:
  - A POST with one row (`sender_name=Alice`, `sender_phone=+15551234567`, `sender_action=allow`, `sender_status=0`) results in `cfg.senders["+15551234567"] == {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"}`
  - A POST with one row (`sender_name=Bob`, `sender_phone=+15558888888`, `sender_action=suppress`, no `sender_status` entry for Bob's row) results in `cfg.senders[<normalized>]["status"] == "disabled"`
  - A POST with three rows where `sender_status=0` and `sender_status=2` are present results in row 0 enabled, row 1 disabled, row 2 enabled (each row's checkbox state is independent)
  - A POST with a row with empty `sender_phone` drops that row from the saved entries
  - A POST with zero senders rows preserves the previous `cfg.senders`
  - A POST with a row with formatted phone (`+1 (555) 123-4567`) stores under the normalized key `+15551234567` and preserves the original in `cfg.senders[<key>]["phone"]`

## 6. /settings template: fix broken iteration, add Action dropdown + Status checkbox, add Filter Rules Enabled checkbox

- [ ] 6.1 In `heart-message-manager/templates/settings.html`, REPLACE the existing "Allowed Senders" panel (which iterates `cfg.allowed_senders`, an attribute that does not exist on `SignConfig`) with a proper iteration over `cfg.senders.items()`. The new panel SHALL contain:
  - A **Senders** section header (replacing "Allowed Senders")
  - A short helper line above the table: "Phone numbers are normalized to +1XXXXXXXXXX."
  - A table with five columns: `Name` (text input), `Phone (E.164)` (text input), `Action` (dropdown: `Allow` / `Suppress`), `Status` (checkbox), and `Remove` (button). Pre-populate one row per entry in `cfg.senders.items()` (key = normalized_phone, value = `{"name", "action", "status", "phone"}`). The Name input's `value` is `entry["name"]`, the Phone input's `value` is the **normalized dict key** (e.g. `+15551234567`, NOT the original `entry["phone"]` like `+1 (555) 123-4567`), the Action dropdown has the matching option selected, AND the Status checkbox is `checked` iff `entry["status"] == "enabled"`. The Status checkbox's `name` attribute SHALL be `sender_status` and its `value` SHALL be the row index (the standard HTML checkbox-with-index pattern for parallel-list forms)
  - An `+ Add Entry` button that appends a new empty row via JS (Action dropdown defaults to `Allow`, Status checkbox defaults to checked)
  - A `Remove` button per row that deletes the row from the form via JS
  - The form posts parallel lists `sender_name`, `sender_phone`, `sender_action`, and the checkbox list `sender_status` (only checked rows appear in the form data, with value equal to their row index)
- [ ] 6.2 In the same template, modify the existing **Filter Rules** panel:
  - Add a `Status` column between `Pattern` and `Action`. Each row SHALL render a checkbox `checked` iff `cfg.filters[i].status == "enabled"`. The checkbox's `name` attribute SHALL be `filter_status_<row_index>` (per-row indexed name) so the POST handler can read each row's state independently
  - Remove the `sender` option from the Add Rule `Type` dropdown (keep `keyword`, `regex`, `message`)
  - Add an `Enabled` checkbox to the Add Rule form, checked by default — the form posts `filter_status=on` for new rules when checked (the new rule is created with `status="enabled"`); an unchecked box produces `status="disabled"`
- [ ] 6.3 Add a test in `tests/settings_template_test.py` (or extend an existing template test) asserting:
  - The template iterates `cfg.senders.items()` (not `cfg.allowed_senders`) — grep the rendered template string for `allowed_senders`, no hits
  - The rendered section title is "Senders" (not "Allowed Senders")
  - A helper line "Phone numbers are normalized to +1XXXXXXXXXX." appears above the table
  - The template renders the Status column with a checkbox per row (NOT a dropdown)
  - The Status checkbox's `name` attribute is `sender_status` and its `value` is the row index
  - The Status checkbox is `checked` when the entry's `status == "enabled"` and unchecked when `status == "disabled"`
  - The template's Phone input shows the normalized phone format (`+15551234567`), not the original (`+1 (555) 123-4567`) — even when `entry["phone"]` carries the original
  - The template's Filter Rules table renders the new `Status` column with a checkbox per row (NOT a dropdown)
  - The template's Add Rule dropdown offers exactly `keyword`, `regex`, `message` (no `sender` option)

## 7. End-to-end regression: existing tests still pass

- [ ] 7.1 Run the full test suite: `PYTHONPATH=. pytest tests/ -v`. Confirm no regressions. Fix any test that breaks because it depended on the deprecated `SignConfig(allowed_senders=...)` parameter (update those tests to use the `senders` dict with `action`+`status`)
- [ ] 7.2 Manually verify the egress-not-ingress guarantee by walking through the message flow on paper: an SMS arrives at `/api/messages`, gets persisted to SQLite + S3 + MQTT (no senders-status check), arrives at the Pi's `MessageManager._handle_message`, populates the ring buffer, gets enriched with the current `senders` dict decision, and either appears or is suppressed on the next `get_messages(suppress=True)` read. The Pi's `MessageManager._handle_config` re-enriches the buffer on every config change so a sender added later flips a previously-suppressed message to visible
- [ ] 7.3 Verify the v2 → v3 migration end-to-end: take a v2 config with `senders=[{"phone": "+15551234567", "name": "Alice", "status": "allowed"}]` and `filters=[{"type": "keyword", "pattern": "spam"}, {"type": "sender", "pattern": "+15559999999"}]`, run it through `SignConfig.from_dict(...)`, and confirm the result has the senders entry with `action="allow"` + `status="enabled"` (renamed + lifecycle backfilled), the keyword filter with `status="enabled"` backfilled, the sender rule converted to a new senders entry with `action="suppress"` + `status="enabled"` + `name="+15559999999"` + `phone="+15559999999"`, and the `filters` list contains only the keyword rule. Round-trip back through `to_dict()` and confirm `from_dict(to_dict(...))` is idempotent (no further migration runs)
- [ ] 7.4 Document the behavior change in the operator-facing release notes: after the upgrade, unlisted senders are suppressed; the operator must explicitly add their known senders with `Action=Allow` + `Status=Enabled` after the upgrade to restore their visibility. Stored `type=sender` rules are migrated to senders list entries (action=suppress, status=enabled) — the operator should review the migrated entries and either delete them or change their action to allow