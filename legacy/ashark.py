#!/usr/bin/env python3
"""
ashark - configure the Attack Shark X11 mouse on Linux without the vendor software.

Talks the vendor HID feature-report protocol directly over /dev/hidraw*.
Protocol reference: github.com/HarukaYamamoto0/attack-shark-x11-driver (docs/)

Everywhere possible: read the device's current config report, modify only the
requested fields, recompute the checksum, write it back. Bytes whose meaning
isn't documented are preserved untouched rather than guessed at.
"""

import argparse
import array
import curses
import fcntl
import glob
import json
import os
import select
import sys
import threading
import time

VENDOR = 0x1D57
PRODUCTS = {0xFA55: "wired", 0xFA60: "dongle"}

RID_DPI = 0x04
RID_LIGHT = 0x05
RID_POLL = 0x06
RID_READ = 0xA0

CONFIG = os.path.expanduser("~/.config/attackshark/x11.json")

POLL_RATES = {125: 0x08, 250: 0x04, 500: 0x02, 1000: 0x01}

LIGHT_MODES = {
    "off": 0x0, "static": 0x1, "breathing": 0x2, "neon": 0x3,
    "colorbreathing": 0x4, "staticdpi": 0x5, "breathingdpi": 0x6,
}
LIGHT_MODE_NAMES = {v: k for k, v in LIGHT_MODES.items()}

DPI_3311 = [
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0e, 0x0f, 0x10, 0x11, 0x12,
    0x13, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x22, 0x23, 0x24, 0x25,
    0x26, 0x27, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2f, 0x30, 0x31, 0x32, 0x33, 0x34, 0x36, 0x37, 0x38,
    0x39, 0x3a, 0x3b, 0x3d, 0x3e, 0x3f, 0x40, 0x41, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x4a, 0x4b,
    0x4c, 0x4d, 0x4e, 0x4f, 0x51, 0x52, 0x53, 0x54, 0x55, 0x57, 0x58, 0x59, 0x5a, 0x5b, 0x5c, 0x5e,
    0x5f, 0x60, 0x61, 0x62, 0x63, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6b, 0x6c, 0x6d, 0x6e, 0x6f, 0x70,
    0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x79, 0x7a, 0x7b, 0x7c, 0x7d, 0x7f, 0x80, 0x81, 0x82, 0x83,
    0x84, 0x86, 0x87, 0x88, 0x89, 0x8a, 0x8b, 0x8d, 0x8e, 0x8f, 0x90, 0x91, 0x93, 0x94, 0x95, 0x96,
    0x97, 0x98, 0x9a, 0x9b, 0x9c, 0x9d, 0x9e, 0x9f, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa7, 0xa8, 0xa9,
    0xaa, 0xab, 0xac, 0xae, 0xaf, 0xb0, 0xb1, 0xb2, 0xb3, 0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xbb, 0xbc,
    0xbd, 0xbe, 0xbf, 0xc0, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcc, 0xcd, 0xcf,
    0xd0, 0xd1, 0xd2, 0xd3, 0xd4, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xdb, 0xdd, 0xde, 0xdf, 0xe0, 0xe1,
    0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xea, 0xeb, 0x76, 0x77, 0x79, 0x7a, 0x7b, 0x7c, 0x7d, 0x7f,
    0x80, 0x81, 0x82, 0x83, 0x84, 0x86, 0x87, 0x88, 0x89, 0x8a, 0x8b, 0x8d
]


# ---------------------------------------------------------------- DPI codec

def dpi_to_bytes(dpi):
    """Encode a DPI value into (x_byte, y_byte, is_double) for the PAW3311."""
    d = max(50, min(int(dpi), 26000))
    if d == 20100:
        return 0xEB, 1, True
    is_double = False
    target = d
    if d > 10000:
        target = int(d / 2 + 0.5)
        is_double = True
    # The 220-entry step-100 table already covers target values up to
    # 22000 (index 219 -> 22000), which is the largest a halved DPI can
    # ever be (26000 max / 2 = 13000). Confirmed against this mouse's own
    # factory-set 22000 DPI stage, which is encoded via that table
    # (x=0x81, y=1), NOT via the 16-bit extended scheme below -- so this
    # branch is unreachable in practice but kept for values past the
    # table's range.
    if target > 22000:
        combined = 199 + (target - 10100) // 100
        return combined & 0xFF, (combined >> 8) & 0xFF, is_double
    if target > 5000 and target % 100 == 0:
        return DPI_3311[target // 100 - 1], 1, is_double
    return DPI_3311[(target - 50) // 50], 0, is_double


def bytes_to_dpi(x, y, is_double):
    """Decode (x_byte, y_byte, is_double) back into a DPI value."""
    if x == 0 and y == 0:
        return 0
    if y == 0:
        i = DPI_3311.index(x) if x in DPI_3311 else -1
        val = i * 50 + 50 if i >= 0 else 50
    elif y == 1:
        i = DPI_3311.index(x) if x in DPI_3311 else -1
        val = i * 100 + 100 if i >= 0 else 100
    else:
        val = 10100 + (((y << 8) | x) - 199) * 100
    if is_double:
        if x == 0xEB:
            return 20100
        val *= 2
    return min(val, 26000)


# ---------------------------------------------------------- hidraw plumbing

def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (typ << 8) | nr


def HIDIOCGFEATURE(size):
    return _ioc(3, ord("H"), 0x07, size)


def HIDIOCSFEATURE(size):
    return _ioc(3, ord("H"), 0x06, size)


def parse_feature_lengths(desc):
    """Walk a HID report descriptor, return {report_id: feature payload bytes}.

    Global items (report size/count) persist across collections, so they are
    tracked as running state exactly as the HID spec requires.
    """
    out = {}
    rid = 0
    size = 0
    count = 0
    i = 0
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:  # long item
            i += 2 + desc[i + 1]
            continue
        tag, typ, blen = b & 0xFC, (b >> 2) & 0x03, b & 0x03
        blen = 4 if blen == 3 else blen
        data = int.from_bytes(desc[i + 1:i + 1 + blen], "little") if blen else 0
        if tag == 0x84:      # Report ID
            rid = data
        elif tag == 0x74:    # Report Size
            size = data
        elif tag == 0x94:    # Report Count
            count = data
        elif tag == 0xB0:    # Feature (main item)
            out[rid] = out.get(rid, 0) + (size * count + 7) // 8
        i += 1 + blen
    return out


class DeviceError(Exception):
    """A problem talking to one specific config interface (skip, don't abort)."""


class Mouse:
    """One config interface of the mouse (the vendor collection with report 0x04)."""

    def __init__(self, path, product, feature_lengths):
        self.path = path
        self.product = product
        self.mode = PRODUCTS[product]
        self.lengths = feature_lengths
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(
                f"no permission to open {self.path}. "
                f"Install the udev rule (see: ashark install-udev) or run with sudo.")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)

    def report_len(self, rid):
        """Total transfer length for a report: 1 report-ID byte + payload."""
        if rid not in self.lengths:
            raise DeviceError(f"device does not expose feature report 0x{rid:02x}")
        return self.lengths[rid] + 1

    def set_feature(self, payload):
        buf = array.array("B", payload)
        fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)

    def get_feature(self, rid, length):
        buf = array.array("B", [0] * length)
        buf[0] = rid
        fcntl.ioctl(self.fd, HIDIOCGFEATURE(length), buf, True)
        return bytearray(buf)

    def read_config(self, rid, retries=3):
        """Unlock a config report for reading (0xA0 handshake), then fetch it.

        The firmware seems to want a short cooldown between successive
        config-report reads; hitting it too fast can stall the control
        endpoint (surfaces as EPIPE/OSError from the ioctl), not just refuse
        the read. Both failure modes are retried the same way.
        """
        length = self.report_len(rid)
        # byte 2 carries the declared packet length, which is the wireless
        # (full) length even when we're wired -- matches the vendor software.
        declared = 0x38 if rid == RID_DPI else (0x0F if rid == RID_LIGHT else 0x09)
        last_err = None
        # Give the firmware a moment to settle after whatever came before
        # this call (e.g. a previous read_config for a different report).
        time.sleep(0.1)
        for attempt in range(retries + 1):
            try:
                self.set_feature([RID_READ, rid, declared, 0x00, 0x01, 0x00, 0x00, 0x00])
                time.sleep(0.25)
                status = self.get_feature(RID_READ, self.report_len(RID_READ))
                if status[1] == 0x01:
                    return self.get_feature(rid, length)
                last_err = f"status {status[1]:#04x}"
            except OSError as e:
                last_err = str(e)
            time.sleep(0.3 * (attempt + 1))
        raise DeviceError(
            f"firmware refused read access to report 0x{rid:02x} "
            f"({last_err}) after {retries + 1} tries -- "
            f"this link (wired cable / wireless dongle) may not have a live "
            f"connection to the mouse right now.")

    def drain_events(self, timeout=2.0):
        """Collect interrupt-endpoint event messages (battery, status)."""
        events = []
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], max(0, end - time.time()))
            if not r:
                break
            try:
                data = os.read(self.fd, 64)
            except BlockingIOError:
                continue
            if len(data) >= 5 and data[0] == 0x03:
                events.append(bytes(data[:5]))
        return events


