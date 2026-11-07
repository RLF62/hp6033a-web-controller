#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ( "$1" != protected && "$1" != password-free && "$1" != dashboard ) ]]; then
    echo "Usage: sudo bash $0 {dashboard|protected|password-free}" >&2
    exit 2
fi
if (( EUID != 0 )); then
    echo "Run this command with sudo." >&2
    exit 1
fi

mode=$1
deploy=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
site=/etc/nginx/sites-available/hp6033a
enabled=/etc/nginx/sites-enabled/hp6033a
exec 9>/run/lock/hp6033a-control-auth.lock
flock -x 9

if [[ ! -f "$site" || -L "$site" || ! -L "$enabled" || "$(readlink -f "$enabled")" != "$site" ]]; then
    echo "Expected the installed HP 6033A Nginx site and enablement symlink." >&2
    exit 1
fi
nginx -t
systemctl is-active --quiet nginx.service

app_dir=$(systemctl show hp6033a.service --property=WorkingDirectory --value)
app_user=$(systemctl show hp6033a.service --property=User --value)
if [[ -z "$app_dir" || -z "$app_user" ]] ||
   ! grep -q '^def control_password_required():' "$app_dir/app.py"; then
    echo "Install the updated HP 6033A application before changing the mode." >&2
    exit 1
fi

if [[ "$mode" == dashboard ]] &&
   ! grep -q '^def set_dashboard_control_auth():' "$app_dir/app.py"; then
    echo "Install the dashboard authentication update first." >&2
    exit 1
fi

work=$(mktemp -d /etc/nginx/.hp6033a-auth.XXXXXX)
replaced=false
cleanup() {
    local result=$?
    trap - EXIT
    if [[ "$replaced" == true && "$result" -ne 0 ]]; then
        echo "Restoring the previous Nginx configuration." >&2
        if ! mv -fT -- "$work/previous" "$site" ||
           ! nginx -t || ! systemctl reload nginx.service; then
            echo "Automatic recovery failed. Inspect $site and $work." >&2
            exit 1
        fi
    fi
    rm -f -- "$work"/{previous,candidate,test.conf}
    rmdir -- "$work"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

cp -p -- "$site" "$work/previous"
cp -p -- "$site" "$work/candidate"
python3 "$deploy/render-control-auth.py" "$mode" "$site" > "$work/candidate"
cat > "$work/test.conf" <<EOF
events {}
http {
    include /etc/nginx/mime.types;
    include $work/candidate;
}
EOF
nginx -t -c "$work/test.conf"

if [[ "$mode" != password-free ]]; then
    app_auth="$app_dir/.control_htpasswd"
    nginx_auth=/etc/nginx/hp6033a.htpasswd
    if [[ ! -e "$app_auth" && ! -L "$app_auth" &&
          ! -e "$nginx_auth" && ! -L "$nginx_auth" ]]; then
        bash "$deploy/setup-auth.sh" "$app_dir" "$app_user"
    fi
    if [[ ! -s "$app_auth" || ! -s "$nginx_auth" ]] ||
       ! cmp -s -- "$app_auth" "$nginx_auth" ||
       ! runuser -u "$app_user" -- test -r "$app_auth" ||
       ! runuser -u www-data -- test -r "$nginx_auth"; then
        echo "Both matching authentication files must be present and readable by their services." >&2
        exit 1
    fi
else
    echo "Password-free control requires a trusted LAN without internet exposure."
fi

replaced=true
mv -fT -- "$work/candidate" "$site"
nginx -t
systemctl reload nginx.service
if [[ "$mode" == dashboard ]]; then
    echo "Dashboard authentication setting enabled. Reload Classic and Modern."
else
    echo "Control authentication mode: $mode. Reload open dashboards to pick up the change."
fi
