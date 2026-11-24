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
check 'grep -qF "ExecStartPre=-$REPO/scripts/polydye-toggle.sh \"$T/config\"" "$T/systemd/klipper.service.d/polydye.conf"' \
    "toggle hook installed"
check 'python3 -c "import json,sys; e=json.load(open(sys.argv[1])); t=[x[\"title\"] for x in e]; \
    sys.exit(not (t.count(\"PolyDye\") == 1 and \"Spoolman\" in t))" "$T/config/.theme/navi.json"' \
    "sidebar entry added once, existing entries kept"
check '! grep -q "added\|created\|installed\|restored" "$T/install2.log"' "second install changes nothing"
check '[ ! -e "$T/config/polydye-active.cfg" ]' "no board: PolyDye stays off"
check 'grep -qxF "ExecStartPre=-$REPO/scripts/polydye-toggle.sh \"$T/config\" \"$T/klipper\"" \
    "$T/systemd/klipper.service.d/polydye.conf"' "toggle hook passes both dirs, quoted for systemd"

# The toggle puts back a lost or broken extras link (and Klipper's git exclude), as the Klipper service starts.
EXTRA="$T/klipper/klippy/extras/polydye.py"
rm "$EXTRA"
: > "$T/klipper/.git/info/exclude"
"$REPO/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle1.log"
check '[ "$(readlink "$EXTRA")" = "$REPO/klipper/polydye.py" ] && grep -q restored "$T/toggle1.log"' \
    "toggle restores a missing extras link"
check 'grep -qx "klippy/extras/polydye.py" "$T/klipper/.git/info/exclude"' "toggle restores the git exclude"
ln -sfn "$T/gone/polydye.py" "$EXTRA"
HOME="$T" "$REPO/scripts/polydye-toggle.sh" "$T/config" > "$T/toggle2.log"
check '[ "$(readlink "$EXTRA")" = "$REPO/klipper/polydye.py" ]' \
    "toggle repairs a broken link (Klipper dir defaults to ~/klipper, as with older hooks)"
"$REPO/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle3.log"
check '! grep -q restored "$T/toggle3.log" && [ "$(grep -c polydye "$T/klipper/.git/info/exclude")" = 1 ]' \
    "a good link is left alone"

# The toggle checks the add-on's own files: a copy of the add-on as a git checkout (with any uncommitted work), its
# files older than git's index as after a real checkout, and a board "plugged in".
A="$T/addon"
mkdir -p "$A"
(cd "$REPO" && git ls-files -z --cached --others --exclude-standard | xargs -0 cp --parents -t "$A")
find "$A" -type f -exec touch -d 2000-01-01 {} +
git -C "$A" init -q
git -C "$A" add -A
git -C "$A" -c user.name=test -c user.email=test@example.com commit -q -m add-on
mkdir -p "$T/by-id"
touch "$T/by-id/usb-PolyDye_PolyDye_ink_controller_E4B063-if00"
export POLYDYE_BY_ID_DIR="$T/by-id"
# What an unsafe shutdown leaves: the size and time git recorded, but zeros instead of the content.
zero_fill() {
    local f="$A/$1" size
    size=$(stat -c %s "$f")
    touch -r "$f" "$T/ref"
    head -c "$size" /dev/zero > "$f"
    touch -r "$T/ref" "$f"
}
"$A/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle4.log" 2>&1
check 'grep -qx "\[mcu polydye\]" "$T/config/polydye-active.cfg" && ! grep -q DAMAGED "$T/toggle4.log"' \
    "board plugged in, files intact: PolyDye on"
zero_fill web/polydye.css
# A crash leaves the times git recorded; this rewrite can't keep the change time, so git is told to ignore it.
check '[ -z "$(git -c core.trustctime=false -C "$A" status --porcelain)" ]' "git status misses a zero-filled file"
"$A/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle5.log" 2>&1
check 'grep -q "ADD-ON FILES DAMAGED: web/polydye.css" "$T/toggle5.log" && [ -f "$T/config/polydye-active.cfg" ]' \
    "a damaged page file is reported; PolyDye stays on"
zero_fill klipper/polydye.py
"$A/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle6.log" 2>&1
check '[ ! -e "$T/config/polydye-active.cfg" ] && grep -q "Klipper files are damaged: PolyDye left off" "$T/toggle6.log"' \
    "a damaged Klipper module: PolyDye left off, so Klipper starts and prints"
# Rewrite every file from git, as a recovery would. git skips files whose size and time still match its index (so a
# plain `git reset --hard` doesn't repair this damage): change the times first.
(cd "$A" && git ls-files -z | xargs -0 touch && git checkout-index -f -a)
"$A/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle7.log" 2>&1
check '[ -f "$T/config/polydye-active.cfg" ] && ! grep -q DAMAGED "$T/toggle7.log"' "recovered files: PolyDye on again"
zero_fill scripts/polydye_files.py
"$A/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle8.log" 2>&1
check '[ ! -e "$T/config/polydye-active.cfg" ] && grep -q "DAMAGED: couldn.t check them" "$T/toggle8.log"' \
    "a damaged file checker: PolyDye left off too"
(cd "$A" && git ls-files -z | xargs -0 touch && git checkout-index -f -a)
# Cut off mid-line, so Python's error message quotes 'print("result: ...'.
python3 -c 'import sys; p = sys.argv[1]; s = open(p).read(); open(p, "w").write(s[:s.index("print(\"result: ") + 25])' \
    "$A/scripts/polydye_files.py"
"$A/scripts/polydye-toggle.sh" "$T/config" "$T/klipper" > "$T/toggle9.log" 2>&1
check '[ ! -e "$T/config/polydye-active.cfg" ] && grep -q "DAMAGED: couldn.t check them" "$T/toggle9.log"' \
    "a truncated file checker isn't trusted either"
unset POLYDYE_BY_ID_DIR

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
