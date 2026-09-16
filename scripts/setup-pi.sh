#!/usr/bin/env bash
# One-time Pi bootstrap for the matrix controller.
#
# Fresh-clones (or refreshes) the lindsay-50 repo at /srv/lindsay-50,
# installs system + Python deps, copies settings.toml from the existing
# /home/mauricio/lindsay-50 install if present, creates the `current/`
# symlink the auto-upgrade loader requires, and installs the systemd
# unit. To pick up new commits later, re-run this script (or rely on
# the auto-upgrade machinery in heart-matrix-controller/loader.py to
# pull them in via MQTT).
#
# Usage (as root on the Pi):
#   sudo /srv/lindsay-50/scripts/setup-pi.sh
#
# Idempotent:
#   - apt packages: skipped if already installed
#   - pip (rgbmatrix C build): skipped if rgbmatrix is already importable
#   - repo: re-runs `git fetch && git reset --hard origin/<branch>` to grab latest
#   - current/ symlink: `ln -sfn . current` always points at the working tree
#   - settings.toml: only copied if missing at the canonical path
#   - systemd unit: overwritten if changed
#
# Prerequisite (one-time, manual): if you're CONVERTING a Pi that already
# has the sign running from a different path (e.g. /home/mauricio/lindsay-50),
# stop and disable the OLD systemd unit first:
#
#   sudo systemctl stop lindsay_50
#   sudo systemctl disable lindsay_50
#   sudo rm /etc/systemd/system/lindsay_50.service
#   sudo systemctl daemon-reload
#
# Then run this script. It will not touch the old install path.
#
# Expected downtime on a fresh Pi: 5-10 minutes (rgbmatrix C build).
# On an already-bootstrapped Pi: < 5 seconds.

set -euo pipefail

REPO_DIR="${REPO_DIR:-/srv/lindsay-50}"
REPO_URL="${REPO_URL:-https://github.com/mbustosorg/lindsay-50.git}"
OLD_REPO_DIR="${OLD_REPO_DIR:-/home/mauricio/lindsay-50}"
SERVICE_NAME="lindsay_50"
UNIT_SRC="$REPO_DIR/scripts/lindsay_50.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"

echo "==> setup-pi: bootstrapping $REPO_DIR"

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
    # Only the Pi's deps — the Flask server's deps (requirements-flask.txt)
    # are NOT installed here. The Pi doesn't run Flask; installing
    # flask/boto3/twilio on it is wasted bandwidth and image size. If
    # the operator wants both, they can `pip install -r
    # requirements-flask.txt` manually after setup, but the matrix
    # controller's runtime never imports them.
    pip install --break-system-packages \
        -r "$REPO_DIR/requirements-pi.txt"
fi

# ---------------------------------------------------------------------------
# Phase 3: Git repo — clone fresh or pull latest
# ---------------------------------------------------------------------------
#
# First run: `git clone` into $REPO_DIR.
# Re-run:    `git fetch && git reset --hard origin/<current-branch>` so the
#            working tree tracks the remote's HEAD. `reset --hard` is
#            acceptable because this is a single-purpose Pi — the operator
#            should not be making local commits here. If they want to
#            preserve local changes, they should not be re-running this
#            script.

if [ -d "$REPO_DIR/.git" ]; then
    echo "==> setup-pi: pulling latest into existing $REPO_DIR"
    cd "$REPO_DIR"
    git fetch origin
    BRANCH=$(git symbolic-ref --short HEAD)
    git reset --hard "origin/$BRANCH"
    echo "==> setup-pi: $REPO_DIR is now at $(git rev-parse --short=7 HEAD)"
else
    echo "==> setup-pi: cloning $REPO_URL to $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
fi

# The auto-upgrade machinery (heart-matrix-controller/loader.py) reads
# "$REPO_DIR/current" as the symlink that names the working tree it
# should exec — the main repo on first install, or a v-<sha>/ worktree
# after a successful upgrade. We start by pointing `current` at `.`
# (this very clone) and let `loader.atomic_swap` redirect it after a
# staged-and-probed upgrade. `ln -sfn` is idempotent: re-runs that
# happen to land on a stale `current → v-<sha>` worktree get a clean
# baseline. `cd` was already done above; `.` resolves to $REPO_DIR.
ln -sfn . current

# Install the post-checkout hook so new worktrees (created by the
# loader's `git worktree add v-<sha> <target>` calls) inherit
# settings.toml. The hook copies the .gitignore'd settings.toml
# from the main checkout into the new worktree via
# scripts/sync_settings.sh. Without it, an upgraded worktree would
# have no MQTT creds and the loader's `execvpe main.py` would crash
# at config_reader.get_config() time. Idempotent.
mkdir -p "$REPO_DIR/.git/hooks"
ln -sfn "$REPO_DIR/hooks/post-checkout" "$REPO_DIR/.git/hooks/post-checkout"

# ---------------------------------------------------------------------------
# Phase 4: settings.toml — copy from old install, or hard-stop
# ---------------------------------------------------------------------------
#
# settings.toml is .gitignore'd, so the freshly-cloned repo doesn't have
# one. If the operator is converting from $OLD_REPO_DIR (e.g.
# /home/mauricio/lindsay-50), copy the canonical file in. Otherwise
# hard-stop with scp instructions — the sign won't boot without it.

SETTINGS="$REPO_DIR/heart-matrix-controller/settings.toml"
OLD_SETTINGS="$OLD_REPO_DIR/heart-matrix-controller/settings.toml"

if [ ! -f "$SETTINGS" ]; then
    if [ -f "$OLD_SETTINGS" ]; then
        echo "==> setup-pi: copying settings.toml from $OLD_SETTINGS"
        cp "$OLD_SETTINGS" "$SETTINGS"
    else
        echo "ERROR: $SETTINGS is missing." >&2
        echo "The sign will not boot without it (no MQTT creds, no panel geometry)." >&2
        echo "" >&2
        echo "Either re-run after copying it from a previous install, or scp it in:" >&2
        echo "  sudo scp <local-settings.toml> root@<this-pi>:$SETTINGS" >&2
        echo "" >&2
        echo "Then re-run: sudo $0" >&2
        exit 1
    fi
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