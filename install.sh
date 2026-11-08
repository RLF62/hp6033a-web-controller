#!/usr/bin/env bash
set -euo pipefail

# HP 6033A Web Controller
# Installer preflight checks
# Hardware interface: Xyphro UsbGpib V2
# https://github.com/xyphro/UsbGpib

APP_USER="$(id -un)"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
if [[ -z "$APP_HOME" || ! -d "$APP_HOME" ]]; then
    echo "ERROR: Cannot determine application user's home directory." >&2
    exit 1
fi
APP_DIR="$APP_HOME/hp6033a"

NGINX_PREEXISTING=false

# Installation functions are defined below.
# The main installation sequence will be added after review.

install_dependencies() {
    echo "Installing system dependencies..."

    sudo apt-get update || return 1
    sudo apt-get install -y \
        python3-venv \
        python3-pip \
        nginx \
        apache2-utils \
        libusb-1.0-0 \
        openssl || return 1

    echo "Creating Python virtual environment..."
    python3 -m venv "$APP_DIR/venv" || return 1

    echo "Installing Python dependencies..."
    "$APP_DIR/venv/bin/python" -m pip install \
        -r "$APP_DIR/requirements.txt" || return 1

    echo "Python dependencies installed successfully."
}

install_application() {
    echo "Installing HP 6033A application files..."

    SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # Never overwrite an existing installation.
    if [[ -e "$APP_DIR" ]]; then
        echo "ERROR: Installation directory already exists: $APP_DIR" >&2
        return 1
    fi

    # Check required files before creating anything.
    for item in app.py requirements.txt config.example.json templates static docs deploy; do
        if [[ ! -e "$SOURCE_DIR/$item" ]]; then
            echo "ERROR: Missing required file: $item" >&2
            return 1
        fi
    done

    mkdir "$APP_DIR" || return 1
    APP_CREATED=true

    cp "$SOURCE_DIR/app.py" \
       "$SOURCE_DIR/requirements.txt" \
       "$SOURCE_DIR/config.example.json" \
       "$APP_DIR/" || return 1

    cp -a "$SOURCE_DIR/templates" \
          "$SOURCE_DIR/static" \
          "$SOURCE_DIR/docs" \
          "$SOURCE_DIR/deploy" \
          "$APP_DIR/" || return 1

    record_install_release || return 1
    echo "Application files installed in $APP_DIR"
}

