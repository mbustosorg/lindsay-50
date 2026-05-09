#!/bin/bash
# Start the Flask dev server and optionally the local service containers.
# Usage: ./start-app.sh [--with-services]
#   --with-services  Also start MinIO (S3) and Mosquitto (MQTT) if not already running.
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
SETTINGS_FILE="$PROJECT_ROOT/heart-sms-receiver/settings.toml"
if [ ! -f "$SETTINGS_FILE" ]; then
    echo "Error: settings.toml not found. Copy settings.toml.example first:"
    echo "  cp heart-sms-receiver/settings.toml.example heart-sms-receiver/settings.toml"
    exit 1
fi

# Validate required settings
SETTINGS_PATH="$SETTINGS_FILE" python3 - <<PYEOF
import tomllib, sys, os
with open(os.environ["SETTINGS_PATH"], "rb") as f:
    cfg = tomllib.load(f)

required = ["AIO_USERNAME", "AIO_KEY", "AIO_FEED", "S3_BUCKET"]
missing = [k for k in required if not cfg.get(k)]
if missing:
    print(f"Error: missing required settings: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
print("Config validated OK")
PYEOF

# Optionally start Docker services
START_SERVICES=false
for arg in "$@"; do
    if [ "$arg" = "--with-services" ]; then
        START_SERVICES=true
    fi
done

if [ "$START_SERVICES" = "true" ]; then
    # MinIO (S3-compatible)
    if ! docker ps --filter "name=minio-local" --format "{{.Names}}" | grep -q minio-local; then
        echo "Starting MinIO..."
        docker run -d --name minio-local \
            -p 9000:9000 -p 9001:9001 \
            -e MINIO_ROOT_USER=minioadmin \
            -e MINIO_ROOT_PASSWORD=minioadmin \
            minio/minio server /data --console-address ':9001'
        echo "MinIO started at http://localhost:9000 (console: http://localhost:9001)"
    else
        echo "MinIO already running"
    fi

    # Mosquitto (MQTT broker)
    if ! docker ps --filter "name=mosquitto-local" --format "{{.Names}}" | grep -q mosquitto-local; then
        echo "Starting Mosquitto..."
        MOSQUITTO_CONF=$(mktemp)
        printf 'listener 1883\nallow_anonymous true\n' > "$MOSQUITTO_CONF"
        docker run -d --name mosquitto-local \
            -p 1883:1883 \
            -v "$MOSQUITTO_CONF:/mosquitto.conf" \
            eclipse-mosquitto \
            mosquitto -c /mosquitto.conf
        echo "Mosquitto started on port 1883"
    else
        echo "Mosquitto already running"
    fi
fi

echo "Starting Flask server on http://0.0.0.0:5001 ..."
cd "$PROJECT_ROOT"
export FLASK_APP=heart-sms-receiver/main.py
# Disable reloader to avoid double-subscriber issues in debug mode
export FLASK_RUN_RELOAD=False
exec flask run --host=0.0.0.0 --port=5001 --debug
