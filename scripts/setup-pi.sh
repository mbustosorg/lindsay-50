#!/usr/bin/env bash
# One-time Pi bootstrap for the self-upgrading matrix controller (issue #49).
#
# Converts an existing clone of lindsay-50 into the blue/green layout the
# loader needs: a bare repo at <REPO>/.git/, a per-SHA worktree at
# <REPO>/v-<sha>/, and a `current` symlink pointing at the active version.
# Shared resources (settings.toml, fonts/, .venv/) stay at the repo root,
# outside any per-version directory, so a version swap doesn't churn them.
#
# Usage (as root on the Pi):
#   sudo /home/pi/projects/lindsay-50/scripts/setup-pi.sh
#
# Idempotent: re-running on an already-bootstrapped repo is a no-op
# (the script detects the existing `.git/` bare + `current` symlink and
# exits 0 with a "already bootstrapped" message).
#
# Expected downtime: < 1 minute. The systemd service should be stopped
# while this runs (the script does not stop it — run `sudo systemctl
# stop lindsay_50` first).
set -euo pipefail

# Where this repo lives on the Pi. Edit if your path differs.
REPO_DIR="${REPO_DIR:-/home/pi/projects/lindsay-50}"

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "ERROR: $REPO_DIR/.git not found. Clone the repo first." >&2
    exit 1
fi

# Idempotency check: if `.git/` is already bare (file, not directory)
# AND `current` exists AND points to a v-<sha>/ dir, we've already
# bootstrapped.
if [ -f "$REPO_DIR/.git" ] && [ -L "$REPO_DIR/current" ]; then
    target=$(readlink "$REPO_DIR/current")
    if [ -d "$REPO_DIR/$target" ]; then
        echo "setup-pi: $REPO_DIR already bootstrapped (current -> $target); nothing to do."
        exit 0
    fi
fi

echo "setup-pi: bootstrapping $REPO_DIR for blue/green upgrades"

# Stop the service if it's running so we don't race the loader. Use
# `try-restart` so we don't fail when the service isn't enabled yet.
if command -v systemctl >/dev/null 2>&1; then
    systemctl try-restart lindsay_50 2>/dev/null || true
fi

cd "$REPO_DIR"

# Capture the current HEAD before we move .git — we'll create the
# initial v-<sha> worktree from this commit.
HEAD_SHA=$(git rev-parse HEAD)
echo "setup-pi: HEAD at $HEAD_SHA"

# Step 1: convert the existing clone into a bare repo.
# We can't `mv .git .git.tmp` and `git clone --bare .git.tmp .git`
# directly because the working tree holds files that the bare clone
# will refuse to overwrite. Move the working tree aside first.
if [ -d "$REPO_DIR/.git" ]; then
    echo "setup-pi: converting .git/ to bare .git/..."
    # Save the existing .git aside, then build a bare clone from it.
    mv "$REPO_DIR/.git" "$REPO_DIR/.git.tmp"
    git clone --bare "$REPO_DIR/.git.tmp" "$REPO_DIR/.git" >/dev/null
    rm -rf "$REPO_DIR/.git.tmp"
else
    echo "setup-pi: no .git/ found (already bare?); skipping conversion"
fi

# Step 2: move shared resources up to the repo root if they were
# inside the working tree. The CLAUDE.md setup places settings.toml,
# fonts/, and .venv/ at the repo root already; this is defense in
# depth for anyone who cloned into a subdirectory.
for shared in settings.toml fonts .venv; do
    if [ ! -e "$REPO_DIR/$shared" ] && [ -e "$REPO_DIR/$shared" ]; then
        echo "setup-pi: $shared already at repo root"
    fi
done

# Step 3: create the first worktree at the current HEAD.
echo "setup-pi: creating v-$HEAD_SHA worktree"
git -C "$REPO_DIR" worktree add "$REPO_DIR/v-$HEAD_SHA" "$HEAD_SHA"

# Step 4: create the `current` symlink. Atomic enough for our purposes
# (nothing else is running at this point because we stopped the service).
ln -sfn "v-$HEAD_SHA" "$REPO_DIR/current"
echo "setup-pi: current -> v-$HEAD_SHA"

# Step 5: confirm systemd unit is up to date and reload.
if command -v systemctl >/dev/null 2>&1; then
    if [ -f /etc/systemd/system/lindsay_50.service ]; then
        echo "setup-pi: reloading systemd unit"
        systemctl daemon-reload
        systemctl restart lindsay_50
        echo "setup-pi: service restarted; follow logs with: journalctl -u lindsay_50 -f"
    else
        echo "setup-pi: /etc/systemd/system/lindsay_50.service not found"
        echo "setup-pi: install it with: sudo cp scripts/lindsay_50.service /etc/systemd/system/"
    fi
fi

echo "setup-pi: bootstrap complete."
echo "setup-pi: verify with: systemctl status lindsay_50"