#!/usr/bin/env bash
# Provision a Raspberry Pi from the operator's laptop, in one shot.
#
# Runs from the lindsay-50 repo root (cwd). Detects the local
# heart-matrix-controller/settings.toml, scps it onto the Pi, and
# hands off to setup-pi.sh (the on-Pi authoritative bootstrap).
# Replaces the "scp manually, then run setup-pi.sh" two-step.
#
# Usage:
#   scripts/provision-pi.sh [PI_HOST]
#   LINDSAY50_PI_HOST=root@1.2.3.4 scripts/provision-pi.sh
#
# Env vars (override the defaults; positional arg takes precedence):
#   LINDSAY50_PI_HOST         SSH target (default: root@lindsay-50)
#   LINDSAY50_PI_REPO_DIR     repo path on the Pi (default: /srv/lindsay-50)
#   LINDSAY50_LOCAL_SETTINGS  local settings.toml path
#                             (default: <cwd>/heart-matrix-controller/settings.toml)
#   LINDSAY50_GIT_REF         ref/commit to check out on the Pi before
#                             running setup-pi.sh (default: current
#                             HEAD of the operator's checkout)
#
# Idempotent: re-running refreshes settings.toml, fetches, and
# re-runs setup-pi.sh — so it's safe to invoke after editing the
# local file or pushing new commits.
#
# Expected downtime on a fresh Pi: 5–10 minutes (rgbmatrix C build is slow).
# On an already-bootstrapped Pi: < 5 seconds.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (env vars or positional)
# ---------------------------------------------------------------------------

PI_HOST="${1:-${LINDSAY50_PI_HOST:-root@lindsay-50}}"
PI_REPO_DIR="${LINDSAY50_PI_REPO_DIR:-/srv/lindsay-50}"
LOCAL_REPO_DIR="$(pwd -P)"

# Default settings path mirrors the on-Pi canonical path so an
# operator who keeps a checkout-with-settings on the laptop has
# nothing to configure. Override with LINDSAY50_LOCAL_SETTINGS for
# any other layout (a `~/secrets/...` directory, a 1Password reference,
# etc.).
LOCAL_SETTINGS="${LINDSAY50_LOCAL_SETTINGS:-$LOCAL_REPO_DIR/heart-matrix-controller/settings.toml}"
GIT_REF="${LINDSAY50_GIT_REF:-$(git rev-parse HEAD)}"

# ---------------------------------------------------------------------------
# Pre-flight: cwd is the repo root, settings.toml is here, ssh works
# ---------------------------------------------------------------------------

# Detect repo root: the cwd must be a git checkout (has .git as a
# directory or a worktree-style .git file) AND contain the
# heart-matrix-controller/ submodule-equivalent directory.
is_git_repo() {
    [ -d "$1/.git" ] || [ -f "$1/.git" ]
}

if ! is_git_repo "$LOCAL_REPO_DIR"; then
    echo "❌ run provision-pi.sh from the lindsay-50 repo root (cwd has no .git)." >&2
    echo "   Current cwd: $LOCAL_REPO_DIR" >&2
    exit 1
fi

if [ ! -d "$LOCAL_REPO_DIR/heart-matrix-controller" ]; then
    echo "❌ heart-matrix-controller/ not found in $LOCAL_REPO_DIR;" >&2
    echo "   not the lindsay-50 repo root." >&2
    exit 1
fi

# The local settings.toml is the operator's canonical copy. Without
# it the script has nothing to ship, so fail fast with a clear
# pointer to where it should live. (Override the path with
# LINDSAY50_LOCAL_SETTINGS=<path> for non-standard layouts.)
if [ ! -f "$LOCAL_SETTINGS" ]; then
    echo "❌ settings.toml not found at $LOCAL_SETTINGS" >&2
    echo "" >&2
    echo "  This script ships your local settings.toml to the Pi." >&2
    echo "  The default path is <repo-root>/heart-matrix-controller/settings.toml." >&2
    echo "  Drop a filled-in copy there (copy from heart-matrix-controller/settings.toml.example)" >&2
    echo "  and re-run, or set LINDSAY50_LOCAL_SETTINGS=<path> to point at a different file." >&2
    exit 1
fi

# Confirm we can reach the Pi before doing anything destructive. A
# 5-second ConnectTimeout means we fail fast on a typo'd hostname.
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_HOST" true; then
    echo "❌ cannot ssh to $PI_HOST" >&2
    echo "   pass PI_HOST as \$1 or set LINDSAY50_PI_HOST env var." >&2
    echo "   (root SSH is assumed because /srv/lindsay-50/ is only writable as root.)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Provision: clone/checkout on the Pi, ship settings.toml, bootstrap
# ---------------------------------------------------------------------------

echo "==> provisioning $PI_HOST (repo at $PI_REPO_DIR, ref $GIT_REF)"

# 1. Ensure the Pi has a clone. Skip if $PI_REPO_DIR/.git already
#    exists (the user's existing repo state — including the bare-repo
#    layout setup-pi.sh creates — is preserved across re-runs).
echo "==> ensuring clone at $PI_HOST:$PI_REPO_DIR"
ssh "$PI_HOST" "test -d '$PI_REPO_DIR/.git' || git clone https://github.com/mbustosorg/lindsay-50.git '$PI_REPO_DIR'"

# 2. Pin the Pi to the right ref. `fetch origin` brings in the
#    remote refs the operator's local commit/branch needs.
#    `checkout -f` is safe because /srv/lindsay-50 is gitignored
#    for settings.toml anyway and the operator should not be
#    editing files on the Pi (defense-in-depth).
echo "==> checking out $GIT_REF on the Pi"
ssh "$PI_HOST" "cd '$PI_REPO_DIR' && git fetch origin && git checkout -f '$GIT_REF'"

# 3. Ship the local settings.toml onto the Pi. scp to a .tmp path
#    then `mv` into place: avoids a partial-file overwrite if the
#    connection drops mid-transfer.
echo "==> shipping settings.toml → $PI_HOST:$PI_REPO_DIR/heart-matrix-controller/"
scp -q "$LOCAL_SETTINGS" "$PI_HOST:$PI_REPO_DIR/heart-matrix-controller/settings.toml.tmp"
ssh "$PI_HOST" "mv '$PI_REPO_DIR/heart-matrix-controller/settings.toml.tmp' '$PI_REPO_DIR/heart-matrix-controller/settings.toml'"

# 4. Hand off to setup-pi.sh on the Pi — that's the authoritative
#    on-Pi bootstrap (Phase 1: apt, Phase 2: pip, Phase 3: bare +
#    worktree, Phase 4: settings.toml check, Phase 5: systemd).
echo "==> handing off to setup-pi.sh on the Pi"
ssh "$PI_HOST" "cd '$PI_REPO_DIR' && ./scripts/setup-pi.sh"

echo ""
echo "==> provisioning complete."
echo "    Service status: ssh $PI_HOST 'systemctl status lindsay_50'"
echo "    Follow logs:    ssh $PI_HOST 'journalctl -u lindsay_50 -f'"
