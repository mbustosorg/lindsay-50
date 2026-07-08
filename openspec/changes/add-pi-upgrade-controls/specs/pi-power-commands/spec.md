## ADDED Requirements

### Requirement: Force-upgrade command from Flask

Flask MUST expose `POST /api/sign/commands/force-upgrade` which publishes exactly one envelope on `MQTT_TOPIC` with shape:

```json
{
  "type": "command",
  "payload": { "action": "force-upgrade" }
}
```

The endpoint MUST require `X-API-Key` auth (matching the existing admin API contract). The endpoint MUST return `202 Accepted` on successful publish and `503 Service Unavailable` if the publish fails. The Flask endpoint MUST NOT block waiting for Pi acknowledgment.

#### Scenario: Operator clicks Force upgrade and Pi is reachable
- **WHEN** operator clicks the Force upgrade button on the Settings page
- **THEN** browser confirms via a `confirm()` dialog, Flask publishes exactly one `force-upgrade` envelope, returns 202, the Pi's existing `MessageManager.dispatch` routes the action to a handler that calls `os.execvpe` into `<repo_dir>/loader.py`, and the Pi either completes the upgrade or falls through to running the current version (existing self-healing path)

#### Scenario: Operator clicks Force upgrade twice in succession
- **WHEN** operator clicks Force upgrade, the Pi is mid-upgrade, and operator clicks again
- **THEN** Flask publishes both envelopes; the second `force-upgrade` handler `os.execvpe`s into a loader that is idempotent — a no-op if already at target — so the worst case is a redundant swap-attempt, not a brick

### Requirement: Restart command from Flask

Flask MUST expose `POST /api/sign/commands/restart` which publishes exactly one envelope:

```json
{
  "type": "command",
  "payload": { "action": "restart" }
}
```

The endpoint requires the same `X-API-Key` auth, returns `202 Accepted` on success, and MUST NOT wait for Pi acknowledgment.

#### Scenario: Operator confirms Restart and Pi reboots
- **WHEN** operator clicks Restart, browser confirms via `confirm()`, Flask publishes the envelope, the Pi's registered `restart` handler calls `subprocess.run(["sudo", "reboot"], check=False)`
- **THEN** the Pi halts its display loop, systemd restarts the loader, the loader reads the latest persisted `PiUpgradeSettings` on boot and decides whether to upgrade; the dashboard Live pill transitions through `live → unsure → offline → live` as the Pi goes down and comes back

#### Scenario: sudo permission is misconfigured
- **WHEN** `sudo reboot` fails (non-zero exit) because the Pi lacks `NOPASSWD` for `reboot`
- **THEN** the Pi's main.py keeps running, the handler logs the non-zero exit, the Settings page shows a transient "Restart command sent — check Pi" status line, and the operator sees no pill change on the dashboard

### Requirement: Shutdown command from Flask

Flask MUST expose `POST /api/sign/commands/shutdown` which publishes exactly one envelope:

```json
{
  "type": "command",
  "payload": { "action": "shutdown" }
}
```

Same auth/response semantics as Restart.

#### Scenario: Operator confirms Shutdown and Pi halts
- **WHEN** operator clicks Shutdown, browser confirms via `confirm()` with text including the word "shutdown", Flask publishes the envelope, the Pi's registered `shutdown` handler calls `subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)`
- **THEN** the Pi halts, the dashboard Live pill transitions to `offline` within 120s (no further status publishes); the operator must manually power the Pi back on

### Requirement: Pi registers force-upgrade, restart, shutdown handlers at boot

`heart-matrix-controller/main.py` MUST register three new handlers via the existing `MessageManager.register_handler(action, fn)` mechanism:

- `force-upgrade` → `command_handlers.force_upgrade` (in `heart-matrix-controller/command_handlers.py`).
- `restart` → `command_handlers.restart` (in the same module).
- `shutdown` → `command_handlers.shutdown` (in the same module).

`main.py` MUST register these handlers after the existing `check-for-update` registration. The handlers MUST NOT take responsibility for routing or auth — `MessageManager.dispatch` handles that. The handlers MUST log their invocation (INFO level).

#### Scenario: Pi boots and receives all four action types via MQTT
- **WHEN** Flask publishes `action=force-upgrade`, then `action=restart`, then `action=shutdown`, on the same `MQTT_TOPIC`
- **THEN** each envelope is dispatched to its registered handler in order; the handler logs and executes its side effect; the existing `check-for-update` handler is unaffected by the new registrations

### Requirement: Force-upgrade handler falls through to existing current version on failure

The `force_upgrade` handler MUST preserve the existing loader fallthrough invariant: any failure during the upgrade path falls through to executing `current/.../main.py` so the Pi cannot brick itself.

The handler MUST:
- Resolve `LINDSAY50_REPO_DIR` from env (default `/home/pi/projects/lindsay-50`).
- Build a `loader_argv` of `[sys.executable, "<repo_dir>/heart-matrix-controller/loader.py"]` plus any existing args.
- Set env `LINDSAY50_ACTIVE_SHA` to current active SHA if known.
- Call `os.execvpe(sys.executable, loader_argv, env)`.
- If the exec raises (e.g. the loader script is missing), log and continue — `main.py` is unaffected.

#### Scenario: Loader script is missing or corrupted
- **WHEN** the Pi receives `action=force-upgrade` and `<repo_dir>/heart-matrix-controller/loader.py` cannot be located
- **THEN** the handler logs an error and returns; `main.py` continues running its render loop unchanged

#### Scenario: Loader execs but the staged version fails to probe
- **WHEN** force-upgrade triggers the loader, which stages a new worktree, but the `.status.json` probe fails (mqtt_connected=False, last_error set, etc.)
- **THEN** the loader aborts the swap, execs the existing `current/.../main.py`, and the Pi continues rendering on the old version
