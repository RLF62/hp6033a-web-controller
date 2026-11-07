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

On a fresh Raspberry Pi OS installation, download this repository, open a terminal in the project directory, and run:

    bash install.sh

The installer configures dependencies, USB-GPIB, authentication, HTTPS, Nginx, and automatic startup. It checks the controller status API before reporting success.

After installation, open the HTTPS URL displayed by the installer. Your browser may warn about the self-signed certificate; verify the Raspberry Pi address before proceeding.

The installer will not overwrite an existing HP 6033A installation.

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
