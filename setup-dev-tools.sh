#!/bin/bash
# Installs OpenSpec, agent-orchestrator, and GitHub CLI.
#
# NOTE: agent-orchestrator is configured and runs OUTSIDE any single project repo.
# Its config lives at ~/.agent-orchestrator/agent-orchestrator.yaml and can manage
# multiple projects simultaneously. See ~/.agent-orchestrator/setup-ao.sh for adding
# projects to the AO dashboard.
set -e

PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$PROJECT_ROOT" ]; then
  echo "Error: Not a git repository."
  exit 1
fi
cd "$PROJECT_ROOT"

echo "=== Seeding project with dev tools ==="

# --- Python venv + deps ---
echo ""
echo "--- Python venv + deps ---"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  echo "Created .venv"
else
  echo "Using existing .venv"
fi

# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip

if [ -f "heart-sms-receiver/requirements.txt" ]; then
  pip install -q -r heart-sms-receiver/requirements.txt
  echo "Installed heart-sms-receiver deps"
fi

if [ -d "tests" ]; then
  pip install -q pytest
  echo "Installed pytest"
fi

# --- VS Code + Pyright LSP ---
echo ""
echo "--- VS Code LSP setup ---"
mkdir -p .vscode
cat > .vscode/settings.json <<'VSCODE'
{
  "python.analysis.extraPaths": ["."],
  "python.analysis.pythonVersion": "3.12",
  "python.analysis.typeCheckingMode": "basic",
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python"
}
VSCODE
echo "Created .vscode/settings.json"

if ! command -v pyright &> /dev/null && ! python -m pyright --version &> /dev/null; then
  pip install -q pyright
fi
echo "Pyright available in .venv"

# --- OpenSpec project rules ---
echo ""
echo "--- OpenSpec project rules ---"
if [ -f "openspec/config.yaml" ]; then
  # Ensure rules.tasks and rules.test are present
  if ! grep -q "rules:" openspec/config.yaml; then
    cat >> openspec/config.yaml <<'OPENSPEC'

# Per-artifact rules
rules:
  tasks:
    - Every feature task must have a corresponding test task
    - Mark each task done in tasks.md with `- [x]` when complete
  test:
    - New functions/modules must have pytest tests covering happy path, edge cases, and errors
    - Run `PYTHONPATH=. pytest tests/ -v` before pushing; fix all failures first
OPENSPEC
    echo "Added rules to openspec/config.yaml"
  else
    echo "OpenSpec rules already present"
  fi
fi

echo ""

# --- Node.js ---
if ! command -v node &> /dev/null; then
  echo "Installing Node.js..."
  if [ "$(uname)" = "Darwin" ]; then
    brew install node
  elif [ -f /etc/debian_version ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt install -y nodejs
  else
    echo "Unsupported OS. Install Node.js manually: https://nodejs.org"
    exit 1
  fi
else
  echo "Node.js already installed: $(node -v)"
fi

NODE_VERSION=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
if [ "$NODE_VERSION" -lt 20 ]; then
  echo "Error: Node.js 20+ is required (found v$(node -v))"
  exit 1
fi

# --- Git ---
if ! command -v git &> /dev/null; then
  echo "Installing Git..."
  if [ "$(uname)" = "Darwin" ]; then
    brew install git
  elif [ -f /etc/debian_version ]; then
    apt install -y git
  else
    echo "Unsupported OS. Install Git manually."
    exit 1
  fi
else
  echo "Git already installed: $(git --version)"
fi

# --- OpenSpec ---
echo ""
echo "--- OpenSpec ---"
npm install -g @fission-ai/openspec@latest
if [ ! -d ".openspec" ]; then
  openspec init
  echo "OpenSpec initialized"
else
  echo "OpenSpec already initialized"
fi

# --- agent-orchestrator ---
# AO runs from ~/.agent-orchestrator/ and manages multiple repos — not per-project.
# Install globally; use ~/.agent-orchestrator/setup-ao.sh to add this repo to the dashboard.
echo ""
echo "--- agent-orchestrator ---"
npm install -g @aoagents/ao@latest

AO_CONFIG="$HOME/.agent-orchestrator/agent-orchestrator.yaml"
if [ ! -f "$AO_CONFIG" ]; then
  echo "No AO config found at $AO_CONFIG."
  echo "Run '~/.agent-orchestrator/setup-ao.sh' after this to add projects to the dashboard."
else
  echo "AO config found — this repo will be available when you run 'cd ~/.agent-orchestrator && ao start'"
fi

# --- GitHub CLI ---
echo ""
echo "--- GitHub CLI ---"
if ! command -v gh &> /dev/null; then
  echo "Installing GitHub CLI..."
  if [ "$(uname)" = "Darwin" ]; then
    brew install gh
  elif [ -f /etc/debian_version ]; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt update && apt install gh
  else
    echo "Unsupported OS. Install gh manually: https://cli.github.com"
  fi
else
  echo "GitHub CLI already installed: $(gh --version)"
fi

if ! gh auth status &> /dev/null; then
  echo "Authenticating GitHub CLI..."
  gh auth login
else
  echo "GitHub CLI already authenticated"
fi

echo ""
echo "=== Done! ==="
echo ""
echo "Next steps:"
echo "  1. ~/.agent-orchestrator/setup-ao.sh   # add this repo to AO dashboard"
echo "  2. cd ~/.agent-orchestrator && ao start  # launch AO dashboard"
echo "  3. Skills /os-new, /os-propose, /os-spawn are in .claude/skills/ — already available"

# --- Local dev services (optional) ---
echo ""
echo "=== Local dev services (optional) ==="
echo ""
echo "MinIO (S3-compatible, for local S3 testing):"
echo "  docker run -d --name minio-local -p 9000:9000 -p 9001:9001 \\"
echo "    -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \\"
echo "    minio/minio server /data --console-address ':9001'"
echo "  # Then set in settings.toml:"
echo "  S3_ENDPOINT_URL = 'http://localhost:9000'"
echo "  AWS_ACCESS_KEY_ID = 'minioadmin'"
echo "  AWS_SECRET_ACCESS_KEY = 'minioadmin'"
echo ""
echo "Mosquitto (local MQTT broker for local dev testing):"
echo "  docker run -d --name mosquitto-local -p 1883:1883 \\"
echo "    eclipse-mosquitto mosquitto.conf # (see below for config)"
echo "  # Then set in settings.toml:"
echo "  MQTT_HOST = 'localhost'"
echo "  MQTT_PORT = 1883"
echo "  MQTT_USERNAME = 'test-user'   # (can be any value for local mosquitto)"
echo "  MQTT_PASSWORD = 'test-key'    # (can be any value for local mosquitto)"
echo ""
echo "To test Flask locally:"
echo "  cp heart-sms-receiver/settings.toml.example heart-sms-receiver/settings.toml"
echo "  # edit settings.toml with your values (MinIO credentials are pre-filled)"
echo "  ./heart-sms-receiver/start.sh"
echo ""
echo "To test MQTT locally (Python subscriber):"
echo "  pip install paho-mqtt"
echo "  python3 scripts/mqtt-subscriber.py"
