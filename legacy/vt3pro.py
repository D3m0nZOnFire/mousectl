#!/usr/bin/env python3
"""
vt3pro - configure the Rapoo VT3 PRO (HSDM, dual-mode) mouse on Linux without
the vendor software.

Talks the vendor protocol directly over /dev/hidraw*: 32-byte A4 (read) /
A5 (write) EEPROM frames on vendor report 0xBA, sent as an output report and
answered through GET_REPORT(Input) -- reverse engineered from Rapoo's Windows
"RapooGameDevDriver" 1.6.29 (class FDeviceVT3PRO_HSDM[_Wireless]_BCut).

Everywhere possible: read the 4/16-byte block the Windows driver itself
reads, modify only the requested bytes, write the block back, then read it
again to verify. Bytes whose meaning isn't confirmed from the driver are
shown read-only (or not at all) and always preserved untouched.
"""

import argparse
import curses
import fcntl
import glob
import json
import os
import sys
import threading
import time

VENDOR = 0x24AE
PRODUCTS = {0x4431: "wired", 0x1231: "dongle"}
CONN_BYTE = {"wired": 0xFF, "dongle": 0xA5}

RID = 0xBA
CMD_READ = 0xA4
CMD_WRITE = 0xA5
CMD_BATTERY = 0xAA
ACK_OK = 0x01

# EEPROM addresses (profile 0 base 0x600 + offset), with the exact block
# length the Windows driver uses for each read-modify-write.
ADDR_PERF = 0x880      # [0]=polling (2.4GHz) [1]=? [2]=polling (cable) [3]=?
ADDR_SENSOR = 0x884    # [0]=lift-off index [1]=motion sync [2..3]=?
ADDR_DPI_X = 0x888     # 7 x u16le DPI, [14]=stage count byte, [15]=?
ADDR_DPI_CUR = 0x898   # [0]=active stage index (0-6) [1..3]=?
ADDR_TIMING = 0x8C0    # [0]=press debounce ms [1]=release debounce ms [2]=sleep min [3]=correction flags
ADDR_ANGLE = 0x8C4     # [0]=sensor angle [1..3]=?
ADDR_DPI_Y = 0x8C8     # same layout as ADDR_DPI_X

BLOCKS = {
    ADDR_PERF: 4, ADDR_SENSOR: 4, ADDR_DPI_X: 16, ADDR_DPI_CUR: 4,
    ADDR_TIMING: 4, ADDR_ANGLE: 4, ADDR_DPI_Y: 16,
}

# Polling byte -> Hz (RapooGameDevDriver setReportRate switch table).
POLL_RATES = {125: 0x08, 250: 0x04, 500: 0x02, 1000: 0x01, 2000: 0x84, 4000: 0x82}
POLL_NAMES = {v: k for k, v in POLL_RATES.items()}
POLL_NAMES[0x81] = 8000   # decoded if present, but not offered: the VT3 PRO config has support8k=false

DEBOUNCE_MS = [1, 2, 4, 8, 16, 24, 32]   # setMouseParameter keyDown/keyUpDelay table
DPI_MIN, DPI_MAX, DPI_STEP = 50, 26000, 50
SLEEP_MIN, SLEEP_MAX = 1, 60

# Correction byte (ADDR_TIMING[3]) stores *disable* flags:
# straightLine on/off -> bit0 clear/set, corrugation on/off -> bit1 clear/set.
FLAG_ANGLE_SNAP_OFF = 0x01
FLAG_RIPPLE_OFF = 0x02

CONFIG_DIR = os.path.expanduser("~/.config/rapoo-vt3pro")
AUTO_BACKUP = os.path.join(CONFIG_DIR, "auto-backup.json")


# ------------------------------------------------------------- hidraw I/O

def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def HIDIOCGINPUT(size):
    return _ioc(3, "H", 0x0A, size)


class DeviceError(Exception):
    pass


