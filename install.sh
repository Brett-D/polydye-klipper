#!/bin/bash
# Installs PolyDye for Klipper. Safe to run again.
#
#   cd ~ && git clone https://github.com/Brett-D/polydye-klipper.git && ~/polydye-klipper/install.sh
#
# What it does (each step is skipped if it's already done):
#   1. Links the Klipper add-on into Klipper's extras and hides it from Klipper's git status.
#   2. Links the Moonraker component (serves the PolyDye page) and hides it from Moonraker's git status.
#   3. Creates polydye.cfg (your settings) from the template if you don't have one.
#   4. Adds [include polydye-active*.cfg] to printer.cfg, above any SAVE_CONFIG block (backup made first).
#   5. Installs a hook so polydye-toggle.sh runs whenever the Klipper service starts: with the PolyDye board
#      plugged in, PolyDye is enabled; without it, Klipper starts exactly as before. It also restores the link
#      from step 1 if that has gone missing.
#   6. Adds [polydye] and, from the main branch, [update_manager polydye] to moonraker.conf.
#   7. Adds a PolyDye entry to Mainsail's sidebar (.theme/navi.json).
#   8. Restarts Moonraker (if its config changed) and Klipper.
#
# Overrides for unusual setups: KLIPPER_DIR, MOONRAKER_DIR, CONFIG_DIR, KLIPPER_SERVICE, MOONRAKER_SERVICE.
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
NO_SERVICES="${NO_SERVICES:-}"  # set to skip systemctl calls (tests)

say() { echo "polydye: $*"; }
die() { echo "polydye: ERROR: $*" >&2; exit 1; }

# Adds a line to a git checkout's private exclude list, so our link doesn't make it "dirty".
git_exclude() {
    local repo="$1" path="$2"
    [ -d "$repo/.git" ] || return 0
    mkdir -p "$repo/.git/info"
    grep -qxF "$path" "$repo/.git/info/exclude" 2>/dev/null || echo "$path" >> "$repo/.git/info/exclude"
}

[ -d "$KLIPPER_DIR/klippy/extras" ] || die "Klipper not found at $KLIPPER_DIR (set KLIPPER_DIR)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "printer.cfg not found in $CONFIG_DIR (set CONFIG_DIR)"
command -v python3 > /dev/null || die "python3 is needed"

# 1. Klipper add-on
ln -sfn "$REPO_DIR/klipper/polydye.py" "$KLIPPER_DIR/klippy/extras/polydye.py"
git_exclude "$KLIPPER_DIR" "klippy/extras/polydye.py"
chmod +x "$REPO_DIR/scripts/polydye-toggle.sh"
say "Klipper add-on linked"

# 2. Moonraker component
MOONRAKER_CHANGED=
COMPONENTS="$MOONRAKER_DIR/moonraker/components"
if [ -d "$COMPONENTS" ]; then
    if [ "$(readlink "$COMPONENTS/polydye.py" 2>/dev/null)" != "$REPO_DIR/moonraker/polydye.py" ]; then
        ln -sfn "$REPO_DIR/moonraker/polydye.py" "$COMPONENTS/polydye.py"
        MOONRAKER_CHANGED=1
    fi
    git_exclude "$MOONRAKER_DIR" "moonraker/components/polydye.py"
    say "Moonraker component linked"
else
    say "Moonraker not found at $MOONRAKER_DIR: skipped the PolyDye page (set MOONRAKER_DIR)"
fi

# 3. User settings
if [ ! -f "$CONFIG_DIR/polydye.cfg" ]; then
    cp "$REPO_DIR/config/polydye.cfg.example" "$CONFIG_DIR/polydye.cfg"
    say "created polydye.cfg: set pass_axis and the offsets for your mount"
fi