def discover():
    """Find every Attack Shark config interface currently attached."""
    found = []
    nodes = sorted(glob.glob("/sys/class/hidraw/hidraw*"),
                    key=lambda p: int(p.rsplit("hidraw", 1)[1]))
    for node in nodes:
        try:
            uevent = open(os.path.join(node, "device/uevent")).read()
            desc = open(os.path.join(node, "device/report_descriptor"), "rb").read()
        except OSError:
            continue
        hid_id = next((l.split("=")[1] for l in uevent.splitlines()
                       if l.startswith("HID_ID=")), None)
        if not hid_id:
            continue
        parts = hid_id.split(":")
        vid, pid = int(parts[1], 16), int(parts[2], 16)
        if vid != VENDOR or pid not in PRODUCTS:
            continue
        lengths = parse_feature_lengths(desc)
        # The config interface is the one exposing the DPI + read reports.
        if RID_DPI in lengths and RID_READ in lengths:
            found.append(Mouse(f"/dev/{os.path.basename(node)}", pid, lengths))
    # Wired first: it's the deterministic, always-live link. The dongle
    # only answers config reads while a mouse is actually paired over it,
    # so trying it first can surface a spurious failure when the cable is
    # also plugged in.
    found.sort(key=lambda m: 0 if m.mode == "wired" else 1)
    return found


def pick(args):
    devs = discover()
    if not devs:
        die("no Attack Shark X11 config interface found. Is the mouse plugged in "
            "(cable) or the dongle connected?")
    if getattr(args, "device", None):
        devs = [d for d in devs if d.mode == args.device]
        if not devs:
            die(f"no '{args.device}' connection present")
    return devs


# ------------------------------------------------------------ report codecs

def decode_dpi(buf):
    dbl = buf[6]
    stages = []
    for i in range(8):
        x, y = buf[8 + i], buf[16 + i]
        stages.append(bytes_to_dpi(x, y, bool(dbl & (1 << i))))
    return {
        "profile": buf[2],
        "lod": buf[3] >> 4,
        "angle_snap": bool(buf[3] & 0x0F),
        "motion_sync": bool(buf[4] >> 4),
        "ripple_control": bool(buf[4] & 0x0F),
        "active_mask": buf[5],
        "stages": stages,
        "current_stage": buf[24],
        "colors": [tuple(buf[25 + i * 3:28 + i * 3]) for i in range(8)],
    }


def dpi_checksum(buf):
    return sum(buf[3:50]) & 0xFFFF


def seal_dpi(buf):
    c = dpi_checksum(buf)
    buf[50] = (c >> 8) & 0xFF
    buf[51] = c & 0xFF
    return buf


def decode_light(buf):
    return {
        "profile": buf[2],
        "mode": LIGHT_MODE_NAMES.get(buf[3] >> 4, f"0x{buf[3] >> 4:x}"),
        "led_speed": buf[4] & 0x0F,
        "brightness": buf[5] & 0x0F,
        "deep_sleep_min": (buf[4] & 0xF0) | (buf[5] >> 4),
        "color": (buf[6], buf[7], buf[8]),
        "sleep_min": buf[9] / 2,
        "debounce_ms": buf[10] * 2,
    }


def seal_light(buf):
    c = sum(buf[3:11]) & 0xFFFF
    buf[11] = (c >> 8) & 0xFF
    buf[12] = c & 0xFF
    return buf


def decode_poll(buf):
    inv = {v: k for k, v in POLL_RATES.items()}
    return {"hz": inv.get(buf[3], f"unknown(0x{buf[3]:02x})")}


# ----------------------------------------------------------------- commands

def die(msg):
    print(f"ashark: {msg}", file=sys.stderr)
    sys.exit(1)


