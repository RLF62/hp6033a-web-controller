# HP 6033A Web Controller

A Raspberry Pi-based web interface for monitoring and controlling the
Hewlett-Packard HP 6033A programmable DC power supply through USB-GPIB.

Developed and successfully tested with a real HP 6033A.

## Features

- Modern browser-based dashboard
- Classic-style instrument interface
- Live voltage and current monitoring
- Programmable voltage and current settings
- Output enable/disable control
- Remote control safety protections
- HTTPS support through Nginx
- Authentication for protected control operations
- Built-in documentation and command reference

## Verified Hardware

- Hewlett-Packard HP 6033A programmable DC power supply
- Raspberry Pi 3 B+
- XyphroLabs UsbGpib V2 USB-GPIB adapter

The HP 6033A is rated for 0–20 V, 0–30 A, and 200 W.

## Compatibility

This software has been successfully tested with the HP 6033A.

Other HP programmable power supplies using compatible GPIB commands
may also work, but have not yet been verified.

Reports of successful operation with other models are welcome.

## Software

- Python 3
- Flask
- PyVISA and PyVISA-py
- Gunicorn
- Nginx

## USB-GPIB Adapter

This project uses the XyphroLabs UsbGpib V2 adapter.

Original project:
https://github.com/xyphro/UsbGpib

Thanks to the developer for making this hardware interface available.

## Installation

**Status: Clean-install validation pending. Do not use on production systems yet.**

On a fresh Raspberry Pi OS installation, download this repository and open a
terminal in the project directory as the user who will run the controller.
The installer uses `sudo` for system setup.

    bash install.sh

Enter the controller's HTTPS hostname or IP and its permitted LAN subnet.
The installer configures dependencies, USB-GPIB permissions, authentication,
HTTPS, Nginx, and automatic startup. It verifies the HTTPS status API and
Setup sections before reporting success. Follow the [installation guide](static/docs/installation.html)
to configure the VISA resource and verify communication with the supply.

After installation, open the HTTPS URL displayed by the installer. Your browser may warn about the self-signed certificate; verify the Raspberry Pi address before proceeding.

The installer will not overwrite an existing HP 6033A installation.

### Versioned installation and upgrades

The [installation guide](static/docs/installation.html) includes a single
command that downloads a pinned stable GitHub tag, verifies its full commit
and Git objects, and starts the installer. Get the tag and commit from a
reviewed [release](https://github.com/RLF62/hp6033a-web-controller/releases).

For a first upgrade, use the download command in the installation guide with
its update arguments. You can also run the updater from a reviewed release
checkout. It downloads the selected stable tag itself. Replace the placeholders and use
the controller's current HTTPS address.

```bash
bash update.sh --check
bash update.sh --release '<stable-tag>' --expect-commit '<full-commit>' --url https://hp6033a.local --dry-run
bash update.sh --release '<stable-tag>' --expect-commit '<full-commit>' --url https://hp6033a.local
```

`--check` is safe to run periodically as the controller user. It reports stable
tags and release links. Applying an upgrade is a separate, explicit command.
An unversioned legacy install is identified by an app file fingerprint.
The updater requires connected readback with output off and remote programming
disabled. See [recovery](static/docs/recovery.html) for backup and rollback.

### Software Updates in the dashboards

On a fresh installation, opt in to Software Updates when prompted and create
a separate update administrator password. Open Setup in Modern or
Setup & Extra Controls in Classic to see the installed version, latest
published stable GitHub release, release notes, and update status.
The helper checks GitHub every six hours. Installing requires confirming a
specific version and commit with the update administrator password, even
when power-supply control is password-free.

For an existing deployment, first install the compatible application using
the verified CLI updater, then provision the helper once from the reviewed
checkout. See the [installation guide](static/docs/installation.html#web-updates).
Release discovery uses published, non-prerelease GitHub releases with a stable
version tag. The helper leaves settings, Nginx, and TLS configuration in place
and keeps protected system backups. It stops if the instrument is disconnected,
the physical output cannot be confirmed off, or remote programming is enabled.

### Control authentication and dashboard settings

The installer defaults to password-protected control and creates a separate
controller login in both modes. Choose password-free control only on a trusted
LAN without internet exposure. Both modes use HTTPS,
the configured LAN allowlist, and the same instrument safety checks.

Open **Setup** in Modern or **Setup & Extra Controls** below the Classic
instrument panel to find the confirmation and control password settings. Changing the mode always requires the controller
credentials. A mode change disarms remote programming and leaves the power
supply's physical output in its current state.

Fresh installs enable the dashboard button. To enable it on a previously
installed controller after updating its application and deployment scripts,
run this once on the Raspberry Pi:

```bash
sudo bash /home/pi/hp6033a/deploy/set-control-auth.sh dashboard
```

The script validates Nginx before reloading and restores the previous site
configuration if activation fails. It prompts for credentials if none exist.
Reload the dashboard to use the button. The explicit `protected` and
`password-free` command modes remain available for SSH-only administration
and disable dashboard policy changes.

The confirmation checkbox is saved per browser profile and controller URL.
In Classic, open **Setup & Extra Controls** to edit the front-panel
labels. The controller saves these labels in its configuration for all browsers.
Changing a label has no effect on electrical limits.

## Safety

This software controls a physical power supply capable of delivering
substantial electrical power.

Verify voltage and current limits before enabling the output.

Remote output control is restricted by default and requires
deliberate configuration and confirmation.

Never rely on software alone for electrical safety.

## Project Status

- HP 6033A communication: Verified
- Raspberry Pi 3 B+ operation: Verified
- Web controller: Operational
- Automated installer: Under development
- Other HP models: Not yet tested

## Contributions

Testing reports, bug reports, documentation improvements, and
compatibility information for additional HP power supplies are welcome.