class Mouse:
    """One VT3 PRO config interface. Use as a context manager to open it."""

    GAP_S = 0.050          # Windows driver sleeps 50ms after every exchange
    ACCEPT_S = 0.100       # busy shows up within ~4ms; none by now = frame lost
    SENDS = 4
    STALE_RETRIES = 4

    def __init__(self, path, mode):
        self.path = path
        self.mode = mode
        self.fd = None
        self._last_done = 0.0
        self._last_resp = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_RDWR)
        except PermissionError:
            raise DeviceError(f"permission denied on {self.path} -- run 'vt3pro install-udev'")
        except OSError as e:
            raise DeviceError(f"cannot open {self.path}: {e}")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _get_input(self):
        buf = bytearray(33)
        buf[0] = RID
        fcntl.ioctl(self.fd, HIDIOCGINPUT(len(buf)), buf)
        # hidraw may or may not keep the report ID in front of the status byte
        return bytes(buf[1:]) if buf[0] == RID else bytes(buf)

    def request(self, frame, timeout=2.0):
        """Send one 32-byte frame, return the 32-byte answer [status, ...].

        The input report keeps the previous answer until the dongle picks
        the new frame up, reads busy (0x02) within a few ms, then 0x01 with
        fresh data after the radio round trip (~20-50ms). The dongle
        occasionally drops a frame without ever going busy -- its OK is then
        the *previous* command's answer -- so an OK only counts after busy
        was seen, and a frame that never produced busy within ACCEPT_S is
        sent again (every frame here is an idempotent block read/write).
        Frames are spaced GAP_S apart like the Windows driver does.
        """
        data = bytes([RID]) + bytes(frame).ljust(32, b"\0")[:32]
        deadline = time.time() + timeout
        for _ in range(self.SENDS):
            wait = self._last_done + self.GAP_S - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                os.write(self.fd, data)
            except OSError as e:
                raise DeviceError(f"write failed: {e}")
            sent = time.time()
            busy = False
            while time.time() < deadline:
                time.sleep(0.002)
                try:
                    resp = self._get_input()
                except OSError as e:
                    raise DeviceError(f"read failed: {e}")
                if resp[0] != ACK_OK:
                    busy = True
                elif busy:
                    self._last_done = time.time()
                    self._last_resp = resp
                    return resp
                elif time.time() - sent >= self.ACCEPT_S:
                    break       # never went busy: frame lost, send again
            self._last_done = time.time()
            if time.time() >= deadline:
                break
        raise DeviceError("no answer from the mouse (asleep? move it and retry)")

    def _frame(self, cmd, addr, data=b"", length=None):
        f = bytearray(32)
        f[0] = CONN_BYTE[self.mode]
        f[1] = cmd
        f[2] = len(data) if length is None else length
        f[3:7] = addr.to_bytes(4, "little")
        f[7:7 + len(data)] = data
        return f

    def _battery_resp(self):
        """Battery answers carry the charge state (1/2) in byte 1, where
        every block answer has 0 -- so a stale battery answer is detectable."""
        for _ in range(self.STALE_RETRIES):
            resp = self.request(self._frame(CMD_BATTERY, 0, length=0))
            if resp[1] != 0:
                return resp
        return None

    def read_block(self, addr, length=None):
        """Read a block, rejecting stale answers.

        Even after a clean busy->OK sequence the dongle now and then hands
        back the previous answer unchanged (~1 in 200 frames). An answer
        byte-identical to the previous one is therefore re-checked: a battery
        query (whose answer can never look like a block answer) replaces the
        "previous answer", then the block is read again -- a real answer can
        no longer match it, a stale one still does. Blocks that genuinely
        equal their predecessor (DPI X and Y) pass on the re-read.
        """
        length = length or BLOCKS[addr]
        frame = self._frame(CMD_READ, addr, length=length)
        if self._last_resp is None:
            # First exchange of this process: the device still holds some
            # earlier answer we never saw, so plant a recognisable one.
            self._battery_resp()
        prev = self._last_resp
        resp = self.request(frame)
        for _ in range(self.STALE_RETRIES):
            if resp != prev:
                return bytearray(resp[4:4 + length])
            marker = self._battery_resp()
            if marker is None:
                # No usable marker (e.g. no battery answer): settle for two
                # identical consecutive reads of this same block.
                again = self.request(frame)
                if again == resp:
                    return bytearray(resp[4:4 + length])
                prev, resp = resp, again
                continue
            prev = marker
            resp = self.request(frame)
        raise DeviceError(f"mouse keeps returning stale data for 0x{addr:03x} -- retry")

    def write_block(self, addr, data):
        """Write a block and read it back; a dropped write frame is resent."""
        for _ in range(self.STALE_RETRIES):
            self.request(self._frame(CMD_WRITE, addr, bytes(data)))
            back = self.read_block(addr, len(data))
            if bytes(back) == bytes(data):
                return
        raise DeviceError(f"verify failed at 0x{addr:03x}: wrote {bytes(data).hex()} "
                          f"read back {bytes(back).hex()}")

    def read_all(self):
        return {addr: self.read_block(addr) for addr in BLOCKS}

    def battery(self):
        resp = self._battery_resp()
        if resp is None:
            raise DeviceError("no battery answer from the mouse")
        state = {1: "discharging", 2: "charging"}.get(resp[1], f"0x{resp[1]:02x}")
        return min(resp[2], 100), state


def _descriptor_has_rid(hidraw_dir):
    try:
        with open(os.path.join(hidraw_dir, "device", "report_descriptor"), "rb") as f:
            return bytes([0x85, RID]) in f.read()
    except OSError:
        return False


def discover():
    devs = []
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(d, "device", "uevent")) as f:
                props = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        except OSError:
            continue
        parts = props.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        vid, pid = int(parts[1], 16), int(parts[2], 16)
        if vid == VENDOR and pid in PRODUCTS and _descriptor_has_rid(d):
            devs.append(Mouse("/dev/" + os.path.basename(d), PRODUCTS[pid]))
    return devs


def pick(args):
    devs = discover()
    if getattr(args, "device", None):
        devs = [d for d in devs if d.mode == args.device]
    if not devs:
        die("no Rapoo VT3 PRO config interface found (is the dongle/cable plugged in?)")
    return devs


# ------------------------------------------------------------ decode/encode

