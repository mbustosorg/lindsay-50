## ADDED Requirements

### Requirement: Loader process is the systemd entrypoint
The systemd unit (`scripts/lindsay_50.service`) MUST execute `heart-matrix-controller/loader.py` as its `ExecStart`. The loader runs as root (required for `git worktree`, `sudo reboot`, and rgbmatrix GPIO access).

#### Scenario: Systemd starts the loader on boot
- **WHEN** the Pi boots (or systemd restarts the service)
- **THEN** systemd executes `loader.py` from the directory that `current/` symlink resolves to

### Requirement: Loader detects version mismatch on startup
The loader MUST query the expected version from Flask, compare it to the local HEAD (resolved through the `current/` symlink), and enter the upgrade flow if they differ.

#### Scenario: Local SHA matches expected SHA
- **WHEN** loader boots and `local_sha == expected_sha`
- **THEN** loader skips staging and execs `current/heart-matrix-controller/main.py` as a subprocess

#### Scenario: Local SHA differs from expected SHA
- **WHEN** loader boots and `local_sha != expected_sha`
- **THEN** loader stages the expected SHA into a new worktree, runs `--healthcheck`, swaps `current` atomically if healthy, and execs the new subprocess

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

### Requirement: Loader runs the app health check before swap
Before swapping the `current` symlink, the loader MUST invoke the staged version's `main.py --healthcheck` and verify exit code 0. If the health check exits non-zero, the loader MUST NOT swap, MUST log the failure, and MUST continue to exec the existing `current/.../main.py`.

#### Scenario: Health check passes
- **WHEN** `v-<expected_sha>/heart-matrix-controller/main.py --healthcheck` exits 0
- **THEN** loader proceeds to atomic symlink swap

#### Scenario: Health check fails (e.g., missing dependency)
- **WHEN** `v-<expected_sha>/heart-matrix-controller/main.py --healthcheck` exits non-zero
- **THEN** loader leaves `current` unchanged, logs the failure, and execs the existing `current/.../main.py`

### Requirement: Atomic symlink swap
The loader MUST swap the active version via `ln -sfn v-<expected_sha> current` after the health check passes. The swap MUST be atomic on the same filesystem.

#### Scenario: Successful swap
- **WHEN** loader runs `ln -sfn v-<expected_sha> current` and the symlink target changes
- **THEN** any subsequent `current/...` resolution resolves to the new version directory

### Requirement: Post-swap rollback on early subprocess exit
After swapping `current` and exec'ing `main.py`, the loader MUST watch the subprocess for 30 seconds. If the subprocess exits unexpectedly during this grace period, the loader MUST swap `current` back to the previous known-good SHA and restart the subprocess.

#### Scenario: Subprocess stays up past grace period
- **WHEN** `main.py` runs continuously for 30 seconds after swap
- **THEN** loader stops watching and exits its own process (systemd restarts the loader, which now execs the new version normally)

#### Scenario: Subprocess exits within grace period
- **WHEN** `main.py` exits non-zero within 30 seconds of swap
- **THEN** loader swaps `current` back to the previous `v-<old_sha>/` directory and execs the previous version's `main.py`

### Requirement: Repository layout uses bare repo + worktrees
The Pi's working tree MUST be organized as a bare git repo (`.git/`) at the repo root, per-version worktrees (`v-<sha>/`) at the repo root, and a `current` symlink pointing at the active version. Shared resources (`settings.toml`, `fonts/`, `.venv/`) MUST live at the repo root, outside any per-version directory.

#### Scenario: Initial one-time setup
- **WHEN** operator bootstraps the Pi for the first time after this change
- **THEN** repo contains `.git/` (bare), `v-<sha>/` (first worktree), `current -> v-<sha>`, and shared `.venv/`, `settings.toml`, `fonts/`

#### Scenario: Settings and venv survive a version swap
- **WHEN** loader swaps `current` from one `v-<sha>/` to another
- **THEN** `.venv/`, `settings.toml`, and `fonts/` continue to be resolvable at the repo root and are not duplicated per version