def read_battery(m, timeout):
    """Wait for a battery event on an already-open dongle Mouse. Returns
    (pct, state_label) or None if nothing arrived within timeout."""
    for ev in m.drain_events(timeout):
        if ev[2] in (0x40, 0x41):
            state = {1: "discharging", 2: "fully charged",
                     3: "charging/wired"}.get(ev[3], f"0x{ev[3]:02x}")
            return ev[4], state
    return None


def cmd_status(args):
    for dev in pick(args):
        try:
            with dev as m:
                print(f"== Attack Shark X11 [{m.mode}]  {m.path}")
                dpi = decode_dpi(m.read_config(RID_DPI))
                light = decode_light(m.read_config(RID_LIGHT))
                poll = decode_poll(m.read_config(RID_POLL))

                n = bin(dpi["active_mask"]).count("1")
                print(f"  polling rate   : {poll['hz']} Hz")
                print(f"  DPI stages     : {n} enabled (mask 0x{dpi['active_mask']:02x})")
                for i in range(8):
                    if dpi["active_mask"] & (1 << i):
                        mark = " <- active" if dpi["current_stage"] == i + 1 else ""
                        r, g, b = dpi["colors"][i]
                        print(f"      stage {i+1}    : {dpi['stages'][i]:>6} DPI  "
                              f"#{r:02x}{g:02x}{b:02x}{mark}")
                print(f"  angle snap     : {'on' if dpi['angle_snap'] else 'off'}")
                print(f"  ripple control : {'on' if dpi['ripple_control'] else 'off'}")
                print(f"  motion sync    : {'on' if dpi['motion_sync'] else 'off'}")
                print(f"  lift-off dist  : {dpi['lod']}")
                print(f"  light mode     : {light['mode']}  "
                      f"colour #{light['color'][0]:02x}{light['color'][1]:02x}{light['color'][2]:02x}  "
                      f"brightness {light['brightness']}/8  speed {light['led_speed']}/5")
                print(f"  debounce       : {light['debounce_ms']} ms")
                print(f"  sleep          : {light['sleep_min']} min idle, "
                      f"{light['deep_sleep_min']} min deep sleep")

                if m.mode == "dongle":
                    battery = read_battery(m, timeout=2.5)
                    if battery:
                        pct, state = battery
                        print(f"  battery        : {pct}%  ({state})")
                    else:
                        print("  battery        : unknown (no report yet -- move/click the mouse)")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


def cmd_dpi(args):
    for dev in pick(args):
        try:
            with dev as m:
                buf = m.read_config(RID_DPI)
                cur = decode_dpi(buf)

                if args.stages:
                    vals = [int(v) for v in args.stages.split(",")]
                    if not 1 <= len(vals) <= 8:
                        die("give between 1 and 8 comma-separated DPI values")
                    dbl = 0
                    for i in range(8):
                        v = vals[i] if i < len(vals) else 0
                        x, y, is_dbl = dpi_to_bytes(v) if v else (0, 0, False)
                        buf[8 + i], buf[16 + i] = x, y
                        if is_dbl:
                            dbl |= 1 << i
                    buf[6] = buf[7] = dbl
                    buf[5] = (1 << len(vals)) - 1
                    if buf[24] > len(vals):
                        buf[24] = 1
                elif args.stage:
                    if not args.value:
                        die("--stage needs --value")
                    i = args.stage - 1
                    x, y, is_dbl = dpi_to_bytes(args.value)
                    buf[8 + i], buf[16 + i] = x, y
                    buf[6] = (buf[6] | (1 << i)) if is_dbl else (buf[6] & ~(1 << i))
                    buf[7] = buf[6]
                    buf[5] |= 1 << i

                if args.active:
                    if not buf[5] & (1 << (args.active - 1)):
                        die(f"stage {args.active} is not enabled")
                    buf[24] = args.active
                if args.angle_snap is not None:
                    buf[3] = (buf[3] & 0xF0) | (1 if args.angle_snap else 0)
                if args.ripple is not None:
                    buf[4] = (buf[4] & 0xF0) | (1 if args.ripple else 0)

                if args.color:
                    if not args.stage:
                        die("--color needs --stage")
                    try:
                        rgb = parse_color(args.color)
                    except ValueError as e:
                        die(str(e))
                    buf[25 + (args.stage - 1) * 3: 28 + (args.stage - 1) * 3] = bytearray(rgb)

                seal_dpi(buf)
                m.set_feature(buf[:m.report_len(RID_DPI)])
                new = decode_dpi(buf)
                act = new["stages"][new["current_stage"] - 1]
                enabled = [f"{new['stages'][i]}" for i in range(8) if new["active_mask"] & (1 << i)]
                print(f"[{m.mode}] DPI stages: {', '.join(enabled)} "
                      f"(active: stage {new['current_stage']} = {act} DPI)")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


def cmd_polling(args):
    if args.hz not in POLL_RATES:
        die(f"polling rate must be one of {sorted(POLL_RATES)}")
    for dev in pick(args):
        try:
            with dev as m:
                buf = bytearray(m.report_len(RID_POLL))
                buf[0] = RID_POLL
                buf[1] = 0x09
                buf[2] = 0x01
                buf[3] = POLL_RATES[args.hz]
                buf[4] = 0xFF - buf[3]
                m.set_feature(buf)
                print(f"[{m.mode}] polling rate: {args.hz} Hz")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


def parse_color(s):
    """Parse an 'RRGGBB' hex string into an (r,g,b) tuple.

    Raises ValueError (not die()) so it's safe to call from contexts that
    need to recover from bad input, like the TUI's inline field validation --
    CLI call sites catch ValueError themselves and call die().
    """
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError("colour must be RRGGBB hex, e.g. ff8800")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ValueError("colour must be RRGGBB hex, e.g. ff8800")


def cmd_light(args):
    for dev in pick(args):
        try:
            with dev as m:
                buf = m.read_config(RID_LIGHT)
                if args.mode:
                    buf[3] = LIGHT_MODES[args.mode] << 4
                if args.color:
                    try:
                        buf[6], buf[7], buf[8] = parse_color(args.color)
                    except ValueError as e:
                        die(str(e))
                if args.brightness is not None:
                    buf[5] = (buf[5] & 0xF0) | (args.brightness & 0x0F)
                if args.speed is not None:
                    buf[4] = (buf[4] & 0xF0) | (args.speed & 0x0F)
                seal_light(buf)
                m.set_feature(buf[:m.report_len(RID_LIGHT)])
                s = decode_light(buf)
                print(f"[{m.mode}] light: {s['mode']}  "
                      f"#{s['color'][0]:02x}{s['color'][1]:02x}{s['color'][2]:02x}  "
                      f"brightness {s['brightness']}/8  speed {s['led_speed']}/5")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


