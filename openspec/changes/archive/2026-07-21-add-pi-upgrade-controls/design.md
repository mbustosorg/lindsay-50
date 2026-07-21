## Context

The lindsay-50 system has two halves deployed from one codebase:

- **Flask app** (`heart-message-manager/`) → Heroku via `git push heroku main`. Operator upgrades are tied to Flask deploys and `HEROKU_SLUG_COMMIT` is the source of truth for the Flask version.
- **Pi controller** (`heart-matrix-controller/`) → runs on a Raspberry Pi 4 via systemd. Subscribes to the same MQTT topic as Flask. Runs `loader.py` (per `self-upgrading-matrix-controller`) which stages new versions, probes via `.status.json`, and atomically swaps a `current -> v-<sha>/` symlink. The Pi publishes a `StatusSnapshot` over MQTT (per the **merged** `add-sign-status-reports` change) on a **5-second** cadence. Flask also publishes a `StatusSnapshot` (with `source: "flask"`) on the same cadence.

Today's Pi upgrade control is binary and admin-UI-free: the loader auto-upgrades on Flask deploys via the `/api/sign/boot-config` HTTP fetch on boot + the one-shot `check-for-update` MQTT hint. The operator has no UI to pin a specific version, no UI to force-upgrade, no UI to restart, no UI to shutdown. Recovery from a bad upgrade means SSH + `git` + `systemctl`. The principal will not always be physically present.

This change adds operator-facing controls without removing the existing recovery paths. The existing `/api/sign/boot-config` endpoint is **kept unchanged** so currently-deployed Pis continue to upgrade through it. A new endpoint `GET /api/sign/settings` is added alongside, returning the resolved short version + timezone for new Pis to call. Both endpoints are served by the same Flask process.

The three new commands (`force-upgrade`, `restart`, `shutdown`) ride the existing `type=command` envelope + `MessageManager.dispatch` routing introduced in `self-upgrading-matrix-controller`. The existing `check-for-update` registration is **kept** during the transitional period (this change) and **will be removed** in a follow-up change once all deployed Pis are confirmed to be running the new `GET /api/sign/settings` boot-fetch path.

The principal will not be physically present to recover from a bricked deploy. Design must be self-healing and never take down a working sign.

## Goals / Non-Goals

**Goals:**