record_install_release() {
    local top release commit
    top="$(git -C "$SOURCE_DIR" rev-parse --show-toplevel 2>/dev/null)" || return 0
    [[ "$top" == "$SOURCE_DIR" ]] || return 0
    [[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || return 0
    release="$(git -C "$SOURCE_DIR" describe --tags --exact-match HEAD 2>/dev/null)" || return 0
    [[ "$release" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || return 0
    commit="$(git -C "$SOURCE_DIR" rev-parse HEAD)" || return 0
    ( umask 077; printf '{"release":"%s","commit":"%s"}\n' "$release" "$commit" > "$APP_DIR/.hp6033a-version.json" )
}


initialize_configuration() {
    echo "Initializing HP 6033A configuration..."

    local config_file="$APP_DIR/config.json"

    if [[ -e "$config_file" ]]; then
        echo "ERROR: Configuration already exists." >&2
        return 1
    fi

    install -m 600 \
        "$APP_DIR/config.example.json" \
        "$config_file"

    echo "Configuration created successfully."
    echo
    echo "IMPORTANT:"
    echo "Configure your USB-GPIB resource address before"
    echo "attempting communication with the HP 6033A."
    echo
    echo "Remote output control remains disabled by default."
}

configure_usbgpib() {
    echo "Configuring USB-GPIB permissions..."

    local rules_source="$APP_DIR/deploy/99-usbgpib.rules"
    local rules_target="/etc/udev/rules.d/99-usbgpib.rules"

    if [[ ! -f "$rules_source" ]]; then
        echo "ERROR: USB-GPIB rules file is missing." >&2
        return 1
    fi

    # Ensure the plugdev group exists.
    if ! getent group plugdev >/dev/null; then
        sudo groupadd --system plugdev
    fi

    # Give the application user access to the USB adapter.
    if ! id -nG "$APP_USER" | grep -qw plugdev; then
        sudo usermod -aG plugdev "$APP_USER"
        echo "Added $APP_USER to plugdev."
        echo "A logout/login or reboot will be required."
    else
        echo "$APP_USER already belongs to plugdev."
    fi

    # Never overwrite existing USB device rules.
    if [[ -e "$rules_target" ]]; then
        if ! sudo cmp -s "$rules_source" "$rules_target"; then
            echo "ERROR: Existing USB-GPIB rules differ." >&2
            echo "Manual review required: $rules_target" >&2
            return 1
        fi
        echo "Matching USB-GPIB rules already installed."
    else
        sudo install -m 644 "$rules_source" "$rules_target"
        echo "USB-GPIB rules installed."
    fi

    sudo udevadm control --reload-rules

    echo "USB-GPIB permissions configured."
    echo "Reconnect the adapter after installation if necessary."
}

install_systemd_service() {
    echo "Installing HP 6033A systemd service..."

    local service_source="$APP_DIR/deploy/hp6033a.service.example"
    local service_target="/etc/systemd/system/hp6033a.service"

    if [[ ! -f "$service_source" ]]; then
        echo "ERROR: systemd service template is missing." >&2
        return 1
    fi

    if [[ -e "$service_target" ]] ||        [[ "$(systemctl show -p LoadState --value hp6033a.service)" != "not-found" ]]; then
        echo "ERROR: An HP 6033A systemd service already exists." >&2
        echo "Existing services will not be overwritten." >&2
        return 1
    fi

    # Replace installation-specific paths and account names.
    sed \
        -e "s|/home/pi/hp6033a|$APP_DIR|g" \
        -e "s|User=pi|User=$APP_USER|g" \
        "$service_source" > "$APP_DIR/hp6033a.service.generated"

    sudo install -m 644 \
        "$APP_DIR/hp6033a.service.generated" \
        "$service_target"
    SERVICE_CREATED=true

    sudo systemctl daemon-reload

    echo "systemd service installed successfully."
    echo "The service has not been started or enabled."
}

configure_https() {
    echo "Configuring HTTPS certificate..."

    local ssl_dir="/etc/nginx/ssl"
    local cert="$ssl_dir/hp6033a.crt"
    local key="$ssl_dir/hp6033a.key"

    if [[ -e "$cert" || -e "$key" ]]; then
        echo "ERROR: An HP 6033A certificate or key already exists." >&2
        echo "Existing certificates will not be overwritten." >&2
        return 1
    fi

    if [[ -z "${CONTROLLER_HOST:-}" ]]; then
        echo "ERROR: CONTROLLER_HOST has not been configured." >&2
        return 1
    fi

    if ! command -v openssl >/dev/null 2>&1; then
        echo "ERROR: OpenSSL is not installed." >&2
        return 1
    fi

    local san
    if [[ "$CONTROLLER_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        san="IP:$CONTROLLER_HOST"
    else
        san="DNS:$CONTROLLER_HOST"
    fi

    sudo install -d -m 755 "$ssl_dir"

    CERT_CREATION_ATTEMPTED=true

    sudo openssl req -x509 -nodes -newkey rsa:3072 \
        -sha256 -days 825 \
        -keyout "$key" \
        -out "$cert" \
        -subj "/CN=$CONTROLLER_HOST" \
        -addext "subjectAltName=$san"

    sudo chmod 600 "$key"
    sudo chmod 644 "$cert"
    CERT_CREATED=true

    echo "HTTPS certificate created successfully."
    echo "Certificate host: $CONTROLLER_HOST"
}

configure_nginx() {
    echo "Preparing HP 6033A Nginx configuration..."

    local template="$APP_DIR/deploy/nginx.conf.example"
    local target="/etc/nginx/sites-available/hp6033a"
    local enabled="/etc/nginx/sites-enabled/hp6033a"
    local generated="$APP_DIR/hp6033a.nginx.generated"
    local renderer="$APP_DIR/deploy/render-control-auth.py"

    if [[ "$CONTROLLER_SCHEME" == http ]]; then
        template="$APP_DIR/deploy/nginx-http.conf.example"
    fi

    if [[ ! -f "$template" ]]; then
        echo "ERROR: Nginx template is missing." >&2
        return 1
    fi

    if [[ -e "$target" || -L "$target" || -e "$enabled" || -L "$enabled" ]]; then
        echo "ERROR: HP 6033A Nginx configuration already exists." >&2
        return 1
    fi

    if [[ -z "${CONTROLLER_HOST:-}" || -z "${LAN_SUBNET:-}" ]]; then
        echo "ERROR: Controller host or LAN subnet is not configured." >&2
        return 1
    fi

    {
        if [[ "$CONTROLLER_SCHEME" == https ]]; then
            python3 "$renderer" dashboard "$template"
        else
            cat "$template"
        fi
    } | sed \
        -e "s|CONTROLLER_HOST|$CONTROLLER_HOST|g" \
        -e "s|LAN_SUBNET|$LAN_SUBNET|g" > "$generated"

    if grep -Eq 'CONTROLLER_HOST|LAN_SUBNET' "$generated"; then
        echo "ERROR: Unresolved Nginx placeholders." >&2
        return 1
    fi

    echo "Nginx configuration generated:"
    echo "$generated"
    echo
    echo "Installation and activation will require a separate"
    echo "validation step before modifying Nginx."
}

verify_authentication() {
    echo "Verifying HP 6033A authentication..."

    local flask_auth="$APP_DIR/.control_htpasswd"
    local nginx_auth="/etc/nginx/hp6033a.htpasswd"

    if [[ ! -s "$flask_auth" ]]; then
        echo "ERROR: Flask authentication file is missing or empty." >&2
        return 1
    fi

    if ! sudo test -s "$nginx_auth"; then
        echo "ERROR: Nginx authentication file is missing or empty." >&2
        return 1
    fi

    if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
        echo "ERROR: Python virtual environment is missing." >&2
        return 1
    fi

    echo "Both authentication files are present."
    echo "Authentication preflight passed."
}

check_nginx_conflicts() {
    echo "Checking existing Nginx configuration..."

    if ! command -v nginx >/dev/null 2>&1; then
        echo "Nginx is not installed; it will be installed later."
        return 0
    fi

    if ! sudo nginx -t; then
        echo "ERROR: Existing Nginx configuration is invalid." >&2
        return 1
    fi

    # Conservative policy: require an unused Nginx installation.
    # Do not disable, replace, or modify existing websites.
    if sudo nginx -T 2>&1 | grep -Eq '^[[:space:]]*listen[[:space:]]+([^[:space:];]+:)?(80|443)([[:space:];]|$)'; then
        echo "ERROR: Nginx already has an active HTTP/HTTPS listener." >&2
        echo "Review existing websites before installing." >&2
        return 1
    fi

    echo "No active HTTP/HTTPS listeners detected."
}

validate_network_settings() {
    echo "Validating controller network settings..."

    if [[ -z "${CONTROLLER_HOST:-}" || -z "${LAN_SUBNET:-}" ]]; then
        echo "ERROR: Controller host and LAN subnet are required." >&2
        return 1
    fi

    if ! python3 - "$CONTROLLER_HOST" "$LAN_SUBNET" <<'PY'
import ipaddress
import re
import sys

host, subnet = sys.argv[1:]

try:
    network = ipaddress.ip_network(subnet, strict=False)
    if network.version != 4 or not network.is_private:
        raise ValueError("LAN subnet must be private IPv4")

    try:
        address = ipaddress.ip_address(host)
        if address.version != 4 or not address.is_private:
            raise ValueError("Controller IP must be private IPv4")
        if address not in network:
            raise ValueError(
                "Controller IP is outside the allowed LAN subnet"
            )
        if address in (network.network_address, network.broadcast_address):
            raise ValueError(
                "Controller IP cannot be the network or broadcast address"
            )
    except ValueError:
        # Numeric dotted addresses must be valid IPv4, not hostnames.
        if re.fullmatch(r"[0-9.]+", host):
            raise ValueError("Invalid IPv4 address")

        labels = host.split(".")
        if (
            len(host) > 253
            or any(
                not re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                    label
                )
                for label in labels
            )
        ):
            raise ValueError("Invalid controller hostname")

except ValueError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"Controller: {host}")
print(f"Allowed network: {network}")
PY
    then
        return 1
    fi

    echo "Network settings validated."
}

check_installation_conflicts() {
    echo "Checking for existing HP 6033A installations..."

    local service="/etc/systemd/system/hp6033a.service"
    local nginx_site="/etc/nginx/sites-available/hp6033a"
    local nginx_enabled="/etc/nginx/sites-enabled/hp6033a"

    if [[ -e "$APP_DIR" || -L "$APP_DIR" ||
          -e "$service" || -L "$service" ||
          -e "$nginx_site" || -L "$nginx_site" ||
          -e "$nginx_enabled" || -L "$nginx_enabled" ]]; then
        echo "ERROR: An existing HP 6033A installation was detected." >&2
        echo "Installation stopped to protect existing files." >&2
        return 1
    fi

    if [[ "$(systemctl show -p LoadState --value hp6033a.service)" != "not-found" ]]; then
        echo "ERROR: HP 6033A service is already registered." >&2
        return 1
    fi

    echo "No conflicting HP 6033A installation detected."
}

check_security_conflicts() {
    echo "Checking for existing HP 6033A security files..."

    local paths=(
        "/etc/nginx/ssl/hp6033a.crt"
        "/etc/nginx/ssl/hp6033a.key"
        "/etc/nginx/hp6033a.htpasswd"
        "$APP_DIR/.control_htpasswd"
    )

    local path

    for path in "${paths[@]}"; do
        if sudo test -e "$path" || sudo test -L "$path"; then
            echo "ERROR: Existing security file detected: $path" >&2
            echo "Installation stopped to protect existing credentials." >&2
            return 1
        fi
    done

    echo "No conflicting security files detected."
}

confirm_installation() {
    echo
    echo "HP 6033A Web Controller"
    echo "Installation Confirmation"
    echo "-------------------------"
    echo "Application directory: $APP_DIR"
    echo "Application user: $APP_USER"
    echo "Controller address: $CONTROLLER_HOST"
    echo "Allowed LAN subnet: $LAN_SUBNET"
    echo
    echo "Installation will configure:"
    echo "  - Python application and dependencies"
    echo "  - USB-GPIB device permissions"
    echo "  - ${CONTROLLER_SCHEME^^} and LAN-restricted control ($CONTROL_AUTH_MODE)"
    echo "  - Nginx reverse proxy"
    echo "  - systemd service"
    echo
    echo "Remote output control will remain disabled"
    echo "until explicitly authorized through the dashboard."
    echo

    local answer
    read -r -p "Proceed with installation? [y/N]: " answer

    if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
        echo "Installation cancelled. No changes made."
        return 2
    fi

    echo "Installation confirmed."
}

run_preflight_checks() {
    echo
    echo "Running HP 6033A installation safety checks..."
    echo

    validate_network_settings || return 1
    check_installation_conflicts || return 1
    check_security_conflicts || return 1
    check_nginx_conflicts || return 1

    echo
    echo "All preliminary installation checks passed."
}

configure_authentication() {
    echo "Setting up HP 6033A controller authentication..."

    local auth_script="$APP_DIR/deploy/setup-auth.sh"

    if [[ ! -f "$auth_script" ]]; then
        echo "ERROR: Authentication setup script is missing." >&2
        return 1
    fi

    if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
        echo "ERROR: Python environment is not ready." >&2
        return 1
    fi

    # Mark authentication setup as started before creating files.
    # This also covers a failure after only one file is created.
    AUTH_CREATED=true
    bash "$auth_script" "$APP_DIR" "$APP_USER" || return 1

    if [[ "${CONTROL_AUTH_MODE:-protected}" == password-free ]]; then
        ( umask 077; printf 'password-free\n' > "$APP_DIR/.control_auth_mode" ) || return 1
    fi
    verify_authentication

    echo "Controller authentication configured successfully."
}

validate_nginx_configuration() {
    echo "Validating HP 6033A Nginx configuration..."

    local generated="$APP_DIR/hp6033a.nginx.generated"
    local target="/etc/nginx/sites-available/hp6033a"
    local enabled="/etc/nginx/sites-enabled/hp6033a"

    if [[ ! -s "$generated" ]]; then
        echo "ERROR: Generated Nginx configuration is missing." >&2
        return 1
    fi

    if [[ -e "$target" || -L "$target" ||
          -e "$enabled" || -L "$enabled" ]]; then
        echo "ERROR: Existing Nginx configuration detected." >&2
        return 1
    fi

    # Validate the generated site before enabling it.
    local test_config
    test_config="$(mktemp /tmp/hp6033a-nginx-XXXXXX.conf)"

    {
        echo 'pid /tmp/hp6033a-nginx-test.pid;'
        echo 'events {}'
        echo 'http {'
        echo '    include /etc/nginx/mime.types;'
        printf '    include %s;\n' "$generated"
        echo '}'
    } > "$test_config"

    if ! sudo nginx -t -p /etc/nginx/ -c "$test_config"; then
        rm -f "$test_config"
        echo "ERROR: Preliminary Nginx validation failed." >&2
        return 1
    fi

    rm -f "$test_config"

    sudo install -m 644 "$generated" "$target"
    NGINX_SITE_CREATED=true

    # Do not enable the website during installation validation.
    # Activation must be performed separately after all checks pass.
    if ! sudo nginx -t; then
        echo "ERROR: Nginx configuration validation failed." >&2
        echo "New Nginx files preserved for manual inspection." >&2
        echo "Automatic activation must not proceed." >&2
        return 1
    fi

    echo "Nginx configuration passed syntax validation."
    echo "Nginx has not been restarted or reloaded."
}

validate_systemd_service() {
    echo "Validating HP 6033A systemd service..."

    local service="/etc/systemd/system/hp6033a.service"

    if [[ ! -f "$service" ]]; then
        echo "ERROR: HP 6033A service file is missing." >&2
        return 1
    fi

    if ! sudo systemd-analyze verify "$service"; then
        echo "ERROR: systemd service validation failed." >&2
        return 1
    fi

    echo "systemd service validation passed."
    echo "The service has not been started."
}

verify_authentication_permissions() {
    echo "Checking authentication file permissions..."

    local flask_auth="$APP_DIR/.control_htpasswd"
    local nginx_auth="/etc/nginx/hp6033a.htpasswd"

    if [[ ! -s "$flask_auth" ]] || ! sudo test -s "$nginx_auth"; then
        echo "ERROR: Authentication files are missing or empty." >&2
        return 1
    fi

    local flask_mode nginx_mode nginx_group

    flask_mode="$(stat -c '%a' "$flask_auth")"
    nginx_mode="$(sudo stat -c '%a' "$nginx_auth")"
    nginx_group="$(sudo stat -c '%G' "$nginx_auth")"

    if [[ "$flask_mode" != "600" ]]; then
        echo "ERROR: Flask authentication file must have mode 600." >&2
        return 1
    fi

    if [[ "$nginx_mode" != "640" || "$nginx_group" != "www-data" ]]; then
        echo "ERROR: Nginx authentication permissions are incorrect." >&2
        return 1
    fi

    if ! sudo -u www-data test -r "$nginx_auth"; then
        echo "ERROR: Nginx cannot read its authentication file." >&2
        return 1
    fi

    echo "Authentication file permissions verified."
}

installation_failed() {
    local exit_code="$1"
    local line_number="$2"

    # Prevent recovery commands from triggering this handler again.
    trap - ERR
    set +e

    echo >&2
    echo "ERROR: HP 6033A installation failed." >&2
    echo "Failure near line: $line_number" >&2
    echo "Exit status: $exit_code" >&2
    echo >&2

    report_installation_state >&2

    echo "The installer will not automatically start the controller." >&2
    # Attempt to remove the newly enabled website first.
    if ! rollback_nginx_activation; then
        echo "CRITICAL: Nginx rollback was incomplete." >&2
        echo "Manual inspection is required." >&2
    fi

    # Stop the application even if Nginx recovery failed.
    if ! safe_installation_recovery; then
        echo "CRITICAL: Application recovery was incomplete." >&2
        echo "Verify the HP 6033A service is stopped." >&2
        echo "Manual intervention may be required." >&2
    fi

    echo "Any files already created must be reviewed before retrying." >&2
    echo "Existing installations and credentials must not be overwritten." >&2

    exit "$exit_code"
}

# Report installation errors, including failures inside functions.
set -E
trap 'installation_failed "$?" "$LINENO"' ERR

# Track resources created by this installation.
# These flags are used for safe failure recovery.
APP_CREATED=false
SERVICE_CREATED=false
NGINX_SITE_CREATED=false
CERT_CREATED=false
CERT_CREATION_ATTEMPTED=false
AUTH_CREATED=false

report_installation_state() {
    echo "Installation state:"
    echo "  Application created: $APP_CREATED"
    echo "  systemd service created: $SERVICE_CREATED"
    echo "  Nginx site created: $NGINX_SITE_CREATED"
    echo "  HTTPS certificate created: $CERT_CREATED"
    echo "  HTTPS certificate creation attempted: $CERT_CREATION_ATTEMPTED"
    echo "  Authentication created: $AUTH_CREATED"
}

safe_installation_recovery() {
    echo "Performing conservative installation recovery..." >&2
    local recovery_failed=0

    # Services are not started until installation is validated.
    # Only stop a service if this installer explicitly started it.
    if [[ "${SERVICE_STARTED:-false}" == true ]]; then
        if ! sudo systemctl stop hp6033a.service; then
            echo "CRITICAL: Failed to stop HP 6033A application service." >&2
            echo "Manual intervention may be required." >&2
            recovery_failed=1
        fi

        if sudo systemctl is-active --quiet hp6033a.service; then
            echo "CRITICAL: HP 6033A application is still running." >&2
            recovery_failed=1
        fi
    fi

    # Undo automatic startup if this installation enabled it.
    if [[ "${SERVICE_ENABLED_BY_INSTALLER:-false}" == true ]]; then
        if ! sudo systemctl disable hp6033a.service; then
            echo "CRITICAL: Could not disable automatic startup." >&2
            recovery_failed=1
        fi
    fi

    # Preserve Nginx files until installation ownership can be
    # verified. Do not remove files based on tracking flags alone.
    if [[ "${NGINX_SITE_CREATED:-false}" == true ]]; then
        echo "WARNING: Newly installed Nginx site requires review." >&2
        echo "Check /etc/nginx/sites-enabled/hp6033a before retrying." >&2
    fi

    # Preserve partially generated HTTPS security files.
    if [[ "${CERT_CREATION_ATTEMPTED:-false}" == true &&
          "${CERT_CREATED:-false}" != true ]]; then
        echo "WARNING: HTTPS certificate creation was incomplete." >&2
        echo "Inspect /etc/nginx/ssl/hp6033a.crt and hp6033a.key." >&2
    fi

    # Preserve partially generated authentication files.
    if [[ "${AUTH_CREATED:-false}" == true ]]; then
        echo "WARNING: Authentication setup was attempted." >&2
        echo "Inspect the Flask and Nginx credential files." >&2
    fi

    # Preserve application files, certificates, and credentials
    # for inspection and manual recovery.
    echo "Remaining installation files preserved for inspection." >&2
    echo "Review the failed installation before retrying." >&2
    return "$recovery_failed"
}

SERVICE_STARTED=false
SERVICE_ENABLED_BY_INSTALLER=false

verify_ready_for_activation() {
    echo "Performing final HP 6033A activation checks..."

    # Confirm configuration exists and contains valid JSON.
    if ! "$APP_DIR/venv/bin/python" -m json.tool \
        "$APP_DIR/config.json" >/dev/null; then
        echo "ERROR: Application configuration is invalid." >&2
        return 1
    fi

    # Confirm the application can be compiled.
    if ! "$APP_DIR/venv/bin/python" - <<PY
import ast
from pathlib import Path
ast.parse(Path("$APP_DIR/app.py").read_text())
PY
    then
        echo "ERROR: Application Python syntax is invalid." >&2
        return 1
    fi

    if [[ "$CONTROLLER_SCHEME" == https ]]; then
        verify_authentication
        verify_authentication_permissions
    fi
    validate_systemd_service

    if ! sudo nginx -t; then
        echo "ERROR: Nginx configuration validation failed." >&2
        return 1
    fi

    echo "Final activation checks passed."
}

verify_gpib_configuration() {
    echo "Checking USB-GPIB configuration..."

    "$APP_DIR/venv/bin/python" - "$APP_DIR/config.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    config = json.load(f)

resource = config.get("gpib_resource", "")

if not isinstance(resource, str):
    sys.exit("ERROR: GPIB resource must be a string.")

if resource and (
    "\n" in resource or "\r" in resource or "\x00" in resource
):
    sys.exit("ERROR: Invalid characters in GPIB resource.")

if not resource:
    print("GPIB resource has not been configured.")
    print("Hardware communication will remain unavailable")
    print("until a valid resource address is supplied.")
else:
    print("Configured GPIB resource:", resource)
    print("Hardware connectivity has not yet been verified.")
PY
}

collect_network_settings() {
    echo
    echo "HP 6033A Controller Network Setup"
    echo "---------------------------------"
    echo "Enter the IP address or hostname used to access"
    echo "this Raspberry Pi from your local network."
    echo

    read -r -p "Controller IP or hostname: " CONTROLLER_HOST
    read -r -p "Allowed LAN subnet (example 192.168.1.0/24): " LAN_SUBNET

    validate_network_settings
}

CONTROLLER_SCHEME=https
CONTROL_AUTH_MODE=protected

collect_connection_mode() {
    local answer
    echo "HP 6033A Connection and Password Setup"
    echo "---------------------------------------"
    echo "Your Pi login is the account you created in Raspberry Pi Imager or Raspberry Pi OS."
    echo "If sudo asks for a password, enter the Pi account password for $APP_USER."
    echo "Both options keep the dashboard limited to the LAN subnet you enter."
    echo "Both require you to enable remote control explicitly before changing the supply."
    echo
    echo "1) HTTPS (default)"
    echo "   Encrypts browser traffic. Your browser may warn about the Pi's self-signed certificate."
    echo "   HTTP visits redirect to HTTPS. Setup creates a separate dashboard username and password"
    echo "   for protected controls and for changing the control password policy in dashboard Setup."
    echo "   You can choose password-free controls and still keep HTTPS."
    echo "   If you enable browser software updates, you will create a separate administrator"
    echo "   password that approves installations, even with password-free controls."
    echo
    echo "2) HTTP only (trusted home LAN)"
    echo "   Opens at http:// with no encryption, certificate, or HTTPS redirect."
    echo "   Skips the controller username and both dashboard password prompts."
    echo "   Anyone on the allowed LAN can enable remote control and operate the supply."
    echo "   Software updates use the command line on the Pi. Keep this mode on a trusted LAN"
    echo "   and do not forward the controller port to the internet."
    echo
    read -r -p "Connection mode [1/2, default 1]: " answer || return 1
    case "$answer" in
        ""|1) CONTROLLER_SCHEME=https; CONTROL_AUTH_MODE=protected ;;
        2) CONTROLLER_SCHEME=http; CONTROL_AUTH_MODE=password-free ;;
        *) echo "Enter 1 or 2." >&2; return 1 ;;
    esac
}

collect_control_auth_mode() {
    local answer
    echo "Password-free control is for a trusted LAN without internet exposure."
    echo "Changing this policy in dashboard Setup requires the controller username and password."
    read -r -p "Require a control password? [Y/n]: " answer || return 1
    case "$answer" in
        ""|y|Y) CONTROL_AUTH_MODE=protected ;;
        n|N) CONTROL_AUTH_MODE=password-free ;;
        *) echo "Enter y or n." >&2; return 1 ;;
    esac
}