def cmd_debounce(args):
    if not (4 <= args.ms <= 50) or args.ms % 2:
        die("debounce must be an even number of ms between 4 and 50")
    for dev in pick(args):
        try:
            with dev as m:
                buf = m.read_config(RID_LIGHT)
                buf[10] = args.ms // 2
                seal_light(buf)
                m.set_feature(buf[:m.report_len(RID_LIGHT)])
                print(f"[{m.mode}] debounce: {args.ms} ms")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


def cmd_sleep(args):
    for dev in pick(args):
        try:
            with dev as m:
                buf = m.read_config(RID_LIGHT)
                if args.idle is not None:
                    if not (0.5 <= args.idle <= 30) or (args.idle * 2) % 1:
                        die("--idle must be 0.5-30 minutes in steps of 0.5")
                    buf[9] = int(args.idle * 2)
                if args.deep is not None:
                    if not (1 <= args.deep <= 60):
                        die("--deep must be 1-60 minutes")
                    buf[4] = (args.deep & 0xF0) | (buf[4] & 0x0F)
                    buf[5] = ((args.deep & 0x0F) << 4) | (buf[5] & 0x0F)
                seal_light(buf)
                m.set_feature(buf[:m.report_len(RID_LIGHT)])
                s = decode_light(buf)
                print(f"[{m.mode}] sleep: {s['sleep_min']} min idle, "
                      f"{s['deep_sleep_min']} min deep sleep")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


def cmd_save(args):
    dev = pick(args)[0]
    with dev as m:
        state = {
            "dpi": decode_dpi(m.read_config(RID_DPI)),
            "light": decode_light(m.read_config(RID_LIGHT)),
            "polling": decode_poll(m.read_config(RID_POLL)),
        }
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(state, f, indent=2)
    print(f"saved current settings to {CONFIG}")


def cmd_battery(args):
    devs = [d for d in pick(args) if d.mode == "dongle"]
    if not devs:
        die("battery is only reported over the wireless dongle link "
            "(wired mode is externally powered, the firmware never reports a level for it)")
    with devs[0] as m:
        print("Waiting for a battery report... (click or move the mouse if nothing appears)")
        battery = read_battery(m, timeout=args.timeout)
        if battery:
            pct, state = battery
            print(f"battery: {pct}%  ({state})")
        else:
            die(f"no battery report received within {args.timeout:.0f}s -- "
                f"move/click the mouse to wake it and try again")


def cmd_dump(args):
    for dev in pick(args):
        try:
            with dev as m:
                print(f"== {m.mode} {m.path}")
                print(f"  feature report lengths: "
                      f"{ {hex(k): v for k, v in sorted(m.lengths.items())} }")
                for rid, name in ((RID_DPI, "dpi"), (RID_LIGHT, "light"), (RID_POLL, "poll")):
                    buf = m.read_config(rid)
                    print(f"  0x{rid:02x} {name:6}: {buf.hex()}")
        except DeviceError as e:
            print(f"[{dev.mode}] skipped: {e}", file=sys.stderr)
            continue


# -------------------------------------------------------------------- TUI
#
# Interactive terminal UI ("ashark tui"). Built entirely on stdlib curses
# (no urwid/textual/rich -- none are installed, and the tool's whole design
# stays zero-dependency). This layer only presents/edits state; it never
# reimplements protocol logic. Every edit mutates the exact same raw
# bytearray objects read_config() returns, at the exact same byte/nibble
# offsets the cmd_dpi/cmd_light/cmd_polling functions above already use --
# decode_dpi/decode_light/decode_poll are the single source of truth for
# what those bytes mean, called fresh after every mutation to refresh the
# display. All *device I/O* stays synchronous and single-threaded, and is
# only ever triggered by an explicit keypress -- no timers, no background
# polling -- because this firmware is timing-sensitive (see read_config's
# retry/backoff above; hammering it caused USB stalls and even corrupted
# reads during development, both here and in an abandoned WebHID prototype).
# The one exception is a small screen-only spinner thread (see
# _tui_run_with_spinner) used to animate the loading indicator while a
# blocking read is in flight -- it never touches the device, only redraws,
# and is always fully joined before the calling thread does anything else
# with curses or the hardware, so there's still only ever one thread of
# device I/O and never two threads drawing at once.

SECTION_DPI, SECTION_POLL, SECTION_LIGHT, SECTION_BATTERY = range(4)
SECTION_NAMES = ["DPI", "Performance", "Lighting", "Battery"]
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # dots spinner (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏)


class TuiState:
    def __init__(self, devs):
        self.devs = devs
        self.dev_idx = 0
        self.raw = {}          # rid -> bytearray, last bytes read/edited
        self.decoded = {}      # rid -> dict, always decode_*(raw[rid]) -- display cache only
        self.dirty = {}        # rid -> bool, unapplied local edits
        self.poll_hz = None    # pending polling rate (poll report has no read-modify-write in the CLI either)
        self.battery = None    # (pct, state) | None
        self.spinner_frame = SPINNER_FRAMES[0]
        self.battery_loading = False
        self.message = "Reading device..."
        self.message_kind = "info"
        self.section = SECTION_DPI
        self.field = 0

    @property
    def dev(self):
        return self.devs[self.dev_idx]


def _safe_addstr(stdscr, y, x, text, attr=0):
    """addstr that swallows the standard curses edge-of-screen error."""
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _tui_draw_loading(stdscr, state, y, w):
    """A panel's loading placeholder, shown until its report has been read.
    Animates when a spinner thread (see _tui_run_with_spinner) is updating
    state.spinner_frame; otherwise just shows the current/first frame."""
    text = f"{state.spinner_frame} Loading..."
    _safe_addstr(stdscr, y, 2, text[:w - 3], curses.color_pair(2) | curses.A_DIM)
    return y + 1


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
    return {SECTION_DPI: 11, SECTION_POLL: 1, SECTION_LIGHT: 7, SECTION_BATTERY: 1}[section]


def _tui_section_attr(state, section):
    return curses.color_pair(3) | curses.A_BOLD if state.section == section else curses.A_BOLD


def _tui_field_attr(state, section, field):
    return curses.color_pair(3) if state.section == section and state.field == field else 0


# ---------------------------------------------------------- modal helpers

