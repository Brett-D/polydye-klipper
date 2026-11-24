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
# It first writes everything pending to the SD card, and checks the add-on's own files: if an unsafe shutdown has
# damaged the ones Klipper loads, PolyDye stays off so Klipper still starts and prints (see the README, "Raspberry
# Pi 3: freezes and damaged files").
#
# Usage: polydye-toggle.sh [klipper config dir] [klipper dir]   (defaults ~/printer_data/config, ~/klipper)
#
# This file is part of polydye-klipper (GPLv3).

CFG_DIR="${1:-$HOME/printer_data/config}"
KLIPPER_DIR="${2:-$HOME/klipper}"
OUT="$CFG_DIR/polydye-active.cfg"
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
BY_ID="${POLYDYE_BY_ID_DIR:-/dev/serial/by-id}"  # the override is for tests

# Moonraker's Update Manager restarts Klipper right after it pulls this add-on, and Klipper may then restart the
# PolyDye board. A Raspberry Pi 3 has frozen when a USB device left and came back, and files from the pull that were
# still only in memory came back empty or full of zeros. Put them on the SD card first.
sync

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

# The add-on's own files against git's record of them (git status only compares sizes and times).
files_ok=1
files=$(python3 "$REPO_DIR/scripts/polydye_files.py" "$REPO_DIR" 2>&1)
rc=$?
# Trusted only from a checker that ran to the end: a damaged one can print a traceback quoting its own code.
case "$rc $(printf '%s\n' "$files" | tail -n 1)" in
    "0 result: "*)
        corrupt=$(printf '%s\n' "$files" | sed -n 's/^corrupt //p' | tr '\n' ' ')
        changed=$(printf '%s\n' "$files" | sed -n 's/^changed //p' | tr '\n' ' ')
        if [ -n "$corrupt" ]; then
            echo "polydye: ADD-ON FILES DAMAGED: ${corrupt}(probably an unsafe shutdown right after an update)." \
                "Recover them: Update Manager, polydye, Recover, Hard recovery; see the add-on's README." >&2
            case " $corrupt" in
                *" klipper/polydye.py "* | *" config/polydye_macros.cfg "*) files_ok="" ;;
            esac
        fi
        [ -z "$changed" ] || echo "polydye: add-on files that differ from git (local edits?): $changed"
        ;;
    *)
        # The checker itself is missing or damaged, most likely by the same shutdown. A copied install (not a git
        # checkout) gets "result: unknown" instead and carries on.
        files_ok=""
        echo "polydye: ADD-ON FILES DAMAGED: couldn't check them ($files)." \
            "Recover them: Update Manager, polydye, Recover, Hard recovery; see the add-on's README." >&2
        ;;
esac

port=""
for _ in 1 2 3 4; do  # at boot, give USB a moment to enumerate
    for p in "$BY_ID"/usb-PolyDye_*-if00; do
        if [ -e "$p" ]; then
            port="$p"
            break
        fi
    done
    [ -n "$port" ] && break
    sleep 0.5
done

if [ -n "$port" ] && [ -z "$files_ok" ]; then
    rm -f "$OUT"
    echo "polydye: board found at $port, but the add-on's Klipper files are damaged: PolyDye left off, so Klipper" \
        "starts and prints without it" >&2
elif [ -n "$port" ]; then
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
