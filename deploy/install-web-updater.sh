#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )) || (( $# != 2 )); then
    echo "Usage: sudo bash deploy/install-web-updater.sh CONTROLLER_USER HTTPS_URL" >&2
    exit 2
fi

account=$1
url=$2
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
program=/usr/local/lib/hp6033a-update
state=/var/lib/hp6033a-update
unit_dir=/etc/systemd/system

if [[ -e "$state/config.json" || -e "$unit_dir/hp6033a-update.service" ||
      -e "$unit_dir/hp6033a-update.socket" ]]; then
    echo "A software update helper is already provisioned or needs recovery." >&2
    exit 1
fi
app=$(systemctl show hp6033a.service --property=WorkingDirectory --value)
running_user=$(systemctl show hp6033a.service --property=User --value)
if [[ -z "$app" || "$running_user" != "$account" ]] ||
   ! systemctl is-active --quiet hp6033a.service ||
   ! grep -q '/api/software-updates' "$app/app.py"; then
    echo "Install and start the compatible controller application first." >&2
    exit 1
fi
if [[ $(grep -c 'proxy_set_header X-HP6033A-Password-Free 2;' /etc/nginx/sites-enabled/hp6033a) != 2 ]]; then
    echo "Enable dashboard-managed authentication before provisioning the helper." >&2
    exit 1
fi

install -d -o root -g root -m 0755 "$program"
install -d -o root -g root -m 0700 "$state"
install -o root -g root -m 0644 "$source_dir/web_update.py" "$source_dir/update.py" "$program/"
/usr/bin/python3 -E -s -B "$program/web_update.py" configure "$account" "$url"
install -o root -g root -m 0644 "$source_dir/hp6033a-update.service" "$source_dir/hp6033a-update.socket" "$unit_dir/"
systemd-analyze verify "$unit_dir/hp6033a-update.service" "$unit_dir/hp6033a-update.socket"
systemctl daemon-reload
systemctl enable --now hp6033a-update.socket hp6033a-update.service
systemctl is-active --quiet hp6033a-update.service
echo "Software Updates is available in both dashboard Setup sections."
