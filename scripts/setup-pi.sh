#!/usr/bin/env bash
# One-time Pi bootstrap for the self-upgrading matrix controller (issue #49).
#
# Converts a fresh clone of lindsay-50 into a fully-running install:
#   - Installs system packages (apt) and Python requirements (pip) if missing
#   - Converts the clone into a bare repo with per-SHA worktrees
#   - Creates a `current` symlink pointing at the active version
#   - Verifies settings.toml is in place (hard-stops if not — the sign won't boot)
#   - Installs the systemd unit and starts the service
#
# Usage (as root on the Pi):
#   sudo /srv/lindsay-50/scripts/setup-pi.sh
#
# Idempotent: re-running on an already-bootstrapped repo is mostly a no-op.
# Existing apt packages, pip packages, settings.toml, and worktrees are
# detected and skipped.
#
# Expected downtime on a fresh Pi: 5–10 minutes (rgbmatrix C build is slow).
# On an already-bootstrapped Pi: < 5 seconds.

set -euo pipefail

REPO_DIR="${REPO_DIR:-/srv/lindsay-50}"
SERVICE_NAME="lindsay_50"
UNIT_SRC="$REPO_DIR/scripts/lindsay_50.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"

echo "==> setup-pi: bootstrapping $REPO_DIR"

