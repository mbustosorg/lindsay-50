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
#
# --------------------------------------------------------------------------
# settings.toml — operator-provided, NOT in the repo
# --------------------------------------------------------------------------
# settings.toml is the one file the operator drops onto the Pi by hand:
# MQTT creds, panel geometry, log level. It is .gitignore'd (so git
# never sees it) and lives at the canonical path:
#
#     $REPO_DIR/heart-matrix-controller/settings.toml
#
# (root-owned, since systemd runs as root). This script does NOT
# handle the canonical copy — the operator runs `scp` once from their
# laptop. On every subsequent `git worktree add`, the chain
#
#     hooks/post-checkout → scripts/sync_settings.sh
#
# auto-copies the canonical file into the new v-<sha>/worktree, so
# a version bump does not require another scp. See
# heart-matrix-controller/README.md#pi-deployment for the scp
# one-liner. If Phase 4 below hard-stops, the canonical file is
# missing; scp it in and re-run this script.

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

# install_post_checkout_hook: wire up the post-checkout hook so
# `git worktree add` (whether in this script now, or in loader.py on
# a future upgrade) auto-copies settings.toml into the new
# worktree via scripts/sync_settings.sh.
#
# Background: git only fires hooks from <gitdir>/hooks/ (i.e.
# /srv/lindsay-50/.git/hooks/). The hook we care about ships in
# the repo at hooks/post-checkout (a regular tracked file). Without
# the symlink, git falls back to post-checkout.sample (disabled),
# the hook silently no-ops, and Phase 4 below hard-stops on missing
# settings.toml. ln -sfn is idempotent — re-running just re-points.
install_post_checkout_hook() {
    if [ -d "$REPO_DIR/.git/hooks" ] && [ -f "$REPO_DIR/hooks/post-checkout" ]; then
        ln -sfn "$REPO_DIR/hooks/post-checkout" "$REPO_DIR/.git/hooks/post-checkout"
        echo "==> setup-pi: installed hooks/post-checkout → .git/hooks/post-checkout"
    fi
}

# Three valid bootstrap states (only valid when the repo IS BARE):
#   (a) bare + current symlink -> valid v-<sha>/ worktree: fully bootstrapped, skip
#   (b) bare + no current symlink: partial bootstrap — finish without
#       re-running the conversion (worktree-add itself is idempotent)
#   (c) non-bare clone (or non-bare with an orphan `current` left over
#       from a previous attempt): full conversion
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
        git -C "$REPO_DIR" fetch origin '+refs/heads/*:refs/remotes/origin/*' \
            >/dev/null 2>&1 || true
    fi
fi

# Pre-flight cleanup: prune stale worktree metadata (bare repo only) and
# remove orphan v-<sha>/ directories that would block `git worktree add`
# on retry. An "orphan" here means "either (1) `current` doesn't point
# at it or (2) the repo is non-bare so it's not a real worktree at all."
# A bare repo's `current` keeps its target dir; any other v-<sha>/ is
# stripped. This was the issue #49 retry failure mode — without the
# non-bare treatment, an orphan dir from a previous attempt would
# survive into the conversion path and fight `worktree add`.
if [ "$IS_BARE" = "true" ]; then
    git -C "$REPO_DIR" worktree prune 2>/dev/null || true
fi
for stale in "$REPO_DIR"/v-*/; do
    if [ -d "$stale" ]; then
        if [ "$CURRENT_TARGET" = "$(basename "$stale")" ] && [ "$IS_BARE" = "true" ]; then
            continue
        fi
        echo "==> setup-pi: removing stale/orphan worktree dir $(basename "$stale")"
        rm -rf "$stale"
        # If `current` pointed at this dir on a non-bare repo, the symlink
        # was lying — it's an orphan, not a worktree. Clear it too so the
        # state machine below sees an honest (no current) repo.
        if [ "$CURRENT_TARGET" = "$(basename "$stale")" ]; then
            rm -f "$REPO_DIR/current"
            CURRENT_TARGET=""
        fi
    fi
