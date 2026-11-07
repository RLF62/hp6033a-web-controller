#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/home/pi/hp6033a}"
APP_USER="${2:-pi}"
APP_GROUP="$(id -gn "$APP_USER")"
APP_AUTH="$APP_DIR/.control_htpasswd"
NGINX_AUTH="/etc/nginx/hp6033a.htpasswd"

if [[ -e "$APP_AUTH" || -L "$APP_AUTH" || -e "$NGINX_AUTH" || -L "$NGINX_AUTH" ]]; then
    echo "Existing authentication files detected."
    echo "Refusing to overwrite credentials."
    exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
    echo "Application directory does not exist: $APP_DIR" >&2
    exit 1
fi

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    echo "Python virtual environment is missing." >&2
    exit 1
fi

read -r -p "Controller username: " USERNAME
if [[ -z "$USERNAME" || "$USERNAME" == *:* ]]; then
    echo "Invalid username." >&2
    exit 1
fi

"$APP_DIR/venv/bin/python" - "$USERNAME" "$APP_AUTH" <<'PY'
import bcrypt
import getpass
import os
import sys

username, destination = sys.argv[1:]
password = getpass.getpass("Controller password: ")
confirm = getpass.getpass("Confirm password: ")

if not password or password != confirm:
    sys.exit("Passwords do not match or are empty.")

hashed = bcrypt.hashpw(
    password.encode("utf-8"),
    bcrypt.gensalt()
).decode("ascii")

fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"{username}:{hashed}\n")
PY

sudo chown "$APP_USER:$APP_GROUP" "$APP_AUTH"
sudo chmod 600 "$APP_AUTH"

if ! sudo install -o root -g www-data -m 0640 \
    "$APP_AUTH" "$NGINX_AUTH"; then
    echo "ERROR: Nginx authentication setup failed." >&2
    echo "The Flask credential was preserved for recovery." >&2
    exit 1
fi

echo "Authentication files created successfully."