# Sanity check: repo must exist
if [ ! -d "$REPO_DIR/.git" ] && [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: $REPO_DIR does not exist. Clone the repo first:" >&2
    echo "  sudo git clone https://github.com/mbustosorg/lindsay-50.git $REPO_DIR" >&2
    exit 1
fi

cd "$REPO_DIR"

# ---------------------------------------------------------------------------
# Phase 1: System packages (apt) — idempotent
# ---------------------------------------------------------------------------

REQUIRED_APT_PACKAGES=(
    git
    python3
    python3-pip
    python3-venv
    build-essential
    python-dev-is-python3
    cython3
    python3-pil
)

missing_apt=()
for pkg in "${REQUIRED_APT_PACKAGES[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        missing_apt+=("$pkg")
    fi
done

if [ ${#missing_apt[@]} -gt 0 ]; then
    echo "==> setup-pi: installing missing system packages: ${missing_apt[*]}"
    apt-get update
    apt-get install -y "${missing_apt[@]}"
else
    echo "==> setup-pi: all system packages already installed"
fi

# ---------------------------------------------------------------------------
# Phase 2: Python requirements (pip) — idempotent
# ---------------------------------------------------------------------------

if python3 -c "import rgbmatrix" 2>/dev/null; then
    echo "==> setup-pi: rgbmatrix already importable, skipping pip install"
else
    echo "==> setup-pi: installing Python requirements (rgbmatrix C build, ~2-5 min)"
    pip install --break-system-packages \
        -r "$REPO_DIR/requirements.txt" \
        -r "$REPO_DIR/heart-matrix-controller/requirements.txt"
fi

# ---------------------------------------------------------------------------
# Phase 3: Bare-repo + worktree layout — idempotent
# ---------------------------------------------------------------------------

# Three valid bootstrap states:
#   (a) current symlink -> valid v-<sha>/ dir: fully bootstrapped, skip
#   (b) bare repo present, no current symlink: partial bootstrap — finish
#       without re-running the conversion (worktree-add itself is idempotent)
#   (c) non-bare clone (default branch checkout): full conversion
#
# Note: `git clone --bare` produces a *directory* (not a file) at the
# target path — it just has no working tree. So `[ -f .git ]` is the wrong
# bare-detector; use `git rev-parse --is-bare-repository`.

CURRENT_TARGET=""
if [ -L "$REPO_DIR/current" ]; then
    CURRENT_TARGET=$(readlink "$REPO_DIR/current")
fi

IS_BARE="false"
if [ -d "$REPO_DIR/.git" ]; then
    IS_BARE=$(git -C "$REPO_DIR" rev-parse --is-bare-repository 2>/dev/null || echo "false")
fi

# Refresh the bare repo's remote refs so re-running setup-pi.sh actually sees
# commits the operator pushed after the original clone. Without this the
# bare repo's HEAD stays pinned at whatever the initial `git clone` carried
# in — every subsequent push was invisible to this script. No-op (exit 0)
# on offline or already-current repos. We tolerate failure so a Pi that's
# briefly offline during bootstrap isn't blocked.
if [ "$IS_BARE" = "true" ]; then
    if git -C "$REPO_DIR" remote >/dev/null 2>&1; then
        git -C "$REPO_DIR" fetch origin \
            $(git -C "$REPO_DIR" for-each-ref --format='%(refname:short)' refs/remotes/origin/ 2>/dev/null \
                | tr '\n' ' ') \
            >/dev/null 2>&1 || true
    fi
fi

# Pre-flight cleanup: prune stale worktree metadata and remove orphan
# v-<sha>/ directories left over from prior failed runs. Without this,
# `git worktree add` bails on the leftover dir even though no live
# worktree references it. This was the issue #49 retry failure mode.
if [ "$IS_BARE" = "true" ]; then
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
fi
for stale in "$REPO_DIR"/v-*/; do
    if [ -d "$stale" ]; then
        # If `current` points at this dir, leave it alone.
        if [ "$CURRENT_TARGET" = "$(basename "$stale")" ]; then
            continue
        fi
        echo "==> setup-pi: removing stale worktree dir $(basename "$stale")"
        rm -rf "$stale"
    fi
done

if [ -n "$CURRENT_TARGET" ] && [ -d "$REPO_DIR/$CURRENT_TARGET" ]; then
    echo "==> setup-pi: repo already bootstrapped (current -> $CURRENT_TARGET); skipping conversion"
    HEAD_SHA=$(git -C "$REPO_DIR" rev-parse "$CURRENT_TARGET")
    # Existing Pi may have a long-form v-<full_sha>/ from before the
    # short-SHA convention landed — preserve its directory name but
    # still use the short form when staging anything new.
    HEAD_SHA_SHORT=$(git -C "$REPO_DIR" rev-parse --short=7 "$HEAD_SHA")
elif [ "$IS_BARE" = "true" ]; then
    # Partial bootstrap — finish without re-converting.
    echo "==> setup-pi: bare repo detected, bootstrap incomplete; finishing"
    HEAD_SHA=$(git -C "$REPO_DIR" rev-parse HEAD)
    HEAD_SHA_SHORT=$(git -C "$REPO_DIR" rev-parse --short=7 HEAD)
    echo "    HEAD at $HEAD_SHA (v-$HEAD_SHA_SHORT)"

    echo "==> setup-pi: creating v-$HEAD_SHA_SHORT worktree"
    git -C "$REPO_DIR" worktree add "$REPO_DIR/v-$HEAD_SHA_SHORT" "$HEAD_SHA"

    ln -sfn "v-$HEAD_SHA_SHORT" "$REPO_DIR/current"
    echo "==> setup-pi: current -> v-$HEAD_SHA_SHORT"
else
    # Non-bare clone — do the full conversion.
    echo "==> setup-pi: converting .git/ to bare .git/..."
    HEAD_SHA=$(git rev-parse HEAD)
    HEAD_SHA_SHORT=$(git rev-parse --short=7 HEAD)
    echo "    HEAD at $HEAD_SHA (v-$HEAD_SHA_SHORT)"

    mv "$REPO_DIR/.git" "$REPO_DIR/.git.tmp"
    git clone --bare "$REPO_DIR/.git.tmp" "$REPO_DIR/.git" >/dev/null
    rm -rf "$REPO_DIR/.git.tmp"

    echo "==> setup-pi: creating v-$HEAD_SHA_SHORT worktree"
    git -C "$REPO_DIR" worktree add "$REPO_DIR/v-$HEAD_SHA_SHORT" "$HEAD_SHA"

    ln -sfn "v-$HEAD_SHA_SHORT" "$REPO_DIR/current"
    echo "==> setup-pi: current -> v-$HEAD_SHA_SHORT"
fi

# Resolve the active worktree (where settings.toml must live)
WORKTREE_DIR="$REPO_DIR/$(readlink "$REPO_DIR/current")"
if [ ! -d "$WORKTREE_DIR" ]; then
    echo "ERROR: $WORKTREE_DIR does not exist after bootstrap" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Phase 4: settings.toml — HARD-STOP if missing
# ---------------------------------------------------------------------------

SETTINGS="$WORKTREE_DIR/heart-matrix-controller/settings.toml"
SETTINGS_EXAMPLE="$WORKTREE_DIR/heart-matrix-controller/settings.toml.example"

if [ ! -f "$SETTINGS" ]; then
    if [ ! -f "$SETTINGS_EXAMPLE" ]; then
        echo "ERROR: $SETTINGS is missing AND $SETTINGS_EXAMPLE does not exist." >&2
        echo "The repo checkout is corrupted. Re-clone and re-run." >&2
        exit 1
    fi
    echo "ERROR: $SETTINGS is missing." >&2
    echo "The sign will not boot without it (no MQTT creds, no panel geometry)." >&2
    echo "" >&2
    echo "To fix (existing Pi with v1 install — preserves creds/geometry):" >&2
    echo "  sudo cp /home/<your-user>/lindsay-50/heart-matrix-controller/settings.toml \\" >&2
    echo "          $SETTINGS" >&2
    echo "" >&2
    echo "To fix (fresh Pi — copy from example and fill in real values):" >&2
    echo "  sudo cp $SETTINGS_EXAMPLE $SETTINGS" >&2
    echo "  sudo nano $SETTINGS" >&2
    echo "" >&2
    echo "Then re-run: sudo $0" >&2
    exit 1
fi
echo "==> setup-pi: settings.toml present"

# ---------------------------------------------------------------------------
# Phase 5: systemd unit — install, reload, enable
# ---------------------------------------------------------------------------

if [ ! -f "$UNIT_SRC" ]; then
    echo "ERROR: $UNIT_SRC not found; cannot install systemd unit" >&2
    exit 1
fi

if [ ! -f "$UNIT_DST" ] || ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    echo "==> setup-pi: installing systemd unit"
    cp "$UNIT_SRC" "$UNIT_DST"
else
    echo "==> setup-pi: systemd unit already up to date"
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "==> setup-pi: bootstrap complete."
echo "    Service status: sudo systemctl status $SERVICE_NAME"
echo "    Follow logs:    sudo journalctl -u $SERVICE_NAME -f"