activate_controller() {
    echo "Activating HP 6033A Web Controller..."

    # All configuration checks must pass before activation.
    verify_ready_for_activation
    verify_gpib_configuration

    # Start the application only after validation.
    # Mark activation as attempted before starting, so recovery
    # can stop the service if systemctl reports a failure.
    SERVICE_STARTED=true
    sudo systemctl start hp6033a.service

    if ! sudo systemctl is-active --quiet hp6033a.service; then
        echo "ERROR: HP 6033A service failed to start." >&2
        return 1
    fi

    echo "Application service started successfully."
    echo "Nginx has not been reloaded or restarted."
}

activate_nginx() {
    echo "Activating HP 6033A ${CONTROLLER_SCHEME^^} interface..."

    local target="/etc/nginx/sites-available/hp6033a"
    local enabled="/etc/nginx/sites-enabled/hp6033a"

    # Refuse to overwrite or reuse an existing enabled site.
    if [[ -e "$enabled" || -L "$enabled" ]]; then
        echo "ERROR: HP 6033A Nginx site is already enabled." >&2
        return 1
    fi

    if [[ ! -f "$target" ]]; then
        echo "ERROR: HP 6033A Nginx configuration is missing." >&2
        return 1
    fi

    # Verify the application is running.
    if ! sudo systemctl is-active --quiet hp6033a.service; then
        echo "ERROR: HP 6033A application is not running." >&2
        return 1
    fi

    # Enable only the site prepared by this installation.
    if [[ "${NGINX_SITE_CREATED:-false}" != true ]]; then
        echo "ERROR: Nginx site ownership is not established." >&2
        return 1
    fi

    sudo ln -s "$target" "$enabled"
    NGINX_SYMLINK_CREATED=true

    # Validate the complete configuration before reloading Nginx.
    if ! sudo nginx -t; then
        echo "ERROR: Nginx configuration validation failed." >&2

        # Remove only the symlink created by this activation.
        if [[ -L "$enabled" ]] &&
           [[ "$(readlink "$enabled")" == "$target" ]]; then
            if sudo rm -- "$enabled"; then
                NGINX_SYMLINK_CREATED=false
                echo "New Nginx symlink removed after validation failure." >&2
            else
                echo "CRITICAL: Failed to remove Nginx symlink." >&2
            fi
        else
            echo "WARNING: Nginx symlink changed; manual review required." >&2
        fi

        return 1
    fi

    # Record activation before attempting it so recovery
    # can handle failures during reload or startup.
    NGINX_ACTIVATION_ATTEMPTED=true

    if sudo systemctl is-active --quiet nginx.service; then
        NGINX_WAS_RUNNING=true
        sudo systemctl reload nginx.service
    else
        NGINX_WAS_RUNNING=false
        sudo systemctl start nginx.service
    fi

    if ! sudo systemctl is-active --quiet nginx.service; then
        echo "ERROR: Nginx failed to activate." >&2
        return 1
    fi

    echo "Nginx ${CONTROLLER_SCHEME^^} interface activated."
    echo "Remote output control remains disabled by default."
}

