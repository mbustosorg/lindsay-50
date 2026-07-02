## 1. MQTT command envelope

- [x] 1.1 Verify `MessageEnvelope` already round-trips `type="command"` without code changes (existing constructor accepts arbitrary strings)
- [x] 1.2 Add `type=command` branch to `MessageManager.dispatch` in `lib_shared/message_manager.py` that routes to a command handler
- [x] 1.3 Implement `handle_command(payload)` in `lib_shared/message_manager.py` — dispatch on `payload["action"]`; `action == "reboot"` runs `os.system("sudo reboot")`; unknown action logs warning and drops
- [x] 1.4 Add defensive logging/guards for missing `payload` or missing `action` key
- [x] 1.5 Test: `MessageEnvelope("command", {"action": "reboot"}).to_json()` round-trips via `from_json`
- [x] 1.6 Test: dispatcher routes `type=command` envelope to reboot handler with correct payload
- [x] 1.7 Test: dispatcher logs and drops unknown command actions (e.g., `action="dance"`)
- [x] 1.8 Test: dispatcher continues to route `type=message` and `type=config` unchanged (regression check)

## 2. Flask expected-sha endpoint + auto-reboot publish

- [x] 2.1 Add `GET /api/sign/expected-sha` to `heart-message-manager/main.py` returning `{"expected_sha": <sha>}`, guarded by existing `api_login_required`
- [x] 2.2 Implement local-dev fallback: if `HEROKU_SLUG_COMMIT` not set, return `subprocess.check_output(["git", "rev-parse", "HEAD"])` from the repo root
- [x] 2.3 In Flask startup, after the paho client finishes its initial connection, publish a one-shot `MessageEnvelope("command", {"action": "reboot"})` on `cfg.MQTT_TOPIC` via the existing `publish_envelope()` path
- [x] 2.4 Test: `/api/sign/expected-sha` returns `HEROKU_SLUG_COMMIT` when env var is set (fixture)
- [x] 2.5 Test: `/api/sign/expected-sha` returns local `git rev-parse HEAD` when env var is unset (fixture)
- [x] 2.6 Test: `/api/sign/expected-sha` returns 401 with missing or invalid `X-API-Key`
- [x] 2.7 Test: Flask startup publishes exactly one `command=reboot` envelope after MQTT connects (mock the paho client, assert publish_envelope called with correct args)

## 3. App-owned health check

- [x] 3.1 Create `heart-matrix-controller/healthcheck.py` with `run_healthcheck() -> bool` function
- [x] 3.2 Implement initial checks in `run_healthcheck()`: all modules import; `Display()` constructor succeeds; paho MQTT client connects to broker; `MessageManager.seed()` completes
- [x] 3.3 Each check logs pass/fail with reason; function returns `True` only if all pass, `False` otherwise (and exits non-zero)
- [x] 3.4 Add `--healthcheck` argparse flag to `heart-matrix-controller/main.py` that calls `run_healthcheck()` and exits with the appropriate code
- [x] 3.5 Test: `run_healthcheck()` returns `True` when all dependencies are reachable (mock Display, MQTT, REST)
- [x] 3.6 Test: `run_healthcheck()` returns `False` when MQTT broker is unreachable (mock connection failure)
- [x] 3.7 Test: `main.py --healthcheck` exits 0 on success and non-zero on failure

## 4. Loader process

- [x] 4.1 Create `heart-matrix-controller/loader.py` skeleton: resolve `REPO_DIR`, read `settings.toml`, import `make_mqtt_client`, set up logging
- [x] 4.2 Implement `fetch_expected_sha(repo_dir)` — GETs `/api/sign/expected-sha` with `X-API-Key`; returns `None` on any error
- [x] 4.3 Implement `current_sha(repo_dir)` — `git -C $REPO_DIR rev-parse HEAD` resolved through the `current/` symlink; returns the active SHA
- [x] 4.4 Implement `stage_version(repo_dir, expected_sha)` — `git worktree add $REPO_DIR/v-<sha> <sha>`; on dirty tree, `reset --hard` first; on network error, raises a typed exception the caller can catch
- [x] 4.5 Implement `run_health_check(repo_dir, expected_sha)` — invokes `v-<sha>/heart-matrix-controller/main.py --healthcheck` as subprocess; returns exit code (0 = pass)
- [x] 4.6 Implement `atomic_swap(repo_dir, expected_sha)` — `ln -sfn v-<sha> current`; logs the swap
- [x] 4.7 Implement `exec_active(repo_dir)` — `os.execvp("python3", ["python3", f"{repo_dir}/current/heart-matrix-controller/main.py"])`
- [x] 4.8 Implement `watch_subprocess(proc, repo_dir, previous_sha, grace_seconds=30)` — if `proc` exits non-zero within grace, swap `current` back to `v-<previous_sha>` and re-exec
- [x] 4.9 Wire the full flow: query Flask → compare SHAs → (if mismatch) stage → health-check → (if pass) swap → exec → watch; on any failure, fall through to exec the existing `current/`
- [x] 4.10 Add `os.execvp` (not `subprocess.run`) for the active version so systemd sees `main.py` as the direct child process (preserves signal handling)
- [x] 4.11 Test: `atomic_swap` updates `current` symlink target and old target is preserved on disk
- [x] 4.12 Test: full upgrade flow against a fixture bare repo with two commits — assert staging creates `v-<sha2>/`, swap retargets `current`, exec runs the new SHA
- [x] 4.13 Test: Flask-unreachable path — loader logs error and execs existing `current/.../main.py` without staging

## 5. Systemd + Pi bootstrap

- [x] 5.1 Update `scripts/lindsay_50.service`: `ExecStart` → `$REPO_DIR/current/heart-matrix-controller/loader.py`; `WorkingDirectory` → `$REPO_DIR`; add `StartLimitIntervalSec=120` and `StartLimitBurst=3`
- [x] 5.2 Update `scripts/startup_matrix_server.sh` if needed (or replace with direct loader invocation); preserve existing env setup (`LOG_LEVEL`, `PYTHONPATH=$REPO_DIR`)
- [x] 5.3 Add `scripts/setup-pi.sh` that documents/runs the one-time bootstrap: convert existing clone to bare repo; create first worktree from current HEAD; create `current` symlink; ensure `settings.toml`, `fonts/`, `.venv/` are at repo root
- [x] 5.4 Validate systemd unit syntax: `sudo systemd-analyze verify scripts/lindsay_50.service` (if available)
- [x] 5.5 Document operator-facing bootstrap steps in `README.md` or `scripts/README.md` (one-time SSH procedure, expected downtime ~1 min)

## 6. Integration & verification

- [x] 6.1 Run full pytest suite: `PYTHONPATH=. pytest tests/ -v` — all existing tests must pass
- [x] 6.2 Local-dev end-to-end: spin up Flask locally, simulate a different `HEROKU_SLUG_COMMIT`, run `loader.py` against a fixture repo, verify staging + swap + exec
- [x] 6.3 Document manual test scenarios in a `Plans/` follow-up note or `tests/README.md`: deploy (match → skip), deploy (mismatch → swap), broken code (health check fails → no swap), mid-render crash (grace period rollback), MQTT reboot command, Flask unreachable on Pi boot
- [x] 6.4 Add a `CHANGELOG.md` entry summarizing the new upgrade mechanism, the operator's new `heroku rollback v123` workflow, and the one-time Pi bootstrap procedure