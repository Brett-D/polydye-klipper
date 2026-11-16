#!/bin/bash
# Removes PolyDye from Klipper, Moonraker and Mainsail. Your polydye.cfg is kept unless you pass --purge.
#
#   ~/polydye-klipper/uninstall.sh [--purge]
#
# Uses the same overrides as install.sh. Afterwards printer.cfg and moonraker.conf are as they were before.
#
# This file is part of polydye-klipper (GPLv3).
set -e

REPO_DIR=$(cd "$(dirname "$0")" && pwd)
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
MOONRAKER_DIR="${MOONRAKER_DIR:-$HOME/moonraker}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
MOONRAKER_SERVICE="${MOONRAKER_SERVICE:-moonraker}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDO="${SUDO-sudo}"
NO_SERVICES="${NO_SERVICES:-}"

say() { echo "polydye: $*"; }

git_unexclude() {
    local file="$1/.git/info/exclude" path="$2"
    [ -f "$file" ] || return 0
    { grep -vxF "$path" "$file" || true; } > "$file.polydye-tmp"
    mv "$file.polydye-tmp" "$file"
}

# Drops one [section] (header to the next header) and the blank lines just before it.
drop_section() {
    local file="$1" header="$2"
    [ -f "$file" ] && grep -qxF "$header" "$file" || return 1
    awk -v header="$header" '
        /^\[/ { skip = ($0 == header) }
        skip { blanks = ""; next }
        /^[ \t]*$/ { blanks = blanks $0 "\n"; next }
        { printf "%s", blanks; blanks = ""; print }
        END { printf "%s", blanks }
    ' "$file" > "$file.polydye-tmp"
    mv "$file.polydye-tmp" "$file"
}

rm -f "$KLIPPER_DIR/klippy/extras/polydye.py"
git_unexclude "$KLIPPER_DIR" "klippy/extras/polydye.py"

MOONRAKER_CHANGED=
if [ -L "$MOONRAKER_DIR/moonraker/components/polydye.py" ]; then
    rm -f "$MOONRAKER_DIR/moonraker/components/polydye.py"
    MOONRAKER_CHANGED=1
fi
git_unexclude "$MOONRAKER_DIR" "moonraker/components/polydye.py"

DROPIN="$SYSTEMD_DIR/$KLIPPER_SERVICE.service.d/polydye.conf"
if [ -f "$DROPIN" ]; then
    $SUDO rm -f "$DROPIN"
    $SUDO rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
    [ -n "$NO_SERVICES" ] || $SUDO systemctl daemon-reload
fi

PRINTER_CFG="$CONFIG_DIR/printer.cfg"
if [ -f "$PRINTER_CFG" ]; then
    grep -vxF '[include polydye-active*.cfg]' "$PRINTER_CFG" > "$PRINTER_CFG.polydye-tmp" || true
    mv "$PRINTER_CFG.polydye-tmp" "$PRINTER_CFG"
fi
rm -f "$CONFIG_DIR/polydye-active.cfg"

MOONRAKER_CONF="$CONFIG_DIR/moonraker.conf"
drop_section "$MOONRAKER_CONF" "[polydye]" && MOONRAKER_CHANGED=1
drop_section "$MOONRAKER_CONF" "[update_manager polydye]" && MOONRAKER_CHANGED=1

[ -f "$CONFIG_DIR/.theme/navi.json" ] && python3 "$REPO_DIR/scripts/navi_entry.py" remove "$CONFIG_DIR/.theme/navi.json"

if [ "$1" = "--purge" ]; then
    rm -f "$CONFIG_DIR/polydye.cfg"
    say "removed polydye.cfg"
fi

if [ -z "$NO_SERVICES" ]; then
    [ -z "$MOONRAKER_CHANGED" ] || $SUDO systemctl restart "$MOONRAKER_SERVICE"
    $SUDO systemctl restart "$KLIPPER_SERVICE"
fi
say "removed. The printer runs exactly as before PolyDye was installed."
