## 1. MQTT command envelope (renamed: reboot → check-for-update)

- [x] 1.1 Verify `MessageEnvelope` already round-trips `type="command"` without code changes (existing constructor accepts arbitrary strings)
- [x] 1.2 Add `type=command` branch to `MessageManager.dispatch` in `lib_shared/message_manager.py` that routes to a registered command handler
- [x] 1.3 `MessageManager.__init__` accepts a `command_handlers` dict mapping action name → callable; expose `register_command_handler` for callers to add handlers late
- [x] 1.4 v2 default handler: `check-for-update` invokes `check_for_update.check_for_update(...)` which compares local SHA to Flask's expected SHA and `os.execvpe`s into the loader on mismatch (NOT a reboot)
- [x] 1.5 Remove the v1 `os.system("sudo reboot")` handler (or leave the registered slot empty — no action means the rebrand is observable at startup only)
- [x] 1.6 Add defensive logging/guards for missing `payload`, missing `action`, non-string action, or handler-raised exceptions (handler exceptions are logged and swallowed — never crash the dispatcher)
- [x] 1.7 Test: `MessageEnvelope("command", {"action": "check-for-update"}).to_json()` round-trips via `from_json`
- [x] 1.8 Test: dispatcher routes `type=command` envelope to the registered `check-for-update` handler with the correct payload
- [x] 1.9 Test: dispatcher logs and drops unknown command actions (e.g., `action="dance"`)
- [x] 1.10 Test: dispatcher continues to route `type=message` and `type=config` unchanged (regression check)
- [x] 1.11 Test: command handler that raises does NOT crash the dispatcher
- [x] 1.12 Test: `MessageManager.command_handlers` mapping returns a copy, not the internal dict

## 2. Flask boot-config endpoint + one-shot MQTT hint at startup

- [x] 2.1 Create `lib_shared/boot_config.py` — `BootConfig` dataclass with `expected_sha: str` field; `from_response(payload)`; `fetch_boot_config(api_url, api_key, *, timeout_s=5.0) -> Optional[BootConfig]`; `current_sha(repo_dir=None)`; `from_heroku_or_git()` helper
- [x] 2.2 Test: `BootConfig.from_response({"expected_sha": "abc"})`; `fetch_boot_config` succeeds/401/500/network/timeout/malformed/missing-key/empty/unparseable-url/custom-timeout
- [x] 2.3 Rename `GET /api/sign/expected-sha` → `GET /api/sign/boot-config` in `heart-message-manager/main.py`
- [x] 2.4 Response shape: `{"expected_sha": "<sha>"}` ONLY (drop `boot_id` and `force_reboot` from the v1 response)
- [x] 2.5 Endpoint uses `lib_shared.boot_config.from_heroku_or_git()` so the local-dev fallback is shared
- [x] 2.6 Flask publishes `MessageEnvelope("command", {"action": "check-for-update"})` ONCE at startup, right after the paho client constructs (NOT on every MQTT reconnect — drop the `on_connect_callback` parameter from v1)
- [x] 2.7 Drop the v1 `on_connect_callback` kwarg from `PahoMqttClient.__init__`; verify absence is part of `test_paho_mqtt_client.py`
- [x] 2.8 Test: `/api/sign/boot-config` returns `HEROKU_SLUG_COMMIT` when env var is set
- [x] 2.9 Test: `/api/sign/boot-config` returns local `git rev-parse HEAD` when env var is unset
- [x] 2.10 Test: `/api/sign/boot-config` returns 401 with missing or invalid `X-API-Key`
- [x] 2.11 Test: `/api/sign/expected-sha` returns 404 (the v1 endpoint is gone)
- [x] 2.12 Test: Flask startup publishes exactly ONE `command=check-for-update` envelope (verify `publish_envelope` call count)

## 3. App-owned status.json (replaces v1 healthcheck.py + --healthcheck)

