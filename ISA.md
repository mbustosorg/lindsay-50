---
task: v2 refactor of self-upgrading matrix controller (issue #49)
slug: v2-refactor-self-upgrading-matrix-controller
effort: E3
phase: execute
progress: 0/36
mode: algorithm
started: 2026-07-02
updated: 2026-07-02
project: lindsay-50
---

## Problem

v1 of the self-upgrading matrix controller (commit 81bf12b, PR #50) works
but has accumulated complexity that the operator flagged in review:

- `healthcheck.py` + `--healthcheck` is a separate code path from the
  render loop; checks drift from what the app actually does
- `PahoMqttClient.on_connect_callback` publishes a reboot envelope on
  every MQTT (re)connect — any network blip becomes a reboot hint
- `watch_subprocess` + 30s grace period requires the loader to keep
  running after exec, complicating the loader's lifecycle
- HTTP fetch + git rev-parse code is duplicated between Flask and the
  loader
- The Pi blindly trusts MQTT hints rather than verifying the running SHA

## Vision

A v2 design where:

- The app reports its own health (writes `status.json` throttled to
  ~3s, atomically via tmp+rename) — the loader never has to spawn a
  separate healthcheck process
- Flask publishes `command=check-for-update` exactly once at startup
  (not on MQTT connect), and reconnects never republish
- The app handles `check-for-update` envelopes via its existing MQTT
  subscription, queries `/api/sign/boot-config`, compares the expected
  SHA to `LINDSAY50_ACTIVE_SHA`, and `os.execvpe`s into the loader
  if they differ
- The loader validates a staged worktree by spawning it briefly,
  reading its `status.json`, and rejecting the swap if the staged
  version reports unhealthy
- Boot-config code (HTTP fetch + git rev-parse + dataclass + endpoint
  path) lives in `lib_shared/boot_config.py` — Flask, the loader, and
  the app's check-for-update handler all import from it
- The loader is gone after `os.execvpe` — no supervision, no grace
  period; systemd `StartLimitBurst=3` handles crash loops and operator
  does `heroku rollback v123` for the rare late-manifesting render bug

Operator experience: `git push heroku main` → the Pi picks up the new
version on its next MQTT `check-for-update` hint (or next boot, which
is fine since the loader checks on every boot too). To roll back, the
operator runs `heroku rollback v123`. The Pi is never touched.

## Out of Scope

- Auto-rollback on post-swap failure (operator does `heroku rollback v123`)
- A separate update-checker process or watchdog (the app handles
  check-for-update, the loader is one-shot)
- New third-party dependencies (stdlib only)
- Changes to SQLite/S3 storage or the `/api/messages` webhook handler
- Changes to admin UI templates
- Browser-side MessageManager surface (only the dispatcher signature
  changes; semantics are the same)
- The Pi's MQTT reconnect publishing a check-for-update hint (only
  Flask's startup publishes)
- Heroku-side changes (only the runtime app changes)

## Principles

- App-owned health: the running app is responsible for reporting its
  health; the loader reads, the app writes
- One MQTT hint per process lifetime: Flask publishes `check-for-update`
  once at startup, never on MQTT reconnect
- Share code, don't duplicate: HTTP fetch + git rev-parse + endpoint
  path live in `lib_shared/boot_config.py`
- Env var over git rev-parse at runtime: `LINDSAY50_ACTIVE_SHA` is set
  by the loader via `os.execvpe`, the app reads it — never re-parses git
- Explicit fall-through: every "fall through to existing current/"
  path logs why; silent fallbacks are bugs

## Constraints

- Loader uses `os.execvp`/`os.execvpe` (not `subprocess.run`) for the
  active version — systemd sees `main.py` as the direct child,
  preserving PID and signal handling
- `ln -sfn` is the atomic swap primitive (same-filesystem atomic)
- Loader fallback: Flask unreachable OR `status.json` probe fails →
  fall through to existing `current/.../main.py` (never brick the Pi)
- No new third-party dependencies (stdlib only)
- Conventional commit prefix (`refactor:`) on the existing
  `feat/issue-49` branch (PR #50 auto-updates)
- Do NOT push the branch or open a new PR

## Goal

Refactor the v1 self-upgrading matrix controller on `feat/issue-49`
into a v2 design that replaces `healthcheck.py`/`--healthcheck`/
`watch_subprocess`/`on_connect_callback` with
`status.json` (throttled, atomic) for the pre-swap probe,
`/api/sign/boot-config` for the version source-of-truth endpoint,
`LINDSAY50_ACTIVE_SHA` env var (set by the loader) for the running
version, `lib_shared/boot_config.py` for shared boot-config code, and
the app's existing MQTT subscription for `check-for-update` handling.
Update all four openspec docs to match. Commit on the same branch with
a `refactor:` prefix. All existing tests pass; new tests cover the v2
modules.

## Criteria

- [ ] ISC-1: `lib_shared/boot_config.py` exists and defines `BootConfig` dataclass with `expected_sha: str`
- [ ] ISC-2: `lib_shared/boot_config.py` defines `BOOT_CONFIG_PATH = "/api/sign/boot-config"` constant
- [ ] ISC-3: `lib_shared/boot_config.py` defines `fetch_boot_config(api_url, api_key, *, requests_module=None, timeout=5.0) -> Optional[BootConfig]` that returns None on any error
- [ ] ISC-4: `lib_shared/boot_config.py` defines `current_sha(repo_dir) -> Optional[str]` via `git -C current/ rev-parse HEAD`
- [ ] ISC-5: `lib_shared/boot_config.py` defines `from_heroku_or_git(repo_dir) -> BootConfig` that prefers `HEROKU_SLUG_COMMIT` and falls back to git rev-parse
- [ ] ISC-6: `heart-message-manager/main.py` endpoint renamed from `/api/sign/expected-sha` to `/api/sign/boot-config`
- [ ] ISC-7: `/api/sign/boot-config` returns `{"expected_sha": <sha>}` and nothing else
- [ ] ISC-8: `/api/sign/boot-config` uses `BootConfig.from_heroku_or_git` for SHA derivation
- [ ] ISC-9: Flask publishes `command=check-for-update` exactly once at startup (not on every MQTT connect)
- [ ] ISC-10: `PahoMqttClient` no longer accepts `on_connect_callback` parameter
- [ ] ISC-11: `MessageManager` constructor accepts an optional `command_handlers` mapping
- [ ] ISC-12: `MessageManager._handle_command` dispatches via registered handlers (not hardcoded reboot)
- [ ] ISC-13: `heart-matrix-controller/main.py` no longer short-circuits on `--healthcheck`
- [ ] ISC-14: `heart-matrix-controller/main.py` registers a `check_for_update` handler with `MessageManager`
- [ ] ISC-15: `heart-matrix-controller/check_for_update.py` defines the handler that queries `/api/sign/boot-config`, compares to `LINDSAY50_ACTIVE_SHA`, and `os.execvpe`s into `loader.py` on mismatch
- [ ] ISC-16: `heart-matrix-controller/status.py` defines `StatusWriter` with throttled atomic writes (tmp file, `os.replace`)
- [ ] ISC-17: `heart-matrix-controller/status.py` writes the `status.json` schema (schema_version, pid, active_sha, started_at, updated_at, uptime_seconds, mqtt_connected, last_tick_age_ms, messages_rendered, last_error)
- [ ] ISC-18: `heart-matrix-controller/main.py` calls `StatusWriter.tick()` once per render-loop iteration (throttled internally to ~3s)
- [ ] ISC-19: `heart-matrix-controller/loader.py` `run_health_check` replaced with a pre-swap `probe` that spawns staged `main.py`, waits ~8s, reads `$REPO_DIR/.status.json`, and rejects swap if unhealthy
- [ ] ISC-20: `heart-matrix-controller/loader.py` `watch_subprocess` and 30s grace period deleted
- [ ] ISC-21: `heart-matrix-controller/loader.py` `os.execvpe` sets `LINDSAY50_ACTIVE_SHA=<sha>` and `LINDSAY50_REPO_DIR=<path>` env vars before exec'ing `main.py`
- [ ] ISC-22: `heart-matrix-controller/healthcheck.py` deleted
- [ ] ISC-23: `heart-matrix-controller/loader.py` `fetch_expected_sha` replaced with `lib_shared.boot_config.fetch_boot_config`
- [ ] ISC-24: `tests/test_boot_config.py` covers `BootConfig.from_response`, `fetch_boot_config` (success/401/500/network), `current_sha` (success/no-symlink/git-error), `from_heroku_or_git` (env/git-fallback)
- [ ] ISC-25: `tests/test_status.py` covers atomic writes, throttling, and defensive `read_status` (missing/corrupt/stale/missing-keys)
- [ ] ISC-26: `tests/test_app_handles_check_for_update.py` covers handler dispatch (SHA matches → no-op; SHA differs → execvpe; fetch fails → no-op; missing env var → no-op)
- [ ] ISC-27: `tests/test_message_manager.py::TestDispatchCommand` updated to test handler-dispatch pattern (replaces reboot-specific tests)
- [ ] ISC-28: `tests/test_loader.py` updated to test status.json probe (replaces `--healthcheck` subprocess tests)
- [ ] ISC-29: `tests/test_healthcheck.py` deleted
- [ ] ISC-30: `tests/test_expected_sha_endpoint.py` replaced by `tests/test_boot_config_endpoint.py` (renamed endpoint, no `on_connect_callback`)
- [ ] ISC-31: `tests/test_systemd_unit.py` unchanged (orthogonal to v2 design)
- [ ] ISC-32: `openspec/changes/self-upgrading-matrix-controller/tasks.md` rewritten for v2 (drop healthcheck tasks, add status.json tasks, drop watch_subprocess tasks, add lib_shared/boot_config tasks, add LINDSAY50_ACTIVE_SHA env var tasks)
- [ ] ISC-33: `openspec/changes/self-upgrading-matrix-controller/proposal.md` updated to reflect v2 simplification
- [ ] ISC-34: `openspec/changes/self-upgrading-matrix-controller/design.md` updated with status.json pre-swap probe, no watch_subprocess, env var version passing
- [ ] ISC-35: `openspec/changes/self-upgrading-matrix-controller/specs/mqtt-command-envelope/spec.md` updated: action=check-for-update (not reboot), one-shot at Flask startup
- [ ] ISC-36: `openspec/changes/self-upgrading-matrix-controller/specs/pi-upgrade-mechanism/spec.md` updated: status.json probe, no watchdog, env var passing
- [ ] ISC-37: `openspec/changes/self-upgrading-matrix-controller/specs/version-coordination/spec.md` updated: endpoint returns just expected_sha, LINDSAY50_ACTIVE_SHA env var carries running version
- [ ] ISC-38: `CHANGELOG.md` updated for v2 entry
- [ ] ISC-39: `README.md` self-upgrading-Pi section updated for v2 flow
- [ ] ISC-40: `PYTHONPATH=. pytest tests/ -v` shows all tests passing
- [ ] ISC-41: `refactor:` commit on `feat/issue-49` branch (no push, no new PR)

## Test Strategy

| ISC | type | check | threshold | tool |
|---|---|---|---|---|
| 1-5 | unit | `from lib_shared.boot_config import BootConfig, fetch_boot_config, current_sha, from_heroku_or_git, BOOT_CONFIG_PATH` works | import succeeds | bash `python -c` |
| 6-8 | unit | `GET /api/sign/boot-config` returns the renamed shape | integration test | pytest |
| 9 | unit | Flask startup publishes exactly one check-for-update envelope | integration test | pytest |
| 10 | typecheck | `PahoMqttClient.__init__` no longer accepts `on_connect_callback` | keyword error | python |
| 11-12 | unit | `MessageManager` with command_handlers dispatch | unit tests | pytest |
| 13-14 | inspection | `grep -- "--healthcheck"` returns 0 hits in `main.py` | grep | bash |
| 15 | unit | `check_for_update` handler | unit tests | pytest |
| 16-18 | unit | `StatusWriter` | unit tests | pytest |
| 19-21 | unit | loader probe + execvpe | unit tests | pytest |
| 22 | inspection | `healthcheck.py` does not exist | filesystem | bash |
| 23 | unit | loader uses shared fetch_boot_config | unit tests | pytest |
| 24-30 | unit | test files added/updated | test discovery | pytest --collect-only |
| 31 | unit | test_systemd_unit.py unchanged | git diff | bash |
| 32-37 | inspection | openspec files reflect v2 | grep | bash |
| 38-39 | inspection | CHANGELOG.md and README.md updated | grep | bash |
| 40 | test | pytest suite | all tests pass | pytest |
| 41 | git | commit hash on feat/issue-49 | git log | bash |

## Features

- **F1: Shared boot-config module** (satisfies ISC-1..5) | Create `lib_shared/boot_config.py` | parallelizable: false (foundation)
- **F2: MessageManager command handler dispatch** (satisfies ISC-11,12) | Refactor `_handle_command` to dispatch via registered handlers | parallelizable: false (foundation for F5)
- **F3: Flask boot-config endpoint + one-shot publish** (satisfies ISC-6..9) | Rename endpoint, simplify response, publish at startup | parallelizable: false (depends on F1)
- **F4: Remove PahoMqttClient.on_connect_callback** (satisfies ISC-10) | Drop the parameter from the constructor and on_connect wiring | parallelizable: false (depends on F3)
- **F5: App-side check_for_update handler** (satisfies ISC-15) | Create `heart-matrix-controller/check_for_update.py` | parallelizable: true (depends on F1, F2)
- **F6: Wire handler into matrix controller main** (satisfies ISC-13,14) | Remove --healthcheck, register handler | parallelizable: false (depends on F5)
- **F7: Status writer + tick wiring** (satisfies ISC-16..18) | Create `heart-matrix-controller/status.py`, wire into render loop | parallelizable: true (depends on F6)
- **F8: Loader refactor — status.json probe + env vars + execvpe** (satisfies ISC-19..21,23) | Replace healthcheck subprocess with status.json probe; drop watchdog; use os.execvpe with env vars | parallelizable: false (depends on F1, F7)
- **F9: Delete healthcheck.py** (satisfies ISC-22) | Remove the file | parallelizable: false (depends on F8)
- **F10: Tests — new modules** (satisfies ISC-24..26) | test_boot_config.py, test_status.py, test_app_handles_check_for_update.py | parallelizable: true
- **F11: Tests — updated modules** (satisfies ISC-27,28,30) | Update test_message_manager.py::TestDispatchCommand, test_loader.py, test_expected_sha_endpoint.py → test_boot_config_endpoint.py | parallelizable: true
- **F12: Tests — deleted** (satisfies ISC-29) | Delete test_healthcheck.py | parallelizable: true
- **F13: OpenSpec docs** (satisfies ISC-32..37) | Rewrite tasks.md, update proposal.md, design.md, and the three specs | parallelizable: false (depends on F1..F12 — needs the actual code shape to write the docs accurately)
- **F14: README + CHANGELOG** (satisfies ISC-38,39) | Update README.md self-upgrading-Pi section; update CHANGELOG.md v2 entry | parallelizable: true (depends on F13)
- **F15: Test gate + commit** (satisfies ISC-40,41) | Run pytest, fix any issues, commit with `refactor:` prefix | parallelizable: false (final)

## Decisions

- **2026-07-02 — Drop healthcheck.py + --healthcheck entirely** | The user asked to drop `healthcheck.py` and replace with `status.json` written by the app itself. The status.json is throttled (~3s) and atomic (tmp+rename), so the loader can read it without coordinating with the app. The pre-swap probe is now "spawn the staged main.py, wait ~8s, read status.json, decide" — not "spawn a separate healthcheck.py".
- **2026-07-02 — Drop instance identifier entirely** | The user said "remove all this boot_id stuff" and clarified again "we're not using this approach anymore — they should ALL be cleaned up". The loader does NOT mint an instance identifier, does NOT set `LINDSAY50_BOOT_ID`, does NOT carry it through `os.execvpe`, and the `boot_id` field is gone from `StatusSnapshot`, `/api/sign/boot-config` response shape, and the boot-config contract. Status correlation, when/if needed, comes from `pid` + `started_at` + `HEROKU_SLUG_COMMIT`, not from a separately minted UUID.
- **2026-07-02 — Flask publishes check-for-update ONCE at startup** | The user flagged that on_connect publishing causes "network flakiness to become reboot spam". Move the publish to startup, not connect. Paho queues the message until the connection is up.
- **2026-07-02 — App handles check-for-update via existing MQTT subscriber** | The user asked: "can the app just handle 'check for update' messages instead?" Yes — the app already has MQTT, the loader doesn't. The loader is now one-shot (stage → probe → swap → exec, then gone).
- **2026-07-02 — Drop watch_subprocess + 30s grace period** | The user accepted: "trust systemd's StartLimitBurst=3 for crash loops, trust the operator's `heroku rollback v123` for the rare late-manifesting render bug". No post-swap watching. The loader is gone after execvpe.
- **2026-07-02 — Share boot-config code in lib_shared/boot_config.py** | The user asked: "is it worth sharing any code between the loader and app? there's some duplication in terms of using the API, and potentially things like checking the current sha values." Yes — extracted.
- **2026-07-02 — Pre-swap probe stays, post-swap grace period goes** | The user asked: "are we dropping the health check entirely?" Answer: no, the pre-swap check stays (now via status.json), but the post-swap grace period is dropped because the loader is gone after exec.
- **2026-07-02 — LINDSAY50_ACTIVE_SHA via os.execvpe, not git rev-parse from the app** | The user asked: "can the actual pi app determine what sha it's running?" Two options: app runs `git -C current/ rev-parse HEAD` or the loader passes it as an env var. Env var is simpler — no git invocation on the hot path, and the loader has the SHA it just staged.
- **2026-07-02 — Conversation-context override to E3** | The classifier returned E3 (fail-safe). Conversation context is a follow-up to extensive prior work on the same feature — the work IS substantial multi-file. Honor E3.
- **2026-07-02 — Use `refactor:` commit prefix on same branch** | Per CLAUDE.md project rules: "Use conventional commits (feat:, fix:, chore:, docs:, refactor:, test:). Link issue numbers in commit messages." The existing PR #50 will auto-update from `feat/issue-49`. Do NOT push or open a new PR.

## Changelog

- conjectured: "Replacing healthcheck.py with status.json will eliminate the drift problem."
  - refuted_by: (pending)
  - learned: (pending)
  - criterion_now: (pending)

## Verification

(To be filled at VERIFY phase.)