def _tui_edit_line(stdscr, prompt, initial="", validate=None):
    """Blocking single-line editor drawn on the second-to-last screen row.

    Enter accepts (re-validating first -- on failure it shows the error and
    keeps editing rather than closing); Esc cancels and returns None.
    """
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
            elif ch == 27:  # Esc
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
    y = h - 1
    _safe_addstr(stdscr, y, 0, " " * (w - 1))
    _safe_addstr(stdscr, y, 0, prompt[:w - 1], curses.color_pair(2))
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, ord("q"), curses.KEY_ENTER, 10, 13):
            return False


def _tui_run_with_spinner(stdscr, state, fn):
    """Run a blocking call while animating state.spinner_frame in the
    background so loading panels actually spin instead of sitting on a
    static glyph. The spinner thread only ever reads state and draws --
    it never touches the device -- and is always joined (in `finally`)
    before this function returns, so by the time the caller does anything
    else with curses or the hardware, exactly one thread is active again.
    """
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


# --------------------------------------------------- field edit/nudge logic

def _tui_nudge(state, d):
    """Handle Left/Right on the currently focused field."""
    sec = state.section
    if sec == SECTION_DPI:
        buf = state.raw[RID_DPI]
        f = state.field
        if f < 8:
            bit = 1 << f
            turning_on = not (buf[5] & bit)
            buf[5] ^= bit
            if turning_on and (buf[5] & bit) and buf[8 + f] == 0 and buf[16 + f] == 0:
                # Enabling a stage that's never had a value set: give it a
                # sane default rather than leaving it at 0 DPI.
                x, y, is_dbl = dpi_to_bytes(800)
                buf[8 + f], buf[16 + f] = x, y
                if is_dbl:
                    buf[6] |= bit
                else:
                    buf[6] &= ~bit
                buf[7] = buf[6]
            elif not turning_on and state.decoded[RID_DPI]["current_stage"] == f + 1:
                remaining = [j + 1 for j in range(8) if buf[5] & (1 << j)]
                buf[24] = remaining[0] if remaining else 1
            state.dirty[RID_DPI] = True
            state.decoded[RID_DPI] = decode_dpi(buf)
        elif f == 8:
            enabled = [j + 1 for j in range(8) if buf[5] & (1 << j)]
            if enabled:
                cur = buf[24] if buf[24] in enabled else enabled[0]
                buf[24] = enabled[(enabled.index(cur) + d) % len(enabled)]
                state.dirty[RID_DPI] = True
                state.decoded[RID_DPI] = decode_dpi(buf)
        elif f == 9:
            buf[3] = (buf[3] & 0xF0) | (0 if (buf[3] & 0x0F) else 1)
            state.dirty[RID_DPI] = True
            state.decoded[RID_DPI] = decode_dpi(buf)
        elif f == 10:
            buf[4] = (buf[4] & 0xF0) | (0 if (buf[4] & 0x0F) else 1)
            state.dirty[RID_DPI] = True
            state.decoded[RID_DPI] = decode_dpi(buf)

    elif sec == SECTION_POLL:
        rates = sorted(POLL_RATES)
        cur = state.poll_hz if state.poll_hz in rates else rates[0]
        state.poll_hz = rates[(rates.index(cur) + d) % len(rates)]
        state.dirty[RID_POLL] = True
        state.decoded[RID_POLL] = {"hz": state.poll_hz}

    elif sec == SECTION_LIGHT:
        buf = state.raw[RID_LIGHT]
        f = state.field
        if f == 0:
            modes = sorted(LIGHT_MODES)
            cur = state.decoded[RID_LIGHT]["mode"]
            cur = cur if cur in modes else modes[0]
            buf[3] = LIGHT_MODES[modes[(modes.index(cur) + d) % len(modes)]] << 4
        elif f == 2:
            buf[5] = (buf[5] & 0xF0) | max(1, min(8, (buf[5] & 0x0F) + d))
        elif f == 3:
            buf[4] = (buf[4] & 0xF0) | max(1, min(5, (buf[4] & 0x0F) + d))
        elif f == 4:
            ms = max(4, min(50, buf[10] * 2 + d * 2))
            buf[10] = ms // 2
        elif f == 5:
            buf[9] = max(1, min(60, buf[9] + d))  # buf[9] is minutes*2, so d is already 0.5-min steps
        elif f == 6:
            deep = max(1, min(60, ((buf[4] & 0xF0) | (buf[5] >> 4)) + d))
            buf[4] = (deep & 0xF0) | (buf[4] & 0x0F)
            buf[5] = ((deep & 0x0F) << 4) | (buf[5] & 0x0F)
        else:
            return
        state.dirty[RID_LIGHT] = True
        state.decoded[RID_LIGHT] = decode_light(buf)


def _tui_edit_stage_value(stdscr, state, i):
    buf = state.raw[RID_DPI]
    cur = state.decoded[RID_DPI]["stages"][i] or 800

    def validate(s):
        try:
            v = int(s)
        except ValueError:
            raise ValueError("not a valid number")
        if not (50 <= v <= 26000):
            raise ValueError("DPI must be 50-26000")

    val = _tui_edit_line(stdscr, f"Stage {i + 1} DPI (50-26000)", str(cur), validate)
    if val is None:
        return
    v = int(val)
    x, y, is_dbl = dpi_to_bytes(v)
    buf[8 + i], buf[16 + i] = x, y
    buf[6] = (buf[6] | (1 << i)) if is_dbl else (buf[6] & ~(1 << i))
    buf[7] = buf[6]
    buf[5] |= 1 << i  # setting a value enables the stage, mirrors cmd_dpi's --stage/--value path
    state.dirty[RID_DPI] = True
    state.decoded[RID_DPI] = decode_dpi(buf)


def _tui_edit_stage_color(stdscr, state):
    i = state.field
    if i >= 8:
        return
    buf = state.raw[RID_DPI]
    r, g, b = state.decoded[RID_DPI]["colors"][i]
    val = _tui_edit_line(stdscr, f"Stage {i + 1} colour (RRGGBB)", f"{r:02x}{g:02x}{b:02x}", parse_color)
    if val is None:
        return
    buf[25 + i * 3:28 + i * 3] = bytearray(parse_color(val))
    state.dirty[RID_DPI] = True
    state.decoded[RID_DPI] = decode_dpi(buf)


def _tui_edit_light_color(stdscr, state):
    buf = state.raw[RID_LIGHT]
    r, g, b = state.decoded[RID_LIGHT]["color"]
    val = _tui_edit_line(stdscr, "Light colour (RRGGBB)", f"{r:02x}{g:02x}{b:02x}", parse_color)
    if val is None:
        return
    buf[6], buf[7], buf[8] = parse_color(val)
    state.dirty[RID_LIGHT] = True
    state.decoded[RID_LIGHT] = decode_light(buf)


