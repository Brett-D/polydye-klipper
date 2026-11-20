# PolyDye for Klipper

Klipper, Moonraker and Mainsail support for **PolyDye**, a full-colour inkjet mod for 3D printers. An HP 63/302/123/803
cartridge driven by an ESP32-S3 board prints ink between layers.

The PolyDye board joins Klipper as a second MCU (`[mcu polydye]`) over USB, so Klipper's own clock synchronisation
tells it exactly when each ink pass starts. No extra wiring to the printer's mainboard is needed.

- **Plug-in toggle:** with the board plugged in, PolyDye is enabled. Without it, Klipper starts exactly as it did
  before PolyDye was installed, so you can keep printing normally.
- **PolyDye page** in Mainsail's sidebar: board status, a dry-run test, settings and, later, calibration.
- **Updates** through Mainsail's update manager, next to Klipper.

The board needs the PolyDye firmware with the Klipper link (phase 3 or later).

Licence: GPLv3.

## Install

On the Raspberry Pi (Klipper, Moonraker and Mainsail, for example MainsailOS):

```
cd ~ && git clone https://github.com/Brett-D/polydye-klipper.git
~/polydye-klipper/install.sh
```

Then reload Mainsail. **PolyDye** appears in the sidebar.

`install.sh` is safe to run again. It:
- links `klipper/polydye.py` into Klipper's extras and `moonraker/polydye.py` into Moonraker's components, and
  hides both from their git status, so Klipper and Moonraker updates keep working;
- creates `polydye.cfg` in your config folder from `config/polydye.cfg.example`, if you don't have one;
- adds `[include polydye-active*.cfg]` to `printer.cfg`, above any `SAVE_CONFIG` block (a backup is saved first);
- installs the plug-in toggle (below) as a hook on the Klipper service;
- adds `[polydye]` and `[update_manager polydye]` to `moonraker.conf`;
- adds a PolyDye entry to Mainsail's sidebar (`.theme/navi.json`, keeping any entries you already have);
- restarts Moonraker and Klipper.

**Uninstall:** `~/polydye-klipper/uninstall.sh` removes all of it and keeps your `polydye.cfg`; `--purge` deletes
that too. Afterwards `printer.cfg` and `moonraker.conf` are byte-for-byte what they were before.

### The plug-in toggle

- `printer.cfg` permanently has `[include polydye-active*.cfg]`.
- `scripts/polydye-toggle.sh` runs each time the Klipper service starts. If the board is on USB, it writes
  `polydye-active.cfg` with `[mcu polydye]`, an include of your `polydye.cfg` and the PolyDye macros. Otherwise it
  deletes that file.
- A wildcard include that matches nothing is ignored, so without the board Klipper starts exactly as before.

**To switch,** plug or unplug the board's J7 USB, then restart the **Klipper service**: Mainsail's power menu →
Services → klipper, or `sudo systemctl restart klipper`. `RESTART` and `FIRMWARE_RESTART` don't re-run the script.

**Unplugging while Klipper runs** shuts Klipper down ("Lost communication with MCU 'polydye'"), as with any MCU. The
board's status updates stop there. Plug the board back in and use `FIRMWARE_RESTART`. Without the board,
`FIRMWARE_RESTART` waits for it (Klipper shows "startup") and gives up with an error after about 90 s. To carry
on without PolyDye, restart the Klipper service instead.

### Hardware notes

- Power the board from its own supply (J4). J7 carries data only: its 5 V pin isn't tied to the board's 5 V
  rail, so a normal USB cable from J7 to the Pi is fine.
- If the FT232 programmer stays wired to the board's J1 header, keep its USB plugged into the Pi. An unpowered
  FT232 holds the board in its bootloader.

## The PolyDye page

Mainsail sidebar → **PolyDye**, or `http://<printer>/server/polydye/ui`. The page talks to Moonraker like Mainsail
does. It shows:
- **Emergency stop**, always at the top: stops all motion at once and shuts Klipper down (like M112), which also
  turns the board's boost off. It works while a macro such as the dry run is running. **Firmware restart** then
  appears to bring Klipper back.
- **Board status**, live: firmware, boost, dry run or live ink, passes queued and printed, faults, worst column
  lateness, stored passes, cartridge template and fire pulse.
- **Controls:** dry-run test, cartridge wipe, status, off, and enabling live ink (with a confirmation).
- **Settings:** pass axis, offsets, direction trim, cartridge template, fire pulse, resolution and pass speed, saved into the
  `[polydye]` section of `polydye.cfg`. Klipper applies them after a restart.
