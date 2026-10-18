# Protocols: sources and what was checked

For each supported mouse: where the protocol comes from, what was checked on a
real mouse, and what is still taken on trust. Settings whose meaning isn't
confirmed are shown read-only and are never written; bytes nobody understands
are always written back unchanged.

"Checked" below means [`tests/hardware_check.py`](../tests/hardware_check.py)
passed on my own mouse: every writable setting was changed to another valid
value, read back from the mouse, then put back, and the mouse ended with the
exact bytes it started with. That proves the mouse stores and returns what
`mousectl` writes. It does not measure the effect itself (for example that
the sensor really tracks at the new DPI).

## Attack Shark X11

- **Transport:** vendor HID feature reports on the config interface (reports
  `0x04` DPI, `0x05` lighting, `0x06` polling/timing, `0xA0` read status).
  Report lengths are read from the report descriptor, not hard-coded. Over
  the dongle a write is sometimes lost (the mouse keeps the old value, it
  doesn't arrive late), so every write is read back and resent, and fails
  with an error rather than silently.
- **Source:** [HarukaYamamoto0/attack-shark-x11-driver](https://github.com/HarukaYamamoto0/attack-shark-x11-driver)
  (MIT), its `docs/`.
- **Checked on my mouse, dongle `1d57:fa60`:**
  - DPI of the enabled stages, active stage. The DPI encoding was also checked
    against this mouse's factory 22000 DPI stage, which uses the step-100
    table, not the 16-bit extended scheme. All 330 DPI values are pinned by
    `tests/fixtures/golden.json`.
  - Polling rate, angle snap, ripple control
  - Lighting mode, brightness, speed
  - Debounce, idle sleep, deep sleep
  - Battery level (reported by the dongle only)
- **Not exercised by the check:** stage colours, lighting colour, turning
  stages on and off, the DPI of disabled stages.
- **Read-only, meaning not confirmed:** motion sync, lift-off distance.
- **Not checked:** the wired link (`1d57:fa55`).

## Rapoo VT3 PRO

- **Transport:** 32-byte EEPROM frames on vendor report `0xBA`, `A4` to read
  and `A5` to write, sent as an output report and answered through
  GET_REPORT(Input). Every write is read back and resent if it was lost.
- **Source:** reverse engineered from Rapoo's Windows driver
  "RapooGameDevDriver" 1.6.29 (`FDeviceVT3PRO_HSDM[_Wireless]_BCut`), with
  research from [pedro3z0/rapoo-software-linux](https://github.com/pedro3z0/rapoo-software-linux) (MIT).
- **Checked on my mouse, dongle `24ae:1231`:** every writable setting:
  - all 7 DPI stages, active stage
  - polling rate of the current link, motion sync, angle snap, ripple control
  - press and release debounce, sleep timer
- **Read-only, meaning not confirmed:** the separate wireless and wired
  polling slots, lift-off distance, sensor angle.
- **Not checked:** the wired link (`24ae:4431`).

## Pulsar X3 (PX3R21)

- **Transport:** Pulsar Fusion's 64-byte vendor protocol: one feature report
  with no report ID on USB interface 3, sent with SET_FEATURE and answered
  through GET_FEATURE, with a 16-bit checksum.
- **Sources:** [wertyg775/pulsar-x3-software](https://github.com/wertyg775/pulsar-x3-software)
  (`docs/protocol.md`, decompiled Fusion),
  [jonkristian/pulsar-x3-python](https://github.com/jonkristian/pulsar-x3-python)
  (X3 USB captures, MIT) and
  [packerlschupfer/pulsar-mouse-linux](https://github.com/packerlschupfer/pulsar-mouse-linux)
  (same framing on another Pulsar 8K model, MIT).
- **Checked on my mouse, dongle `3710:5402`:** every setting:
  - all 6 DPI stages, active stage
  - polling rate (1000 Hz to 2000 Hz and back)
  - motion sync, angle snap, ripple control, lift-off distance, debounce, sleep
- **Not checked:** the 8K dongle (`3710:5403`), whether polling above 1000 Hz
  takes effect through this dongle, the wired link (`3710:3409` / `3410`).

## Checking a mouse yourself

`python3 -m tests.hardware_check -m <driver-id>` runs the same round trips on
a connected mouse and restores its settings afterwards; keep the mouse awake
(move it now and then) while it runs. Reports from other owners, especially
for the wired links and the Pulsar 8K dongle, are welcome as issues.
