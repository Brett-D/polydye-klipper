#!/bin/bash
# Round trip of install.sh and uninstall.sh in a fake Klipper/Moonraker/config tree (no systemd, no sudo).
#
# This file is part of polydye-klipper (GPLv3).
# shellcheck disable=SC2016  # check() takes single-quoted expressions and evaluates them later
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
fail=0
check() { if eval "$1"; then echo "PASS  $2"; else echo "FAIL  $2"; fail=1; fi; }

mkdir -p "$T/klipper/klippy/extras" "$T/moonraker/moonraker/components" "$T/config/.theme" "$T/systemd"
git init -q "$T/klipper"
git init -q "$T/moonraker"
cat > "$T/config/printer.cfg" <<'EOF'
[include mainsail.cfg]
[printer]
kinematics: cartesian

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [bltouch]
#*# z_offset = 1.0
EOF
printf '[server]\nhost: 0.0.0.0\n\n[update_manager]\nrefresh_interval: 168\n' > "$T/config/moonraker.conf"
printf '[\n  {"title": "Spoolman", "href": "http://spoolman:7912"}\n]\n' > "$T/config/.theme/navi.json"
cp "$T/config/printer.cfg" "$T/printer.orig"
cp "$T/config/moonraker.conf" "$T/moonraker.orig"
cp "$T/config/.theme/navi.json" "$T/navi.orig"

export KLIPPER_DIR="$T/klipper" MOONRAKER_DIR="$T/moonraker" CONFIG_DIR="$T/config" SYSTEMD_DIR="$T/systemd"
export SUDO="" NO_SERVICES=1

bash "$REPO/install.sh" > "$T/install1.log"
bash "$REPO/install.sh" > "$T/install2.log"

check '[ "$(readlink "$T/klipper/klippy/extras/polydye.py")" = "$REPO/klipper/polydye.py" ]' "Klipper add-on linked"
check '[ "$(readlink "$T/moonraker/moonraker/components/polydye.py")" = "$REPO/moonraker/polydye.py" ]' \
    "Moonraker component linked"
check 'grep -qx "klippy/extras/polydye.py" "$T/klipper/.git/info/exclude"' "hidden from Klipper's git status"
check 'grep -qx "moonraker/components/polydye.py" "$T/moonraker/.git/info/exclude"' "hidden from Moonraker's git status"
check '[ -f "$T/config/polydye.cfg" ]' "polydye.cfg created"
check '[ "$(grep -c "^\[include polydye-active\*\.cfg\]$" "$T/config/printer.cfg")" = 1 ]' "include added once"
check 'awk "/include polydye-active/{i=NR} /SAVE_CONFIG/{s=NR} END{exit !(i && i < s)}" "$T/config/printer.cfg"' \
    "include is above SAVE_CONFIG"
check '[ "$(grep -c "^\[polydye\]$" "$T/config/moonraker.conf")" = 1 ]' "[polydye] added to moonraker.conf once"
check 'grep -qx "klipper_service: klipper" "$T/config/moonraker.conf"' "[polydye] names the Klipper service for Enable PolyDye"
check 'grep -q "ExecStartPre=-$REPO/scripts/polydye-toggle.sh $T/config" "$T/systemd/klipper.service.d/polydye.conf"' \
    "toggle hook installed"
check 'python3 -c "import json,sys; e=json.load(open(sys.argv[1])); t=[x[\"title\"] for x in e]; \
    sys.exit(not (t.count(\"PolyDye\") == 1 and \"Spoolman\" in t))" "$T/config/.theme/navi.json"' \
    "sidebar entry added once, existing entries kept"
check '! grep -q "added\|created\|installed" "$T/install2.log"' "second install changes nothing"
check '[ ! -e "$T/config/polydye-active.cfg" ]' "no board: PolyDye stays off"

bash "$REPO/uninstall.sh" > /dev/null
check 'cmp -s "$T/printer.orig" "$T/config/printer.cfg"' "printer.cfg restored byte for byte"
check 'cmp -s "$T/moonraker.orig" "$T/config/moonraker.conf"' "moonraker.conf restored byte for byte"
check 'python3 -c "import json,sys; sys.exit(json.load(open(sys.argv[1])) != json.load(open(sys.argv[2])))" \
    "$T/navi.orig" "$T/config/.theme/navi.json"' "sidebar back to the original entries"
check '[ ! -e "$T/klipper/klippy/extras/polydye.py" ] && [ ! -e "$T/moonraker/moonraker/components/polydye.py" ]' \
    "links removed"
check '! grep -q polydye "$T/klipper/.git/info/exclude" "$T/moonraker/.git/info/exclude"' "git excludes removed"
check '[ ! -e "$T/systemd/klipper.service.d" ]' "toggle hook removed"
check '[ -f "$T/config/polydye.cfg" ]' "polydye.cfg kept"

exit $fail