def decode(raw, mode):
    perf, sensor, cur = raw[ADDR_PERF], raw[ADDR_SENSOR], raw[ADDR_DPI_CUR]
    timing, angle = raw[ADDR_TIMING], raw[ADDR_ANGLE]
    xs = [int.from_bytes(raw[ADDR_DPI_X][i * 2:i * 2 + 2], "little") for i in range(7)]
    ys = [int.from_bytes(raw[ADDR_DPI_Y][i * 2:i * 2 + 2], "little") for i in range(7)]
    poll_byte = 2 if mode == "wired" else 0

    def hz(b):
        return POLL_NAMES.get(b, f"unknown(0x{b:02x})")

    return {
        "dpi_x": xs,
        "dpi_y": ys,
        "active_stage": cur[0] + 1,
        "stage_count_byte": raw[ADDR_DPI_X][14],
        "polling_hz": hz(perf[poll_byte]),
        "polling_wireless_hz": hz(perf[0]),
        "polling_wired_hz": hz(perf[2]),
        "motion_sync": bool(sensor[1]),
        "lod_index": sensor[0],
        "debounce_press_ms": timing[0],
        "debounce_release_ms": timing[1],
        "sleep_min": timing[2],
        "angle_snap": not (timing[3] & FLAG_ANGLE_SNAP_OFF),
        "ripple_control": not (timing[3] & FLAG_RIPPLE_OFF),
        "sensor_angle": int.from_bytes(angle[0:1], "little", signed=True),
    }


def set_stage_dpi(raw, i, dpi):
    v = int(dpi).to_bytes(2, "little")
    raw[ADDR_DPI_X][i * 2:i * 2 + 2] = v
    raw[ADDR_DPI_Y][i * 2:i * 2 + 2] = v


def set_polling(raw, mode, hz):
    raw[ADDR_PERF][2 if mode == "wired" else 0] = POLL_RATES[hz]


def set_flag(raw, off_bit, on):
    if on:
        raw[ADDR_TIMING][3] &= ~off_bit & 0xFF
    else:
        raw[ADDR_TIMING][3] |= off_bit


def validate_dpi(v):
    if not (DPI_MIN <= v <= DPI_MAX) or v % DPI_STEP:
        raise ValueError(f"DPI must be {DPI_MIN}-{DPI_MAX} in steps of {DPI_STEP}")


def ensure_backup(m, raw=None):
    """Save the untouched EEPROM blocks once, before the very first write."""
    if os.path.exists(AUTO_BACKUP):
        return
    raw = raw or m.read_all()
    write_backup(AUTO_BACKUP, m.mode, raw)


def write_backup(path, mode, raw):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"mode": mode, "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "blocks": {f"0x{a:03x}": bytes(b).hex() for a, b in raw.items()},
                   "decoded": decode(raw, mode)}, f, indent=2)


def apply_changes(m, before, after):
    """Write every block that differs, in address order, verifying each."""
    changed = [a for a in sorted(BLOCKS) if bytes(before[a]) != bytes(after[a])]
    if changed:
        ensure_backup(m, before)
    for a in changed:
        m.write_block(a, after[a])
        before[a] = bytearray(after[a])
    return changed


# ----------------------------------------------------------------- commands

def die(msg):
    print(f"vt3pro: {msg}", file=sys.stderr)
    sys.exit(1)


def print_status(m, raw, battery=None):
    d = decode(raw, m.mode)
    print(f"== Rapoo VT3 PRO [{m.mode}]  {m.path}")
    print(f"  polling rate   : {d['polling_hz']} Hz  "
          f"(2.4GHz slot {d['polling_wireless_hz']}, cable slot {d['polling_wired_hz']})")
    print("  DPI stages     :")
    for i in range(7):
        mark = " <- active" if d["active_stage"] == i + 1 else ""
        xy = f"{d['dpi_x'][i]:>6}" if d["dpi_x"][i] == d["dpi_y"][i] else \
            f"{d['dpi_x'][i]:>6} x {d['dpi_y'][i]}"
        print(f"      stage {i + 1}    : {xy} DPI{mark}")
    print(f"  motion sync    : {'on' if d['motion_sync'] else 'off'}")
    print(f"  angle snap     : {'on' if d['angle_snap'] else 'off'}")
    print(f"  ripple control : {'on' if d['ripple_control'] else 'off'}")
    print(f"  debounce       : {d['debounce_press_ms']} ms press, {d['debounce_release_ms']} ms release")
    print(f"  sleep          : {d['sleep_min']} min")
    print(f"  lift-off       : level {d['lod_index']}  (read-only)")
    print(f"  sensor angle   : {d['sensor_angle']}  (read-only)")
    if battery:
        print(f"  battery        : {battery[0]}%  ({battery[1]})")


def for_each(args, fn):
    for dev in pick(args):
        try:
            with dev as m:
                fn(m)
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)


def cmd_status(args):
    def run(m):
        raw = m.read_all()
        try:
            bat = m.battery()
        except DeviceError:
            bat = None
        print_status(m, raw, bat)
    for_each(args, run)


def cmd_dump(args):
    def run(m):
        print(f"== {m.mode} {m.path}")
        for a, b in m.read_all().items():
            print(f"  0x{a:03x} (+{a - 0x600:3d}): {bytes(b).hex(' ')}")
    for_each(args, run)


def modify(args, mutate, describe):
    """Read all blocks, apply `mutate(raw, mode)`, write back what changed."""
    def run(m):
        before = m.read_all()
        after = {a: bytearray(b) for a, b in before.items()}
        mutate(after, m.mode)
        changed = apply_changes(m, before, after)
        note = "" if changed else " (unchanged)"
        print(f"[{m.mode}] {describe(decode(after, m.mode))}{note}")
    for_each(args, run)