# 4. printer.cfg include
PRINTER_CFG="$CONFIG_DIR/printer.cfg"
INCLUDE_LINE='[include polydye-active*.cfg]'
if ! grep -qxF "$INCLUDE_LINE" "$PRINTER_CFG"; then
    cp "$PRINTER_CFG" "$PRINTER_CFG.polydye-backup-$(date +%Y%m%d-%H%M%S)"
    awk -v line="$INCLUDE_LINE" '
        !done && /^#\*# <-+ SAVE_CONFIG -+>/ { print line; done = 1 }
        { print }
        END { if (!done) print line }
    ' "$PRINTER_CFG" > "$PRINTER_CFG.polydye-tmp"
    mv "$PRINTER_CFG.polydye-tmp" "$PRINTER_CFG"
    say "added $INCLUDE_LINE to printer.cfg (backup saved next to it)"
fi

# 5. Plug-in toggle hook on the Klipper service
DROPIN_DIR="$SYSTEMD_DIR/$KLIPPER_SERVICE.service.d"
DROPIN="$DROPIN_DIR/polydye.conf"
DROPIN_TEXT="[Service]
ExecStartPre=-$REPO_DIR/scripts/polydye-toggle.sh $CONFIG_DIR $KLIPPER_DIR"
if [ "$(cat "$DROPIN" 2>/dev/null)" != "$DROPIN_TEXT" ]; then
    $SUDO mkdir -p "$DROPIN_DIR"
    printf '%s\n' "$DROPIN_TEXT" | $SUDO tee "$DROPIN" > /dev/null
    [ -n "$NO_SERVICES" ] || $SUDO systemctl daemon-reload
    say "installed the plug-in toggle ($DROPIN)"
fi

# 6. moonraker.conf
MOONRAKER_CONF="$CONFIG_DIR/moonraker.conf"
if [ -f "$MOONRAKER_CONF" ]; then
    if [ -d "$COMPONENTS" ] && ! grep -q '^\[polydye\]' "$MOONRAKER_CONF"; then
        printf '\n[polydye]\n# Serves the PolyDye page (Mainsail sidebar: PolyDye). Installed by polydye-klipper.\nklipper_service: %s\n' \
            "$KLIPPER_SERVICE" >> "$MOONRAKER_CONF"
        MOONRAKER_CHANGED=1
        say "added [polydye] to moonraker.conf"
    fi
    if [ -d "$REPO_DIR/.git" ] && ! grep -q '^\[update_manager polydye\]' "$MOONRAKER_CONF"; then
        ORIGIN=$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)
        BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
        if [ -n "$ORIGIN" ] && [ "$BRANCH" = "main" ]; then
            cat >> "$MOONRAKER_CONF" <<EOF

[update_manager polydye]
type: git_repo
path: $REPO_DIR
origin: $ORIGIN
primary_branch: main
managed_services: $KLIPPER_SERVICE $MOONRAKER_SERVICE
EOF
            MOONRAKER_CHANGED=1
            say "added [update_manager polydye] to moonraker.conf: updates appear in Mainsail"
        elif [ -n "$ORIGIN" ]; then
            say "on branch '$BRANCH', not main: skipped the Mainsail update entry (run install.sh again on main)"
        fi
    fi
fi

# 7. Mainsail sidebar entry
if [ -d "$COMPONENTS" ]; then
    mkdir -p "$CONFIG_DIR/.theme"
    python3 "$REPO_DIR/scripts/navi_entry.py" add "$CONFIG_DIR/.theme/navi.json"
fi

# 8. Apply
"$REPO_DIR/scripts/polydye-toggle.sh" "$CONFIG_DIR" "$KLIPPER_DIR"
if [ -z "$NO_SERVICES" ]; then
    [ -z "$MOONRAKER_CHANGED" ] || $SUDO systemctl restart "$MOONRAKER_SERVICE"
    $SUDO systemctl restart "$KLIPPER_SERVICE"
    say "restarted $KLIPPER_SERVICE${MOONRAKER_CHANGED:+ and $MOONRAKER_SERVICE}"
fi
say "done. Reload Mainsail to see PolyDye in the sidebar. Plug in the board and restart the Klipper service to switch"
say "PolyDye on; unplug it and restart to switch it off."
