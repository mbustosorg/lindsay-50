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
# Each run logs every decision to stdout and to .settings_copy.log at the
# worktree root (overwritten on each run with a timestamp header), so a
# future operator can `cat .settings_copy.log` to see what last ran.

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

# In a worktree, .git is a file: "gitdir: <abs>/.git/worktrees/<name>".
# Two dirname calls land on the common git dir; its parent is the main
# checkout. Resolving through `cd && pwd` normalizes the path.
if [ -f .git ]; then
    GITDIR_PATH="$(tr -d '[:space:]' < .git | sed 's/^gitdir://')"
    COMMON_GITDIR="$(dirname "$(dirname "$GITDIR_PATH")")"
    MAIN_REPO="$(cd "$COMMON_GITDIR/.." && pwd)"
else
    echo "❌ .git is not a file (run from inside a git worktree)" >&2
    exit 1
fi

if [ ! -d "$MAIN_REPO" ]; then
    echo "❌ MAIN_REPO=$MAIN_REPO does not exist" >&2
    exit 1
fi

log=()
for proj in heart-message-manager heart-matrix-controller; do
    src="$MAIN_REPO/$proj/settings.toml"
    dst="$proj/settings.toml"

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

    cp "$src" "$dst"
    log+=("✅ $proj: copied $src → $dst")
done

printf '%s\n' "${log[@]}"
{
    echo "=== sync_settings.sh at $(date -u +'%Y-%m-%dT%H:%M:%SZ') ==="
    printf '%s\n' "${log[@]}"
} > .settings_copy.log