def _tui_edit_light_numeric(stdscr, state, f):
    buf = state.raw[RID_LIGHT]
    specs = {
        2: ("Brightness (1-8)", state.decoded[RID_LIGHT]["brightness"], 1, 8, int),
        3: ("LED speed (1-5)", state.decoded[RID_LIGHT]["led_speed"], 1, 5, int),
        4: ("Debounce ms (4-50, even)", state.decoded[RID_LIGHT]["debounce_ms"], 4, 50, int),
        5: ("Idle sleep min (0.5-30)", state.decoded[RID_LIGHT]["sleep_min"], 0.5, 30, float),
        6: ("Deep sleep min (1-60)", state.decoded[RID_LIGHT]["deep_sleep_min"], 1, 60, int),
    }
    label, cur, lo, hi, typ = specs[f]

    def validate(s):
        try:
            v = typ(s)
        except ValueError:
            raise ValueError("not a valid number")
        if not (lo <= v <= hi):
            raise ValueError(f"must be {lo}-{hi}")
        if f == 4 and int(v) % 2:
            raise ValueError("debounce must be even")
        if f == 5 and (v * 2) % 1:
            raise ValueError("idle sleep must be a multiple of 0.5")

    val = _tui_edit_line(stdscr, label, str(cur), validate)
    if val is None:
        return
    v = typ(val)
    if f == 2:
        buf[5] = (buf[5] & 0xF0) | (int(v) & 0x0F)
    elif f == 3:
        buf[4] = (buf[4] & 0xF0) | (int(v) & 0x0F)
    elif f == 4:
        buf[10] = int(v) // 2
    elif f == 5:
        buf[9] = int(v * 2)
    elif f == 6:
        deep = int(v)
        buf[4] = (deep & 0xF0) | (buf[4] & 0x0F)
        buf[5] = ((deep & 0x0F) << 4) | (buf[5] & 0x0F)
    state.dirty[RID_LIGHT] = True
    state.decoded[RID_LIGHT] = decode_light(buf)


def _tui_edit_field(stdscr, state):
    """Enter key: open the modal editor for free-form fields. Closed-set
    fields (mode, Hz, toggles, active-stage) are Left/Right-only -- Enter
    does nothing there since there's nothing to type."""
    sec, f = state.section, state.field
    if sec == SECTION_DPI and f < 8:
        _tui_edit_stage_value(stdscr, state, f)
    elif sec == SECTION_LIGHT and f == 1:
        _tui_edit_light_color(stdscr, state)
    elif sec == SECTION_LIGHT and f in (2, 3, 4, 5, 6):
        _tui_edit_light_numeric(stdscr, state, f)


# --------------------------------------------------------- apply / refresh

def _tui_apply_dpi(state):
    buf = state.raw[RID_DPI]
    seal_dpi(buf)
    with state.dev as m:
        m.set_feature(buf[:m.report_len(RID_DPI)])
    state.dirty[RID_DPI] = False


def _tui_apply_light(state):
    buf = state.raw[RID_LIGHT]
    seal_light(buf)
    with state.dev as m:
        m.set_feature(buf[:m.report_len(RID_LIGHT)])
    state.dirty[RID_LIGHT] = False


def _tui_apply_poll(state):
    if state.poll_hz not in POLL_RATES:
        raise DeviceError("no valid polling rate selected -- use Left/Right to pick one first")
    with state.dev as m:
        buf = bytearray(m.report_len(RID_POLL))
        buf[0] = RID_POLL
        buf[1] = 0x09
        buf[2] = 0x01
        buf[3] = POLL_RATES[state.poll_hz]
        buf[4] = 0xFF - buf[3]
        m.set_feature(buf)
    state.dirty[RID_POLL] = False


_TUI_APPLY_FNS = {SECTION_DPI: _tui_apply_dpi, SECTION_POLL: _tui_apply_poll, SECTION_LIGHT: _tui_apply_light}


def _tui_apply_section(stdscr, state):
    fn = _TUI_APPLY_FNS.get(state.section)
    if fn is None:
        state.message = f"Nothing to apply in {SECTION_NAMES[state.section]}."
        state.message_kind = "info"
        return
    state.message = f"Applying {SECTION_NAMES[state.section]}..."
    state.message_kind = "info"
    _tui_draw(stdscr, state)
    try:
        fn(state)
        state.message = f"{SECTION_NAMES[state.section]} applied."
        state.message_kind = "info"
    except DeviceError as e:
        state.message = str(e)
        state.message_kind = "error"


def _tui_apply_all(stdscr, state):
    state.message = "Applying all sections..."
    state.message_kind = "info"
    _tui_draw(stdscr, state)
    for sec in (SECTION_DPI, SECTION_POLL, SECTION_LIGHT):
        try:
            _TUI_APPLY_FNS[sec](state)
        except DeviceError as e:
            state.message = f"{SECTION_NAMES[sec]}: {e}"
            state.message_kind = "error"
            return
    state.message = "All sections applied."
    state.message_kind = "info"


def _tui_do_refresh(stdscr, state, confirm_if_dirty=True):
    if confirm_if_dirty and any(state.dirty.values()):
        pending = ", ".join(SECTION_NAMES[s] for s, rid in
                             ((SECTION_DPI, RID_DPI), (SECTION_POLL, RID_POLL), (SECTION_LIGHT, RID_LIGHT))
                             if state.dirty.get(rid))
        if not _tui_confirm(stdscr, f"Unapplied edits to {pending} will be discarded. Refresh? [y/N] "):
            return
    # Redraw between each report so panels populate progressively instead of
    # all sitting on a loading placeholder until the whole sequence (DPI +
    # Light + Poll + battery, ~1-3s+) finishes. Each individual read is
    # wrapped in _tui_run_with_spinner so its panel actually animates while
    # that one call is in flight, instead of a static glyph.
    state.message = "Reading device..."
    state.message_kind = "info"
    _tui_draw(stdscr, state)
    try:
        with state.dev as m:
            state.raw[RID_DPI] = _tui_run_with_spinner(stdscr, state, lambda: m.read_config(RID_DPI))
            state.decoded[RID_DPI] = decode_dpi(state.raw[RID_DPI])
            _tui_draw(stdscr, state)

            state.raw[RID_LIGHT] = _tui_run_with_spinner(stdscr, state, lambda: m.read_config(RID_LIGHT))
            state.decoded[RID_LIGHT] = decode_light(state.raw[RID_LIGHT])
            _tui_draw(stdscr, state)

            state.raw[RID_POLL] = _tui_run_with_spinner(stdscr, state, lambda: m.read_config(RID_POLL))
            state.decoded[RID_POLL] = decode_poll(state.raw[RID_POLL])
            state.poll_hz = state.decoded[RID_POLL]["hz"]
            state.dirty = {RID_DPI: False, RID_LIGHT: False, RID_POLL: False}
            _tui_draw(stdscr, state)

            if m.mode == "dongle":
                state.message = "Reading battery..."
                state.battery_loading = True
                _tui_draw(stdscr, state)
                try:
                    state.battery = _tui_run_with_spinner(stdscr, state, lambda: read_battery(m, timeout=2.5))
                finally:
                    state.battery_loading = False
            else:
                state.battery = None
        state.message = "Refreshed."
        state.message_kind = "info"
    except DeviceError as e:
        state.message = str(e)
        state.message_kind = "error"