def cmd_dpi(args):
    try:
        if args.stages:
            vals = [int(v) for v in args.stages.split(",")]
            if len(vals) != 7:
                die("give exactly 7 comma-separated DPI values (one per stage)")
            for v in vals:
                validate_dpi(v)
        elif args.stage and args.value is not None:
            validate_dpi(args.value)
        elif args.stage and args.active is None:
            die("--stage needs --value")
    except ValueError as e:
        die(str(e))

    def mutate(raw, mode):
        if args.stages:
            for i, v in enumerate(vals):
                set_stage_dpi(raw, i, v)
        elif args.stage and args.value is not None:
            set_stage_dpi(raw, args.stage - 1, args.value)
        if args.active:
            raw[ADDR_DPI_CUR][0] = args.active - 1

    def describe(d):
        act = d["dpi_x"][d["active_stage"] - 1]
        return (f"DPI stages: {', '.join(map(str, d['dpi_x']))} "
                f"(active: stage {d['active_stage']} = {act} DPI)")
    modify(args, mutate, describe)


def cmd_polling(args):
    if args.hz not in POLL_RATES:
        die(f"polling rate must be one of {sorted(POLL_RATES)}")
    modify(args, lambda raw, mode: set_polling(raw, mode, args.hz),
           lambda d: f"polling rate: {d['polling_hz']} Hz")


def cmd_debounce(args):
    for v in (args.press, args.release):
        if v is not None and v not in DEBOUNCE_MS:
            die(f"debounce must be one of {DEBOUNCE_MS} ms")
    if args.press is None and args.release is None:
        die("give --press and/or --release")

    def mutate(raw, mode):
        if args.press is not None:
            raw[ADDR_TIMING][0] = args.press
        if args.release is not None:
            raw[ADDR_TIMING][1] = args.release
    modify(args, mutate, lambda d: f"debounce: {d['debounce_press_ms']} ms press, "
                                   f"{d['debounce_release_ms']} ms release")


def cmd_sleep(args):
    if not SLEEP_MIN <= args.minutes <= SLEEP_MAX:
        die(f"sleep must be {SLEEP_MIN}-{SLEEP_MAX} minutes")

    def mutate(raw, mode):
        raw[ADDR_TIMING][2] = args.minutes
    modify(args, mutate, lambda d: f"sleep: {d['sleep_min']} min")


def cmd_sensor(args):
    if args.motion_sync is None and args.angle_snap is None and args.ripple is None:
        die("give --motion-sync, --angle-snap and/or --ripple")

    def mutate(raw, mode):
        if args.motion_sync is not None:
            raw[ADDR_SENSOR][1] = 1 if args.motion_sync else 0
        if args.angle_snap is not None:
            set_flag(raw, FLAG_ANGLE_SNAP_OFF, args.angle_snap)
        if args.ripple is not None:
            set_flag(raw, FLAG_RIPPLE_OFF, args.ripple)

    def onoff(b):
        return "on" if b else "off"
    modify(args, mutate, lambda d: f"motion sync {onoff(d['motion_sync'])}, "
                                   f"angle snap {onoff(d['angle_snap'])}, "
                                   f"ripple control {onoff(d['ripple_control'])}")


def cmd_battery(args):
    def run(m):
        pct, state = m.battery()
        print(f"[{m.mode}] battery: {pct}%  ({state})")
    for_each(args, run)


def cmd_save(args):
    dev = pick(args)[0]
    path = os.path.expanduser(args.file) if args.file else os.path.join(CONFIG_DIR, "settings.json")
    try:
        with dev as m:
            write_backup(path, m.mode, m.read_all())
    except DeviceError as e:
        die(str(e))
    print(f"saved current settings to {path}")


def cmd_restore(args):
    path = os.path.expanduser(args.file)
    try:
        with open(path) as f:
            saved = json.load(f)
        target = {int(a, 16): bytearray.fromhex(h) for a, h in saved["blocks"].items()}
    except (OSError, ValueError, KeyError) as e:
        die(f"cannot read {path}: {e}")
    if set(target) != set(BLOCKS) or any(len(target[a]) != n for a, n in BLOCKS.items()):
        die(f"{path} is not a vt3pro settings file")

    def run(m):
        before = m.read_all()
        changed = apply_changes(m, before, target)
        print(f"[{m.mode}] restored {len(changed)} changed block(s) from {path}")
    for_each(args, run)


# -------------------------------------------------------------------- TUI
#
# Interactive terminal UI ("vt3pro tui"), modelled on ashark's: stdlib curses
# only, and it never reimplements protocol logic -- edits mutate the same raw
# EEPROM blocks read_all() returns via the same helpers the CLI uses, and
# decode() is the single source of truth for what they mean. Device I/O is
# synchronous, single-threaded and only triggered by an explicit keypress
# (the dongle answers stale data when rushed, see Mouse.request). The one
# extra thread is a screen-only spinner that is always joined before any
# further curses or device work.

