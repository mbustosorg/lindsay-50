#!/usr/bin/env bash
# Launch the heart-matrix-controller (64x64 LED panel display) on the Raspberry Pi.
# Invoked by lindsay_50.service. Runs as root (the rgbmatrix library needs GPIO).
#
# Runs main.py directly. To update the Pi's code, re-run
# scripts/setup-pi.sh on the Pi (or scripts/provision-pi.sh from a
# laptop). No auto-upgrade machinery.
set -e

# Where this repo is cloned on the Pi — adjust if yours differs.
REPO_DIR="${REPO_DIR:-/srv/lindsay-50}"

# Run from heart-matrix-controller/ so config_reader finds settings.toml
# in cwd. PYTHONPATH points at $REPO_DIR so `import lib_shared` resolves.
cd "$REPO_DIR/heart-matrix-controller"
export PYTHONPATH="$REPO_DIR"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# System Python with rgbmatrix installed via setup-pi.sh. (No venv on this
# single-purpose Pi — keeps the install trivial.)
exec python3 "$REPO_DIR/heart-matrix-controller/main.py"