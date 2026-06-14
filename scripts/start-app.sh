#!/bin/bash
# Start the Flask dev server and optionally the local service containers.
# Usage: ./start-app.sh [--flask-only]
#   --flask-only  Skip MinIO (S3) and Mosquitto (MQTT) containers.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Activate venv
if [ -d "$PROJECT_ROOT/.venv" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
else
    echo "Error: .venv not found. Run: python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# Check settings.toml exists
SETTINGS_FILE="$PROJECT_ROOT/heart-message-manager/settings.toml"
if [ ! -f "$SETTINGS_FILE" ]; then
    echo "Error: settings.toml not found. Copy settings.toml.example first:"
    echo "  cp heart-message-manager/settings.toml.example heart-message-manager/settings.toml"
    exit 1
fi

# Optionally start Docker services (enabled by default)
START_SERVICES=true
for arg in "$@"; do
    if [ "$arg" = "--flask-only" ]; then
        START_SERVICES=false
    fi
done

if [ "$START_SERVICES" = "true" ]; then
    # MinIO (S3-compatible)
    if docker ps -a --filter "name=minio-local" --format "{{.Names}}" | grep -q minio-local; then
        if docker ps --filter "name=minio-local" --format "{{.Names}}" | grep -q minio-local; then
            echo "MinIO already running"
        else
            echo "Starting existing MinIO container..."
            docker start minio-local
            echo "MinIO started at http://localhost:9000 (console: http://localhost:9001)"
        fi
    else
        echo "Starting MinIO..."
        docker run -d --name minio-local \
            -p 9000:9000 -p 9001:9001 \
            -e MINIO_ROOT_USER=minioadmin \
            -e MINIO_ROOT_PASSWORD=minioadmin \
            minio/minio server /data --console-address ':9001'
        echo "MinIO started at http://localhost:9000 (console: http://localhost:9001)"
    fi

    # Wait for MinIO to be ready, then create the bucket
    echo "Checking AWS_S3_BUCKET..."
    AWS_S3_BUCKET=$(SETTINGS_PATH="$SETTINGS_FILE" python3 - <<'PYEOF'
import tomllib, sys, os
with open(os.environ["SETTINGS_PATH"], "rb") as f:
    cfg = tomllib.load(f)
print(cfg.get("AWS_S3_BUCKET") or "")
PYEOF
)

    if [ -z "$AWS_S3_BUCKET" ]; then
        echo "AWS_S3_BUCKET not configured; skipping bucket creation"
    else
        # Install mc if needed
        if ! command -v mc &> /dev/null; then
            echo "Installing MinIO client (mc)..."
            if [ "$(uname)" = "Darwin" ]; then
                brew install minio-mc 2>/dev/null || brew install minio-client
            else
                curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc && chmod +x /usr/local/bin/mc
            fi
        fi

        # Configure mc alias for local MinIO
        mc alias set local http://localhost:9000 minioadmin minioadmin 2>/dev/null || true

        # Create bucket if it doesn't exist
        if mc ls local/"$AWS_S3_BUCKET" &>/dev/null; then
            echo "S3 bucket '$AWS_S3_BUCKET' already exists"
        else
            echo "Creating S3 bucket '$AWS_S3_BUCKET'..."
            mc mb local/"$AWS_S3_BUCKET"
            echo "Bucket '$AWS_S3_BUCKET' created"
        fi
    fi

    # Mosquitto (MQTT broker). Container listens on 1883 (plain MQTT, for the
    # device) and 9001 (MQTT-over-WebSocket, for the browser's MqttWsClient).
    # The 9001 container port is mapped to HOST port 9002 to avoid colliding
    # with MinIO's console (which already uses host 9001). The Flask app's
    # _derive_mqtt_ws_url() returns ws://<host>:9002/mqtt for local dev.
    # Both listeners accept anonymous connections for local dev — the
    # browser's MqttWsClient + js.fetch layer is what actually authenticates
    # the session, not the broker itself.
    #
    # Uses the `latest` tag (currently eclipse-mosquitto:2.1.2). 2.1.0
    # added built-in websockets support (no libwebsockets build flag
    # needed); if the browser's WS handshake fails after a future image
    # bump, check the 2.1.x mosquitto.conf syntax for the websocket
    # listener before pinning.
    if docker ps -a --filter "name=mosquitto-local" --format "{{.Names}}" | grep -q mosquitto-local; then
        if docker ps --filter "name=mosquitto-local" --format "{{.Names}}" | grep -q mosquitto-local; then
            echo "Mosquitto already running"
        else
            echo "Recreating stale Mosquitto container..."
            docker rm mosquitto-local
            MOSQUITTO_CONF=$(mktemp)
            printf 'listener 1883\nlistener 9001\nprotocol websockets\nallow_anonymous true\n' > "$MOSQUITTO_CONF"
            docker run -d --name mosquitto-local \
                -p 1883:1883 \
                -p 9002:9001 \
                -v "$MOSQUITTO_CONF:/mosquitto.conf" \
                eclipse-mosquitto \
                mosquitto -c /mosquitto.conf
            echo "Mosquitto started on port 1883 (MQTT) and 9002→9001 (MQTT-over-WebSocket)"
        fi
    else
        echo "Starting Mosquitto..."
        MOSQUITTO_CONF=$(mktemp)
        printf 'listener 1883\nlistener 9001\nprotocol websockets\nallow_anonymous true\n' > "$MOSQUITTO_CONF"
        docker run -d --name mosquitto-local \
            -p 1883:1883 \
            -p 9002:9001 \
            -v "$MOSQUITTO_CONF:/mosquitto.conf" \
            eclipse-mosquitto \
            mosquitto -c /mosquitto.conf
        echo "Mosquitto started on port 1883 (MQTT) and 9002→9001 (MQTT-over-WebSocket)"
    fi
fi

# Get port from settings.toml (default 6000)
SERVER_PORT=$(SETTINGS_PATH="$SETTINGS_FILE" python3 - <<'PYEOF'
import tomllib, sys, os
with open(os.environ["SETTINGS_PATH"], "rb") as f:
    cfg = tomllib.load(f)
print(cfg.get("PORT", 6000))
PYEOF
)

echo "Starting gunicorn Flask server on http://0.0.0.0:$SERVER_PORT ..."
cd "$PROJECT_ROOT/heart-message-manager"
exec gunicorn main:app --bind "0.0.0.0:$SERVER_PORT" --worker-class=gthread
