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

**Stock baseline.** By default PolyDye drives the cartridge the way the stock PolyDye firmware does, under Klipper:
the `color_b_late` waveform with a 6-word (2.10 µs) pulse at the stock bus clock (2.857 MHz, the only one; Klipper
sets it on every connect), stock's drive strengths and nozzle order, the stock firmware's random pattern (a random
24.9 % of positions 14–85, each column twice) in the spit and fire tests, a column every ~504–508 µs, and the boost left
on between tests (live ink off) as stock keeps it on from boot. A shutdown, Klipper going away, `POLYDYE_DISABLE` or 30
minutes unused turn it off. Everything else (the all-nozzle pattern, the stock pattern's knobs, warm-ups, pulses of up
to 2.80 µs, the black templates) is an opt-in experiment checked against that baseline. This needs this add-on's
bundled board firmware; leave `cartridge_template`, `pulse_words`, `dpi` and `pass_speed` unset in `polydye.cfg` to get
it. The other experiments (other clocks and templates, `mid` and `sparse`, `EXTENDED`) were removed: see
[Removed in the stock-only cleanup](#removed-in-the-stock-only-cleanup).

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
- **Controls:** dry-run test, cartridge wipe, head clean (live ink into the sponge, with a confirmation; also in a
  paused print, never mid-layer; see "Cleaning the head"), status, off, and enabling live ink (with a confirmation).
- **Settings:** pass axis, offsets, direction trim, cartridge template, fire pulse, resolution and pass speed, saved into the
  `[polydye]` section of `polydye.cfg`. Klipper applies them after a restart.
- **Ink position:** live nudges for lining the ink up with the plastic. Pick a step (0.02–0.4 mm) and move the
  ink ±X or ±Y, or move only the reverse passes along the pass axis (direction trim). Nudges apply from the next
  pass, also during a print, with no restart. The colour registration is shown too. Values not yet in `polydye.cfg`
  show "not stored". **Store** writes them into `polydye.cfg` (`POLYDYE_SAVE`) and **Back to stored values** undoes
  the nudges.
- **Calibration:**
  - **Test page** (live ink on paper, with a confirmation): nozzle checks, solid colours and a feathered join, printed
    by a colour print's rules (`PD_TEST_PAGE`; see "Test page").
  - **Fire test** (live ink on paper, with a confirmation): one stripe per template and fire pulse, side by side, with
    a table of where each stripe goes (`PD_FIRE_TEST`). The default is one stripe of the stock firmware's settings and
    pattern.
  - **Spit test** (live ink, no motion, with a confirmation): fires like the stock firmware, or every nozzle 1000 times
    a second, for a few seconds into a paper towel held under the cartridge (`POLYDYE_SPIT`). It also makes a steady
    load for a USB-C power meter on the board's power input, to compare the current with the cartridge in and out.
  - Both tests have the **like stock** pattern with its knobs (density, repeat, colours, first and last position,
    heaters per pulse; shown when it is picked), and a **warm-up** (seconds, pulse, period): the spit test's runs in
    the same board run right before the spit, the fire test's right before each stripe.
  - **Alignment page** and **Offset print** (see "Calibration prints"): print buttons, then boxes for the numbers you
    read off the print; **Apply and store** corrects the settings and writes them into `polydye.cfg`.
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
#cartridge_template:  # color_b_late, black_a or black_b; default: the board's own (color_b_late)
#pulse_words:         # fire pulse in bus words (0.35 us each): 6-8 (2.10-2.80 us); default: the board's own (6)
#dpi: 847             # bus columns per inch along the pass (stock: 3,333 per 100 mm, each image column twice)
#pass_speed: 59.5     # mm/s (stock's F3570: a column every 504 us at 847)
#travel_speed: 150    # mm/s, moves to the start of each pass
#extra_run_up: 1.0    # mm added to the computed run-up
#settle_time:         # s; default: the input shaper's impulse length for the pass axis
#min_lead_time: 0.025 # s; a pass must be scheduled at least this far ahead
#warm_seconds: 6      # s of automatic head warm-up before a live pass on a cold head; 0 turns it off
#prewarm_seconds: 6   # s of a POLYDYE_PREWARM burst without SECONDS
#warm_pulse: 1        # its pulse in bus words (1 = 0.35 us: no ink even on a hot head; up to 8 = 2.80 us)
#warm_period_us: 254  # one all-nozzle warm-up column every this many us (at most 0.58 % heater duty)
#warm_idle_time: 5    # s without firing (or a prewarm burst) after which the head counts as cold again
#warm_position: sponge # where warm-ups run: sponge (POLYDYE_WIPE's X), X, Y over printed plastic, or pass
#warm_clearance: 10   # mm behind the pass's run-up start, for warm_position: pass
#warm_prespit: 0      # s of stock-pattern spit at the warm spot right after a warm-up (0: none; up to 5)
#prime_seconds: 0.3   # s of POLYDYE_PRIME's stock-pattern spit over the wipe sponge without SECONDS (up to 5)
#clean_seconds: 1.5   # s of POLYDYE_CLEAN's stock-pattern spit over the wipe sponge without SECONDS (up to 5)
#cyan_columns: 0      # colour registration (POLYDYE_ALIGN sets these): cyan's and yellow's dots against magenta's,
#cyan_positions: 0    #   in bus columns along the pass (DIR=1's order) and nozzle positions across it
#yellow_columns: 0
#yellow_positions: 0
#nozzle_skew: 0       # the nozzle row's yaw (see "Calibration prints"); POLYDYE_OFFSET SKEW= sets it
#nozzle_dpi: 300      # nozzles per inch across the pass: the offset print's X scale
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

`cartridge_template` picks the nozzle waveform: `color_b_late`, the stock PolyDye firmware's (Sprite_tm's color_b
from his printercart templates, with the stock timing, derived by rule: the data, CSYNC and F3 edges come one bus word
after the DCLK edges instead of with them, the last packet's CSYNC two words after), or `black_a` or `black_b`,
Sprite_tm's two variants for a black cartridge. A black template makes the board expect pass data for a black
cartridge. Klipper sends the template to the board on every connect. It needs board firmware with template support,
and for `color_b_late` with `polydye_set_clock` (this add-on's bundled firmware or newer); with older firmware Klipper
refuses to start and asks you to update the board or remove the option.

`pulse_words` sets how long each nozzle's heater is fired, in bus words of 0.35 µs: 6 to 8 (2.10–2.80 µs). 6 words is
the stock firmware's pulse and the board's default (Sprite_tm's 1.8 µs, rounded up to whole words). A longer pulse puts
more energy into each drop, for when the board's boost rails are below the cartridge's nominal voltage. Klipper sends
it on every connect, and the board's safety check limits every pulse to it. It needs the same board firmware as
`cartridge_template`.

**Live ink is limited to 2.9 µs per pulse:** 100 % of the heater energy of Sprite_tm's 1.8 µs at the cartridge's 16 V,
at the 12.6 V the PolyDye high rail measures (energy goes with V² × t). That is 8 words at the stock clock. The add-on
refuses longer settings, and ones under 1.8 µs, with a plain error (in `polydye.cfg`, `POLYDYE_CARTRIDGE`, the fire
test and the spit test), and board firmware with `POLYDYE_PULSE_MAX_NS` refuses them too. Messages show each pulse's
energy in % of that reference.

**Bus clock.** The cartridge bus runs only at the stock PolyDye firmware's clock: 40 MHz crystal, integer divider 1
and pixel divider 7, with no blank cycles. On paper that is 5.714 MHz, but a divider of 1 is outside what ESP-IDF
allows and the LCD treats it as 2: the board measures 2.82–2.83 MHz, so 2.857 MHz, and a pulse word lasts 0.35 µs.
Klipper sends `polydye_set_clock stock=1` on every connect: a no-op for current board firmware, and older firmware
that booted on our old clock (3.333 MHz) switches to stock, after a pulse within the limit there (polydye.cfg's, or 6
words). The board measures the rate from each column it sends, and `printer.polydye.bus_hz_measured` and the Board
card show it. `bus_clock: stock` in `polydye.cfg` is still taken (it changes nothing; you can remove it); any other
`bus_clock` stops Klipper with "remove bus_clock from polydye.cfg".

`POLYDYE_ENABLE LIVE=1` waits, through the move queue, until the boost has been on for 0.15 s before the next move,
because the board refuses live ink sooner than 0.1 s after the boost comes on.

## Colour prints from OrcaSlicer

`scripts/polydye_post.py` turns a colour model sliced in OrcaSlicer into a PolyDye print: the model prints in the one
white (or natural) spool loaded, and its colours go on as ink between the layers. Each layer's ink is printed right
after the layer's last extrusion, `--gap` mm (1) above it, so the next layer seals it in.

**Set up OrcaSlicer (once):**
1. On the PC: Python 3 with numpy (`pip install numpy`), and `scripts/polydye_post.py` from this repository.
2. Printer settings (the PolyDye printer profile): G-code flavour Klipper; Multimaterial > Single extruder
   multi-material on, with one extruder per colour you want; Machine G-code > **Change filament G-code empty** (every
   filament is the spool loaded: no M600).
3. Filaments: one per colour, all with the loaded spool's settings (the same temperatures and diameter: the
   post-processor refuses differing ones). **A filament's colour is the ink**: white (`#FFFFFF`) prints none; any other
   colour becomes cyan, magenta and yellow (`1 − R`, `1 − G`, `1 − B`).
4. Process: **Prime tower off**; **Arc fitting off** (Quality > Precision); Print sequence **By layer**; no spiral vase;
   Multimaterial > *Maximum width of a segmented region* **2.5 mm**, so painted colours are a shell that deep: through
   translucent PLA a deeper shell reads stronger (on the first full-colour box, 2–2.5 mm deep walls came out solid and
   crisp, 1.6 mm streaky, 1.2 mm light and streaked; 2.5 mm at 100 % bonded fine). A **brim** is recommended: ink is
   only put where a drop that lands up to `--edge-margin` (0.5 mm) off still lands on plastic, so without one the
   first layers' colours stop short of the model's edge.
5. Others > Post-processing Scripts: `"C:\path\to\python.exe" "C:\path\to\polydye_post.py";` (add options before
   the `;`, e.g. `--prime-idle 60`).

**Colour the model** with Orca's colour painting tool. A textured model (OBJ, 3MF with a texture) can be baked into
colour painting in OrcaSlicer itself (the OrcaSlicer wiki's texture-to-colour-painting steps,
`prepare_texture_displacement` / `prepare_color_painting`); printpal.io's AMS converter (GLB/OBJ to a painted 3MF) is
another way.

**What the post-processor does:** the tool changes go; per layer, where each coloured filament's lines are becomes
ink, one object at a time.
- **Long sweeps:** each object's ink in a layer is printed as sweeps along Y over its whole footprint, every colour in
  each, about 4.2 mm apart (55 nozzle positions, 28–82), in alternating directions, each trimmed to its first and
  last ink. The sweep grid shifts every layer (by multiples of the golden ratio over the spare width), so the seams
  don't stack up the walls. Long sweeps held the head steadier than short passes (P box #4, 2026-11-25).
- **Feathered joins:** neighbouring sweeps overlap by 6 positions (`--feather 6`, 0.51 mm). In that band the dots are
  shared out in cells of one position by two column pairs (so each nozzle still fires at least two columns running):
  each cell goes to one sweep or the other by a fixed random value, fixed on the plate, against a ramp across the
  band, so the share passes smoothly from one sweep to the next and every dot prints exactly once. Sweeps side by
  side left a thin line at every join; feathered, no seam was left (polydye_picture3 and 4, 2026-11-25).
- **Gentle moves while the ink prints:** `POLYDYE_INK_MOTION ACCEL=1000 SCV=1` before the passes (acceleration and
  square corner velocity at most that), `POLYDYE_INK_MOTION RESTORE=1` after them puts the print's limits back
  (`--ink-accel`, `--ink-scv`; 0: the print's own).
- **Solid colours fire every dot of their inks, in one coat** (`--ink-limit 100`, `--wall-coats 1`, `--top-coats 1`):
  on paper 100 % in one coat was the cleanest and most vivid; 60 % in two coats came close, 35 % was pale and grainy.
- **Partial tones** (a colour's ink between 0 and 100 %) are an ordered dither (an 8 × 8 Bayer matrix fixed on the
  plate), decided per pair of columns: every nozzle fires at least two columns running, because a fire on its own
  doesn't eject. Cyan, magenta and yellow read the matrix shifted, so up to 25 % each they fall on different dots.
- **At most 4 heaters on each fire pulse, and 5 on a packet's two pulses together** (a packet fires its F3 and F5
  pulses in the same slot from the one 12.6 V rail; across the colours). Cyan and yellow together at full width
  printed solid green at 3–4 per pulse and failed at 6; each line alone takes 4, but with F3 at 4 the F5 half
  streaked even at 3, while 3 + 2 printed solid (ladder 904). So where a sweep would need more (two or three inks
  solid), the add-on sends it to the board as **two passes, one per fire line**, over the same ground: data positions
  0–55 (F3), then 56–111 (F5) as pass number + 32768. Each then fires at full density up to 4 per pulse. Positions
  28–82 put two groups of 14 positions on each line, so two inks want exactly 4 per line. One ink always stays one
  pass (2 + 2 at 28–82; at wider positions it's capped instead, 3 + 3 at 14–84 becoming 3 + 2). Where more than
  allowed still want a pulse (three inks), they share by an even rotation, the same for both columns of a pair; a fire
  left on its own is dropped. The add-on does this when it sends a job's passes, after the colour
  registration (which moves cyan one position).
- **Prewarm and prime:** `POLYDYE_PREWARM SECONDS=6` goes in about 6 s of moves before each ink layer's end (the moves
  timed as Klipper plans them; within 3 % of Orca's estimate), so the burst ends as the ink starts; a layer shorter
  than that warms through. `POLYDYE_PRIME` comes before the first ink layer's passes, and before any layer using a
  colour that hasn't ejected for `--prime-idle` s (90) by that estimate (see "Priming").
- **The job:** the passes' nozzle data goes into the G-code too, compressed, after the print and before the slicer's
  config block. `POLYDYE_JOB` at the start of the print has the add-on read it from the print's own file and upload
  the passes to the board as the print runs, a few MB ahead, each deleted once printed; `POLYDYE_JOB_PASS N=` prints
  one. The colour registration, nozzle skew and direction trim are applied on the Pi from `polydye.cfg`, so a
  recalibration needs no re-slice (the registration and skew can't change while a job is loaded: its passes are
  made with them); `dpi` and `nozzle_dpi` must match (the job is refused otherwise). It needs `data_port`.

**Options** (all optional): `--ink-limit` %, `--wall-coats`, `--top-coats`, `--gap` mm, `--dry` s (5: the ink dries
before the next layer), `--retract` mm (1.5), `--travel` mm/s (150), `--edge-margin` mm (0.5), `--inset` mm (0.05: the ink stops that far
inside the model's surface, so none lands on a base or brim below it), `--positions` (28-82: two groups of positions
on each fire line, with room for the colour registration), `--feather` (6; 0: side by side), `--ink-accel` mm/s²
(1000), `--ink-scv` mm/s (1), `--one-direction`, `--prewarm` s (6; 0: none), `--prime-idle` s (90; 0: never),
`--dpi`, `--nozzle-dpi`, `--pass-speed` (polydye.cfg's), and `--accel`, `--scv`, `--max-velocity` for
the timing (default: the profile's). `--dry-run` runs the passes with live ink off, and without bursts or primes.

It refuses G-code it can't ink safely, leaving the file unchanged: a prime tower, a Change filament G-code, differing
filament temperatures, print by object, spiral vase, arcs, or a file already processed. Not tested yet: how well wet
ink at 100 % holds the next layer on plastic.

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
| `POLYDYE_PASS LAYER= PASS= X= Y= [DIR=1] [SPEED=] [WARM=] [WARM_PULSE=] [WARM_PERIOD_US=] [PRESPIT=]` | Prints one pass (above), after an automatic head warm-up off the print if live ink is on and the head is cold; `WARM` asks for one (seconds), `WARM=0` for none, `PRESPIT` adds a stock-pattern pre-spit after it (see "Automatic head warm-up") |
| `POLYDYE_PREWARM [SECONDS=6]` | Warms the head in the background while the moves after it run, so it's hot for the next pass (see "Prewarm bursts"): `SECONDS` (default `prewarm_seconds`, 6) of every nozzle at 1 word (0.35 µs, which doesn't eject) every `warm_period_us`, from where the moves before it end, without stopping them. Turns live ink on. A pass never waits for it: one that comes first cuts it short, and needs no warm-up of its own. At most 16.6 s at 254 µs. Needs this add-on's bundled board firmware |
| `POLYDYE_PRIME [SECONDS=0.3]` | Primes the nozzles over the wipe sponge before an ink layer whose colours have been idle (see "Priming"): waits for the passes before it, turns live ink on, goes up by `POLYDYE_WIPE`'s `z_hop` and over the sponge, spits `SECONDS` (default `prime_seconds`, 0.3; up to 5) of the stock pattern in every colour, then comes back. Allowed during a print, no `CONFIRM`. Colour templates only; needs the `POLYDYE_WIPE` macro and this add-on's bundled board firmware |
| `POLYDYE_CLEAN [WARM=6] [SECONDS=1.5] [STROKES=3]` | Cleans the nozzles over the wipe sponge (see "Cleaning the head"): waits for the passes before it (refused if they're still running 5 s later), turns live ink on, goes up by `POLYDYE_WIPE`'s `z_hop` and over the sponge, warms the head for `WARM` s (default `prewarm_seconds`; 0: none), spits `SECONDS` (default `clean_seconds`, 1.5; up to 5) of the stock pattern in every colour, wipes with `POLYDYE_WIPE`'s strokes there (`STROKES`, default its `strokes`; 0: none), then comes back. Live ink is afterwards as it was before. Allowed during a print, no `CONFIRM`. Colour templates only; needs the `POLYDYE_WIPE` macro and this add-on's bundled board firmware. Doesn't home (`PD_CLEAN` does) |
| `POLYDYE_JOB [FILE=]` | At the start of a print sliced with `polydye_post.py` (it puts it there): loads the print's ink job and uploads its passes to the board as the print runs (see "Colour prints from OrcaSlicer"). Refuses, before anything moves, a job sliced for another `dpi` or `nozzle_dpi`, one the colour registration would move off nozzle positions 14–85, ink beyond the axis limits (each pass's run-up at the acceleration the G-code sets for the ink) or above the Z axis (`--gap` over the top layer), or bursts and primes the board or macros can't run. While it's loaded the colour registration and `nozzle_skew` can't change. Needs `data_port` |
| `POLYDYE_JOB_PASS N=` | Prints the job's pass `N` with `POLYDYE_PASS` once it's on the board (waits for it): one board pass, or two over the same ground where it's split by fire line (see "Colour prints from OrcaSlicer"); skipped if its object is excluded |
| `POLYDYE_INK_MOTION [ACCEL=1000] [SCV=1] [RESTORE=1]` | Gentler moves for an ink block: acceleration and square corner velocity at most `ACCEL`, `SCV` (never raised), until `RESTORE=1` puts back the limits from before; a print that ends inside an ink block gets them back too. `polydye_post.py` puts it around each layer's ink |
| `POLYDYE_JOB_END` | Stops the job's uploads and says what was printed (also done when the print ends, however it ends) |
| `POLYDYE_CANCEL` | Drops passes that are scheduled but not started (for a cancel macro). The add-on does this itself, and turns live ink off, within 2 s of a print being cancelled or stopping with an error: the print's own `POLYDYE_ENABLE LIVE=0` never runs then (`CANCEL_PRINT` waits for the command running, such as a pass's warm-up) |
| `POLYDYE_STATUS` | Board status: boost, live, passes printed and queued, worst column lateness, faults, stored passes, cartridge template, fire pulse, the last prewarm burst; and the offsets and direction trim in use |
| `POLYDYE_NUDGE [X=] [Y=] [TRIM=]` | Moves where the ink lands by `X`, `Y` mm on the bed, and the ink of reverse passes by `TRIM` mm along the pass axis (each at most ±5 mm), from the next pass on. Changes `offset_x`/`offset_y` (in the opposite direction: the offset is where the cartridge is) and `direction_trim` until the next restart |
| `POLYDYE_OFFSET [X=] [Y=] [TRIM=] [CYAN_COLUMNS=] [CYAN_POSITIONS=] [YELLOW_COLUMNS=] [YELLOW_POSITIONS=] [SKEW=] [X_TICK=] [Y_TICK=] [SAVE=]` | Sets `offset_x`, `offset_y`, `direction_trim`, the colour registration and `nozzle_skew` (`SKEW`, ±0.2) until the next restart; without arguments, shows them. `X_TICK`, `Y_TICK` are the offset print's readings: they move the ink by that many ticks and store the offsets in `polydye.cfg` (`SAVE=0`: don't). `SAVE=1` stores the others too |
| `POLYDYE_SAVE` | Stores the offsets, direction trim, colour registration and nozzle skew in use in `polydye.cfg` (next to `printer.cfg`; the colour registration and skew once they aren't zero) |
| `POLYDYE_TEST_PAGE [X=] [Y=] [Z=1] [WARM=6] [PRIME=0.3]` | **Live ink, on paper only** (`pass_axis: y`). The test page (see "Test page"), centred on `X`, `Y` (default: centred where the cartridge reaches), at `Z`, after a `WARM` s warm-up (default `prewarm_seconds`) and a `PRIME` s prime (default `prime_seconds`) over the wipe sponge; 0 leaves either out. Refused during a print, or if the colour registration would move its squares off nozzle positions 14–85, before anything moves. Needs `data_port`, the `POLYDYE_WIPE` macro and this add-on's bundled board firmware. Doesn't home (`PD_TEST_PAGE` does) |
| `POLYDYE_ALIGN_PRINT [X=] [Y=] [Z=] [SPACING=8]` | **Live ink, on paper only.** The alignment page (see "Calibration prints"), its rows `SPACING` mm apart across the pass axis, the first at `X`, `Y` (default: centred where the cartridge reaches). Moves to `Z` first if given. Doesn't home (`PD_ALIGN_PRINT` does) |
| `POLYDYE_ALIGN [CYAN=] [YELLOW=] [DIRECTION=] [CYAN_X=] [YELLOW_X=] [SAVE=1]` | The alignment page's readings, the block number where each row lines up (−16 to 16; e.g. −11.5 between two blocks): corrects the colour registration and `direction_trim` and stores them in `polydye.cfg` (`SAVE=0`: until the next restart only) |
| `POLYDYE_OFFSET_PRINT [X=] [Y=] [LAYERS=5] [LAYER_HEIGHT=0.2] [GAP=1] [EXTRUDER_TEMP=] [BED_TEMP=] [PLAQUE=1] [Z=]` | **PLA and live ink** (`pass_axis: y`). A PLA plaque with a cross-shaped groove in its top 3 layers, centred on `X`, `Y`, then ink scales across the groove from `GAP` mm above the plaque; read them with `POLYDYE_OFFSET X_TICK= Y_TICK=` (see "Calibration prints"). Heats to `EXTRUDER_TEMP` and `BED_TEMP` if given, else needs the hotend's target set for PLA; the hotend and the part fan are off afterwards, also after an error. `PLAQUE=0` prints only the ink onto a plaque printed before, at `Z` (default: its top, `LAYERS` × `LAYER_HEIGHT`, plus `GAP`). Refused if the scales wouldn't stay 6.5 mm inside the plaque at the `dpi` and `nozzle_dpi` set. Doesn't home (`PD_OFFSET_PRINT` does) |
| `POLYDYE_CARTRIDGE [TEMPLATE=color_b_late\|black_a\|black_b] [PULSE=6..8]` | Switches the board's waveform template and fire pulse until the next restart (to compare them at the nozzle check); without arguments, shows them and the bus clock. Only with live ink off and no passes queued (with older board firmware, the boost off too) |
| `POLYDYE_UPLOAD_TEST_PASS [LAYER=0] [PASS=0] [COLUMNS=600] [PATTERN=alternate\|all\|none\|stock] [REPEAT=1] [DENSITY=] [COLORS=] [FIRST=] [LAST=] [PER_PULSE=]` | Uploads a test pass over the board's data port. `alternate` (the dry run's) fires every nozzle in every other column, `all` in every column and `none` in none; `stock` is the stock firmware's random pattern with its knobs (below; `REPEAT` defaults to 2 there, at most 16; colour cartridges only); `REPEAT` sends each column that many times in a row, so the pass has `COLUMNS` × `REPEAT` columns (at most 65535). Not during a print: it briefly blocks Klipper while it sends |
| `POLYDYE_FIRE_TEST [X=70] [Y=60] [Z=] [SPACING=18] [STRIPES=…] [PATTERN=stock\|all] [DENSITY=] [REPEAT=] [COLORS=] [FIRST=] [LAST=] [PER_PULSE=] [WARM=0] [WARM_PULSE=] [WARM_PERIOD_US=] [COLUMNS=600] [UPLOAD=1] [LAYER=0] [PASS=0]` | **Live ink.** Prints one stripe per `TEMPLATE:PULSE` in `STRIPES` (e.g. `color_b_late:6,color_b_late:8`): a pass of `COLUMNS` columns along the pass axis, the stripes `SPACING` mm apart across it, the first at `X`, `Y`. The default is one stripe with the stock firmware's settings, `color_b_late:6`. `PATTERN=stock` (the default) prints the stock firmware's random pattern, the one that ejects ink on a cold head, with the spit test's knobs (below), on any board firmware: the add-on makes the columns. Without `WARM` each stripe's pass warms a cold head by itself (see "Automatic head warm-up"); `WARM` instead warms the head for that many seconds right before each stripe (its `POLYDYE_PASS WARM`, with the stripe's template, at the pass's warm spot, so only a short travel and the run-up come in between; board firmware with the warm-up), and `WARM=0` not at all. `UPLOAD=0` prints the pass already stored as `LAYER`, `PASS` instead. Moves to `Z` first if given. Refused during a print, also a paused one, and with a black stripe for `PATTERN=stock` (a colour pattern) before anything moves. Afterwards, also after an error, turns live ink off (and the boost too with older board firmware; with this add-on's bundled firmware the boost stays on, as stock keeps it, until `POLYDYE_DISABLE`, a shutdown or 30 min unused; see "Stock baseline") and puts the template, pulse and G-code state back. Doesn't home (`PD_FIRE_TEST` does) |
| `POLYDYE_SPIT [SECONDS=2] [TEMPLATE=] [PULSE=] [PATTERN=stock\|all] [DENSITY=] [REPEAT=] [COLORS=] [FIRST=] [LAST=] [PER_PULSE=] [PERIOD_US=1000] [WARM=0] [WARM_PULSE=] [WARM_PERIOD_US=] CONFIRM=1` | **Live ink, no motion.** Fires like the stock firmware (`PATTERN=stock`, the default, with its knobs, see below) a column every 508 µs, or every nozzle (`all`) at 1 kHz, or a column every `PERIOD_US`, for `SECONDS` (at most 10, and at most 10000 columns) into a paper towel held under the cartridge, through the same waveform and safety checks as a pass, starting at least 0.1 s after the boost came on. Refused without `CONFIRM=1`, during a print (also a paused one) or with passes queued. Turns live ink off afterwards (and the boost too with older board firmware; see "Stock baseline") and puts the template and pulse back. `WARM` warms the head first, in the same board run (see below) |
| `POLYDYE_WARM [SECONDS=2] [WARM_PULSE=] [WARM_PERIOD_US=] [TEMPLATE=] CONFIRM=1` | **Experimental, heaters on, no motion.** The head warm-up of `POLYDYE_SPIT WARM=` on its own (see below), at most 30 s |
| `POLYDYE_LEVEL LINE=D1\|D2\|D3\|CSYNC\|S1..S5\|DCLK [STATE=high\|low\|off]` | Signal check, **cartridge out**: holds one cartridge line at its rail voltage (`high`) or 0 V for a multimeter; `STATE=off` puts every line LOW. Needs `POLYDYE_ENABLE LIVE=0` first and nothing queued. The fire lines are never held. A line goes back to LOW after 5 minutes, or when a pass is scheduled or PolyDye turns off |

Macros (Mainsail's Macros panel, while the board is plugged in):

| Macro | Does |
|---|---|
| `PD_STATUS` | `POLYDYE_STATUS` |
| `PD_DRY_RUN_TEST [X=100] [Y=40]` | Homes if needed, lifts Z to 20 mm, uploads a test pass and prints it in both directions without ink, then reports status. The printer moves; nothing fires |
| `PD_OFF` | Drops queued passes, then turns live output and the boost off |
| `PD_CLEAN [WARM=] [SECONDS=] [STROKES=]` | **Live ink** into the wipe sponge. Homes if needed, then cleans the head (`POLYDYE_CLEAN`); also during a print |
| `PD_TEST_PAGE [X=] [Y=] [Z=1] [WARM=] [PRIME=]` | **Live ink, on paper only.** Refused during a print. Homes if needed, then prints the test page (`POLYDYE_TEST_PAGE`) at `Z` |
| `PD_ALIGN_PRINT [X=] [Y=] [Z=1] [SPACING=8]` | **Live ink, on paper only.** Refused during a print. Homes if needed, then prints the alignment page (`POLYDYE_ALIGN_PRINT`) at `Z` |
| `PD_OFFSET_PRINT [X=] [Y=] [LAYERS=] [LAYER_HEIGHT=] [GAP=] [EXTRUDER_TEMP=] [BED_TEMP=]` | **PLA and live ink.** Refused during a print. Homes if needed, then runs `POLYDYE_OFFSET_PRINT` |
| `PD_FIRE_TEST [X=70] [Y=60] [Z=0.5] [SPACING=18] [PATTERN=stock\|all] [STRIPES=…] [COLUMNS=] [DENSITY=] [REPEAT=] [COLORS=] [FIRST=] [LAST=] [PER_PULSE=] [WARM=] [WARM_PULSE=] [WARM_PERIOD_US=]` | **Live ink.** Refused during a print (also a paused one) before anything moves. Homes if needed, then runs `POLYDYE_FIRE_TEST` at `Z`; the knobs mean `PATTERN=stock` unless you give another |
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
remaining}` in seconds, or `null` (missing with older board firmware); and `bus_clock` (`stock`; older board firmware
switched to another on its console says which), `bus_hz` (nominal) and `bus_hz_measured` (from the last column the
board sent, `null` before one), missing with older board firmware. `pulse_us` is at the stock clock (2.857 MHz, also
with board firmware that reports 5.714). `templates` lists the templates the board firmware offers (`color_b_late`,
`black_a`, `black_b`; without `polydye_set_clock` only the black ones) and `spit_patterns` the spit test's patterns
(`all`, and `stock` with board firmware that also takes `PERIOD_US`); `spit_random` says whether the spit test takes the
stock pattern's knobs, and `warm_max_us`, `warm_max_s` and `warm_duty_ppm` are the board's warm-up limits (`null` duty:
older firmware, 0.75 µs pulses at most). It also has `last_pass`, `pass_axis`, `offset_x`, `offset_y` and `direction_trim`, the
values in use including nudges.

### Removed in the stock-only cleanup

The add-on and its bundled board firmware keep the stock baseline and drop the dead ends of earlier testing:

- the bus clocks `ours` (3.333 MHz) and `half` (1.667 MHz), switching clocks at run time, `CLOCK=` choices,
  `:clock` stripes other than `:stock`, `bus_clock` in `polydye.cfg` and `POLYDYE_CLOCK_TEST`;
- the templates `color_a`, `color_b` and `deskjet`;
- the `mid` (positions 14–90) and `sparse` (one nozzle per fire pulse) patterns of the spit test, the fire test and
  `POLYDYE_UPLOAD_TEST_PASS`: use `stock` with its knobs (`PER_PULSE=1` for one heater per fire pulse) or `all`;
- the extended spit test (`POLYDYE_SPIT EXTENDED=1`, pulses of 3–5 µs): the fire pulse is at most 2.9 µs (8 words).

Old parameters are refused with a plain "removed" error before anything is sent to the board, and by the
`POLYDYE_*` commands before anything moves (the `PD_FIRE_TEST` macro homes an unhomed printer first), e.g. `POLYDYE_SPIT: EXTENDED
was removed (stock baseline): the fire pulse is at most 2.9 us (8 words)`, `POLYDYE_CARTRIDGE: TEMPLATE=deskjet was
removed (stock baseline): use color_b_late, black_a or black_b` or `[polydye] bus_clock: ours was removed: the board
runs only the stock clock (2.857 MHz); remove bus_clock from polydye.cfg`. `CLOCK=stock`, `:stock` stripes,
`EXTENDED=0` and `bus_clock: stock` are still taken and change nothing. Board firmware from before the cleanup (no
`POLYDYE_STOCK_ONLY` in its data dictionary) still works with what it has, and Klipper logs a warning to update it
(Board firmware: Update to bundled firmware).

### Stock-emulation spit test

`POLYDYE_SPIT PATTERN=stock` fires the way the stock PolyDye firmware does (from a behavioural reverse-engineering
report): each of nozzle positions 14–85 of each colour fires with probability 255/1024 (24.9 %, its random dither at
full ink), and every column goes out twice in a row. That is about 2.2 heaters per fire pulse on average, at most 9.
Unless you give `TEMPLATE` or `PULSE`, it also uses the stock firmware's timing: `color_b_late` and 6 words (2.10 µs).
Colour templates only.

`PERIOD_US` sets the column period for any spit test (default 1000 µs, 1 kHz). The stock firmware sends a column every
508 µs or so, with the bus all LOW in between, the same as ours. The period must be at least the column's time on the
bus plus 20 µs: 192 µs with a 6-word pulse, more with a longer one (212 µs with 8); the add-on says the minimum. A
spit test is at most 10000 columns, so at 508 µs it lasts at most 5.08 s. That limits
the total number of pulses, not how fast they come: a shorter period heats the nozzles harder, about twice as hard at
508 µs and four times at 254 µs as at 1 kHz. Both need board firmware with them (this add-on's bundled firmware); older
firmware refuses with the update hint.

```
POLYDYE_SPIT SECONDS=3 PATTERN=stock PERIOD_US=508 CONFIRM=1
POLYDYE_SPIT SECONDS=2 PATTERN=stock PERIOD_US=254 CONFIRM=1
```

#### The stock pattern's knobs

On 7601ba6 only the stock pattern ejected ink (template, clock, period and a pulse of 1.8–3.6 µs made no difference;
`all` and the since-removed `mid` and `sparse` never did), so it has knobs for finding out why. Each is optional and
starts from the stock pattern's value; any of them means `PATTERN=stock`:

- `DENSITY`: the % of positions that fire in each column (stock 24.9).
- `REPEAT`: each random column goes out that many times in a row, 1–16 (stock 2: the same nozzles fire again one period
  later).
- `COLORS`: any of `c`, `m` and `y` (stock `cmy`).
- `FIRST`, `LAST`: the range of Sprite_tm's colour positions, 0–111 (stock 14–85).
- `PER_PULSE`: exactly that many random heaters on every F3 and F5 fire pulse (1–12; all of the eligible ones if fewer),
  instead of `DENSITY`. Stock averages 2.2 heaters per pulse and `all` fires 12; `PER_PULSE=1` fires one, as the
  removed `sparse` did, but at random.

The spit test needs board firmware with the knobs (this add-on's bundled firmware); `POLYDYE_FIRE_TEST` and test passes
make the same columns in the add-on, on any board firmware.

```
POLYDYE_SPIT SECONDS=3 PERIOD_US=508 PER_PULSE=1 CONFIRM=1
POLYDYE_SPIT SECONDS=3 PERIOD_US=508 DENSITY=25 REPEAT=1 CONFIRM=1
POLYDYE_SPIT SECONDS=3 PERIOD_US=508 COLORS=m CONFIRM=1
PD_FIRE_TEST STRIPES=color_b_late:6,color_b_late:8 DENSITY=25 REPEAT=2
PD_FIRE_TEST PATTERN=stock STRIPES=color_b_late:6,color_b_late:8 X=130 Y=100 Z=1.0 WARM=10 WARM_PULSE=8 WARM_PERIOD_US=508
```

On paper the stock pattern needs a hot head too (4 of 4 bold three-colour spits after a warm-up, hit-or-miss without):
`WARM` on the fire test warms the head right before each stripe. 8 words every 508 µs is 2.8 µs at the stock clock,
0.55 % heater duty.

### Automatic head warm-up

The cartridge prints only with a warm head: on 13c5d27 (2026-11-24) a cold head gave nothing, or cyan only, and the
same stripe or spit right after a warm-up printed all three colours. It warms in about 4 s and is too cold to eject
again in under 30 s (2026-11-25: lines printed right after a 10 s warm-up, almost nothing 30 s later; after 30 s idle
a warm-up of 1–3 s printed nothing to streaks, 4–5 s full magenta and cyan lines, 6–10 s every line full and solid).
So a live `POLYDYE_PASS` (a print's, the fire test's, an alignment print's) warms the head first when it won't have
fired, or been warmed by a prewarm burst, since Klipper started or for `warm_idle_time` seconds (5), by the time the
pass can start (after the moves already queued). It waits for the moves and passes before it, moves to the warm spot,
and the board runs `warm_seconds` (6: 4 s and half again) of every nozzle at `warm_pulse` words (1: 0.35 µs) every
`warm_period_us` (254) in one run; then the pass follows with live ink still on. A dry-run pass (`POLYDYE_ENABLE LIVE=0`) fires nothing, so it neither warms
nor keeps the head warm; live passes (also with the warm-up off), spit tests and warm-ups count as firing.
`POLYDYE_PASS WARM=` asks for a warm-up of that many seconds (with `WARM_PULSE`, `WARM_PERIOD_US`) whatever the head,
and `WARM=0` for none.

- **The pulse width matters, not just the energy.** Brett's ladder on 48483ab (2026-11-25), each warm-up on a hot head
  17 mm before a cyan text pass: 0.35 µs every 508 µs lost the word's first 10 mm; **0.35 µs every 254 µs printed it
  complete, with no dot where it warmed** (2 of 39,370 column times missed); 0.70 µs every 508 µs printed it complete
  but left a small dot, every 192 µs a blob; 2.80 µs spat a puddle.
- **Where: `warm_position`, never over the print, and no ink on the glass bed.** A hot head can put ink down during a
  warm-up, a pre-spit does on purpose, and the spot is often beyond the print. So by default (`sponge`) it warms over
  the wipe sponge at the X gantry's right end, at `POLYDYE_WIPE`'s X (G-code coordinates; `variable_x`, 245). Nothing
  wipes the cartridge there (`POLYDYE_WIPE` does that on its own). Or:
  - `X, Y`: a bed spot for the cartridge (bed coordinates, like `POLYDYE_PASS`'s X and Y) over printed plastic, e.g. a
    prime pad printed first;
  - `pass`: `warm_clearance` mm (10) behind the pass's run-up start along the pass axis, at the pass's Z and within the
    axis, and the pass travels on from there. Only where printed plastic lies under it, such as a base under the part.
    A long travel after a warm-up lets the head cool (140 mm from a park spot lost the first ~6 mm of the pass), and
    `pass` keeps it short; a pre-spit at the sponge is the other cure.

  For `sponge` and `X, Y` the toolhead rises by `POLYDYE_WIPE`'s `z_hop` (1 mm, no higher than the Z axis allows),
  travels there at `travel_speed`, warms, travels back at that height and comes down.
- **Optional pre-spit (testing):** `warm_prespit` (or `POLYDYE_PASS PRESPIT=`, passed on by the fire test) adds that
  many seconds of the stock pattern every 508 µs right after the warm-up, at the warm spot, in the same board run, for
  testing whether cyan, which still missed the first 9–14 mm of some passes after a gentle warm-up, needs real
  ejection first. It puts ink down there: at the sponge by default, or on the printed plastic under an `X, Y` or `pass`
  spot; never on the glass. Off by default; colour templates only.
- **If a warm-up fails,** the board's run is cancelled, and the cartridge goes back (or the pass goes on) only once the
  board is seen idle; otherwise it stays over the warm spot and the pass doesn't run.
- During a print it costs `warm_seconds` each time the head has been idle that long (between layers, typically), with
  the hotend standing still.
- `POLYDYE_FIRE_TEST` without `WARM` warms through its passes like this; `WARM=` gives each stripe's pass that warm-up
  instead, and `WARM=0` none (for a cold-head comparison). `POLYDYE_SPIT` and `POLYDYE_WARM` warm only when asked, in
  place (they don't move).
- `warm_seconds: 0` turns it off. It needs board firmware with the warm-up duty limit (this add-on's bundled firmware);
  with older firmware passes go ahead unwarmed, and the page says so.
- `printer.polydye.auto_warm` has the settings (and `available`), `head_idle_s` the seconds since the head last fired
  or a burst warmed it (none yet: null), `prewarm` the last burst (`state`: none, queued, firing, done, made way,
  skipped; `seconds` fired); the page's Board card shows the head.

### Prewarm bursts

A warm-up at the sponge stops the print for its 6 s and the trips there and back. A layer's ink can instead come with
the head already hot: `POLYDYE_PREWARM` about 6 s of moves before the layer's first pass starts a burst
(`prewarm_seconds`, 6: 4 s was enough after 30 s idle, 6 s gives half again), the
automatic warm-up's recipe (every nozzle at 1 word, 0.35 µs, every `warm_period_us`: 254), while the plastic moves run
on. It starts where the moves before it end (Klipper's lookahead, like `SET_PIN`), so the moves never stop for it, and
the board runs it in the background:

- **A pass is never delayed.** A pass due before the burst ends cuts it short (the board stops it in time); so do a
  spit test or warm-up, live ink going off, a cancel, and the board's boost or power going off.
- **The next pass needs no warm-up of its own** while the burst is queued or firing, or for `warm_idle_time` (5 s)
  after it ended; otherwise (it ended too long before, or never ran) the pass warms at the sponge as usual.
- **It fires wherever the cartridge is**, over the glass bed too, so its pulse is fixed in the board firmware at 1 word:
  0.35 µs left no mark even on a hot head (60 s of it over paper stayed clean), 0.70 µs left a dot. The host can't ask
  for more. It needs live ink on, and turns it on (without pausing the moves; the board waits for the boost itself).
- If the board can't run it (live ink went off, a spit test, a firmware update, or a pass already queued over its
  start) it's skipped, never a shutdown; `POLYDYE_STATUS` says what the last burst did.
- A layer shorter than the burst: put `POLYDYE_PREWARM` at the layer's start; the pass cuts it short.

Hand-written G-code, the burst about 6 s of moves before the ink:

```
POLYDYE_PREWARM
; ... the layer's last ~6 s of plastic ...
POLYDYE_PASS LAYER=5 PASS=0 X=120 Y=100 DIR=1
```

**Proof test before relying on it (paper):** print lines (`POLYDYE_UPLOAD_TEST_PASS` passes) in groups with 30 s and
60 s gaps after the previous group, each group led by a `POLYDYE_PREWARM` timed to end as it starts: every group should
print in full, and the paper under the bursts stay clean.

### Priming

A burst warms the head but doesn't eject, and a hot head isn't enough: a nozzle that hasn't ejected for a while misses
about its first 60 full-energy fires. Cyan printed in full 30, 60 and 120 s after its last ejection, but lost about
5 mm of a pass after 4.5 min. So an ink layer starts with `POLYDYE_PRIME` when it's the print's first, or when any
colour it uses hasn't ejected for 90 s or more:

- **Over the wipe sponge only.** It rides on the X gantry, so the cartridge's gap to it stays the same; spits from over
  about 5 mm mist, so never over the base or the bed.
- **A short spit:** `prime_seconds` (0.3 s: 591 columns) of the stock pattern every 508 µs, all three colours. After
  0.2 s a full-width cyan bar still started ragged. The stock pattern fires nozzle positions 14–85 of each colour, the
  ones the stock firmware prints with; the add-on's calibration prints stay within them too.
- **After the burst, right before the passes:** the passes before it finish first (it waits for the moves), then up by
  `POLYDYE_WIPE`'s `z_hop`, over the sponge, spit, and back across at that height and down. It counts as firing, so the
  passes after it need no warm-up of their own for `warm_idle_time`. If the board can't be seen idle afterwards, the
  cartridge stays over the sponge and the command fails.

```
POLYDYE_PREWARM
; ... the layer's last ~6 s of plastic ...
POLYDYE_PRIME
POLYDYE_PASS LAYER=5 PASS=0 X=120 Y=100 DIR=1
```

On paper, a 6 s burst, then a 0.2 s prime, then one layer's eight wall passes printed all four walls; without the
prime only the colour that had just fired did.

### Cleaning the head

HP's own service cycle for these cartridges is spit, wipe and cap. PolyDye has the first two at the wipe sponge:

| | Fires | Wipes | For |
|---|---|---|---|
| `POLYDYE_WIPE` | nothing | 3 strokes | taking ink, fluff or plastic strings off the nozzle plate |
| `POLYDYE_PRIME` | 0.3 s of the stock pattern | no | nozzles that have sat idle for a minute or two, right before ink (a print does it by itself) |
| `POLYDYE_CLEAN` (`PD_CLEAN`, **Clean head** on the page) | a 6 s warm-up, then 1.5 s of the stock pattern | 3 strokes | nozzles that have dried or crusted: a cartridge that sat out, or gaps in the test page's nozzle check |

So the sponge alone wipes but doesn't clear a nozzle: only firing does. `POLYDYE_CLEAN` warms first because the head
only ejects when it's hot, then spits about 2950 columns (each nozzle fires about 700 times), all in one board run
over the sponge, wipes the ink off the plate there and comes back. Keep the sponge damp. Then print the test page
(`PD_TEST_PAGE`) again; a nozzle still missing after a few cleans is blocked for good. Capping the cartridge between
prints, the third step, would need a parking spot on a damp sponge; there's none yet.

### Test page

`PD_TEST_PAGE` (**live ink, on paper only**) checks on paper that the head prints the way a colour print needs it to,
with a colour print's rules: nozzle positions 28–82 (`polydye_post.py`'s default), at most 4 heaters on a fire line
and 5 on a packet, a two-ink square printed as two passes, one per fire line, the colour registration and
`nozzle_skew`, and the ink blocks' gentle moves (`POLYDYE_INK_MOTION`). Like the calibration prints, the add-on draws
it on the Pi and stores it on the board as layer 65000 (replacing what was there). First the head warms and primes over
the wipe sponge, in one board run: `WARM` s (`prewarm_seconds`, 6) of the warm-up recipe, then `PRIME` s
(`prime_seconds`, 0.3) of the stock pattern at the passes' template and pulse. Then come 8 passes (13 on the board) at
`Z` (1 mm), in alternating directions. Afterwards, also after an error, the motion limits and the G-code state go back
and live ink goes off.

The page is about 34 × 23 mm, centred on `X`, `Y` (default: the middle of where the cartridge reaches). The command
says where its ink can land: tape paper over that with 5 mm to spare. Seen from the front, left to right, a column each
for cyan, magenta and yellow, then the blue join:

| Where | What | Look for |
|---|---|---|
| Front | Nozzle check: every nozzle 14–85 fires a 16-column (0.5 mm) dash in turn, in 9 staircases of 8 dashes | A missing or faint dash is a nozzle that didn't fire: nozzle 14 + 8 × its staircase (0–8, from the right) + its dash (0–7, from the front). Every staircase should look alike |
| Middle | A solid square of the colour (one ink: one pass) | Even and full. Streaks or a pale start: a cold or unprimed head, or weak nozzles |
| Back | Red, green and blue squares (two inks: two passes over the same ground, one per fire line) | As solid as the single colours. A paler half (one side of the square) is one fire line not keeping up |
| Right | Two sweeps of blue side by side, overlapping by 6 positions with the feathered join, printed in opposite directions like a print's | No line where they meet. A line: `direction_trim` (the alignment page's D row) |

### Calibration prints

Two prints, drawn by the add-on on the Pi (no PC or slicer), set the colour registration, the direction trim and the
cartridge offset. **No ink may land on the glass bed**, so all their ink goes on paper (the alignment page) or on
printed PLA (the offset print), the priming included, and the head warms over the wipe sponge. Each strip starts with a priming block, 300
columns (9 mm) of the stock pattern in all three colours, printed first: nozzles that hadn't fired for a while missed
their first marks even right after a warm-up, and strips led in like this printed every colour from their first column
(vernier page 2, 2026-11-25). The passes go on the board as layer 65000 (it replaces what was there), at 128 kB/s.
Then the head warms once with the automatic warm-up's settings (if it's on) over the wipe sponge at `POLYDYE_WIPE`'s
X, whatever `warm_position` says: up by its `z_hop`, across, warm, back over the first strip at that height and down.
The passes follow without warming again; each strip's priming block fires real ink right before its marks.
Afterwards, also after an error, live ink goes off and the G-code state is put back.

**Alignment page** (`PD_ALIGN_PRINT`, **on paper only**: tape at least 70 × 140 mm of paper over the glass where the
page goes; the command says where its ink can land; 7 rows 8 mm apart, 113 mm long, 125 mm with the priming blocks at
both ends. Settings that would need more paper, a bigger `SPACING` or another `dpi`, are refused). Every row has blocks −16 to 16,
2.9 mm apart, numbered on the first and last rows; find the block where the row's marks line up best:

| Row | Marks | Reading |
|---|---|---|
| C, Y | a magenta bar, and the cyan (yellow) bar 3k columns further | `CYAN=k`, `YELLOW=k`: that colour's dots move 3k columns |
| D | magenta bars printed forwards, and backwards 2k columns further | `DIRECTION=k`: `direction_trim` changes by 2k × 25.4 / dpi mm |
| CX, YX | a 2-position magenta line, and the cyan (yellow) line k positions further across | `CYAN_X=k`, `YELLOW_X=k`: that colour's dots move k positions |

Then `POLYDYE_ALIGN CYAN= YELLOW= DIRECTION= CYAN_X= YELLOW_X=` (any of them) corrects the settings and stores them;
print the page again to check that every row reads 0. The colour rows sit side by side along the pass, so their
offset is fixed on the bed: in a reverse pass a colour's dots move the other way in column order (the corrected word
printed without fringes in both directions on page 2). Brett's cartridge: `cyan_columns: -34`, `cyan_positions: 1`,
`yellow_columns: 27`, `yellow_positions: 0`, `direction_trim: -0.41`.

**The colour registration applies to the passes the add-on draws or sends:** these prints, the test page, ink jobs
from `polydye_post.py`, and the test image to come. The board doesn't know a pass's direction, so `.pdj` files made
elsewhere need it applied by whatever makes them.

**Nozzle skew.** The cartridge's nozzle row isn't quite square to the pass: on Brett's mount it's yawed about 1.7°, so
nozzle position p's dots land `nozzle_skew` × (p − 50) nozzle pitches further along +Y than position 50's. Solid ink
several swaths wide then shows a sawtooth step at every join (each swath's edges slope, then jump back). With
`nozzle_skew` set, every pass the add-on draws or sends moves each position's dots back by that, rounded to whole bus
columns (at 847/300 dpi and 0.03: ±3 columns at the row's ends), fixed on the bed like the colour registration, so the
same for both directions. Position 50 is the pivot, the one `POLYDYE_PASS`'s X names, so the cartridge offset stays as
calibrated. A ladder on paper put it at **0.03** (edge roughness 0.048 mm against 0.096 mm uncorrected). Set it with
`POLYDYE_OFFSET SKEW=0.03 SAVE=1`, then **print the alignment page again**: its C, Y and D rows compare marks on
different nozzle positions, so their readings shift by about 2–3 columns once the skew is corrected.

**Offset print** (`PD_OFFSET_PRINT`, `pass_axis: y`). A 26 × 38 mm PLA plaque, `LAYERS` of `LAYER_HEIGHT` (5 × 0.2
mm), with a 1 mm cross-shaped groove through its top 3 layers centred on `X`, `Y`; then, from `GAP` (1 mm) above the
plaque, two magenta scales across the groove: the X scale's lines run along Y, 3 nozzle positions apart (0.254 mm at
`nozzle_dpi: 300`), the Y scale's run along X, about 0.24 mm apart (8 columns at `dpi: 847`). The plaque reaches 6.5 mm
beyond all the ink, so ink that lands that far off still lands on plastic. Tick 0 is where the groove's middle should
be: it is the longest, every 4th tick is long and every 8th numbered. On each scale find the tick in the middle of the
groove (halfway between the last whole ticks either side of it; between two ticks reads as e.g. 2.5) and enter it:
`POLYDYE_OFFSET X_TICK= Y_TICK=` moves the ink there and stores `offset_x` and `offset_y`. Print again to check that
both read 0. If the groove misses a scale (beyond ±2.8 mm in X or ±2.9 mm in Y), correct the offset by what you
measure with a ruler (`POLYDYE_NUDGE`) and print again. `nozzle_dpi` sets the X scale's tick size; if it's off, the
reading is too by the same factor, and a second print corrects the rest. A prime line of PLA goes down 3 mm left of
the plaque.

### Experimental: head warm-up

`POLYDYE_SPIT ... WARM=20` warms the cartridge's head for 20 s (at most 30) right before the spit: every nozzle fires
as fast as the column and the heaters' duty limit allow, and the spit's first column follows the last warm-up column
with no gap (the board runs both in one go, so the head doesn't cool in between). `POLYDYE_WARM SECONDS=5 CONFIRM=1`
does the warm-up alone. With 12 heaters on every fire pulse an all-nozzle column heats the head rather than spits at the
PolyDye's 12.6 V: 21 s of every nozzle at 2.7 µs every 508 µs (the removed deskjet template, at our old clock) never
ejected on its own, but straight after it the stock pattern gave a big blob in all three colours, where a cold head gave
cyan only. It cools fast: too cold to eject again in under 30 s (2026-11-25).

- `WARM_PULSE` is in bus words, up to the live limit of 2.9 µs (8 words). The default is the longest within 0.75 µs,
  far too short to boil ink, the way HP's printers warm the head: 2 words (0.70 µs). Older board firmware takes at most
  0.75 µs and refuses longer ones with the update hint.
- The heaters' duty, pulse / period, is at most 0.58 % (2.9 µs every 500 µs). `WARM_PERIOD_US` defaults to the fastest
  that and the column allow: the column's time on the bus plus 20 µs for a short pulse (192 µs), 483 µs for 2.8 µs
  (8 words).
- The warm-up uses the spit's template (one run): give the spit `TEMPLATE` to warm with it.
- The messages give the warm-up's total: per nozzle, the heating time and the energy in reference pulses, and the
  heaters' duty.
- It works with any spit test, and needs board firmware with it (this add-on's bundled firmware).

```
POLYDYE_SPIT SECONDS=3 PATTERN=stock PERIOD_US=508 WARM=20 WARM_PULSE=8 WARM_PERIOD_US=508 CONFIRM=1
POLYDYE_WARM SECONDS=10 WARM_PULSE=8 WARM_PERIOD_US=508 CONFIRM=1
```

### Bus clock check

On 2026-11-25 the stock bus clock measured 16.8–18.5 MHz instead of 2.857 MHz, intermittently after a switch from our
old clock (one reason the stock-only cleanup removed switching), stayed wrong until the board was power-cycled, and
nothing fired at it. Board firmware with the check (this add-on's bundled firmware) sets the clock up from scratch,
measures the clock before every live run with the cartridge
lines detached (setting it up again, up to three times, if it is off), and fires nothing if it is still more than 5 %
fast or 8 % slow: Klipper shuts down with "the cartridge bus
clock measured more than 5 % off its setting; nothing fired", and the add-on adds what it measured, e.g. `bus clock
measured 18.491 MHz (stock clock 2.857 MHz, +547.2 %): WRONG, outside the board's +5/-8 % clock check, so it fires
nothing at it`. It also checks every live column against those bounds: single columns that look slow (interrupt latency, 16–20 % on hardware) are only counted, and the run stops after three in a row or one 1.5 times too fast (firmware 13c5d27 and later; a5bfdac stopped on single columns). A spit test or warm-up says the clock its columns go
out at while it runs, and the add-on adds the board's last measurement to any driver fault. If the clock is refused
twice in a row after `FIRMWARE_RESTART`, power-cycle the board (unplug its USB).

### What the board says about a run

Each spit test and warm-up ends with the board's account of it, e.g. `PolyDye board: 36113 columns sent, worst lateness
2.3 us, slowest column preparation 104.1 us`. The preparation is the board rendering a column and checking it for safety
before it goes out; it has to stay below the column period.

If the cartridge driver stops a run (a spit test, a warm-up, a pass or the fire test), Klipper shuts down with the fault
it was, e.g. `MCU 'polydye' shutdown: polydye: cartridge driver fault: a column wasn't ready at its time (columns
prepared too slowly)`, and the add-on adds the board's account: the columns sent, the warm-up columns not sent, the
column times missed with no column ready or the bus still busy, the worst lateness and the slowest preparation. Use
`FIRMWARE_RESTART` to carry on. A spit test (no motion, so no position to keep) doesn't stop for a missed column time:
the column goes at the next one and the account counts it. This needs board firmware with it (this add-on's bundled
firmware). Older firmware says only "cartridge driver fault", and up to 7601ba6 it couldn't prepare columns fast enough
for a warm-up or a column period of 254 µs, so those stopped with that fault.

## Uploading passes

Pass data goes over the board's second USB serial port (`-if02`), not through Klipper:

```
python3 ~/polydye-klipper/scripts/polydye_upload.py info
python3 ~/polydye-klipper/scripts/polydye_upload.py put --layer 0 --pass 0 --columns 600 --pattern alternate
python3 ~/polydye-klipper/scripts/polydye_upload.py job print.pdj
python3 ~/polydye-klipper/scripts/polydye_upload.py list
```

It needs pyserial; use `~/klippy-env/bin/python` if the system Python lacks it. Prints sliced in OrcaSlicer carry
their passes in the G-code instead (see "Colour prints from OrcaSlicer").

The board keeps passes in its PSRAM: a board restart (a firmware update, a power-cycle) empties the store, so upload
them again. Ink jobs (`POLYDYE_JOB`) and the calibration prints upload their passes every time, so they aren't
affected.

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
