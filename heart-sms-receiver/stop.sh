#!/bin/bash
# Stop the Flask dev server and optionally local service containers.
# Usage: ./stop.sh [--with-services]
#   --with-services  Also stop MinIO and Mosquitto containers.
set -e

echo "Stopping Flask..."
pkill -f "flask run" 2>/dev/null && echo "Flask stopped" || echo "Flask not running"

if [ "$1" = "--with-services" ]; then
    echo "Stopping Docker services..."
    docker stop minio-local 2>/dev/null && echo "MinIO stopped" || echo "MinIO not running"
    docker stop mosquitto-local 2>/dev/null && echo "Mosquitto stopped" || echo "Mosquitto not running"
    docker rm minio-local 2>/dev/null
    docker rm mosquitto-local 2>/dev/null
fi