# Nginx activation tracking for installation recovery.
NGINX_ACTIVATION_ATTEMPTED=false
NGINX_WAS_RUNNING=false
NGINX_SYMLINK_CREATED=false

main() {
    echo "========================================"
    echo " HP 6033A Web Controller Installer"
    echo "========================================"

    if (( EUID == 0 )); then
        echo "Run bash install.sh as the controller user. The installer will use sudo when needed." >&2
        return 1
    fi

    collect_connection_mode || return 1
    collect_network_settings || return 1
    if [[ "$CONTROLLER_SCHEME" == https ]]; then
        collect_control_auth_mode || return 1
    fi
    run_preflight_checks || return 1

    if confirm_installation; then
        :
    else
        confirmation_status=$?

        if [[ "$confirmation_status" -eq 2 ]]; then
            echo "Installation cancelled cleanly."
            return 0
        fi

        return "$confirmation_status"
    fi

    # Record whether Nginx existed before installing dependencies.
    if command -v nginx >/dev/null 2>&1; then
        NGINX_PREEXISTING=true
    else
        NGINX_PREEXISTING=false
    fi

    install_application || return 1
    install_dependencies || return 1

    # Existing Nginx installations must remain conflict-free.
    # A fresh installation may contain Debian's default website.
    if [[ "$NGINX_PREEXISTING" == true ]]; then
        check_nginx_conflicts
    else
        echo "Validating newly installed Nginx configuration..."
        sudo nginx -t || return 1

        # A fresh Debian Nginx installation may include its default
        # HTTP site. Never allow an unexpected HTTPS listener.
        local nginx_config
        nginx_config="$(sudo nginx -T 2>&1)" || return 1

        if grep -Eq '^[[:space:]]*listen[[:space:]]+([^[:space:];]+:)?443([[:space:];]|$)' <<< "$nginx_config"; then
            echo "ERROR: Unexpected existing HTTPS listener." >&2
            return 1
        fi
    fi

    initialize_configuration
    configure_usbgpib
    install_systemd_service
    if [[ "$CONTROLLER_SCHEME" == https ]]; then
        configure_https
    fi
    configure_nginx
    if [[ "$CONTROLLER_SCHEME" == https ]]; then
        configure_authentication || return 1
    fi

    validate_nginx_configuration
    validate_systemd_service
    if [[ "$CONTROLLER_SCHEME" == https ]]; then
        verify_authentication_permissions
    fi
    verify_gpib_configuration
    verify_ready_for_activation

    echo
    echo "Installation files and configuration validated."

    activate_controller || return 1
    activate_nginx || return 1

    # Verify that the selected interface reaches the actual HP 6033A application.
    if ! "$APP_DIR/venv/bin/python" - "$CONTROLLER_HOST" "$CONTROLLER_SCHEME" "$CONTROL_AUTH_MODE" <<'PYVERIFY'
import json
import ssl
import sys
import urllib.request

host, scheme, mode = sys.argv[1:]
base = f"{scheme}://{host}"
url = f"{base}/api/status"

try:
    context = ssl._create_unverified_context() if scheme == "https" else None
    with urllib.request.urlopen(url, context=context, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Unexpected HTTP status: {response.status}")
        status = json.load(response)

    required = {"gpib_connected", "gpib_status", "mode"}
    if not isinstance(status, dict) or not required.issubset(status):
        raise RuntimeError("Unexpected controller status response")

    expected = "Disable control password" if mode == "protected" else "Require control password"
    for route in ("/", "/classic-dual"):
        with urllib.request.urlopen(f"{base}{route}", context=context, timeout=15) as response:
            if response.url != f"{base}{route}":
                raise RuntimeError(f"Unexpected redirect on {route}")
            page = response.read().decode("utf-8")
        has_auth_setup = 'id="control-auth-setting-dialog"' in page
        if scheme == "https" and (not has_auth_setup or expected not in page):
            raise RuntimeError(f"Expected {mode} authentication setup on {route}")
        if scheme == "http" and (has_auth_setup or "if(!false)return Promise.resolve({});" not in page):
            raise RuntimeError(f"Expected HTTP-only password-free controls on {route}")
    print(f"{scheme.upper()} status and dashboard setup verification passed.")
except Exception as exc:
    print(f"ERROR: {scheme.upper()} controller verification failed: {exc}", file=sys.stderr)
    sys.exit(1)
PYVERIFY
    then
        return 1
    fi

    # Enable automatic startup only after successful dashboard verification.
    SERVICE_ENABLED_BY_INSTALLER=true
    if ! sudo systemctl enable hp6033a.service; then
        echo "ERROR: Could not enable automatic startup." >&2
        return 1
    fi

    echo
    echo "========================================"
    echo " HP 6033A INSTALLATION SUCCESSFUL"
    echo "========================================"
    echo "Controller URL: ${CONTROLLER_SCHEME}://${CONTROLLER_HOST}/"
    echo "Control authentication mode: $CONTROL_AUTH_MODE"
    echo "Documentation: ${CONTROLLER_SCHEME}://${CONTROLLER_HOST}/docs/"

    if [[ "$CONTROLLER_SCHEME" == http ]]; then
        echo "HTTP-only mode uses command-line software updates. See the installation guide."
        return 0
    fi

    echo "Your browser may warn about the self-signed certificate."
    echo "Control preferences: Setup in Modern or Setup & Extra Controls in Classic."
    echo "Use the controller credentials to change the authentication mode."

    local web_updates
    if [[ -t 0 ]] && read -r -p "Enable Software Updates in the dashboards? [y/N]: " web_updates &&
       [[ "$web_updates" == [yY] ]]; then
        if ! sudo bash "$SOURCE_DIR/deploy/install-web-updater.sh" "$APP_USER" "https://${CONTROLLER_HOST}"; then
            echo "The controller is working. See the installation guide to recover Software Updates setup." >&2
        fi
    fi
}


rollback_nginx_activation() {
    echo "Checking Nginx rollback requirements..." >&2

    if [[ "${NGINX_SITE_CREATED:-false}" != true ]]; then
        echo "No newly created Nginx site to roll back." >&2
        return 0
    fi

    local target="/etc/nginx/sites-available/hp6033a"
    local enabled="/etc/nginx/sites-enabled/hp6033a"

    if [[ "${NGINX_SYMLINK_CREATED:-false}" != true ]]; then
        echo "No installer-owned Nginx symlink to remove." >&2
        return 0
    fi

    if [[ -L "$enabled" ]] &&
       [[ "$(readlink "$enabled")" == "$target" ]]; then
        sudo rm -- "$enabled" || {
            echo "WARNING: Could not remove Nginx symlink." >&2
            return 1
        }
        echo "New Nginx site symlink removed." >&2
    elif [[ -e "$enabled" || -L "$enabled" ]]; then
        echo "WARNING: Unexpected Nginx site; manual review required." >&2
        return 1
    fi

    if [[ "${NGINX_ACTIVATION_ATTEMPTED:-false}" == true ]]; then
        if sudo nginx -t; then
            if [[ "${NGINX_WAS_RUNNING:-false}" == true ]]; then
                if ! sudo systemctl reload nginx.service; then
                    echo "CRITICAL: Nginx rollback reload failed." >&2
                    echo "Manual recovery is required." >&2
                    return 1
                fi
            else
                if ! sudo systemctl stop nginx.service; then
                    echo "CRITICAL: Could not stop newly started Nginx." >&2
                    echo "Manual recovery is required." >&2
                    return 1
                fi
            fi
        else
            echo "CRITICAL: Nginx configuration is invalid during rollback." >&2
            echo "Nginx may still be serving the previous configuration." >&2
            echo "Manual recovery is required." >&2
            return 1
        fi
    fi

    echo "Nginx rollback assessment completed." >&2
}

# Start installation only when this script is executed directly.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
