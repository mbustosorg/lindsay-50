#!/usr/bin/env bash
# Launch the heart-matrix-controller (64x64 LED panel display) on the Raspberry Pi.
# Invoked by lindsay_50.service. Runs as root (the rgbmatrix library needs GPIO).
#
# Issue #49: this script now exec's loader.py — the systemd entrypoint that
# handles the blue/green upgrade flow (see heart-matrix-controller/loader.py).
# loader.py then os.execvp's main.py once the right version is staged and
# health-checked. systemd sees main.py as the direct child so signal
# handling is preserved.
set -e

# Where this repo is cloned on the Pi — adjust if yours differs.
REPO_DIR="/home/pi/projects/lindsay-50"

# Run from the repo root so config_reader (called by both loader.py and
# healthcheck.py) finds settings.toml in $REPO_DIR/heart-matrix-controller/.
cd "$REPO_DIR"

# Activate the repo-root venv (created per CLAUDE.md: python3 -m venv .venv).
. "/home/mauricio/.virtualenvs/lindsay-50/bin/activate"

# lib_shared lives at the repo root; LOG_LEVEL is read by both loader.py and main.py.
export PYTHONPATH="$REPO_DIR"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

exec python3 "$REPO_DIR/current/heart-matrix-controller/loader.py"