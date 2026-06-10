#!/usr/bin/env bash
# Launch the heart-matrix-controller (64x64 LED panel display) on the Raspberry Pi.
# Invoked by lindsay_50.service. Runs as root (the rgbmatrix library needs GPIO).
set -e

# Where this repo is cloned on the Pi — adjust if yours differs.
REPO_DIR="/home/pi/projects/lindsay-50"

# Run from the controller dir so settings.toml and the relative FONT_PATH resolve.
cd "$REPO_DIR/heart-matrix-controller"

# Activate the repo-root venv (created per CLAUDE.md: python3 -m venv .venv).
. "$REPO_DIR/.venv/bin/activate"

# lib_shared lives at the repo root; LOG_LEVEL is read by main.py.
export PYTHONPATH="$REPO_DIR"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

exec python3 main.py
