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
#
# --------------------------------------------------------------------------
# SSH auth — publickey preferred, password (interactive prompt) as fallback
# --------------------------------------------------------------------------
# Prefers publickey auth (BatchMode=yes preflight). If the publickey
# preflight fails and stdin is a TTY, prompts once for the root password,
# encrypts it with a per-run Fernet key, and routes subsequent ssh/sftp
# invocations through SSH_ASKPASS. The plaintext password never touches
# disk — the only filesystem artifact is a Fernet ciphertext file under
# a private temp dir, useless without the in-memory key. On non-TTY
# stdin, the script aborts with a "set up publickey auth" message; the
# password path is interactive-only by design (no env-var input, to
# avoid the password's brief lifetime in /proc/<pid>/environ or `ps e`).
# To skip the prompt, install a publickey at /root/.ssh/authorized_keys
# on the Pi — see heart-matrix-controller/README.md#pi-deployment.

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

# setup_password_auth: interactive password fallback for the ssh/sftp
# invocations below. Called only when the publickey preflight fails.
#
# Flow:
#   1. Fail fast with a clear message if stdin is not a TTY — the
#      password path is interactive-only by design (no env-var input,
#      see header comment).
#   2. Resolve the venv python ($LOCAL_REPO_DIR/.venv/bin/python3) and
#      confirm `cryptography` is importable. The askpass binary needs
#      cryptography to decrypt; fail fast if the venv is missing or
#      incomplete.
#   3. Prompt for the password with `read -rs` (silent, no echo).
#   4. Generate a per-run Fernet key in script memory, encrypt the
#      password, write the ciphertext to a private temp dir. The
#      plaintext is unset immediately after encryption.
#   5. Write a one-line askpass shell script (shebang → venv python)
#      that decrypts the ciphertext file and prints the plaintext
#      for ssh to consume.
#   6. Export SSH_ASKPASS and SSH_ASKPASS_REQUIRE=force so every
#      subsequent ssh/sftp invocation in this script inherits the
#      askpass and never prompts.
#   7. Re-run the ssh preflight WITHOUT BatchMode=yes to confirm the
#      password works. If it doesn't, abort with the same error the
#      original preflight would have shown.
#   8. Register a trap to rm -rf the temp dir on EXIT/INT/TERM/HUP.
#
# Security: the plaintext password exists in three places during the
# script's lifetime — the `read` builtin, the askpass process's stdout,
# and ssh's stdin. It never touches disk. The only filesystem artifact
# is the encrypted file, worthless without the in-memory Fernet key
# (which lives only in this script's process memory and is unset on
# script exit).
setup_password_auth() {
    if [ ! -t 0 ]; then
        echo "❌ cannot ssh to $PI_HOST via publickey, and stdin is not a TTY." >&2
        echo "   provision-pi.sh only accepts the password interactively (no env var)," >&2
        echo "   to keep it off disk. Run the script directly from a terminal, or" >&2
        echo "   install a publickey at /root/.ssh/authorized_keys on the Pi." >&2
        echo "   (See heart-matrix-controller/README.md#pi-deployment for the scp" >&2
        echo "   one-liner to install one.)" >&2
        return 1
    fi

    # Resolve the venv python — the askpass binary needs it to import
    # cryptography.fernet. We don't fall back to system python because
    # cryptography may not be installed there, and pip-installing into
    # the venv mid-provision is exactly the kind of side effect this
    # script is designed to avoid.
    PYTHON_BIN="$LOCAL_REPO_DIR/.venv/bin/python3"
    if [ ! -x "$PYTHON_BIN" ]; then
        echo "❌ $PYTHON_BIN not found." >&2
        echo "   provision-pi.sh needs the repo's .venv to encrypt the password." >&2
        echo "   Run from the repo root after: python3 -m venv .venv && pip install -r requirements-provisioner.txt" >&2
        return 1
    fi
    if ! "$PYTHON_BIN" -c "from cryptography.fernet import Fernet" 2>/dev/null; then
        echo "❌ cryptography.fernet not importable in $PYTHON_BIN." >&2
        echo "   Run: source .venv/bin/activate && pip install -r requirements-provisioner.txt" >&2
        return 1
    fi

    # Per-run private temp dir for the encrypted password and the
    # askpass script. Cleaned up by the trap registered below.
    PW_TMPDIR="$(mktemp -d -t lindsay-50-pw.XXXXXX)"
    chmod 700 "$PW_TMPDIR"
    PW_ENC="$PW_TMPDIR/pw.enc"
    PW_ASKPASS="$PW_TMPDIR/askpass"

    # Cleanup on any exit path (success, error, signals). Runs after
    # the script's own set -e propagation, so even a SIGKILL after
    # the trap is registered gets the dir removed. (SIGKILL itself
    # can't be trapped — that's a kernel-level limitation, not a
    # shell one — but a graceful kill via SIGTERM/HUP/INT is covered.)
    trap 'rm -rf "$PW_TMPDIR"' EXIT INT TERM HUP

    # Prompt once. read -rs = silent (no terminal echo), raw (no
    # backslash escaping). The trailing echo is just to put the
    # cursor on a new line after the silent read.
    read -rs -p "Pi root password for $PI_HOST: " PI_PASSWORD
    echo

    if [ -z "$PI_PASSWORD" ]; then
        echo "❌ empty password; aborting." >&2
        return 1
    fi

    # Generate the Fernet key in script memory, encrypt the password,
    # write only the ciphertext. The plaintext is unset immediately
    # after — the only place it still exists is the askpass process's
    # stdout at the moment ssh reads it.
    FERNET_KEY="$("$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    printf '%s' "$PI_PASSWORD" | \
        "$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; import sys; print(Fernet(sys.argv[1].encode()).encrypt(sys.stdin.buffer.read()).decode())' \
        "$FERNET_KEY" > "$PW_ENC"
    chmod 600 "$PW_ENC"
    unset PI_PASSWORD

    # Askpass binary: shebang → venv python; decrypts the ciphertext
    # and prints the plaintext for ssh. 700 because it contains the
    # key as a literal in argv.
    cat > "$PW_ASKPASS" <<EOF
#!/bin/sh
exec "$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; import sys; print(Fernet(sys.argv[1].encode()).decrypt(sys.stdin.buffer.read()).decode())' "$FERNET_KEY" < "$PW_ENC"
EOF
    chmod 700 "$PW_ASKPASS"
    unset FERNET_KEY

    # SSH_ASKPASS_REQUIRE=force makes ssh invoke the askpass binary
    # even when it has a tty (otherwise ssh might prompt via the tty
    # directly, bypassing our encryption). PYTHONDONTWRITEBYTECODE
    # avoids stray .pyc files in the temp dir that would survive
    # cleanup if the trap fires mid-import.
    export SSH_ASKPASS="$PW_ASKPASS"
    export SSH_ASKPASS_REQUIRE=force
    export PYTHONDONTWRITEBYTECODE=1

    # Re-confirm reachability with the password. Same ConnectTimeout
    # as the original preflight. If the password is wrong, the user
    # sees the same "cannot ssh" error they would have without the
    # password fallback — no new error surface to learn.
    if ! ssh -o ConnectTimeout=5 "$PI_HOST" true; then
        echo "❌ cannot ssh to $PI_HOST (publickey and password both failed)" >&2
        echo "   pass PI_HOST as \$1 or set LINDSAY50_PI_HOST env var." >&2
        echo "   (root SSH is assumed because /srv/lindsay-50/ is only writable as root.)" >&2
        return 1
    fi

    echo "==> password auth confirmed; proceeding via SSH_ASKPASS"
    return 0
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
    # Publickey preflight failed — fall back to interactive password
    # auth if we're on a TTY. The helper sets up SSH_ASKPASS for the
    # remaining ssh/sftp invocations and re-runs the preflight to
    # confirm the password works.
    setup_password_auth || exit 1
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

# 2. Fetch the laptop's ref into the Pi's repo. We do NOT
#    `git checkout -f` here because:
#      - On a fresh non-bare clone (post step 1), checkout would
#        work but the working tree gets overwritten by setup-pi.sh's
#        bare conversion in Phase 3 anyway — wasted work.
#      - On an already-bootstrapped bare repo (the common re-run
#        case), bare repos reject checkout with "this operation must
#        be run in a work tree" — fatal, breaking the re-run.
#    The active version is controlled by the `current` symlink,
#    which setup-pi.sh manages. All this step needs to do is bring
#    in the laptop's commit so the bare repo's refdb has it before
#    setup-pi.sh runs `git worktree add ... $GIT_REF`.
echo "==> fetching refs from origin on the Pi"
ssh "$PI_HOST" "cd '$PI_REPO_DIR' && git fetch origin '+refs/heads/*:refs/remotes/origin/*'"

# 3. Ship the local settings.toml onto the Pi. We use sftp (not scp)
#    because scp doesn't honor SSH_ASKPASS reliably across OpenSSH
#    versions, and we need the password path to work consistently.
#    Write a one-line batch file to /tmp, point sftp at it, and
#    `mv` into place on the Pi side to avoid a partial-file overwrite
#    if the connection drops mid-transfer.
#
#    On the publickey path, SSH_ASKPASS is unset, so this is just a
#    sftp call. On the password path, SSH_ASKPASS is set by
#    setup_password_auth and sftp picks it up automatically.
echo "==> shipping settings.toml → $PI_HOST:$PI_REPO_DIR/heart-matrix-controller/"
SFTP_BATCH="$(mktemp -t lindsay-50-sftp.XXXXXX)"
# Augment the existing EXIT/INT/TERM/HUP trap (set up by
# setup_password_auth, or a no-op if publickey is in use) to also
# clean up the sftp batch file. Re-registering the trap with a
# combined command avoids overwriting the password-cleanup trap.
trap "rm -f '$SFTP_BATCH'; rm -rf '$PW_TMPDIR'" EXIT INT TERM HUP
printf 'put %s %s/heart-matrix-controller/settings.toml.tmp\n' \
    "$LOCAL_SETTINGS" "$PI_REPO_DIR" > "$SFTP_BATCH"
sftp -b "$SFTP_BATCH" "$PI_HOST"
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
