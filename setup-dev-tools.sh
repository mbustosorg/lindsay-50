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