SECTION_DPI, SECTION_PERF, SECTION_TIMING, SECTION_BATTERY = range(4)
SECTION_NAMES = ["DPI", "Performance", "Timing", "Battery"]
SECTION_BLOCKS = {
    SECTION_DPI: (ADDR_DPI_X, ADDR_DPI_Y, ADDR_DPI_CUR),
    SECTION_PERF: (ADDR_PERF, ADDR_SENSOR, ADDR_TIMING),
    SECTION_TIMING: (ADDR_TIMING,),
}
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class TuiState:
    def __init__(self, devs):
        self.devs = devs
        self.dev_idx = 0
        self.dev_raw = {}       # addr -> bytearray, as last read from / written to the device
        self.raw = {}           # addr -> bytearray, local copy with pending edits
        self.battery = None
        self.battery_loading = False
        self.spinner_frame = SPINNER_FRAMES[0]
        self.message = "Reading device..."
        self.message_kind = "info"
        self.section = SECTION_DPI
        self.field = 0

    @property
    def dev(self):
        return self.devs[self.dev_idx]

    @property
    def decoded(self):
        return decode(self.raw, self.dev.mode) if self.raw else None

    def block_dirty(self, addr):
        return bool(self.raw) and bytes(self.raw[addr]) != bytes(self.dev_raw[addr])

    def section_dirty(self, sec):
        # Timing's flag byte is edited from Performance, so a section is
        # dirty only for the bytes its own fields own.
        if not self.raw:
            return False
        if sec == SECTION_DPI:
            return any(self.block_dirty(a) for a in (ADDR_DPI_X, ADDR_DPI_Y, ADDR_DPI_CUR))
        if sec == SECTION_PERF:
            return (self.block_dirty(ADDR_PERF) or self.block_dirty(ADDR_SENSOR)
                    or self.raw[ADDR_TIMING][3] != self.dev_raw[ADDR_TIMING][3])
        if sec == SECTION_TIMING:
            return self.raw[ADDR_TIMING][:3] != self.dev_raw[ADDR_TIMING][:3]
        return False

    def any_dirty(self):
        return any(self.section_dirty(s) for s in SECTION_BLOCKS)


def _safe_addstr(stdscr, y, x, text, attr=0):
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _tui_init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_RED, bg)                      # error
    curses.init_pair(2, curses.COLOR_YELLOW, bg)                   # pending/dirty
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)     # focused field
    curses.init_pair(4, curses.COLOR_GREEN, bg)                    # info/success


def _tui_field_count(section):
    return {SECTION_DPI: 8, SECTION_PERF: 4, SECTION_TIMING: 3, SECTION_BATTERY: 1}[section]


def _tui_section_attr(state, section):
    return curses.color_pair(3) | curses.A_BOLD if state.section == section else curses.A_BOLD


def _tui_field_attr(state, section, field):
    return curses.color_pair(3) if state.section == section and state.field == field else 0


def _tui_edit_line(stdscr, prompt, initial="", validate=None):
    """Blocking single-line editor on the last row; Enter accepts, Esc cancels."""
    curses.curs_set(1)
    buf = list(initial)
    err = ""
    try:
        while True:
            h, w = stdscr.getmaxyx()
            y = h - 1
            _safe_addstr(stdscr, y, 0, " " * (w - 1))
            text = f"{prompt}: {''.join(buf)}"
            _safe_addstr(stdscr, y, 0, text[:w - 1])
            _safe_addstr(stdscr, y - 1, 0, " " * (w - 1))
            if err:
                _safe_addstr(stdscr, y - 1, 0, f"! {err}"[:w - 1], curses.color_pair(1))
            stdscr.move(y, min(len(text), w - 2))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_ENTER, 10, 13):
                val = "".join(buf)
                if validate:
                    try:
                        validate(val)
                    except ValueError as e:
                        err = str(e)
                        continue
                return val
            elif ch == 27:
                return None
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                err = ""
            elif ch == 21:  # Ctrl-U
                buf = []
                err = ""
            elif 32 <= ch < 127:
                buf.append(chr(ch))
                err = ""
    finally:
        curses.curs_set(0)


def _tui_confirm(stdscr, prompt):
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 1, 0, " " * (w - 1))
    _safe_addstr(stdscr, h - 1, 0, prompt[:w - 1], curses.color_pair(2))
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q"), curses.KEY_ENTER, 10, 13):
            return False