def _tui_poll_battery(stdscr, state):
    if state.dev.mode != "dongle":
        state.message = "Battery is only reported over the wireless dongle link."
        state.message_kind = "info"
        return
    state.message = "Waiting for a battery report... (move/click the mouse)"
    state.message_kind = "info"
    state.battery_loading = True
    _tui_draw(stdscr, state)
    try:
        with state.dev as m:
            state.battery = _tui_run_with_spinner(stdscr, state, lambda: read_battery(m, timeout=2.5))
        if state.battery:
            state.message = "Battery updated."
            state.message_kind = "info"
        else:
            state.message = "No battery report received -- try again."
            state.message_kind = "error"
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
    if any(state.dirty.values()):
        if not _tui_confirm(stdscr, "Unapplied edits will be discarded. Switch device? [y/N] "):
            return
    state.dev_idx = (state.dev_idx + 1) % len(state.devs)
    _tui_do_refresh(stdscr, state, confirm_if_dirty=False)


def _tui_rescan(stdscr, state, args):
    if any(state.dirty.values()):
        if not _tui_confirm(stdscr, "Unapplied edits will be discarded. Rescan? [y/N] "):
            return
    devs = discover()
    if getattr(args, "device", None):
        devs = [d for d in devs if d.mode == args.device]
    if not devs:
        state.message = "No Attack Shark X11 config interface found."
        state.message_kind = "error"
        return
    state.devs = devs
    state.dev_idx = 0
    _tui_do_refresh(stdscr, state, confirm_if_dirty=False)


# -------------------------------------------------------------------- draw

def _tui_draw_header(stdscr, state, y, w):
    m = state.dev
    title = f"ashark tui -- Attack Shark X11 [{m.mode}]  {m.path}"
    idx = f"(D)evice: {m.mode} [{state.dev_idx + 1}/{len(state.devs)}]"
    _safe_addstr(stdscr, y, 0, title[:w - 1], curses.A_BOLD)
    if len(title) + len(idx) + 2 < w:
        _safe_addstr(stdscr, y, w - len(idx) - 1, idx)
    y += 1
    _safe_addstr(stdscr, y, 0, "-" * min(w - 1, 100))
    return y + 1


def _tui_draw_dpi_panel(stdscr, state, y, w):
    _safe_addstr(stdscr, y, 0, "DPI", _tui_section_attr(state, SECTION_DPI))
    y += 1
    d = state.decoded.get(RID_DPI)
    if not d:
        return _tui_draw_loading(stdscr, state, y, w)
    pending = curses.color_pair(2) if state.dirty.get(RID_DPI) else 0
    for i in range(8):
        enabled = bool(d["active_mask"] & (1 << i))
        mark = " <- active" if d["current_stage"] == i + 1 else ""
        if enabled:
            r, g, b = d["colors"][i]
            line = f"Stage {i + 1}  {d['stages'][i]:>6} DPI  #{r:02x}{g:02x}{b:02x}{mark}"
        else:
            line = f"Stage {i + 1}  -- disabled --"
        attr = _tui_field_attr(state, SECTION_DPI, i) or pending
        _safe_addstr(stdscr, y, 2, line[:w - 3], attr)
        y += 1
    _safe_addstr(stdscr, y, 2, f"Active stage: {d['current_stage']}",
                 _tui_field_attr(state, SECTION_DPI, 8) or pending)
    y += 1
    _safe_addstr(stdscr, y, 2, f"Angle snap: {'on' if d['angle_snap'] else 'off'}",
                 _tui_field_attr(state, SECTION_DPI, 9) or pending)
    _safe_addstr(stdscr, y, 30, f"Ripple control: {'on' if d['ripple_control'] else 'off'}",
                 _tui_field_attr(state, SECTION_DPI, 10) or pending)
    _safe_addstr(stdscr, y, 62,
                 f"Motion sync: {'on' if d['motion_sync'] else 'off'}  LOD: {d['lod']}  (read-only)")
    y += 1
    if state.dirty.get(RID_DPI):
        _safe_addstr(stdscr, y, 2, "[pending -- press 'a' to apply]", curses.color_pair(2))
        y += 1
    return y


def _tui_draw_poll_panel(stdscr, state, y, w):
    _safe_addstr(stdscr, y, 0, "Performance", _tui_section_attr(state, SECTION_POLL))
    y += 1
    if state.poll_hz is None:
        return _tui_draw_loading(stdscr, state, y, w)
    pending = curses.color_pair(2) if state.dirty.get(RID_POLL) else 0
    _safe_addstr(stdscr, y, 2, f"Polling rate: {state.poll_hz} Hz",
                 _tui_field_attr(state, SECTION_POLL, 0) or pending)
    y += 1
    if state.dirty.get(RID_POLL):
        _safe_addstr(stdscr, y, 2, "[pending -- press 'a' to apply]", curses.color_pair(2))
        y += 1
    return y


def _tui_draw_light_panel(stdscr, state, y, w):
    _safe_addstr(stdscr, y, 0, "Lighting", _tui_section_attr(state, SECTION_LIGHT))
    y += 1
    d = state.decoded.get(RID_LIGHT)
    if not d:
        return _tui_draw_loading(stdscr, state, y, w)
    pending = curses.color_pair(2) if state.dirty.get(RID_LIGHT) else 0
    fields = [
        f"Mode: {d['mode']}",
        f"Colour: #{d['color'][0]:02x}{d['color'][1]:02x}{d['color'][2]:02x}",
        f"Brightness: {d['brightness']}/8",
        f"Speed: {d['led_speed']}/5",
        f"Debounce: {d['debounce_ms']} ms",
        f"Idle sleep: {d['sleep_min']} min",
        f"Deep sleep: {d['deep_sleep_min']} min",
    ]
    for i, text in enumerate(fields):
        _safe_addstr(stdscr, y, 2, text[:w - 3], _tui_field_attr(state, SECTION_LIGHT, i) or pending)
        y += 1
    if state.dirty.get(RID_LIGHT):
        _safe_addstr(stdscr, y, 2, "[pending -- press 'a' to apply]", curses.color_pair(2))
        y += 1
    return y


