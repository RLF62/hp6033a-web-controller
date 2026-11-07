# HP 6033A Web Controller — Project Handoff

## Project
Repository: https://github.com/RLF62/hp6033a-web-controller

A Raspberry Pi-based Flask web controller for the HP 6033A programmable DC power supply using the XyphroLabs UsbGpib V2 adapter.

## Hardware and Development Environment

Production:
- Raspberry Pi 3 B+
- Working HP 6033A controller
- DO NOT modify without explicit permission.

Development:
- Raspberry Pi Zero 2 W
- Hostname: hp6033a-test
- IP: 192.168.1.178
- SSH user: pi
- OS: Debian 13 Trixie
- Working application: /home/pi/hp6033a
- Git repository: /home/pi/hp6033a-github-update
- Original source: /home/pi/hp6033a-web-controller-main

## Architecture

- Python Flask application
- Gunicorn application server
- Nginx HTTPS reverse proxy
- systemd service: hp6033a.service
- PyVISA / PyVISA-py
- USB-GPIB interface
- Browser-based modern and classic interfaces
- HTTPS status endpoint: /api/status

The test controller has successfully communicated with the real HP 6033A.

## GitHub Status

The public `main` branch carries the project source and installer. Use
`git log -1 --oneline` in the checkout to record the exact release revision.

SSH authentication was configured for the Pi Zero 2 W.

The GitHub repository uses:
git@github.com:RLF62/hp6033a-web-controller.git

## Installer Status

- Installer syntax verification passed.
- 53 automated tests passed during development.
- HTTPS status API tested successfully.
- Nginx and systemd integration tested on the development Pi.
- Clean installation on fresh Raspberry Pi OS NOT YET VERIFIED.
- Do not remove the README testing warning until clean-install validation succeeds.

## Development Status

- The wired-address origin fix is in the installer template and passed development
  HTTPS origin checks. The production Android wired-path retest is pending.
- Modern Setup and Classic Setup & Extra Controls group the saved confirmation
  preference and the control password setting. Authentication changes require
  controller credentials in either mode. Classic labels and the password
  visibility buttons are implemented.
- The existing development Pi has dashboard-managed authentication enabled.
  Fresh installations enable it automatically. The clean-install warning still applies.
- Production remains a separate deployment.

## In-place updater

The updater uses an explicit stable tag and reviewed full commit, stages the
application and runtime, then switches the app directory after stopping only
the controller service. It preserves the existing Nginx and systemd policies.
The installer and recovery guides cover the verified bootstrap, private
backups and manual recovery. Use \`bash update.sh --check\` for periodic
read-only checks. Release publication and production commissioning are separate.

### Dashboard Software Updates

Modern and Classic share the Software Updates section. The root-owned
systemd helper checks GitHub on startup and every six hours. The Flask
application forwards small JSON requests over a Unix socket restricted to
the controller UID. The helper checks a separate administrator password and
issues a short-lived, single-use approval bound to a published stable tag and
commit. It rechecks the pin before an unprivileged worker fetches and verifies
the release.

Privileged operations use a private worker channel with a fixed backup and
controller-service stop/start sequence. The helper retains system archives
under its root-only job directory. The worker uses the CLI transaction for
application backup, activation, read-only verification and automatic
rollback. It also verifies that the new dashboard reconnects to the helper.
See the installation and recovery guides for provisioning and job logs.

## Original Improvement Requests

### 1. Verify the .250 Fix

Determine whether the previously discussed ".250 issue" fix is included in the GitHub repository and installer.

The exact nature of this issue must be confirmed before changing code.

Compare the deployed application, repository, and relevant configuration.

Do not assume the fix was included.

### 2. Optional Control Authentication

Allow users to choose whether control operations require a password.

Requirements:
- Offer password-protected or password-free control during installation.
- Default to password-protected operation.
- Allow changing the setting after installation.
- Keep HTTPS enabled independently of password authentication.
- Restrict access to the configured local network.
- Preserve existing control safety protections.
- Validate Nginx configuration before applying changes.
- Provide safe rollback if configuration changes fail.
- Avoid requiring browser Basic Authentication when password-free mode is selected.

Security:
Password-free control must not be enabled for an internet-exposed controller without appropriate safeguards.

### 3. Persistent Confirmation Preference

The "Skip control confirmation popups" setting currently resets when the browser is closed.

Requirements:
- Remember the setting across page reloads.
- Remember the setting after closing and reopening Chrome.
- Support desktop and Android browsers.
- Store preference separately on each device.
- Preserve existing confirmation behavior by default.

Suggested implementation:
Browser localStorage.

### 4. Customizable Classic Display Labels

Allow users to customize the classic HP front-panel identification.

Current display:
6033A SYSTEM POWER SUPPLY
0-20V/0-30A, 200W

Requirements:
- Configurable model/name.
- Configurable voltage range label.
- Configurable current range label.
- Configurable maximum power label.
- Persist settings across restarts.
- Display consistently across browsers.
- Provide sensible HP 6033A defaults.

These are display labels only, not automatically enforced electrical limits.

### 5. Password Visibility Toggle

Add an eye icon to application-managed password fields.

Requirements:
- Work on desktop Chrome.
- Work on Android Chrome.
- Toggle between hidden and visible password text.
- Keep passwords hidden by default.
- Do not store passwords in browser localStorage.
- Apply consistently to relevant password entry fields.

Note:
The application cannot customize Chrome's native HTTP Basic Authentication dialog.

## Development Rules

1. Work on the Pi Zero 2 W first.
2. Do not modify the production Pi 3 without explicit approval.
3. Back up affected files before editing.
4. Review existing code before implementing changes.
5. Preserve existing working functionality.
6. Run automated tests after changes.
7. Test changes against the development controller.
8. Review Git differences before committing.
9. Do not push to GitHub without approval.
10. Never place passwords, tokens, or private SSH keys in the repository.

## Recommended Development Order

1. Verify the .250 fix.
2. Implement persistent confirmation preferences.
3. Add configurable classic display labels.
4. Add password visibility controls.
5. Implement optional authentication and post-install configuration.
6. Update installer tests and documentation.
7. Complete fresh-install validation.
8. Review and publish approved changes.

## Work Mode Instructions

Read this document, README.md, install.sh, app.py, and relevant templates before modifying code.

Inspect the actual implementation rather than assuming features or file locations.

Use SSH access if supported and authorized.

Maintain separation between the production and development Raspberry Pis.
