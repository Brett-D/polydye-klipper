#!/bin/sh
# Turns PolyDye on in Klipper only when the board is plugged in.
#
# Run it before Klipper starts (a systemd ExecStartPre installed by install.sh). printer.cfg has
#   [include polydye-active*.cfg]
# which matches nothing when this script has removed the file, so Klipper starts without PolyDye.
#
# It also puts back the add-on's link in Klipper's extras if it has gone missing or broken (a Klipper reinstall, a
# damaged SD card), so restarting the Klipper service heals it instead of Klipper halting on
# "Section 'polydye' is not a valid config section".
#
# Usage: polydye-toggle.sh [klipper config dir] [klipper dir]   (defaults ~/printer_data/config, ~/klipper)
#
# This file is part of polydye-klipper (GPLv3).

CFG_DIR="${1:-$HOME/printer_data/config}"
KLIPPER_DIR="${2:-$HOME/klipper}"
OUT="$CFG_DIR/polydye-active.cfg"
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

EXTRA="$KLIPPER_DIR/klippy/extras/polydye.py"
# -e follows the link, so this is true for a missing link and for one whose target is gone. Anything else there
# (a valid link, a copied file) is left alone.
if [ -d "$KLIPPER_DIR/klippy/extras" ] && [ ! -e "$EXTRA" ]; then
    if ln -sfn "$REPO_DIR/klipper/polydye.py" "$EXTRA"; then
        echo "polydye: restored the Klipper add-on link $EXTRA"
        # Keep it out of Klipper's git status, as install.sh does (a fresh Klipper clone has lost that too).
        if [ -d "$KLIPPER_DIR/.git" ]; then
            mkdir -p "$KLIPPER_DIR/.git/info"
            grep -qxF "klippy/extras/polydye.py" "$KLIPPER_DIR/.git/info/exclude" 2>/dev/null \
                || echo "klippy/extras/polydye.py" >> "$KLIPPER_DIR/.git/info/exclude"
        fi
    else
        echo "polydye: could not restore the Klipper add-on link $EXTRA; run install.sh again" >&2
    fi
fi

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
# Klipper only learns that the board restarts on command once it has connected to it; until then FIRMWARE_RESTART
# would try an Arduino-style reset on the port, which fails if the board is unplugged and leaves Moonraker connected
# to a dead Klipper instance.
restart_method: command

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
