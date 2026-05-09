#!/bin/bash
# Start the Flask dev server.
# Usage: ./start.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Activate venv
if [ -d "$PROJECT_ROOT/.venv" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
else
    echo "Error: .venv not found. Run: python -m venv .venv && pip install -r heart-sms-receiver/requirements.txt"
    exit 1
fi

# Check settings.toml exists
if [ ! -f "$SCRIPT_DIR/settings.toml" ]; then
    echo "Error: settings.toml not found. Copy settings.toml.example first:"
    echo "  cp heart-sms-receiver/settings.toml.example heart-sms-receiver/settings.toml"
    exit 1
fi

# Validate required settings
python3 - <<'PYEOF'
import tomllib, sys
with open("heart-sms-receiver/settings.toml", "rb") as f:
    cfg = tomllib.load(f)

required = ["AIO_USERNAME", "AIO_KEY", "AIO_FEED", "S3_BUCKET"]
missing = [k for k in required if not cfg.get(k)]
if missing:
    print(f"Error: missing required settings: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
print("Config validated OK")
PYEOF

echo "Starting Flask server on http://0.0.0.0:5000 ..."
cd "$PROJECT_ROOT"
exec flask run --host=0.0.0.0 --port=5000
