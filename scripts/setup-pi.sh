#!/usr/bin/env bash
# One-time Pi bootstrap for the self-upgrading matrix controller (issue #49).
#
# Converts a fresh clone of lindsay-50 into a fully-running install:
#   - Installs system packages (apt) and Python requirements (pip) if missing
#   - Converts the clone into a bare repo with per-SHA worktrees
#   - Creates a `current` symlink pointing at the active version
#   - Verifies settings.toml is in place (hard-stops if not — the sign won't boot)
#   - Vendors the BDF font into the worktree if missing
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

# Idempotency check: if `.git` is already a file (bare) AND `current` is a
# symlink pointing at a v-<sha>/ dir, we've already bootstrapped the layout.
if [ -f "$REPO_DIR/.git" ] && [ -L "$REPO_DIR/current" ]; then
    target=$(readlink "$REPO_DIR/current")
    if [ -d "$REPO_DIR/$target" ]; then
        echo "==> setup-pi: repo already bootstrapped (current -> $target); skipping conversion"
    else
        echo "==> setup-pi: WARNING: current symlink points at missing $target; will repair"
    fi
else
    echo "==> setup-pi: converting .git/ to bare .git/..."
    HEAD_SHA=$(git rev-parse HEAD)
    echo "    HEAD at $HEAD_SHA"

    # Move existing .git aside, build a fresh bare clone from it.
    mv "$REPO_DIR/.git" "$REPO_DIR/.git.tmp"
    git clone --bare "$REPO_DIR/.git.tmp" "$REPO_DIR/.git" >/dev/null
    rm -rf "$REPO_DIR/.git.tmp"

    # Create the first worktree at HEAD
    echo "==> setup-pi: creating v-$HEAD_SHA worktree"
    git -C "$REPO_DIR" worktree add "$REPO_DIR/v-$HEAD_SHA" "$HEAD_SHA"

    # Symlink `current` at the worktree
    ln -sfn "v-$HEAD_SHA" "$REPO_DIR/current"
    echo "==> setup-pi: current -> v-$HEAD_SHA"
fi

# Resolve the active worktree (where settings.toml and fonts/ must live)
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
    echo "To fix:" >&2
    echo "  sudo cp $SETTINGS_EXAMPLE $SETTINGS" >&2
    echo "  sudo nano $SETTINGS" >&2
    echo "  sudo $0" >&2
    exit 1
fi
echo "==> setup-pi: settings.toml present"

# ---------------------------------------------------------------------------
# Phase 5: BDF font — idempotent (the chore commit vendors it; this is
#           defense for an older checkout that doesn't have it yet)
# ---------------------------------------------------------------------------

FONT_DIR="$WORKTREE_DIR/heart-matrix-controller/fonts"
FONT_FILE="$FONT_DIR/6x9.bdf"
if [ -f "$FONT_FILE" ]; then
    echo "==> setup-pi: font already vendored"
else
    echo "==> setup-pi: vendoring BDF font"
    mkdir -p "$FONT_DIR"
    curl -fsSL -o "$FONT_FILE" \
        https://raw.githubusercontent.com/hzeller/rpi-rgb-led-matrix/master/fonts/6x9.bdf
    chmod 644 "$FONT_FILE"
fi

# ---------------------------------------------------------------------------
# Phase 6: systemd unit — install, reload, enable
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