- [x] 3.1 Create `heart-matrix-controller/status.py` with `StatusSnapshot` dataclass and `StatusWriter` (throttled, atomic, self-throttle default 3s)
- [x] 3.2 `StatusSnapshot` fields: `schema_version=1`, `pid`, `active_sha`, `boot_id`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error: Optional[str]`
- [x] 3.3 Atomic write via `os.replace` over a `.tmp` sibling; `tick()` is called from the render loop and is a no-op until `tick_interval_s` has elapsed
- [x] 3.4 `read_status(path, *, stale_after_s=10.0)` returns the dict on success; None on missing file / corrupt JSON / schema mismatch / missing keys / stale mtime
- [x] 3.5 Render loop in `heart-matrix-controller/main.py` constructs a `StatusWriter` keyed on its render-loop `tick`; snapshot builder returns live values
- [x] 3.6 Test: `StatusWriter.tick()` throttles to `tick_interval_s`; `tick()` writes one snapshot then suppresses subsequent calls inside the interval
- [x] 3.7 Test: writer swallows `snapshot_builder` exceptions and write failures (logs warning, does NOT raise)
- [x] 3.8 Test: writer writes to a `.tmp` sibling and uses `os.replace` so readers see old-or-new, never a half-written file
- [x] 3.9 Test: `read_status` returns parsed dict on healthy file; returns None on missing / corrupt / schema mismatch / missing required keys / stale mtime
- [x] 3.10 Test: staleness is wall-clock (`time.time()` vs `path.stat().st_mtime`), NOT monotonic — avoid mixing clocks with the snapshot's `updated_at`

## 4. Loader process (env-var driven, status.json probe — no subprocess watch)

- [x] 4.1 Create `heart-matrix-controller/loader.py` skeleton: resolve `REPO_DIR`, read `settings.toml`, import `make_mqtt_client`, set up logging
- [x] 4.2 Loader reads three optional env vars: `LINDSAY50_REPO_DIR` (override), `LINDSAY50_ACTIVE_SHA` (running version, set by `check_for_update.check_for_update`), `LINDSAY50_BOOT_ID` (instance identifier, falls back to a generated UUID)
- [x] 4.3 Implement `fetch_expected_sha(repo_dir)` using `lib_shared.boot_config.fetch_boot_config` (shared HTTP + auth code with Flask)
- [x] 4.4 Implement `current_sha(repo_dir)` — `git -C $REPO_DIR rev-parse HEAD` resolved through the `current/` symlink
- [x] 4.5 Implement `stage_version(repo_dir, expected_sha)` — `git worktree add $REPO_DIR/v-<sha> <sha>`; on dirty tree, `reset --hard` first; on network error, raises a typed exception the caller catches
- [x] 4.6 Implement `_is_status_healthy(staged_path, timeout_s)` — spawns `v-<sha>/heart-matrix-controller/main.py` as a subprocess; reads `.status.json` once it reports `mqtt_connected=true` and no `last_error`; kills the subprocess; returns True/False
- [x] 4.7 Implement `atomic_swap(repo_dir, expected_sha)` — `ln -sfn v-<expected_sha> current`; logs the swap
- [x] 4.8 Implement `_build_exec_env(repo_dir, active_sha, boot_id)` — returns a dict inheriting `os.environ` and adding the three LINDSAY50_* vars so the next `main.py` instance knows its identity
- [x] 4.9 Implement `exec_active(repo_dir, exec_fn=os.execvpe)` — `exec_fn(sys.executable, [...loader_dir.../main.py], env=...)`; tests inject a no-op `exec_fn`
- [x] 4.10 Drop the v1 `run_health_check` (CLI subprocess `--healthcheck` flag) — the v2 flow uses `.status.json` instead
- [x] 4.11 Drop the v1 `watch_subprocess(proc, repo_dir, previous_sha, grace_seconds=30)` — the `--healthcheck` + grace combination is replaced by the status.json probe, which is much faster and runs against the staged dir without `os.execvpe`
- [x] 4.12 Wire `run_upgrade_flow(repo_dir, current_sha_hint=None)`: query Flask via `lib_shared.boot_config` → compare SHAs → (if mismatch) stage → status probe → (if healthy) swap → exec with env vars; on any failure, fall through to exec the existing `current/.../main.py`
- [x] 4.13 Use `os.execvpe` (not `subprocess.run`) for the active version so systemd sees `main.py` as the direct child (preserves signal handling)
- [x] 4.14 Test: `atomic_swap` updates `current` symlink target and old target is preserved on disk
- [x] 4.15 Test: full happy-path upgrade flow against a fixture bare repo with two commits — assert staging creates `v-<sha2>/`, swap retargets `current`, exec runs `main.py` from the new SHA with env vars set
- [x] 4.16 Test: Flask-unreachable path — loader logs error and execs existing `current/.../main.py` without staging
- [x] 4.17 Test: status-probe unhealthy path — loader logs error and does NOT swap (leaves `current` pointing at the previous SHA)
- [x] 4.18 Test: SHA-match path — loader skips staging entirely and execs `current/.../main.py`
- [x] 4.19 Test: env vars passed to the spawned `main.py` carry `LINDSAY50_ACTIVE_SHA`, `LINDSAY50_REPO_DIR`, `LINDSAY50_BOOT_ID`; other vars are inherited

## 5. App-side `check-for-update` handler

- [x] 5.1 Create `heart-matrix-controller/check_for_update.py` — register `LINDSAY50_REPO_DIR`, `LINDSAY50_ACTIVE_SHA`, `LINDSAY50_BOOT_ID` constants; expose `check_for_update(api_url, api_key, repo_dir=None)`
- [x] 5.2 `_resolve_active_sha()` reads `LINDSAY50_ACTIVE_SHA` from env; returns None if unset/empty/whitespace-only
- [x] 5.3 `_resolve_repo_dir(repo_dir)` returns the kwarg if set, else `LINDSAY50_REPO_DIR` env var, else `Path("/home/pi/projects/lindsay-50")` fallback
- [x] 5.4 `check_for_update` flow: if active_sha missing → no-op; fetch expected_sha via `lib_shared.boot_config.fetch_boot_config`; if fetch fails → no-op; if expected == active → no-op; else call `_exec_into_loader`
- [x] 5.5 `_exec_into_loader(repo_dir, active_sha)` builds env dict inheriting `os.environ` + `LINDSAY50_ACTIVE_SHA=<active_sha>` (NOT `LINDSAY50_REPO_DIR` — that's a startup-only value, and the loader path is computed from `repo_dir/`), then `os.execvpe(python, [python, repo_dir/current/.../loader.py], env=...)`
- [x] 5.6 Register the handler with `MessageManager` in `heart-matrix-controller/main.py`'s startup; pass through `api_url` and `api_key` from `settings.toml`
- [x] 5.7 Test: `_resolve_active_sha` returns None for missing/empty/whitespace
- [x] 5.8 Test: `_resolve_repo_dir` honors the kwarg, then the env var, then the fallback path
- [x] 5.9 Test: `check_for_update` is a no-op when fetch fails (Flask unreachable)
- [x] 5.10 Test: `check_for_update` is a no-op when SHAs match
- [x] 5.11 Test: `check_for_update` calls `os.execvpe` with the new SHA in `LINDSAY50_ACTIVE_SHA` env var on mismatch
- [x] 5.12 Test: `check_for_update` honors the explicit `repo_dir=` kwarg in the loader path computation

## 6. Systemd + Pi bootstrap

- [x] 6.1 `scripts/lindsay_50.service` `ExecStart` points at the loader (the loader is what runs; `main.py` is invoked by the loader via `os.execvpe`)
- [x] 6.2 `scripts/lindsay_50.service` adds `StartLimitIntervalSec=120` and `StartLimitBurst=3` to throttle crash loops
- [x] 6.3 `scripts/startup_matrix_server.sh` keeps the env setup (`LOG_LEVEL`, `PYTHONPATH=$REPO_DIR`); cds into the loader (not `main.py`)
- [x] 6.4 `scripts/setup-pi.sh` one-time bootstrap: convert existing clone to bare repo; create first worktree from current HEAD; create `current` symlink; ensure `.venv/`, `settings.toml`, `fonts/` live at the repo root (not in worktrees)
- [x] 6.5 Validate systemd unit syntax (best-effort; not blocking)
- [x] 6.6 Document operator-facing bootstrap steps in `README.md` (one-time SSH procedure, expected downtime ~30s)

## 7. Integration & verification

- [x] 7.1 Run full pytest suite: `PYTHONPATH=heart-matrix-controller:. pytest tests/ -v` — all existing tests must pass
- [x] 7.2 Local-dev end-to-end: spin up Flask locally, simulate a different `HEROKU_SLUG_COMMIT`, run `check_for_update` against a fixture repo, verify staging + swap + exec with env vars
- [x] 7.3 Document manual test scenarios: deploy (match → skip), deploy (mismatch → swap), broken code (status.json probe fails → no swap), Flask unreachable on Pi boot, `command=check-for-update` MQTT envelope triggers loader
- [x] 7.4 Add a `CHANGELOG.md` entry summarizing the new upgrade mechanism, the operator's new `heroku rollback v123` workflow, and the one-time Pi bootstrap procedure