- The Settings page exposes three version fields (Flask, Pi, Target Pi) and a Target-version input + Clear button so the operator can pin a specific SHA from the admin UI alone.
- The Settings page exposes three buttons (Force upgrade, Restart, Shutdown) that publish one-shot MQTT command envelopes; the Pi handles them with no SSH required.
- A new `GET /api/sign/settings` endpoint returns `{"target_version": "<short-sha>", "timezone": "US/Pacific"}` — both fields always concrete on the wire (Flask resolves operator-pin vs Flask-self before responding).
- The loader on the Pi calls `GET /api/sign/settings` on boot with a 5-second timeout. On success, the loader uses `target_version` as the upgrade target (compared as 7-char short SHAs). On any failure, the loader falls through to running `current/.../main.py` without upgrade (same safe default as today's `boot-config` failure path).
- The existing `GET /api/sign/boot-config` endpoint is **kept unchanged** so existing Pis can continue upgrading through it during the transitional period.
- The existing `check-for-update` MQTT hint registration is **kept** during the transitional period. Both the old and new upgrade paths coexist until a follow-up change confirms all Pis are on the new code.
- `target_version` is added to `SignSettings` (alongside the existing `name` field). `timezone` stays at the top level of `SignConfig` (no move). `SignConfig.CURRENT_VERSION` stays at **2** — no schema bump, no migration needed.
- The wire form is **short only** — 7-char SHA, no long form anywhere new. `git worktree add` accepts short SHAs (per `git-worktree(1)` and the comment at `loader.py:174`); the existing `worktree_dir(repo_dir, sha)` naming convention uses `f"v-{short_sha(sha)}"`; the loader's local-vs-target comparison becomes `short_sha(local_git_rev_parse_head) == target_version`.
- The existing `AUTO_UPDATE` key in `heart-matrix-controller/settings.toml` (read by `loader.py:833`) continues to gate the loader's auto-upgrade decision. This change does not modify that key. Future change will add a Pi-side `settings.json` override file mirroring `effects_settings.json`.
- All three new commands are routed through the loader's existing fallthrough — a failure in the handler leaves the Pi running on its current version, never bricks it.

**Non-Goals:**

- A MQTT events topic for discrete log entries (parked per `add-sign-status-reports`).
- Pi-side persistent version pinning beyond the server-side `sign.target_version` field.
- A Flask-side rollback UI (the existing `heroku rollback v123` operator workflow remains the rollback path).
- Per-pattern health checks beyond `.status.json`; the loader's existing `mqtt_connected` + `last_error` probe stays the source of truth for "is the new version safe to swap to?".
- A UI toggle for `AUTO_UPDATE`. The key is settings.toml-only (pre-existing).
- Returning the long-form SHA on any endpoint. The long form is derivable locally via `git rev-parse HEAD` (or `short_sha_inverse`) and is not needed on the wire for any current operation.
- Deleting `/api/sign/boot-config` or `check-for-update`. Both are retained for the transitional period and removed in a follow-up change.
- Any local-state cache files on the Pi. The Pi fetches the resolved target on every boot via `GET /api/sign/settings` — no disk caching of config or status snapshots. (A future change may add caching; out of scope here per the user's explicit direction.)
- Moving `timezone` from top-level into `sign.timezone`. `timezone` stays where it is — no v3 schema bump, no migration.

## Decisions

### D1. Three actions, one envelope type, `MessageManager.dispatch`

The three new commands ride the existing `type=command` envelope + `MessageManager.dispatch` routing introduced in `self-upgrading-matrix-controller`. The action strings are:

- `force-upgrade` — handler in `heart-matrix-controller/command_handlers.py:force_upgrade`. Calls `os.execvpe(sys.executable, [sys.argv[0], *sys.argv[1:]], env)` where `sys.argv[0]` is set to `<repo_dir>/heart-matrix-controller/loader.py` and `LINDSAY50_REPO_DIR` is on env. The loader then runs the same SHA-check + stage + probe + swap (or no-op) as on boot, bypassing the `AUTO_UPDATE` gate. The loader reads the resolved target from `GET /api/sign/settings` on its next boot phase.
- `restart` — handler calls `subprocess.run(["sudo", "reboot"], check=False)`. The loader (systemd ExecStart) restarts. Loader reads the resolved target from `GET /api/sign/settings` on the new boot.
- `shutdown` — handler calls `subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)`. Pi halts; loader handles reboot if/when it comes back.

Each command envelope is published exactly once on click; no retries, no debouncing. The Flask endpoint just publishes and returns 202.

- **Why:** Reuses an existing, tested routing path. No new envelope type, no new dispatch logic — just three new entries in the handler registry that `main.py:register_command_handlers` adds at boot alongside the (retained) `check-for-update` registration.
- **Alternative considered:** HTTP POST from Flask to a Pi-side webhook endpoint. Rejected — there's already MQTT plumbing; HTTP would be a parallel path.

### D2. `target_version` added to `SignSettings`; `timezone` stays at top level

`SignSettings` already carries `name` — the display name shown on the sign. This change adds one field to `SignSettings`:

- `target_version: str` (default: Flask's running short SHA, resolved at construction time) — the SHA the Pi should be running. Always concrete on the wire — Flask resolves operator-pin vs Flask-self before persisting and before responding. The UI input is nullable (operator's intent); the persisted value is never empty.

`SignSettings.timezone` is NOT added in this change. `timezone` continues to live at the top level of `SignConfig` (where it already lives as `SignConfig.timezone`). The wire shape for the new endpoint reads `sign.target_version` and `timezone` (top-level) — two fields, both always concrete.

`SignConfig.CURRENT_VERSION` stays at **2**. No migration is needed. The `SignConfig.from_dict` path ignores the unknown `sign.target_version` key on pre-change Flask deployments (forward-compatible); post-change Flask includes it on the wire but pre-change code does not read it (backward-compatible).

- **Why:** Adding `target_version` to `SignSettings` keeps per-sign identity (name) grouped with the upgrade pin. `timezone` is a pre-existing top-level field with no compelling reason to move it in this change — moving it would force a v2→v3 migration with no functional benefit (Flask's existing top-level `timezone` field already round-trips correctly).
- **Alternative considered:** Move `timezone` into `sign.timezone` for wire-shape consistency with `sign.target_version`. Rejected — adds a migration for cosmetic benefit, with no functional gain. The two fields can live in different places; the Settings UI groups them visually anyway.
- **Alternative considered:** A nullable `target_version` field on the wire with the Pi resolving the "track Flask" fallback. Rejected — server-side resolution is simpler, matches the user's "always concrete on the wire" direction, and removes a branch from the loader.

### D3. Keep `/api/sign/boot-config`; add `/api/sign/settings` alongside

Today the loader calls `/api/sign/boot-config` on every boot. With this change:

- The existing endpoint is **kept unchanged** (returns `{"expected_sha": "<long>", "short_sha": "<short>"}`). Currently-deployed Pis continue to call it and upgrade through it.
- A new endpoint `GET /api/sign/settings` is added. Returns `{"target_version": "<short-sha>", "timezone": "US/Pacific"}`. New Pis (post-this-change) call this endpoint on boot.
- The loader on a new Pi calls `/api/sign/settings` with a 5-second timeout. On success, the loader uses `target_version` as the upgrade target. On any failure (timeout, HTTP 5xx, malformed JSON, missing `target_version` field), the loader falls through to running `current/.../main.py` without upgrade — same safe default as today's `boot-config` failure path.
- The loader's local-vs-target comparison changes from `local == expected_sha` (full equality) to `short_sha(local_git_rev_parse_head) == target_version` (both are now short SHAs). This is the only loader-internal change needed to drop the long form.

The `check-for-update` MQTT hint registration is **kept** during the transitional period (this change). Both old and new upgrade paths coexist until a follow-up change confirms all Pis are on the new code. A future change will delete `heart-matrix-controller/check_for_update.py`, remove the registration from `main.py`, and stop Flask from publishing the one-shot `check-for-update` envelope at startup.

- **Why:** Keeping the legacy path means the existing Pi can roll forward through this change without breaking — Flask deploys the new code, the existing Pi still understands `check-for-update`, and the new Pi uses the new endpoint. Two paths, one Flask process. The follow-up change is the consolidation: delete the old path after the rollout is verified.
- **Alternative considered:** Drop `/api/sign/boot-config` and `check-for-update` immediately. Rejected — requires the existing Pi to upgrade through a code change before the new code is in place. The `check-for-update` envelope is the bridge.
- **Alternative considered:** Loader fetches both endpoints and prefers `/api/sign/settings` if it succeeds. Rejected — too clever; two paths means two code branches forever. The follow-up change cleanly removes the old path.

### D4. Wire form is short SHA only — no long form anywhere new

The new `/api/sign/settings` endpoint returns `target_version` as a 7-character short SHA. The legacy `/api/sign/boot-config` endpoint still returns both `expected_sha` and `short_sha` (unchanged). The long form does not appear in:

- The new endpoint's response shape.
- The new `SignSettings.target_version` field on the wire.
- The `SignConfig` schema (no `target_version_long` field anywhere).
- The new `/api/sign/commands/<action>` endpoint or its publish payload.

The long form remains a Flask internal (used for `git rev-parse HEAD` derivation, debugging logs, and as the discriminator in the existing `BootConfig` dataclass). The Pi truncates its own `git rev-parse HEAD` to 7 chars via `short_sha(local)` for the comparison (see D3). The worktree directory naming convention `v-<short_sha>` is unchanged (already short).

- **Why:** User's explicit direction — the long form is confusing and not needed for any current operation. The wire form is what the operator sees in the Settings UI ("currently pinned to: `abc1234`"), the UI input, and the dispatcher logs. A single short SHA on the wire is consistent with the operator's mental model and the worktree directory naming convention already in use.
- **Alternative considered:** Keep the long form available for debugging via an opt-in verbose mode. Rejected — `git rev-parse HEAD` runs locally on the Pi (or `HEROKU_SLUG_COMMIT` env on Flask) and is always available for debugging; no need for an endpoint surface.

### D5. `AUTO_UPDATE` continues to live in `heart-matrix-controller/settings.toml`

`AUTO_UPDATE = true|false` is the existing key in the Pi's local `settings.toml`. The loader reads it on every boot via `config_reader.py` (see `loader.py:833`). When `false`, the loader holds the current version even if the effective target differs. This change does not modify the key, the loader's read path, or the docstring.

- **Why:** The Pi-side override is a low-frequency operational escape hatch ("I'm near the Pi, something is wrong, hold this version") — not a per-deploy setting. `settings.toml` is the right place for low-frequency Pi-local knobs. Future change adds a `settings.json` override file mirroring `effects_settings.json` for richer use cases.
- **Alternative considered:** Persist `AUTO_UPDATE` in the `type=config` envelope. Rejected — keeps the override surface area on Flask, which is the wrong side when the operator's whole point is to *escape* Flask.

### D6. Loader consults `GET /api/sign/settings` + `AUTO_UPDATE`

The loader's upgrade-decision logic becomes:

```
def should_attempt_upgrade(active_short_sha, target_short_sha, auto_update):
    if active_short_sha == target_short_sha:
        return False  # nothing to do
    if not auto_update:
        return False  # Pi-local override: hold current
    return True
```

Where `target_short_sha` is read from `GET /api/sign/settings`'s response and `active_short_sha = short_sha(git rev-parse HEAD)`. The loader's existing `BOOT_HOLD_S = 17.0` (set by the merged `add-sign-status-reports` change) and `.status.json` probe remain the source of truth for "is the new version safe to swap to?".

`force-upgrade` (from the `type=command` envelope) bypasses the `AUTO_UPDATE` gate but still uses the effective target.

- **Why:** Two facts, one decision: resolved target + a settings.toml flag. One HTTP call, one settings.toml read. No MQTT wait, no disk cache.
- **Alternative considered:** Per-Pi override in the config envelope. Rejected — see D5.
- **Alternative considered:** Pi-side cache of the resolved target (mirrored from MQTT). Deferred — out of scope per the user's direction ("let's not tackle caching anything locally yet"). The single HTTP call on boot is the v1 mechanism.

### D7. UI: existing Settings page, new "Pi Upgrade Control" sub-section

Renders below the existing Settings page sections (Sign Name, Twilio, etc.) and above the Sign Health section (added by `add-sign-status-reports`). Reuses the existing Bootstrap 5 form layout. The variant for `/playful/settings` gets the same section with Tailwind styles.

Three read-only version displays use the same `data-sign-status-field="<name>"` attribute pattern as the Sign Health section, so `sign_status.js` and `pi_upgrade_settings.js` can co-populate the two sections from the same status-WS client. The Target Pi version input uses `data-upgrade-settings-field="target_version"` and is always editable; a small "Clear" button (`data-action="clear-target"`) empties the input. There is no `AUTO_UPDATE` checkbox — that knob is settings.toml-only (pre-existing).

- **Why:** Reuses the existing Settings page chrome — no new navigation, no new auth gate, no new templating pattern. The dual `data-*` attributes let both modules populate their fields without stepping on each other.

### D8. Flask-side status reporting: same topic, same 5s cadence, 8-key shape with a `source` discriminator

Flask publishes a small JSON dict on `MQTT_STATUS_TOPIC` every **5 seconds** (matching the merged Pi-side cadence — one cadence constant for the whole system):

```json
{
  "source": "flask",
  "active_sha": "abc123def456...",
  "short_sha": "abc1234",
  "started_at": "2026-07-07T10:00:00Z",
  "updated_at": "2026-07-07T11:00:00Z",
  "uptime_seconds": 3600,
  "mqtt_connected": true,
  "last_error": ""
}
```

The browser's existing status-WS client (per `add-sign-status-reports`) routes payloads by `source` field: Pi-published snapshots populate the Sign Health section + Dashboard pill via `sign_status.js`'s existing 4-state logic; Flask-published snapshots populate the new "Flask health" mini-section + the read-only Flask-version field in the Pi Upgrade Control section.

The Flask-side publisher also publishes the Flask short SHA in `short_sha` so the Settings page can read it without truncation.

- **Why:** The operator wants to know what the *server* thinks it's running (Flask version) and what the *sign* thinks it's running (Pi version). Both fields are needed for "is everything in sync?". One topic, one cadence, one browser subscriber, one schema.
- **Alternative considered:** Separate topic `MQTT_FLASK_STATUS_TOPIC`. Deferred — adds a third WS client to the browser.

### D9. Force-upgrade, restart, shutdown are 202-on-publish with no client confirmation

The Flask endpoint publishes the command envelope and returns `202 Accepted` immediately. There is no per-client ACK, no request-to-confirm modal (besides a regular browser `confirm()` dialog before publishing). The operator sees success/failure indirectly: the status flow's next snapshot reflects the new state (Pi version changes for upgrade; Pi goes offline for restart/shutdown).

- **Why:** Implementing a request-reply RPC over MQTT adds a new envelope type and a correlation-ID roundtrip for low-value UX. The Settings page already shows Pi status via the Sign Health section — that's the operator's actual feedback channel.

### D10. Loader fallthrough is unchanged: any failure → exec existing `current/.../main.py`

The three new handlers and any new loader logic preserve the `self-upgrading-matrix-controller` invariant: a failure anywhere in the upgrade path drops the user back into the currently-running `v-<sha>/main.py` via `os.execvpe`. A failed `GET /api/sign/settings` (timeout, HTTP error, malformed response) falls through to running the current version without staging anything.

- **Why:** The original loader design was specifically built around "Pi can never brick itself", and adding operator-driven commands is the highest-risk surface for introducing a brick. The fallthrough is the existing safety net; this change must not weaken it.

## Risks / Trade-offs

- **Operator pins Target to an old SHA, then forgets Flask has moved on.** → The Pi will hold the pinned version; the operator sees both Flask and Pi versions on the Settings page and can clear Target to resume tracking. No silent data loss; the visual mismatch is the cue.
- **Operator sets `AUTO_UPDATE=false` in settings.toml, then forgets to re-enable.** → The Pi holds the current version indefinitely. Recovery: SSH in, flip the flag, `sudo systemctl restart lindsay_50`. The Settings page can't help (it's the Pi-local override).
- **`GET /api/sign/settings` fails on boot (network error, Flask briefly unreachable).** → The loader logs the failure and falls through to running `current/.../main.py` without staging. Safe default — same failure mode as today's `boot-config`. Operator can wait for Flask to recover and trigger a force-upgrade from the Settings page.
- **`GET /api/sign/settings` returns an empty `target_version`.** → Should not happen — Flask always resolves to a concrete value before responding. If it does (a Flask bug), the loader treats `""` as a failure mode and falls through. Defensive against the "Flask couldn't resolve its own SHA at startup" edge case.
- **Force-upgrade arrives while a config envelope is mid-arrival.** → The handler `os.execvpe`s into a fresh loader, which re-fetches the resolved target on its next boot phase. The cached MQTT envelope is irrelevant — the new loader boot always re-reads from `GET /api/sign/settings`. Worst case: the operator's target-version save right before clicking force-upgrade causes the loader to stage the new pin instead of the previous target. Match to operator intent.
- **Browser loses the status-WS connection while operator is editing target SHA.** → The Save button sends a POST to Flask; Flask persists the change and publishes the `type=config` envelope (via the existing publish path). The Pi's `_handle_config` updates its in-memory config. The Settings page can't help the operator see the new state in the read-only fields until the WS reconnects, but the persistence + publish path is fully server-driven.
- **Settings stored in SQLite are lost if SQLite is wiped** (e.g. S3 rebuild-from-S3 on startup per `sqlite.py`). → SQLite rebuilds from S3; `SignConfig` rebuilds from S3. `sign.target_version` reverts to the Flask running short SHA via the default-construction path — approximately safe — but clears any pin the operator set, without notice.
- **Operator clears Target while `AUTO_UPDATE=false`.** → No upgrade happens. The flag overrides the pin. Match to operator intent.
- **`sudo reboot` in the restart handler fails silently** (sandbox misconfig, missing NOPASSWD). → The handler logs the `subprocess.run` result; if non-zero, the existing main.py keeps running and the operator sees the Pi never went offline via the status pill. UI surfaces a transient "Restart command sent — check Pi" hint; operator verifies via the existing dashboard pill.
- **Pi boots with a fresh SD card and `GET /api/sign/settings` fails.** → Loader falls through to running the current version without staging. The next MQTT envelope arrival updates the Pi's in-memory config. The operator can trigger a force-upgrade manually once Flask is reachable.
- **Pre-change Flask publishes a `type=config` envelope that lacks `sign.target_version`.** → The Pi's `_handle_config` reads `sign.target_version` and gets the dataclass default (Flask running short SHA, resolved at construction time). No exception; existing config payloads work unchanged.
- **The transitional period runs longer than expected** (the follow-up change to delete `check-for-update` and `/api/sign/boot-config` doesn't ship for weeks). → Two paths coexist: legacy Pis upgrade via `boot-config` + `check-for-update`; new Pis upgrade via `sign/settings`. Both flows are independent and tested. The follow-up change is a clean deletion when ready.

## Migration Plan

1. **Code lands on `main`:** All Python + JS + template changes merged. `SignConfig.CURRENT_VERSION` stays at 2. `sign.target_version` is added to `SignSettings` and defaults to Flask's running short SHA at construction time. `timezone` stays at the top level of `SignConfig` (no change). `AUTO_UPDATE` key in `heart-matrix-controller/settings.toml` is unchanged (already present in the example). New `GET /api/sign/settings` route added; existing `/api/sign/boot-config` route kept. New `force-upgrade`/`restart`/`shutdown` handlers registered alongside the existing `check-for-update` registration.
2. **Flask deploy:** `git push heroku main`. Flask restarts → on startup, the `SignConfig` is loaded from S3/SQLite; the migration registry has no entry for v2 → v2 (no migration runs). Flask publishes the latest `type=config` envelope once (or skips if the persisted hash matches the sidecar hash). Flask's one-shot `check-for-update` envelope continues to be published (the transitional period). The Flask-side status publisher continues on the same 5s cadence.
3. **Existing Pi (pre-this-change) receives a `check-for-update` envelope** at startup → handles it as before → upgrades to the new code. (The existing Pi only understands `check-for-update` until it runs the new code; once running the new code, it understands both `check-for-update` and the three new commands.)
4. **New Pi (post-this-change) calls `GET /api/sign/settings` on boot** → receives the resolved target → stages from there. The existing `/api/sign/boot-config` endpoint remains in case the operator needs to roll a Pi back to a version that uses it (legacy fallback).
5. **Operator visits the Settings page** → sees the new "Pi Upgrade Control" sub-section. All three version fields populate within the first status snapshot (≤5s after the operator opens the page).
6. **Operator pins a Target SHA.** Types the SHA into the Target Pi version input → Save → SQLite row updated with `sign.target_version = "<sha>"` → `type=config` envelope published → Pi's `_handle_config` updates in-memory `SignConfig`.
7. **Operator issues Force Upgrade / Restart / Shutdown.** Click → `confirm()` dialog → POST → 202 → handler executes.
8. **Rollback (if anything goes wrong):** `heroku rollback v<previous>` brings Flask back to the pre-change version. The pre-change code does not understand `sign.target_version` (it just ignores the unknown key per `SignConfig.from_dict`). The Pi's loader continues to work with the new code's `sign.target_version` field — the `check-for-update` envelope still triggers the upgrade via the legacy path. The Pi-side `AUTO_UPDATE` flag in settings.toml is independent of the Flask version and survives rollback.
9. **Follow-up change:** Once all deployed Pis are confirmed to be on the new code, the follow-up change deletes `/api/sign/boot-config`, removes `check-for-update` registration from `main.py`, deletes `heart-matrix-controller/check_for_update.py`, and stops Flask from publishing the one-shot `check-for-update` envelope.

## Open Questions

- **Pi-side `settings.json` override file for `AUTO_UPDATE`.** A future change will add a JSON file on the Pi (mirroring `effects_settings.json`) that the loader reads on startup and overlays over the `settings.toml` value. Lets the operator flip the flag without redeploying settings.toml — the recovery path when SSH is flaky but file-edits are possible. Out of scope here; flagged for follow-up.
- **Pi-side auto-detect logic for `AUTO_UPDATE`.** A future change might add Pi-side logic to set `AUTO_UPDATE=false` automatically (e.g. "if render pattern X throws for >5 minutes, set auto-update off and notify Flask"). The wire shape already supports this — the Pi just sets the flag locally. Out of scope here; flagged for follow-up.
- **Pi-side cache of the resolved target.** A future change may mirror the resolved target to disk via `local_state.write_cached_target_version(target)` so the loader's boot-time HTTP call can be eliminated. Out of scope here — user explicitly deferred caching.
- **Confirm-modals for destructive commands.** Restart and Shutdown get `confirm()` dialogs in v1; we may want a typed-confirmation pattern (force-upgrade is non-destructive; restart/shutdown are). Tracked in tasks.