def _tui_draw_battery_panel(stdscr, state, y, w):
    _safe_addstr(stdscr, y, 0, "Battery", _tui_section_attr(state, SECTION_BATTERY))
    y += 1
    attr = _tui_field_attr(state, SECTION_BATTERY, 0)
    if state.battery_loading:
        return _tui_draw_loading(stdscr, state, y, w)
    if state.dev.mode != "dongle":
        _safe_addstr(stdscr, y, 2, "-- wired mode: not applicable --", attr)
    elif state.battery:
        pct, st = state.battery
        _safe_addstr(stdscr, y, 2, f"{pct}%  ({st})   press 'b' to poll again", attr)
    else:
        _safe_addstr(stdscr, y, 2, "unknown -- press 'b' to poll", attr)
    return y + 1


def _tui_draw_footer(stdscr, state, h, w):
    hint1 = "Tab:section  Up/Dn:field  Left/Right:change  Enter:edit  c:colour(DPI)"
    hint2 = "a:apply  A:apply-all  r:refresh  b:battery  D:device  R:rescan  q:quit"
    _safe_addstr(stdscr, h - 3, 0, hint1[:w - 1], curses.A_DIM)
    _safe_addstr(stdscr, h - 2, 0, hint2[:w - 1], curses.A_DIM)
    attr = curses.color_pair(1) if state.message_kind == "error" else curses.color_pair(4)
    _safe_addstr(stdscr, h - 1, 0, state.message[:w - 1], attr)


def _tui_draw(stdscr, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    y = _tui_draw_header(stdscr, state, 0, w) + 1
    y = _tui_draw_dpi_panel(stdscr, state, y, w) + 1
    y = _tui_draw_poll_panel(stdscr, state, y, w) + 1
    y = _tui_draw_light_panel(stdscr, state, y, w) + 1
    _tui_draw_battery_panel(stdscr, state, y, w)
    _tui_draw_footer(stdscr, state, h, w)
    stdscr.refresh()


# --------------------------------------------------------------- key loop

def _tui_handle_key(stdscr, state, ch, args):
    if ch in (ord("q"), 27):
        if any(state.dirty.values()):
            if not _tui_confirm(stdscr, "Unapplied edits will be lost. Quit? [y/N] "):
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
    elif ch == ord("c") and state.section == SECTION_DPI and state.field < 8:
        _tui_edit_stage_color(stdscr, state)
    elif ch == ord("a"):
        _tui_apply_section(stdscr, state)
    elif ch == ord("A"):
        _tui_apply_all(stdscr, state)
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
    devs = pick(args)  # dies here (pre-screen) if nothing's found -- safe, nothing to restore yet
    curses.wrapper(_tui_main, devs, args)


UDEV_RULE = (
    'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1d57", '
    'ATTRS{idProduct}=="fa55", TAG+="uaccess"\n'
    'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1d57", '
    'ATTRS{idProduct}=="fa60", TAG+="uaccess"\n'
)


def cmd_install_udev(args):
    path = os.path.expanduser("~/.config/attackshark/99-attackshark.rules")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(UDEV_RULE)
    print("Run these two commands to grant your user access to the mouse:\n")
    print(f"  sudo install -m 644 {path} /etc/udev/rules.d/99-attackshark.rules")
    print("  sudo udevadm control --reload-rules && sudo udevadm trigger")
    print("\nThen unplug/replug the mouse (or dongle).")


def main():
    p = argparse.ArgumentParser(
        prog="ashark", description="Configure the Attack Shark X11 mouse.")
    p.add_argument("--device", choices=["wired", "dongle"],
                   help="target only one connection (default: all present)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show current settings").set_defaults(fn=cmd_status)
    sub.add_parser("dump", help="raw config reports (debugging)").set_defaults(fn=cmd_dump)
    sub.add_parser("save", help="save current settings to a JSON file").set_defaults(fn=cmd_save)
    sub.add_parser("install-udev", help="print udev setup commands").set_defaults(fn=cmd_install_udev)
    sub.add_parser("tui", help="interactive terminal UI").set_defaults(fn=cmd_tui)

    bat = sub.add_parser("battery", help="show wireless battery level (dongle only)")
    bat.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for a report (default 5)")
    bat.set_defaults(fn=cmd_battery)

    d = sub.add_parser("dpi", help="set DPI stages")
    d.add_argument("--stages", help="comma-separated list, e.g. 800,1600,3200")
    d.add_argument("--stage", type=int, choices=range(1, 9), help="stage to modify")
    d.add_argument("--value", type=int, help="DPI value for --stage")
    d.add_argument("--active", type=int, choices=range(1, 9), help="stage to switch to")
    d.add_argument("--color", help="RRGGBB colour for --stage")
    d.add_argument("--angle-snap", type=lambda s: s == "on", choices=[True, False],
                   metavar="on|off", help="straight-line correction")
    d.add_argument("--ripple", type=lambda s: s == "on", choices=[True, False],
                   metavar="on|off", help="ripple control (jitter smoothing)")
    d.set_defaults(fn=cmd_dpi)

    r = sub.add_parser("polling", help="set polling rate")
    r.add_argument("hz", type=int, help="125, 250, 500 or 1000")
    r.set_defaults(fn=cmd_polling)

    l = sub.add_parser("light", help="set LED mode and colour")
    l.add_argument("--mode", choices=sorted(LIGHT_MODES))
    l.add_argument("--color", help="RRGGBB, e.g. ff8800")
    l.add_argument("--brightness", type=int, choices=range(1, 9))
    l.add_argument("--speed", type=int, choices=range(1, 6))
    l.set_defaults(fn=cmd_light)

    b = sub.add_parser("debounce", help="set click debounce time")
    b.add_argument("ms", type=int, help="4-50 ms, even numbers")
    b.set_defaults(fn=cmd_debounce)

    s = sub.add_parser("sleep", help="set idle and deep-sleep timers")
    s.add_argument("--idle", type=float, help="minutes, 0.5-30")
    s.add_argument("--deep", type=int, help="minutes, 1-60")
    s.set_defaults(fn=cmd_sleep)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
