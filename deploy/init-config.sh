#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/home/pi/hp6033a}"
CONFIG="$APP_DIR/config.json"
EXAMPLE="$APP_DIR/config.example.json"

if [[ ! -f "$EXAMPLE" ]]; then
    echo "ERROR: Missing $EXAMPLE" >&2
    exit 1
fi

if [[ -e "$CONFIG" ]]; then
    echo "Existing configuration preserved: $CONFIG"
    exit 0
fi

install -m 0600 "$EXAMPLE" "$CONFIG"
echo "Created: $CONFIG"
echo "IMPORTANT: Configure gpib_resource before starting the controller."
