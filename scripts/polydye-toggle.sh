#!/bin/sh
# Turns PolyDye on in Klipper only when the board is plugged in.
#
# Run it before Klipper starts (a systemd ExecStartPre installed by install.sh). printer.cfg has
#   [include polydye-active*.cfg]
# which matches nothing when this script has removed the file, so Klipper starts without PolyDye.
#
# Usage: polydye-toggle.sh [klipper config dir]   (default ~/printer_data/config)
#
# This file is part of polydye-klipper (GPLv3).

CFG_DIR="${1:-$HOME/printer_data/config}"
OUT="$CFG_DIR/polydye-active.cfg"
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

port=""
for _ in 1 2 3 4; do  # at boot, give USB a moment to enumerate
    for p in /dev/serial/by-id/usb-PolyDye_*-if00; do
        if [ -e "$p" ]; then
            port="$p"
            break
        fi
    done
    [ -n "$port" ] && break
    sleep 0.5
done

if [ -n "$port" ]; then
    cat > "$OUT.tmp" <<EOF
# Written by polydye-toggle.sh when Klipper started, because the PolyDye board was plugged in.
# To print without PolyDye: unplug the board's USB, then restart the Klipper service.
# Don't edit this file; PolyDye settings are in polydye.cfg.
[mcu polydye]
serial: $port

[include $REPO_DIR/config/polydye_macros.cfg]
[include polydye.cfg]
EOF
    mv "$OUT.tmp" "$OUT"
    echo "polydye: board found at $port, enabled"
else
    rm -f "$OUT"
    echo "polydye: board not plugged in, disabled"
fi
exit 0