def _tui_run_with_spinner(stdscr, state, fn):
    stop = threading.Event()

    def animate():
        i = 0
        while not stop.is_set():
            state.spinner_frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            try:
                _tui_draw(stdscr, state)
            except curses.error:
                pass
            i += 1
            stop.wait(0.08)

    t = threading.Thread(target=animate, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join()
        state.spinner_frame = SPINNER_FRAMES[0]


def _cycle(values, cur, d):
    cur = cur if cur in values else values[0]
    return values[(values.index(cur) + d) % len(values)]


def _tui_nudge(state, d):
    """Left/Right on the focused field."""
    if not state.raw:
        return
    raw, dec, f = state.raw, state.decoded, state.field
    if state.section == SECTION_DPI:
        if f < 7:
            v = max(DPI_MIN, min(DPI_MAX, dec["dpi_x"][f] // DPI_STEP * DPI_STEP + d * DPI_STEP))
            set_stage_dpi(raw, f, v)
        else:
            raw[ADDR_DPI_CUR][0] = (raw[ADDR_DPI_CUR][0] + d) % 7
    elif state.section == SECTION_PERF:
        if f == 0:
            set_polling(raw, state.dev.mode, _cycle(sorted(POLL_RATES), dec["polling_hz"], d))
        elif f == 1:
            raw[ADDR_SENSOR][1] = 0 if raw[ADDR_SENSOR][1] else 1
        elif f == 2:
            set_flag(raw, FLAG_ANGLE_SNAP_OFF, not dec["angle_snap"])
        elif f == 3:
            set_flag(raw, FLAG_RIPPLE_OFF, not dec["ripple_control"])
    elif state.section == SECTION_TIMING:
        if f in (0, 1):
            raw[ADDR_TIMING][f] = _cycle(DEBOUNCE_MS, raw[ADDR_TIMING][f], d)
        else:
            raw[ADDR_TIMING][2] = max(SLEEP_MIN, min(SLEEP_MAX, raw[ADDR_TIMING][2] + d))


def _tui_edit_field(stdscr, state):
    """Enter: type a value for free-form fields (DPI, sleep)."""
    if not state.raw:
        return
    sec, f = state.section, state.field
    if sec == SECTION_DPI and f < 7:
        def validate(s):
            try:
                v = int(s)
            except ValueError:
                raise ValueError("not a valid number")
            validate_dpi(v)
        val = _tui_edit_line(stdscr, f"Stage {f + 1} DPI ({DPI_MIN}-{DPI_MAX}, step {DPI_STEP})",
                             str(state.decoded["dpi_x"][f]), validate)
        if val is not None:
            set_stage_dpi(state.raw, f, int(val))
    elif sec == SECTION_TIMING and f == 2:
        def validate(s):
            try:
                v = int(s)
            except ValueError:
                raise ValueError("not a valid number")
            if not SLEEP_MIN <= v <= SLEEP_MAX:
                raise ValueError(f"must be {SLEEP_MIN}-{SLEEP_MAX}")
        val = _tui_edit_line(stdscr, f"Sleep minutes ({SLEEP_MIN}-{SLEEP_MAX})",
                             str(state.raw[ADDR_TIMING][2]), validate)
        if val is not None:
            state.raw[ADDR_TIMING][2] = int(val)


def _tui_apply(state, sections):
    """Write the given sections' pending edits. Other sections' pending bytes
    inside a shared block (the Timing block holds Performance's flag byte)
    are left pending: the written block takes the device's current bytes for
    them."""
    target = {a: bytearray(b) for a, b in state.dev_raw.items()}
    for sec in sections:
        if sec == SECTION_DPI:
            for a in (ADDR_DPI_X, ADDR_DPI_Y, ADDR_DPI_CUR):
                target[a] = bytearray(state.raw[a])
        elif sec == SECTION_PERF:
            target[ADDR_PERF] = bytearray(state.raw[ADDR_PERF])
            target[ADDR_SENSOR] = bytearray(state.raw[ADDR_SENSOR])
            target[ADDR_TIMING][3] = state.raw[ADDR_TIMING][3]
        elif sec == SECTION_TIMING:
            target[ADDR_TIMING][:3] = state.raw[ADDR_TIMING][:3]
    with state.dev as m:
        apply_changes(m, state.dev_raw, target)


def _tui_apply_sections(stdscr, state, sections, label):
    sections = [s for s in sections if s in SECTION_BLOCKS]
    if not sections:
        state.message = f"Nothing to apply in {label}."
        state.message_kind = "info"
        return
    state.message = f"Applying {label}..."
    state.message_kind = "info"
    try:
        _tui_run_with_spinner(stdscr, state, lambda: _tui_apply(state, sections))
        state.message = f"{label} applied and verified."
        state.message_kind = "info"
    except DeviceError as e:
        state.message = str(e)
        state.message_kind = "error"


def _tui_do_refresh(stdscr, state, confirm_if_dirty=True):
    if confirm_if_dirty and state.any_dirty():
        if not _tui_confirm(stdscr, "Unapplied edits will be discarded. Refresh? [y/N] "):
            return
    state.message = "Reading device..."
    state.message_kind = "info"
    state.raw = {}
    try:
        with state.dev as m:
            dev_raw = _tui_run_with_spinner(stdscr, state, m.read_all)
            state.dev_raw = dev_raw
            state.raw = {a: bytearray(b) for a, b in dev_raw.items()}
            state.battery_loading = True
            try:
                state.battery = _tui_run_with_spinner(stdscr, state, m.battery)
            finally:
                state.battery_loading = False
        state.message = "Refreshed."
        state.message_kind = "info"
    except DeviceError as e:
        state.message = str(e)
        state.message_kind = "error"


def _tui_poll_battery(stdscr, state):
    state.message = "Reading battery..."
    state.message_kind = "info"
    state.battery_loading = True
    try:
        with state.dev as m:
            state.battery = _tui_run_with_spinner(stdscr, state, m.battery)
        state.message = "Battery updated."
        state.message_kind = "info"
    except DeviceError as e:
        state.message = str(e)
        state.message_kind = "error"
    finally:
        state.battery_loading = False


def _tui_switch_device(stdscr, state):
    if len(state.devs) < 2:
        state.message = "Only one connection present -- nothing to switch to."
        state.message_kind = "info"
        return
    if state.any_dirty() and not _tui_confirm(stdscr, "Unapplied edits will be discarded. Switch device? [y/N] "):
        return
    state.dev_idx = (state.dev_idx + 1) % len(state.devs)
    _tui_do_refresh(stdscr, state, confirm_if_dirty=False)


def _tui_rescan(stdscr, state, args):
    if state.any_dirty() and not _tui_confirm(stdscr, "Unapplied edits will be discarded. Rescan? [y/N] "):
        return
    devs = discover()
    if getattr(args, "device", None):
        devs = [d for d in devs if d.mode == args.device]
    if not devs:
        state.message = "No Rapoo VT3 PRO config interface found."
        state.message_kind = "error"
        return
    state.devs = devs
    state.dev_idx = 0
    _tui_do_refresh(stdscr, state, confirm_if_dirty=False)


def _tui_draw_loading(stdscr, state, y, w):
    _safe_addstr(stdscr, y, 2, f"{state.spinner_frame} Loading..."[:w - 3],
                 curses.color_pair(2) | curses.A_DIM)
    return y + 1


def _tui_draw_pending(stdscr, state, sec, y):
    if state.section_dirty(sec):
        _safe_addstr(stdscr, y, 2, "[pending -- press 'a' to apply]", curses.color_pair(2))
        y += 1
    return y


def _tui_draw(stdscr, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    m = state.dev
    title = f"vt3pro tui -- Rapoo VT3 PRO [{m.mode}]  {m.path}"
    idx = f"(D)evice: {m.mode} [{state.dev_idx + 1}/{len(state.devs)}]"
    _safe_addstr(stdscr, 0, 0, title[:w - 1], curses.A_BOLD)
    if len(title) + len(idx) + 2 < w:
        _safe_addstr(stdscr, 0, w - len(idx) - 1, idx)
    _safe_addstr(stdscr, 1, 0, "-" * min(w - 1, 100))
    y = 3
    d = state.decoded

    # DPI
    _safe_addstr(stdscr, y, 0, "DPI", _tui_section_attr(state, SECTION_DPI))
    y += 1
    if not d:
        y = _tui_draw_loading(stdscr, state, y, w)
    else:
        pending = curses.color_pair(2) if state.section_dirty(SECTION_DPI) else 0
        for i in range(7):
            mark = " <- active" if d["active_stage"] == i + 1 else ""
            xy = f"{d['dpi_x'][i]:>6}" if d["dpi_x"][i] == d["dpi_y"][i] else \
                f"{d['dpi_x'][i]:>6} x {d['dpi_y'][i]}"
            _safe_addstr(stdscr, y, 2, f"Stage {i + 1}  {xy} DPI{mark}"[:w - 3],
                         _tui_field_attr(state, SECTION_DPI, i) or pending)
            y += 1
        _safe_addstr(stdscr, y, 2, f"Active stage: {d['active_stage']}",
                     _tui_field_attr(state, SECTION_DPI, 7) or pending)
        y = _tui_draw_pending(stdscr, state, SECTION_DPI, y + 1)
    y += 1

    # Performance
    _safe_addstr(stdscr, y, 0, "Performance", _tui_section_attr(state, SECTION_PERF))
    y += 1
    if not d:
        y = _tui_draw_loading(stdscr, state, y, w)
    else:
        pending = curses.color_pair(2) if state.section_dirty(SECTION_PERF) else 0
        link = "cable" if m.mode == "wired" else "2.4GHz"
        fields = [
            f"Polling rate ({link}): {d['polling_hz']} Hz",
            f"Motion sync: {'on' if d['motion_sync'] else 'off'}",
            f"Angle snap: {'on' if d['angle_snap'] else 'off'}",
            f"Ripple control: {'on' if d['ripple_control'] else 'off'}",
        ]
        for i, text in enumerate(fields):
            _safe_addstr(stdscr, y, 2, text[:w - 3], _tui_field_attr(state, SECTION_PERF, i) or pending)
            y += 1
        _safe_addstr(stdscr, y, 2, f"Lift-off: level {d['lod_index']}   Sensor angle: "
                                   f"{d['sensor_angle']}   (read-only)"[:w - 3], curses.A_DIM)
        y = _tui_draw_pending(stdscr, state, SECTION_PERF, y + 1)
    y += 1

    # Timing
    _safe_addstr(stdscr, y, 0, "Timing", _tui_section_attr(state, SECTION_TIMING))
    y += 1
    if not d:
        y = _tui_draw_loading(stdscr, state, y, w)
    else:
        pending = curses.color_pair(2) if state.section_dirty(SECTION_TIMING) else 0
        fields = [
            f"Debounce (press): {d['debounce_press_ms']} ms",
            f"Debounce (release): {d['debounce_release_ms']} ms",
            f"Sleep after: {d['sleep_min']} min",
        ]
        for i, text in enumerate(fields):
            _safe_addstr(stdscr, y, 2, text[:w - 3], _tui_field_attr(state, SECTION_TIMING, i) or pending)
            y += 1
        y = _tui_draw_pending(stdscr, state, SECTION_TIMING, y)
    y += 1

    # Battery
    _safe_addstr(stdscr, y, 0, "Battery", _tui_section_attr(state, SECTION_BATTERY))
    y += 1
    attr = _tui_field_attr(state, SECTION_BATTERY, 0)
    if state.battery_loading:
        _tui_draw_loading(stdscr, state, y, w)
    elif state.battery:
        pct, st = state.battery
        _safe_addstr(stdscr, y, 2, f"{pct}%  ({st})   press 'b' to read again", attr)
    else:
        _safe_addstr(stdscr, y, 2, "unknown -- press 'b' to read", attr)

    hint1 = "Tab:section  Up/Dn:field  Left/Right:change  Enter:edit"
    hint2 = "a:apply  A:apply-all  r:refresh  b:battery  D:device  R:rescan  q:quit"
    _safe_addstr(stdscr, h - 3, 0, hint1[:w - 1], curses.A_DIM)
    _safe_addstr(stdscr, h - 2, 0, hint2[:w - 1], curses.A_DIM)
    attr = curses.color_pair(1) if state.message_kind == "error" else curses.color_pair(4)
    _safe_addstr(stdscr, h - 1, 0, state.message[:w - 1], attr)
    stdscr.refresh()


def _tui_handle_key(stdscr, state, ch, args):
    if ch in (ord("q"), 27):
        if state.any_dirty() and not _tui_confirm(stdscr, "Unapplied edits will be lost. Quit? [y/N] "):
            return True
        return False
    if ch == 9:  # Tab
        state.section = (state.section + 1) % len(SECTION_NAMES)
        state.field = 0
    elif ch == getattr(curses, "KEY_BTAB", -1):
        state.section = (state.section - 1) % len(SECTION_NAMES)
        state.field = 0
    elif ch in (curses.KEY_DOWN, ord("j")):
        state.field = (state.field + 1) % _tui_field_count(state.section)
    elif ch in (curses.KEY_UP, ord("k")):
        state.field = (state.field - 1) % _tui_field_count(state.section)
    elif ch in (curses.KEY_LEFT, ord("h")):
        _tui_nudge(state, -1)
    elif ch in (curses.KEY_RIGHT, ord("l")):
        _tui_nudge(state, 1)
    elif ch in (curses.KEY_ENTER, 10, 13):
        _tui_edit_field(stdscr, state)
    elif ch == ord("a"):
        _tui_apply_sections(stdscr, state, [state.section], SECTION_NAMES[state.section])
    elif ch == ord("A"):
        _tui_apply_sections(stdscr, state, list(SECTION_BLOCKS), "All sections")
    elif ch == ord("r"):
        _tui_do_refresh(stdscr, state, confirm_if_dirty=True)
    elif ch == ord("b"):
        _tui_poll_battery(stdscr, state)
    elif ch == ord("D"):
        _tui_switch_device(stdscr, state)
    elif ch == ord("R"):
        _tui_rescan(stdscr, state, args)
    return True


def _tui_main(stdscr, devs, args):
    curses.curs_set(0)
    _tui_init_colors()
    state = TuiState(devs)
    _tui_do_refresh(stdscr, state, confirm_if_dirty=False)
    while True:
        _tui_draw(stdscr, state)
        ch = stdscr.getch()
        if not _tui_handle_key(stdscr, state, ch, args):
            return


def cmd_tui(args):
    devs = pick(args)
    curses.wrapper(_tui_main, devs, args)


UDEV_RULE = (
    'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="24ae", '
    'ATTRS{idProduct}=="1231", TAG+="uaccess"\n'
    'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="24ae", '
    'ATTRS{idProduct}=="4431", TAG+="uaccess"\n'
)


def cmd_install_udev(args):
    path = os.path.join(CONFIG_DIR, "99-rapoo-vt3pro.rules")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(UDEV_RULE)
    print("Run these two commands to grant your user access to the mouse:\n")
    print(f"  sudo install -m 644 {path} /etc/udev/rules.d/99-rapoo-vt3pro.rules")
    print("  sudo udevadm control --reload-rules && sudo udevadm trigger")
    print("\nThen unplug/replug the dongle (or cable).")


def main():
    p = argparse.ArgumentParser(prog="vt3pro", description="Configure the Rapoo VT3 PRO mouse.")
    p.add_argument("--device", choices=["wired", "dongle"],
                   help="target only one connection (default: all present)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show current settings").set_defaults(fn=cmd_status)
    sub.add_parser("dump", help="raw EEPROM blocks (debugging)").set_defaults(fn=cmd_dump)
    sub.add_parser("battery", help="show battery level").set_defaults(fn=cmd_battery)
    sub.add_parser("tui", help="interactive terminal UI").set_defaults(fn=cmd_tui)
    sub.add_parser("install-udev", help="print udev setup commands").set_defaults(fn=cmd_install_udev)

    sv = sub.add_parser("save", help="save current settings (raw blocks) to a JSON file")
    sv.add_argument("file", nargs="?", help=f"default {os.path.join(CONFIG_DIR, 'settings.json')}")
    sv.set_defaults(fn=cmd_save)

    rs = sub.add_parser("restore", help=f"write back a file from 'save' (first-write backup: {AUTO_BACKUP})")
    rs.add_argument("file")
    rs.set_defaults(fn=cmd_restore)

    d = sub.add_parser("dpi", help="set DPI stages / active stage")
    d.add_argument("--stages", help="7 comma-separated values, e.g. 400,800,1200,1600,3200,6400,26000")
    d.add_argument("--stage", type=int, choices=range(1, 8), help="stage to modify")
    d.add_argument("--value", type=int, help="DPI value for --stage")
    d.add_argument("--active", type=int, choices=range(1, 8), help="stage to switch to")
    d.set_defaults(fn=cmd_dpi)

    r = sub.add_parser("polling", help="set polling rate for the current connection")
    r.add_argument("hz", type=int, help=", ".join(map(str, sorted(POLL_RATES))))
    r.set_defaults(fn=cmd_polling)

    onoff = dict(type=lambda s: s == "on", choices=[True, False], metavar="on|off")
    s = sub.add_parser("sensor", help="motion sync / angle snap / ripple control")
    s.add_argument("--motion-sync", **onoff)
    s.add_argument("--angle-snap", **onoff, help="straight-line correction")
    s.add_argument("--ripple", **onoff, help="ripple control (jitter smoothing)")
    s.set_defaults(fn=cmd_sensor)

    b = sub.add_parser("debounce", help="set click debounce time")
    b.add_argument("--press", type=int, help=f"ms, one of {DEBOUNCE_MS}")
    b.add_argument("--release", type=int, help=f"ms, one of {DEBOUNCE_MS}")
    b.set_defaults(fn=cmd_debounce)

    sl = sub.add_parser("sleep", help="set idle sleep timer")
    sl.add_argument("minutes", type=int, help=f"{SLEEP_MIN}-{SLEEP_MAX}")
    sl.set_defaults(fn=cmd_sleep)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