- **Ink position:** live nudges for lining the ink up with the plastic. Pick a step (0.02–0.4 mm) and move the
  ink ±X or ±Y, or move only the reverse passes along the pass axis (direction trim). Nudges apply from the next
  pass, also during a print, with no restart. Values not yet in `polydye.cfg` show "not stored". **Store** writes
  them into `polydye.cfg` and **Back to stored values** undoes the nudges.
- **Calibration:** nozzle check, cartridge offset, direction alignment and pulse length. The printed patterns need
  ink and are not enabled yet; the Ink position nudges already work.
- **Board firmware:** the board's version and the one bundled with this add-on, with buttons to update to it or
  to a `.bin` you choose (see below).
- The pill at the top right sums up the state: Moonraker offline, Klipper starting or shut down, PolyDye not
  enabled, PolyDye ready, dry run, LIVE ink. If the board is plugged in but Klipper started without it, **Enable
  PolyDye** restarts the Klipper service so the plug-in toggle picks it up.

## Board firmware updates

The board's firmware updates over its USB cable (J7), from the PolyDye page or the command line:

```
python3 ~/polydye-klipper/scripts/polydye_upload.py firmware ~/polydye-klipper/firmware/polydye.bin
```

`firmware/polydye.bin` is the firmware this version of the add-on was tested with, built from
[polydye-firmware](https://github.com/Brett-D/polydye-firmware); add-on updates in Mainsail bring new builds.
- The image goes into the board's spare firmware slot while the board keeps running, is checked, and the board
  restarts into it. Klipper loses the board for about half a minute and reconnects (the page runs
  `FIRMWARE_RESTART`). An update takes about 25 s: the transfer is paced, because a Raspberry Pi 3 lost USB data
  and locked up at full speed.
- The new firmware must be confirmed by the Pi within two minutes. If it doesn't come up working, the board goes
  back to the previous firmware by itself.
- The page refuses updates during a print, while PolyDye is on, and while the Pi reports under-voltage. The board
  itself refuses while Klipper has passes queued or the boost or live output on. The command-line updater skips
  the page's checks: run it only with the printer idle and the Pi on a good supply. A Pi 3 needs 5.1 V, 2.5 A;
  phone chargers often sag.
- The first firmware with USB updates has to be flashed once with the FT232, bootloader included (see
  polydye-firmware's README); after that, updates go over USB.

## Configuration

`polydye-active.cfg` (generated; don't edit) contains:

```
[mcu polydye]
serial: /dev/serial/by-id/usb-PolyDye_PolyDye_ink_controller_<MAC>-if00
restart_method: command

[include /home/<user>/polydye-klipper/config/polydye_macros.cfg]
[include polydye.cfg]
```

`restart_method: command` makes `FIRMWARE_RESTART` reboot the board with Klipper's reset command. Klipper picks
that by itself once it has talked to the board. Without the option, a `FIRMWARE_RESTART` while the board is missing
would try an Arduino-style reset on its absent port. That fails, and Moonraker is left talking to a Klipper that
no longer answers until the Klipper service restarts.

Your `polydye.cfg`:

```
[polydye]
# Required. They depend on how the cartridge is mounted; measure, then calibrate.
pass_axis: y          # x or y: the axis the cartridge moves along while printing
offset_x: 58.6        # cartridge nozzle-row centre minus hotend nozzle, mm (bed coordinates)
offset_y: -8.7
# Optional:
#direction_trim: 0    # mm: moves the ink of reverse (DIR=-1) passes along the pass axis to meet forward passes
#cartridge_template:  # color_a, color_b, black_a or black_b; default: the board's own (color_b)
#pulse_words:         # fire pulse, 6-12 words of 0.3 us; default: the board's own (6 = 1.8 us)
#dpi: 600             # dots per inch along the pass
#pass_speed: 50       # mm/s
#travel_speed: 150    # mm/s, moves to the start of each pass
#extra_run_up: 1.0    # mm added to the computed run-up
#settle_time:         # s; default: the input shaper's impulse length for the pass axis
#min_lead_time: 0.025 # s; a pass must be scheduled at least this far ahead
#mcu: polydye
#data_port:           # the board's data port; default: the [mcu polydye] serial path with -if02 instead of -if00
```

The template has the nominal values (±5 mm) for the PolyDye mount on an Ender 3 with a Sprite extruder: the
carrier bolts to the four side holes of the Sprite's motor, the cartridge sits right of the hotend and prints along
Y, and the BLTouch stays in its stock position. With these offsets the cartridge can ink bed X 59–229 and Y 0–199
of the 229 × 208 mm print area. Other mounts need their own values; measure from the hotend nozzle to the centre
of the cartridge's nozzles, negative Y if the cartridge is in front.

On a bed-slinger such as the Ender 3, Y moves the bed, so Y passes shake it. Configure an `[input_shaper]` for
the pass axis, and the run-up will include its settle time automatically.

`cartridge_template` picks the nozzle waveform, from Sprite_tm's printercart templates: a and b are two variants
for each cartridge type. Klipper sends it to the board on every connect. A black template makes the board expect
pass data for a black cartridge. It needs board firmware with template support (this add-on's bundled firmware or
newer); with older firmware Klipper refuses to start and asks you to update the board or remove the option.

`pulse_words` sets how long each nozzle's heater is fired, in words of 0.3 µs: 6 (1.8 µs, Sprite_tm's value and the
board's default) to 12 (3.6 µs). A longer pulse puts more energy into each drop. Try it if the ink doesn't fire,
for example because the board's boost rails are below the cartridge's nominal voltage. Klipper sends it on every
connect, and the board's safety check limits every pulse to it. It needs the same board firmware as
`cartridge_template`.

## How a pass works

Each ink pass is one G-code command:

```
POLYDYE_PASS LAYER=3 PASS=7 X=120.0 Y=40.0 DIR=1 [SPEED=50]
```

- `X`, `Y`: bed coordinates where the first column lands. On the pass axis that's the first column; on the other
  axis it's the centre of the nozzle rows.
- `DIR`: `1` or `-1`, the direction along the pass axis.
- `SPEED`: pass speed in mm/s; defaults to `pass_speed`.

The add-on:
1. Asks the board whether the pass is uploaded and how many columns it has.
2. Moves the hotend to the start of the run-up (through the bed mesh, if any), then stops.
3. Plans the pass as one straight move from rest along the pass axis, at constant Z.
4. Computes when the first column passes the cartridge, from Klipper's acceleration, the speed and the run-up. It
   then checks the plan against the move Klipper actually queued; if they differ, no ink is scheduled.
5. Sends the board the first column's time on the board's clock and the column period (`25.4 / dpi / speed`).

The run-up is long enough to reach full speed, let the input shaper settle (its impulse length for the pass axis)
and satisfy Klipper's `minimum_cruise_ratio`, plus `extra_run_up`. The same length is used after the last column.

If anything would print a pass wrongly, Klipper stops with a readable reason, for example:
- `polydye: pass data has not been uploaded`
- `polydye: column period too short for the cartridge`
- `Lost communication with MCU 'polydye'`

## Commands

| Command | Does |
|---|---|
| `POLYDYE_ENABLE [LIVE=1]` | Turns the board's boost on. `LIVE=1` (default) allows real ink; `LIVE=0` is a dry run: the bus runs with the fire lines held off |
| `POLYDYE_DISABLE` | Waits for queued moves, then turns live output and the boost off |
| `POLYDYE_PASS LAYER= PASS= X= Y= [DIR=1] [SPEED=]` | Prints one pass (above) |
| `POLYDYE_CANCEL` | Drops passes that are scheduled but not started (for a cancel macro) |
| `POLYDYE_STATUS` | Board status: boost, live, passes printed and queued, worst column lateness, faults, stored passes, cartridge template, fire pulse; and the offsets and direction trim in use |
| `POLYDYE_NUDGE [X=] [Y=] [TRIM=]` | Moves where the ink lands by `X`, `Y` mm on the bed, and the ink of reverse passes by `TRIM` mm along the pass axis (each at most ±5 mm), from the next pass on. Changes `offset_x`/`offset_y` (in the opposite direction: the offset is where the cartridge is) and `direction_trim` until the next restart |
| `POLYDYE_OFFSET [X=] [Y=] [TRIM=]` | Sets `offset_x`, `offset_y` and `direction_trim` until the next restart; without arguments, shows them |
| `POLYDYE_CARTRIDGE [TEMPLATE=color_a\|color_b\|black_a\|black_b] [PULSE=6..12]` | Switches the board's waveform template and fire pulse until the next restart (to compare them at the nozzle check); without arguments, shows them. Only with PolyDye off and no passes queued |
| `POLYDYE_UPLOAD_TEST_PASS [LAYER=0] [PASS=0] [COLUMNS=600] [PATTERN=alternate\|all\|none]` | Uploads a test pass over the board's data port, for dry runs. Not during a print: it briefly blocks Klipper while it sends |

Macros (Mainsail's Macros panel, while the board is plugged in):

| Macro | Does |
|---|---|
| `PD_STATUS` | `POLYDYE_STATUS` |
| `PD_DRY_RUN_TEST [X=100] [Y=40]` | Homes if needed, lifts Z to 20 mm, uploads a test pass and prints it in both directions without ink, then reports status. The printer moves; nothing fires |
| `PD_OFF` | Drops queued passes, then turns live output and the boost off |
| `POLYDYE_WIPE [X=245] [LENGTH=25] [STROKES=3] [SPEED=50] [Z_HOP=1]` | Wipes the cartridge on a damp sponge at the right end of the X gantry: lifts Z by `Z_HOP`, moves to `X` (G-code coordinates; 245 suits the PolyDye mount and keeps clear of the parts at the end of the gantry), strokes `STROKES` times between `X` and `X - LENGTH` at `SPEED` mm/s, then returns. It refuses an `X` at or beyond the X axis maximum (your hard stop) before homing or moving. Homes first if needed |

The macros are loaded before `polydye.cfg`, so you can change their defaults there instead of editing the add-on
(which would stop Mainsail updating it):

```
[gcode_macro POLYDYE_WIPE]
variable_strokes: 5
variable_length: 20
```

`printer.polydye` (macros, Mainsail, the PolyDye page) has the board status, refreshed every 2 s: `boost`, `live`,
`running`, `queued`, `passes`, `late_max_us`, `faults`, `store_count`, `store_free`, `cartridge_template`, `pulse_words`
and `pulse_us` (`null` with older board firmware). It also has `last_pass`, `pass_axis`, `offset_x`, `offset_y` and `direction_trim`, the
values in use including nudges.

## Uploading passes

Pass data goes over the board's second USB serial port (`-if02`), not through Klipper:

```
python3 ~/polydye-klipper/scripts/polydye_upload.py info
python3 ~/polydye-klipper/scripts/polydye_upload.py put --layer 0 --pass 0 --columns 600 --pattern alternate
python3 ~/polydye-klipper/scripts/polydye_upload.py job print.pdj
python3 ~/polydye-klipper/scripts/polydye_upload.py list
```

It needs pyserial; use `~/klippy-env/bin/python` if the system Python lacks it. Generating `.pdj` jobs from a
sliced model is the next step (the post-processor).

### Data port

Frames, in both directions (integers little-endian):

```
"PD" | type u8 | seq u8 | length u32 | payload[length] | crc32 u32
```

The CRC is zlib's CRC-32 over type, seq, length and payload. Every request gets one reply with the same `seq`:
`0x80` (OK) or `0xFF` (error), both with a JSON payload.

| Request | Payload | Reply |
|---|---|---|
| `0x01` HELLO | none | `{"protocol":2,"firmware":…,"nozdata_size":42,"cart":"color"\|"black","passes":N,"max_passes":N,"free":bytes,"partition":"ota_0"\|"ota_1","app_state":"valid"\|"pending"\|"unknown","rolled_back":null\|version}` |
| `0x10` PUT | layer u16, pass u16, cart u8, flags u8 (1 = delete after printing), columns u16, then columns × 42 bytes | `{"stored":[layer,pass],"columns":N}` |
| `0x11` DELETE | layer u16, pass u16 | `{}` |
| `0x12` CLEAR | none | `{"deleted":N}` |
| `0x13` LIST | none | `{"passes":[[layer,pass,columns,cart,flags],…]}` |
| `0x20` FW_BEGIN | image size u32, SHA-256 (32 bytes) | `{"partition":…}` |
| `0x21` FW_DATA | offset u32, then up to 4096 image bytes, in order (a resent last block is accepted) | `{}` |
| `0x22` FW_END | none | `{"version":…}`: checked and selected for the next boot |
| `0x23` FW_ABORT | none | `{}` |
| `0x24` FW_CONFIRM | none | `{"app_state":"valid"}`: keeps a new firmware that would otherwise roll back |
| `0x25` REBOOT | none | `{}`, then the board unplugs from USB and restarts |

The firmware requests are refused while Klipper is using the board.

Job files (`.pdj`, version 0): `b"PDJ0"`, a u32 pass count, then for each pass a u32 length and a PUT payload.

## Development

- `tests/` has unit tests for the pass timing and geometry, the macros, the firmware image checks, and a round trip
  of `install.sh` / `uninstall.sh` in a fake Klipper/Moonraker tree. CI runs them on every push.
- The end-to-end test (real klippy against a simulated PolyDye board) lives with the firmware, because it builds
  the board's protocol code. It uses this repo's add-on and macros.
