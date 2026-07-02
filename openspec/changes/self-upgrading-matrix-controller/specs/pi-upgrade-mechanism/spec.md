## ADDED Requirements

### Requirement: Loader process is the systemd entrypoint
The systemd unit (`scripts/lindsay_50.service`) MUST execute `heart-matrix-controller/loader.py` as its `ExecStart`. The loader runs as root (required for `git worktree`, `os.execvpe`, and rgbmatrix GPIO access).

The loader MUST resolve its working directory from `LINDSAY50_REPO_DIR` if set, falling back to `/home/pi/projects/lindsay-50`.

#### Scenario: Systemd starts the loader on boot
- **WHEN** the Pi boots (or systemd restarts the service)
- **THEN** systemd executes `loader.py` which queries Flask and ultimately execs `current/heart-matrix-controller/main.py` as its child process

### Requirement: Loader detects version mismatch on startup
The loader MUST query the expected version from Flask via `lib_shared.boot_config.fetch_boot_config`, compare it to the local HEAD (resolved through the `current/` symlink), and enter the upgrade flow if they differ.

If `LINDSAY50_ACTIVE_SHA` is set in the environment (the loader was entered from the running app via `check_for_update`), the loader MUST prefer it for the "current" comparison since `current/` may not yet reflect the swap.

#### Scenario: Local SHA matches expected SHA
- **WHEN** loader boots and `local_sha == expected_sha`
- **THEN** loader skips staging and execs `current/heart-matrix-controller/main.py`

#### Scenario: Local SHA differs from expected SHA
- **WHEN** loader boots and `local_sha != expected_sha`
- **THEN** loader stages the expected SHA into a new worktree, probes via `.status.json`, atomically swaps `current` if healthy, and execs the new version with the new `LINDSAY50_ACTIVE_SHA` in its env

#### Scenario: Flask is unreachable on boot
- **WHEN** loader cannot reach Flask (network error, 5xx, timeout)
- **THEN** loader logs the error and execs `current/heart-matrix-controller/main.py` on the existing local SHA (no upgrade attempt)

### Requirement: New versions are staged via git worktree
The loader MUST stage a new version using `git worktree add` against the existing bare repo at `$REPO_DIR/.git`, creating a directory named `v-<expected_sha>` at the repo root. The loader MUST reset any dirty working tree in the current version dir to its HEAD before staging.

#### Scenario: Clean local working tree
- **WHEN** loader stages a new worktree and the current version's working tree is clean
- **THEN** `git worktree add $REPO_DIR/v-<expected_sha> <expected_sha>` succeeds and creates the new directory

#### Scenario: Dirty local working tree
- **WHEN** loader stages a new worktree and the current version's working tree has uncommitted changes
- **THEN** loader runs `git -C <old_dir> reset --hard <local_sha>` first, then stages

#### Scenario: Network error during worktree creation
- **WHEN** `git worktree add` fails because git cannot reach the remote
- **THEN** loader logs the error and continues to exec `current/.../main.py` on the existing local SHA

### Requirement: Loader probes the staged version via `.status.json`
Before swapping the `current` symlink, the loader MUST spawn `v-<sha>/heart-matrix-controller/main.py` as a subprocess, wait up to a probe budget for `$REPO_DIR/v-<sha>/.status.json` to report `mqtt_connected=true` with no `last_error`, then kill the subprocess. If the file becomes healthy in time the probe returns True; otherwise the swap MUST NOT happen and the loader falls through to execing the existing `current/.../main.py`.

The probe MUST NOT install or set up `LINDSAY50_ACTIVE_SHA` — only the post-swap exec does.

#### Scenario: Status probe passes
- **WHEN** `.status.json` reports `mqtt_connected=true` and `last_error=None` within the probe budget
- **THEN** loader proceeds to atomic symlink swap, kills the probe subprocess, and execs the new version

#### Scenario: Status probe times out
- **WHEN** `.status.json` never reports `mqtt_connected=true` within the probe budget (or reports `last_error` set)
- **THEN** loader logs the failure, kills the probe subprocess, and execs the existing `current/.../main.py` without swapping

#### Scenario: `.status.json` missing entirely
- **WHEN** the staged version's `main.py` did not write `.status.json` at all within the probe budget
- **THEN** loader treats the staged version as unhealthy and falls through

### Requirement: Atomic symlink swap
The loader MUST swap the active version via `ln -sfn v-<expected_sha> current` after the status probe passes. The swap MUST be atomic on the same filesystem.

#### Scenario: Successful swap
- **WHEN** loader runs `ln -sfn v-<expected_sha> current` and the symlink target changes
- **THEN** any subsequent `current/...` resolution resolves to the new version directory

### Requirement: Post-exec env vars carry deployment context
After `os.execvpe` into the new `main.py`, the loader's env dict MUST include `LINDSAY50_ACTIVE_SHA=<new_sha>`, `LINDSAY50_REPO_DIR=<repo_dir>`, and `LINDSAY50_BOOT_ID=<boot_id>` (minted at loader startup if not already set in `os.environ`). All other variables in `os.environ` MUST be inherited unchanged.

#### Scenario: New `main.py` inherits loader env
- **WHEN** loader execs the new version's `main.py`
- **THEN** the new process sees `LINDSAY50_ACTIVE_SHA`, `LINDSAY50_REPO_DIR`, `LINDSAY50_BOOT_ID` plus the loader's other env vars (PATH, LOG_LEVEL, etc.)

### Requirement: Repository layout uses bare repo + worktrees
The Pi's working tree MUST be organized as a bare git repo (`.git/`) at the repo root, per-version worktrees (`v-<sha>/`) at the repo root, and a `current` symlink pointing at the active version. Shared resources (`settings.toml`, `fonts/`, `.venv/`) MUST live at the repo root, outside any per-version directory.

#### Scenario: Initial one-time setup
- **WHEN** operator bootstraps the Pi for the first time after this change
- **THEN** repo contains `.git/` (bare), `v-<sha>/` (first worktree), `current -> v-<sha>`, and shared `.venv/`, `settings.toml`, `fonts/`

#### Scenario: Settings and venv survive a version swap
- **WHEN** loader swaps `current` from one `v-<sha>/` to another
- **THEN** `.venv/`, `settings.toml`, and `fonts/` continue to be resolvable at the repo root and are not duplicated per version
