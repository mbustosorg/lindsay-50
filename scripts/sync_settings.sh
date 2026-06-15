#!/usr/bin/env bash
# Copy settings.toml from the main worktree into this one.
#
# Called by hooks/post-checkout on worktree creation. Also runnable
# directly to refresh after a clobber:
#
#   scripts/sync_settings.sh           # skip existing, copy missing
#   scripts/sync_settings.sh --force   # overwrite existing with main's copy
#   scripts/sync_settings.sh --check   # print what would happen, don't copy
#
# Each non-dry-run logs every decision to stdout and to .settings_copy.log
# at the worktree root (overwritten on each run with a timestamp header),
# so a future operator can `cat .settings_copy.log` to see what last ran.
# --check only writes to stdout; the .settings_copy.log file is reserved
# for what actually happened.

set -euo pipefail

FORCE=0
CHECK=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --check) CHECK=1 ;;
        -h | --help)
            echo "Usage: $0 [--force] [--check]"
            echo "  --force  Overwrite existing settings.toml files"
            echo "  --check  Print what would happen, don't copy"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

# Resolve worktree root + main checkout via git itself, so the script
# is cwd-independent and doesn't need to read the .git file. This also
# lets the user invoke it from any subdirectory of the worktree.
if ! command -v git >/dev/null 2>&1; then
    echo "❌ git is not on PATH" >&2
    exit 1
fi

WORKTREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$WORKTREE_ROOT" ]; then
    echo "❌ not inside a git working tree" >&2
    exit 1
fi

# A worktree's .git is a file pointing to <common>/worktrees/<name>;
# the resolved --git-dir therefore lives under <common>/worktrees/.
# A standalone repo (or the main worktree of a multi-worktree setup)
# has --git-dir equal to --git-common-dir, both being <repo>/.git.
# This script is only meaningful for non-main worktrees — there's
# nothing to copy when WORKTREE_ROOT == MAIN_REPO.
GIT_DIR_ABS="$(git rev-parse --absolute-git-dir 2>/dev/null || true)"
if [ -z "$GIT_DIR_ABS" ] || [[ "$GIT_DIR_ABS" != *"/worktrees/"* ]]; then
    echo "❌ not inside a git worktree (this script is for sub-worktrees only)" >&2
    exit 1
fi

GIT_COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
# --git-common-dir can be a relative path; resolve it.
case "$GIT_COMMON_DIR" in
    /*) ;;
    *) GIT_COMMON_DIR="$WORKTREE_ROOT/$GIT_COMMON_DIR" ;;
esac

if [ ! -d "$GIT_COMMON_DIR" ]; then
    echo "❌ git common dir $GIT_COMMON_DIR does not exist" >&2
    exit 1
fi

MAIN_REPO="$(cd "$GIT_COMMON_DIR/.." && pwd)"
if [ ! -d "$MAIN_REPO" ]; then
    echo "❌ MAIN_REPO=$MAIN_REPO does not exist" >&2
    exit 1
fi

log=()
for proj in heart-message-manager heart-matrix-controller; do
    src="$MAIN_REPO/$proj/settings.toml"
    dst="$WORKTREE_ROOT/$proj/settings.toml"

    if [ ! -f "$src" ]; then
        log+=("ℹ️  $proj: no settings.toml in $MAIN_REPO — skipped")
        continue
    fi

    if [ -f "$dst" ] && [ "$FORCE" -ne 1 ]; then
        log+=("⏭️  $proj: $dst already exists — skipped (use --force to overwrite)")
        continue
    fi

    if [ "$CHECK" -eq 1 ]; then
        log+=("📝 $proj: would copy $src → $dst")
        continue
    fi

    # Defensive: destination directory may not exist if this branch
    # is fresh and hasn't been built yet. We don't want a single
    # missing directory to abort the whole run.
    mkdir -p "$(dirname "$dst")" || {
        log+=("❌ $proj: failed to create $(dirname "$dst") — skipped")
        continue
    }

    if cp "$src" "$dst"; then
        log+=("✅ $proj: copied $src → $dst")
    else
        log+=("❌ $proj: copy failed ($src → $dst) — skipped")
    fi
done

printf '%s\n' "${log[@]}"

# --check is for inspection, not for the log of-record. Skip the
# .settings_copy.log write so the file always reflects the last
# real (non-dry-run) execution.
if [ "$CHECK" -ne 1 ]; then
    {
        echo "=== sync_settings.sh at $(date -u +'%Y-%m-%dT%H:%M:%SZ') ==="
        printf '%s\n' "${log[@]}"
    } > "$WORKTREE_ROOT/.settings_copy.log"
fi
