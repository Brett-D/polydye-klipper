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
- It also restores the add-on's link in `~/klipper/klippy/extras` if it has gone missing or broken. A lost link
  makes Klipper halt with "Section 'polydye' is not a valid config section"; restarting the Klipper service fixes
  it.
- It first writes everything pending to the SD card (`sync`), then checks the add-on's own files against git
  (`scripts/polydye_files.py`). If the files Klipper loads are damaged, it leaves PolyDye off so Klipper starts and
  prints without it; see [Raspberry Pi 3: freezes and damaged files](#raspberry-pi-3-freezes-and-damaged-files).

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
- **Calibration:**
  - **Fire test** (live ink on paper, with a confirmation): one stripe per template, fire pulse and bus clock, side by
    side, with a table of where each stripe goes (`PD_FIRE_TEST`). The default stripes are an A/B matrix of our timing
    and the stock PolyDye firmware's: color_b vs color_b_late, our clock vs the stock one, at pulse 6 and the longest
    allowed (9 words with our clock, 8 with the stock one).
  - **Spit test** (live ink, no motion, with a confirmation): every nozzle (or positions 14–90) fires 1000 times a
    second for a few seconds into a paper towel held under the cartridge (`POLYDYE_SPIT`). It also makes a steady load for a USB-C
    power meter on the board's power input, to compare the current with the cartridge in and out.
  - Both tests also have a **sparse** pattern: each fire pulse (PWRA/PWRB) enables exactly one nozzle instead of up
    to 12, going through every nozzle in 12 columns. Column k fires bits b and b + 4, with b = (k / 3) mod 4, on data
    line k mod 3 (D1, D2, D3) in all 14 packets. It shows whether the heaters fire when they don't share a pulse, and
    its supply, with 11 others. The fire test's sparse stripes work with any board firmware; the sparse spit test
    needs board firmware with it (Update to bundled firmware).
  - Both tests can also run at the **half bus clock** (1.667 MHz, `CLOCK=half` or `:half` stripes, with a 3 or 4-word
    pulse): the same waveform with twice the time for every edge, in case the level shifters are too slow under the
    cartridge's load. It needs current board firmware (Update to bundled firmware).
- **Signal check** (cartridge out, multimeter): a guided check of where each cartridge signal arrives. The page
  holds one line HIGH at a time (`POLYDYE_LEVEL`) and shows the voltage to expect. You probe at one of two places:
  - **CD4504 output legs** on the daughter board, black lead on pin 8: the page shows the leg each line should
    come out on (U2 legs 2, 4, 6, 10, 12, 15 for S5, S1, S4, S2, S3, DCLK; U1 legs 4, 6, 10, 12 for D1, D2, CSYNC,
    D3, from the schematic draft and not verified yet), and you note the leg that actually reads it;
  - **pogo pin joints** on the interface board, slid out of the holder and laid face down: the page shows the
    cartridge pad each line should reach, and you note the joint. A power-off continuity step covers the fire
    lines (PWRA, PWRB) and ground.

  Results stay in the browser and can be saved to `polydye_signal_check.txt` in the config folder, with both
  layouts' findings.
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
  restarts into it. Restarting, it leaves USB and comes back, which has frozen Raspberry Pi 3s. So the page first
  writes everything pending to the SD card and stops the Klipper service, waits until nothing has the board's
  Klipper port open, updates, syncs again and starts Klipper once the board is back (about half a minute in all).
  The transfer is paced (about 25 s), because a Raspberry Pi 3 lost USB data and locked up at full speed.
- From the command line, stop Klipper first (`sudo systemctl stop klipper`) and start it again afterwards: the
  updater refuses while a program has the board's Klipper port open.
- The new firmware must be confirmed by the Pi within two minutes. If it doesn't come up working, the board goes
  back to the previous firmware by itself.
- The page refuses updates during a print, while PolyDye is on, and while the Pi reports under-voltage. The board
  itself refuses while Klipper has passes queued or the boost or live output on. The command-line updater skips
  the page's checks: run it only with the printer idle and the Pi on a good supply. A Pi 3 needs 5.1 V, 2.5 A;
  phone chargers often sag.
- The first firmware with USB updates has to be flashed once with the FT232, bootloader included (see
  polydye-firmware's README); after that, updates go over USB.

## Raspberry Pi 3: freezes and damaged files

On a Raspberry Pi 3, the board leaving USB and coming back has frozen the whole Pi (no network, no ping) until it was
power-cycled. It happened when Klipper reset the board right after an add-on update, which Klipper does by itself
when the board's configuration no longer matches. Files the update had just written were still only in memory, and
came back empty or full of zeros: Klipper then stopped with "Section 'polydye' is not a valid config section", the
PolyDye page said the bundled firmware was "not an ESP32 firmware image", and yet git and the Update Manager called
the add-on unchanged, because git only compares sizes and times.

It is a known problem of the Pi's `dwc_otg` USB driver with USB serial (ACM) devices on recent kernels
([raspberrypi/linux#6172](https://github.com/raspberrypi/linux/issues/6172)), not a power problem. What PolyDye does
about it:

- Klipper's reset (`FIRMWARE_RESTART`, and Klipper's automatic restart after a configuration change) resets the board
  in place, without leaving USB, with board firmware that has `POLYDYE_SOFT_RESET`. With older firmware the PolyDye
  page's Board firmware card says so: update it once, preferably as described below.
- Only a firmware update still makes the board leave USB. The page writes everything pending to the SD card and
  stops Klipper first, and the board waits for the Pi to close its ports and leaves USB cleanly before it restarts.
- Klipper's start hook and the Klipper module write everything pending to the SD card before Klipper connects to
  the board, so a freeze can no longer cost files from an update.
- At every start, the add-on compares its files with git. Damaged ones are listed by a Moonraker warning (Mainsail's
  notifications) and on the PolyDye page; if the files Klipper loads are damaged, Klipper starts without PolyDye.

**Recovering damaged files:** in Mainsail, Machine → Update Manager → polydye → Recover → **Hard Recovery** (it
clones the add-on again). If Mainsail doesn't offer Recover because it thinks the add-on is fine, ask Moonraker
directly: `curl -X POST "http://localhost:7125/machine/update/recover?name=polydye&hard=true"` on the Pi. Or clone
it again by hand: `cd ~ && rm -rf polydye-klipper && git clone https://github.com/Brett-D/polydye-klipper.git &&
~/polydye-klipper/install.sh`. Your `polydye.cfg` is in the config folder and isn't touched. A plain
`git reset --hard` doesn't repair this damage: git skips files whose size and time still match its records.

**Updating board firmware that still leaves USB on Klipper's reset:** the safest way is with the FT232 from a PC,
with the board's J7 USB unplugged from the Pi (polydye-firmware README, "Flash and monitor"), then plug J7 back in and
restart the Klipper service. From the Pi, stop Klipper first and sync: `sync && sudo systemctl stop klipper`, then
the command-line update above, then `sync && sudo systemctl start klipper`. The board's restart can still freeze the
Pi that one time, but no files are lost.

**Recommended on a Raspberry Pi 3 (optional; PolyDye's installer never changes the Pi's boot setup).** Verified on a
Pi 3 B with Debian 13 and kernel 6.18:

1. Turn off the `dwc_otg` driver's FIQ state machine. Back up `/boot/firmware/cmdline.txt` (`/boot/cmdline.txt` on
   older systems), then add ` dwc_otg.fiq_fsm_enable=0` to the end of its single line, on that same line:
   ```
   sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
   sudo sed -i '1 s/$/ dwc_otg.fiq_fsm_enable=0/' /boot/firmware/cmdline.txt
   ```
   Reboot. Check: `cat /sys/module/dwc_otg/parameters/fiq_fsm_enable` prints `N`.
2. Let the hardware watchdog reboot a frozen Pi: create `/etc/systemd/system.conf.d/watchdog.conf` with
   ```
   [Manager]
   RuntimeWatchdogSec=15
   ```
   then reboot (or `sudo systemctl daemon-reexec`). Check: `systemctl show -p RuntimeWatchdogUSec` prints
   `RuntimeWatchdogUSec=15s`. A freeze then reboots the Pi within about 15 s instead of needing a power-cycle.

If freezes continue, the fallback is the Pi's other USB driver, `dwc2`: add `dtoverlay=dwc2,dr_mode=host` to
`/boot/firmware/config.txt` (`/boot/config.txt` on older systems) and reboot. It may handle full-speed devices behind
the Pi 3's internal USB hub less well (the PolyDye board and many printer boards are full-speed); remove the line to
go back. To undo step 1, restore the backup of `cmdline.txt`.

## Configuration

`polydye-active.cfg` (generated; don't edit) contains:

```
[mcu polydye]
serial: /dev/serial/by-id/usb-PolyDye_PolyDye_ink_controller_<MAC>-if00
restart_method: command

[include /home/<user>/polydye-klipper/config/polydye_macros.cfg]
[include polydye.cfg]
```

`restart_method: command` makes `FIRMWARE_RESTART` reset the board with Klipper's reset command. Klipper picks
that by itself once it has talked to the board. Board firmware with `POLYDYE_SOFT_RESET` in its data dictionary
resets in place and stays on USB; older firmware restarts the chip, so the board leaves USB and comes back (the
PolyDye page warns about that, see below). Without the option, a `FIRMWARE_RESTART` while the board is missing
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
#cartridge_template:  # color_a, color_b, black_a, black_b, color_b_late or deskjet; default: the board's own (color_b)
#pulse_words:         # fire pulse in bus words, 1.8-2.9 us: 6-9 with our clock, 6-8 with stock, 3-4 with half; default: 6
#bus_clock:           # ours (3.333 MHz), stock (2.857 MHz) or half (1.667 MHz); default: the board's own (ours)
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
for each cartridge type. `color_b_late` is color_b with the stock PolyDye firmware's timing, derived by rule: the
data, CSYNC and F3 edges come one bus word after the DCLK edges instead of with them (the last packet's CSYNC two
words after). `deskjet` (colour) copies the timing of a real HP 63 printer, an HP DeskJet 2130, from a logic capture
of one printing: S1–S5 in turn, each DCLK rising with its S line, CSYNC a copy of S1, and the data lines changing only
a bus word after a falling edge and staying a word past the one that latches them, so they never change at a clock
edge (details in the polydye-firmware README). It needs board firmware with it (this add-on's bundled firmware); with
older firmware Klipper refuses it with the same hint. Klipper sends the template to the board on every connect. A black template makes the board expect
pass data for a black cartridge. It needs board firmware with template support (this add-on's bundled firmware or
newer); with older firmware Klipper refuses to start and asks you to update the board or remove the option.

`pulse_words` sets how long each nozzle's heater is fired, in bus words: 0.3 µs each with our clock, 0.35 µs with the
stock one, 0.6 µs with half. 6 words (1.8 µs) is Sprite_tm's value and the board's default. With `bus_clock` set and
`pulse_words` unset, Klipper sends 1.8 µs at that clock: 6 words, or 3 with half. A setting must be at least 1.8 µs at
its clock. A longer pulse puts more energy into each
drop, for when the board's boost rails are below the cartridge's nominal voltage. Klipper sends it on every connect,
and the board's safety check limits every pulse to it. It needs the same board firmware as `cartridge_template`.

**Live ink is limited to 2.9 µs per pulse, whatever the clock:** 100 % of the heater energy of Sprite_tm's 1.8 µs at
the cartridge's 16 V, at the 12.6 V the PolyDye high rail measures (energy goes with V² × t). That is 9 words with
our clock, 8 with the stock one and 4 with half. The add-on refuses longer settings with a plain error (in `polydye.cfg`,
`POLYDYE_CARTRIDGE`, the fire test and the spit test), and board firmware with `POLYDYE_PULSE_MAX_NS` refuses them
too. Messages show each pulse's energy in % of that reference. The one exception is the experimental extended spit test
(`POLYDYE_SPIT ... EXTENDED=1`, see Commands), which can burn out nozzles.

`bus_clock: stock` runs the cartridge bus with the stock PolyDye firmware's setup instead of ours: 40 MHz crystal,
integer divider 1 and pixel divider 7, with no blank cycles. On paper that is 5.714 MHz, but a divider of 1 is
outside what ESP-IDF allows and the LCD treats it as 2: the board measures 2.82–2.83 MHz, so 2.857 MHz. Pulses are
then 0.35 µs per word (6 words = 2.1 µs, 12 words = 4.2 µs). The board measures the rate from each column it sends,
and `printer.polydye.bus_hz_measured` and the Board card show it. Needs board firmware with `polydye_set_clock`
(this add-on's bundled firmware); `color_b_late` too.

`bus_clock: half` runs our bus setup at half our clock, 1.667 MHz, with the same templates, so every edge of the
waveform has twice the time to settle. The CD4504 level shifters are weak and slow under the cartridge's load
(Sprite_tm notes the same of the MC14504 in his design), and the PolyDye's rails (12.6 V and 7.6 V) are lower than his
16 V and 9 V. A packet then takes 21 µs and a column 294 µs. The fire pulse is 3 words (1.8 µs) or 4 (2.4 µs); 5 words
(3.0 µs) are over the limit. Klipper switches to it through our clock and the new pulse, so the board never sees an
over-limit combination. **The column rate halves:** the board refuses a column period shorter than the column plus
20 µs (315 µs at half, 168 µs at our clock), so the fastest pass at 600 dpi is about 134 mm/s instead of about 250; a
pass scheduled faster shuts Klipper down with "column period too short for the cartridge" (`pass_speed` defaults to
50). Needs board firmware with the `POLYDYE_BUS_CLOCKS` dictionary constant (this add-on's bundled firmware): with
older firmware Klipper refuses to start with `bus_clock: half` and the commands refuse `CLOCK=half`, all with an
"Update to bundled firmware" hint.

`POLYDYE_ENABLE LIVE=1` waits, through the move queue, until the boost has been on for 0.15 s before the next move,
because the board refuses live ink sooner than 0.1 s after the boost comes on.

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
| `POLYDYE_CARTRIDGE [TEMPLATE=color_a\|color_b\|black_a\|black_b\|color_b_late\|deskjet] [PULSE=3..9] [CLOCK=ours\|stock\|half]` | Switches the board's waveform template, fire pulse and bus clock until the next restart (to compare them at the nozzle check); without arguments, shows them. Only with PolyDye off and no passes queued |
| `POLYDYE_UPLOAD_TEST_PASS [LAYER=0] [PASS=0] [COLUMNS=600] [PATTERN=alternate\|all\|none\|mid\|sparse] [REPEAT=1]` | Uploads a test pass over the board's data port. `mid` fires nozzle positions 14–90 of every colour (what the stock firmware fires; colour cartridges only); `sparse` fires one nozzle per fire pulse, a different one each column (12 columns go through them all); `REPEAT` sends each column that many times in a row, so the pass has `COLUMNS` × `REPEAT` columns (at most 65535). Not during a print: it briefly blocks Klipper while it sends |
| `POLYDYE_FIRE_TEST [X=70] [Y=60] [Z=] [SPACING=18] [STRIPES=…] [PATTERN=all\|mid\|sparse] [COLUMNS=600] [UPLOAD=1] [LAYER=0] [PASS=0]` | **Live ink.** Prints one stripe per `TEMPLATE:PULSE[:CLOCK]` in `STRIPES` (`CLOCK` ours, stock or half, e.g. `color_b:4:half`): a pass of `COLUMNS` columns along the pass axis, the stripes `SPACING` mm apart across it, the first at `X`, `Y`. A stripe without a clock uses the one in effect. The default is the A/B matrix `color_b` / `color_b_late` × `ours` / `stock` × pulse 6 / the longest allowed (9 words with our clock, 8 with the stock one; with older board firmware: `color_b:6,color_b:8,color_b:9,color_a:6,color_a:9`). `UPLOAD=0` prints the pass already stored as `LAYER`, `PASS` instead. Moves to `Z` first if given. Refused during a print, also a paused one. Turns PolyDye off and puts the template, pulse, clock and G-code state back afterwards, also after an error. Doesn't home (`PD_FIRE_TEST` does) |
| `POLYDYE_SPIT [SECONDS=2] [TEMPLATE=] [PULSE=] [CLOCK=ours\|stock\|half] [PATTERN=all\|mid\|sparse\|stock] [PERIOD_US=1000] [WARM=0] [WARM_PULSE=1\|2] [WARM_PERIOD_US=] [EXTENDED=1] CONFIRM=1` | **Live ink, no motion.** Fires every nozzle (or positions 14–90, or `sparse`: one nozzle per fire pulse in turn, which needs board firmware with it, or `stock`: like the stock firmware, see below) at 1 kHz, or a column every `PERIOD_US`, for `SECONDS` (at most 10, and at most 10000 columns) into a paper towel held under the cartridge, through the same waveform and safety checks as a pass, starting at least 0.1 s after the boost came on. Refused without `CONFIRM=1`, during a print (also a paused one) or with passes queued. Turns PolyDye off and puts the template, pulse and clock back afterwards. `WARM` warms the head first (see below). **`EXTENDED=1` is experimental and can burn out nozzles** (see below) |
| `POLYDYE_WARM [SECONDS=2] [WARM_PULSE=1\|2] [WARM_PERIOD_US=] [TEMPLATE=] [CLOCK=] CONFIRM=1` | **Experimental, heaters on, no motion.** The head warm-up of `POLYDYE_SPIT WARM=` on its own (see below), at most 10 s |
| `POLYDYE_LEVEL LINE=D1\|D2\|D3\|CSYNC\|S1..S5\|DCLK [STATE=high\|low\|off]` | Signal check, **cartridge out**: holds one cartridge line at its rail voltage (`high`) or 0 V for a multimeter; `STATE=off` puts every line LOW. Needs `POLYDYE_ENABLE LIVE=0` first and nothing queued. The fire lines are never held. A line goes back to LOW after 5 minutes, or when a pass is scheduled or PolyDye turns off |

Macros (Mainsail's Macros panel, while the board is plugged in):

| Macro | Does |
|---|---|
| `PD_STATUS` | `POLYDYE_STATUS` |
| `PD_DRY_RUN_TEST [X=100] [Y=40]` | Homes if needed, lifts Z to 20 mm, uploads a test pass and prints it in both directions without ink, then reports status. The printer moves; nothing fires |
| `PD_OFF` | Drops queued passes, then turns live output and the boost off |
| `PD_FIRE_TEST [X=70] [Y=60] [Z=0.5] [SPACING=18] [PATTERN=all\|mid\|sparse] [STRIPES=…]` | **Live ink.** Refused during a print (also a paused one) before anything moves. Homes if needed, then runs `POLYDYE_FIRE_TEST` at `Z` |
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
and `pulse_us` (`null` with older board firmware), and `level`: the signal-check line held, as `{line, state,
remaining}` in seconds, or `null` (missing with older board firmware); and `bus_clock` (`ours`, `stock` or `half`),
`bus_hz` (nominal) and `bus_hz_measured` (from the last column the board sent, `null` before one), missing with older
board firmware. `pulse_us` follows the clock (2.857 MHz for stock, also with board firmware that reports 5.714).
`bus_clocks` lists the clocks the board firmware offers, `templates` its templates and `spit_patterns` the spit test's
patterns (`stock` with board firmware that also takes `PERIOD_US`). It also has `last_pass`, `pass_axis`, `offset_x`, `offset_y` and `direction_trim`, the
values in use including nudges.

### Stock-emulation spit test

`POLYDYE_SPIT PATTERN=stock` fires the way the stock PolyDye firmware does (from a behavioural reverse-engineering
report): each of nozzle positions 14–85 of each colour fires with probability 255/1024 (24.9 %, its random dither at
full ink), and every column goes out twice in a row. That is about 2.2 heaters per fire pulse on average, at most 9.
Unless you give `TEMPLATE`, `CLOCK` or `PULSE`, it also uses the stock firmware's timing: `color_b_late`, the stock
clock (2.857 MHz) and 6 words (2.10 µs; 1.8 µs at the clock you give). Colour templates only.

`PERIOD_US` sets the column period for any spit test (default 1000 µs, 1 kHz). The stock firmware sends a column every
508 µs or so, with the bus all LOW in between, the same as ours. The period must be at least the column's time on the
bus plus 20 µs: 192 µs for `color_b_late` at the stock clock, more with a longer pulse; the add-on says the minimum. A
spit test is at most 10000 columns (3000 with `EXTENDED=1`), so at 508 µs it lasts at most 5.08 s (1.52 s). That limits
the total number of pulses, not how fast they come: a shorter period heats the nozzles harder, about twice as hard at
508 µs and four times at 254 µs as at 1 kHz. Both need board firmware with them (this add-on's bundled firmware); older
firmware refuses with the update hint.

```
POLYDYE_SPIT SECONDS=3 PATTERN=stock PERIOD_US=508 CONFIRM=1
POLYDYE_SPIT SECONDS=2 PATTERN=stock PERIOD_US=254 CONFIRM=1
```

### Experimental: head warm-up

`POLYDYE_SPIT ... WARM=5` warms the cartridge's head for 5 s (at most 10) right before the spit, the way HP's printers
warm the head before printing: every nozzle fires with a pulse far too short to boil ink, as fast as the column allows,
and the spit's first column follows the last warm-up column with no gap (the board runs both in one go).
`POLYDYE_WARM SECONDS=5 CONFIRM=1` does the warm-up alone.

- `WARM_PULSE` is 1 or 2 bus words, at most 0.75 µs: 0.3 or 0.6 µs with our clock, 0.35 or 0.7 µs with the stock one,
  0.6 µs (1 word only) with half. The default is the longer one the clock allows. Sprite_tm's ink pulse is 1.8 µs at
  16 V; 0.6 µs at the PolyDye's 12.6 V is about 21 % of its energy.
- `WARM_PERIOD_US` defaults to the fastest the column allows, the column's time on the bus plus 20 µs: 168 µs for
  color_b and the stock firmware's templates with our clock, 151 µs for deskjet, 192 µs with the stock clock. A longer
  period warms more gently.
- The messages give the warm-up's total: per nozzle, the heating time and the energy in reference pulses, and the
  heaters' duty (about 0.4 % at 0.6 µs every 151 µs, against 0.48 % for an `EXTENDED=1` spit at 4.8 µs and 1 kHz).
- It works with any spit test, `EXTENDED=1` too, and needs board firmware with it (this add-on's bundled firmware).

```
POLYDYE_SPIT SECONDS=3 TEMPLATE=deskjet PULSE=12 PATTERN=sparse EXTENDED=1 WARM=5 CONFIRM=1
```

### Experimental: extended spit test

> **Warning: `POLYDYE_SPIT ... EXTENDED=1` can burn out nozzles.** It is for one question only: are the heaters
> under-powered, or is the cartridge dead? Use it on a cartridge you are prepared to lose, never for printing.

`EXTENDED=1` lets that one spit test fire a pulse of up to **5.0 µs** instead of 2.9 µs: about 172 % of the heater
energy of Sprite_tm's 1.8 µs at 16 V, at the PolyDye's 12.6 V. `PULSE` is then required, in bus words: 6–16 with our
clock (13 = 3.9 µs, 134 %; 16 = 4.8 µs, 165 %), 6–14 with the stock one (14 = 4.9 µs) and 3–8 with half (8 = 4.8 µs).
Step it up a word at a time. `SECONDS` is at most 3. Everything else is the normal spit test: `CONFIRM=1`, live ink
armed, 1 kHz, no passes queued. Board firmware from before the 5.0 µs limit allows 4.0 µs; the add-on follows what the
board says (its `POLYDYE_PULSE_MAX_EXT_NS`).

The pulse goes to the board with the spit (`polydye_spit_ext`, or `polydye_spit_run` with `PERIOD_US` or `WARM`) and
only that spit uses it: the board builds the
spit's waveform from it, a longer pulse lengthens each packet so F3 and F5 never overlap, and its safety check holds
every pulse to it. The configured pulse, passes, the fire test and `POLYDYE_CARTRIDGE` keep the 2.9 µs limit. It needs
board firmware with it (this add-on's bundled firmware); older firmware refuses with the update hint. The page has no
button for it.

```
POLYDYE_SPIT SECONDS=3 PULSE=11 PATTERN=sparse EXTENDED=1 CONFIRM=1
```

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