done

if [ -n "$CURRENT_TARGET" ] && [ -d "$REPO_DIR/$CURRENT_TARGET" ] && [ "$IS_BARE" = "true" ]; then
    echo "==> setup-pi: repo already bootstrapped (current -> $CURRENT_TARGET); skipping conversion"
    # The basename of `current`'s target IS the short SHA — derive it
    # directly. The previous implementation called `git rev-parse
    # v-<sha>` here but the result wasn't used downstream, and the
    # call fataled on a non-bare repo with a stale `current` symlink.
    HEAD_SHA_SHORT="${CURRENT_TARGET#v-}"
    # Self-heal: make sure the post-checkout hook is wired for future
    # worktree-adds (e.g. loader.py upgrade flow). Idempotent.
    install_post_checkout_hook
elif [ "$IS_BARE" = "true" ]; then
    # Partial bootstrap — finish without re-converting.
    echo "==> setup-pi: bare repo detected, bootstrap incomplete; finishing"
    HEAD_SHA=$(git -C "$REPO_DIR" rev-parse HEAD)
    HEAD_SHA_SHORT=$(git -C "$REPO_DIR" rev-parse --short=7 HEAD)
    echo "    HEAD at $HEAD_SHA (v-$HEAD_SHA_SHORT)"

    # Wire the post-checkout hook before worktree-add so the just-
    # created worktree receives its settings.toml copy.
    install_post_checkout_hook

    echo "==> setup-pi: creating v-$HEAD_SHA_SHORT worktree"
    git -C "$REPO_DIR" worktree add "$REPO_DIR/v-$HEAD_SHA_SHORT" "$HEAD_SHA"

    ln -sfn "v-$HEAD_SHA_SHORT" "$REPO_DIR/current"
    echo "==> setup-pi: current -> v-$HEAD_SHA_SHORT"
else
    # Non-bare clone (with or without a stale `current` symlink; any
    # orphans were cleared by the pre-flight loop above).
    echo "==> setup-pi: converting .git/ to bare .git/..."
    HEAD_SHA=$(git rev-parse HEAD)
    HEAD_SHA_SHORT=$(git rev-parse --short=7 HEAD)
    echo "    HEAD at $HEAD_SHA (v-$HEAD_SHA_SHORT)"

    # Capture the origin URL before the mv destroys .git/config.
    # `git clone --bare <local-path> <target>` rewrites origin to the
    # local source path; we then rm -rf that path — leaving the new
    # bare repo with a broken origin pointing at .git.tmp. Every
    # subsequent `git fetch origin` (loader.py upgrades, re-running
    # provision-pi.sh) would fatal. We restore the original URL after
    # the conversion so future fetches hit GitHub, not a deleted path.
    ORIGIN_URL=$(git -C "$REPO_DIR/.git" config remote.origin.url 2>/dev/null || true)

    mv "$REPO_DIR/.git" "$REPO_DIR/.git.tmp"
    git clone --bare "$REPO_DIR/.git.tmp" "$REPO_DIR/.git" >/dev/null
    rm -rf "$REPO_DIR/.git.tmp"

    if [ -n "$ORIGIN_URL" ]; then
        git -C "$REPO_DIR/.git" remote set-url origin "$ORIGIN_URL"
    fi

    # The bare .git/ now exists; install the hook before worktree-add
    # so settings.toml lands in the just-created worktree.
    install_post_checkout_hook

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
    echo "The canonical copy lives at the bare repo's parent dir:" >&2
    echo "  $REPO_DIR/heart-matrix-controller/settings.toml" >&2
    echo "" >&2
    echo "On your laptop, scp it from wherever you keep the canonical copy:" >&2
    echo "  sudo scp <local-settings.toml> \\" >&2
    echo "      root@<this-pi>:$REPO_DIR/heart-matrix-controller/settings.toml" >&2
    echo "" >&2
    echo "Then re-run: sudo $0" >&2
    echo "(see heart-matrix-controller/README.md#pi-deployment for details)" >&2
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