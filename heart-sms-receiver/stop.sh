#!/bin/bash
# Stop the Flask dev server.
# Usage: ./stop.sh
pkill -f "flask run.*0.0.0.0:5001" 2>/dev/null || true
echo "Flask server stopped"
