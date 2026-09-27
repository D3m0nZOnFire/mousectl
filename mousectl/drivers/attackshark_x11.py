"""Attack Shark X11 (PAW3311).

Talks the vendor HID feature-report protocol directly over /dev/hidraw*.
Protocol reference: github.com/HarukaYamamoto0/attack-shark-x11-driver (docs/)

Everywhere possible: read the device's current config report, modify only the
requested fields, recompute the checksum, write it back. Bytes whose meaning
isn't documented are preserved untouched rather than guessed at.
"""

import array
import fcntl
import os
import select
import time

from ..core.driver import DeviceError, Driver, Session
from ..core.hidraw import HIDIOCGFEATURE, HIDIOCSFEATURE, parse_feature_lengths
from ..core.settings import Bool, Choice, Color, Number, Setting

VENDOR = 0x1D57

RID_DPI = 0x04
RID_LIGHT = 0x05
RID_POLL = 0x06
RID_READ = 0xA0

POLL_RATES = {125: 0x08, 250: 0x04, 500: 0x02, 1000: 0x01}
POLL_NAMES = {v: k for k, v in POLL_RATES.items()}

LIGHT_MODES = {
    "off": 0x0, "static": 0x1, "breathing": 0x2, "neon": 0x3,
    "colorbreathing": 0x4, "staticdpi": 0x5, "breathingdpi": 0x6,
}
LIGHT_MODE_NAMES = {v: k for k, v in LIGHT_MODES.items()}

STAGES = 8

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

# The values the codec below round-trips exactly: 50-DPI steps are encoded
# directly; above 10000 the sensor doubles a halved value (100-DPI steps).
# Above 20000 an odd-hundred value would land in the table's duplicated
# tail (indices 200+ repeat earlier bytes) and read back as a different
# DPI, so only 200-DPI steps are offered there.
DPI_KIND = Number(50, 26000, steps=((10000, 50), (20000, 100), (26000, 200)), unit=" DPI")


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


def seal_dpi(buf):
    c = sum(buf[3:50]) & 0xFFFF
    buf[50] = (c >> 8) & 0xFF
    buf[51] = c & 0xFF
    return buf


def seal_light(buf):
    c = sum(buf[3:11]) & 0xFFFF
    buf[11] = (c >> 8) & 0xFF
    buf[12] = c & 0xFF
    return buf


# ------------------------------------------------------------------ session

class X11Session(Session):
    """One config interface of the mouse (the vendor collection with report 0x04)."""

    def __init__(self, link):
        super().__init__(link)
        self.lengths = link.info["lengths"]
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.link.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(f"no permission to open {self.link.path} -- run 'mousectl install-udev'")
        except OSError as e:
            raise DeviceError(f"cannot open {self.link.path}: {e}")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def report_len(self, rid):
        """Total transfer length for a report: 1 report-ID byte + payload."""
        if rid not in self.lengths:
            raise DeviceError(f"device does not expose feature report 0x{rid:02x}")
        return self.lengths[rid] + 1

    def set_feature(self, payload):
        buf = array.array("B", payload)
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as e:
            raise DeviceError(f"write failed: {e}")

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
            except (OSError, DeviceError) as e:
                last_err = str(e)
            time.sleep(0.3 * (attempt + 1))
        raise DeviceError(
            f"firmware refused read access to report 0x{rid:02x} "
            f"({last_err}) after {retries + 1} tries -- "
            f"this link (wired cable / wireless dongle) may not have a live "
            f"connection to the mouse right now.")

    def read(self, units=None):
        return {rid: self.read_config(rid) for rid in (units or X11.units)}

    def write(self, before, after):
        written = []
        for rid in X11.units:
            if rid not in after or bytes(before.get(rid, b"")) == bytes(after[rid]):
                continue
            buf = after[rid]
            if rid == RID_DPI:
                self.set_feature(seal_dpi(buf)[:self.report_len(rid)])
            elif rid == RID_LIGHT:
                self.set_feature(seal_light(buf)[:self.report_len(rid)])
            else:
                # The poll report is write-only in practice: always send a
                # fresh frame, like the vendor software.
                frame = bytearray(self.report_len(RID_POLL))
                frame[0:5] = bytes([RID_POLL, 0x09, 0x01, buf[3], 0xFF - buf[3]])
                self.set_feature(frame)
            before[rid] = bytearray(buf)
            written.append(rid)
        return written

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

    def battery(self, timeout=None):
        """Wait for a battery event. Only the dongle link reports one (wired
        mode is externally powered, the firmware never reports a level)."""
        if self.mode != "dongle":
            return None
        for ev in self.drain_events(2.5 if timeout is None else timeout):
            if ev[2] in (0x40, 0x41):
                state = {1: "discharging", 2: "fully charged",
                         3: "charging/wired"}.get(ev[3], f"0x{ev[3]:02x}")
                return ev[4], state
        return None


# ------------------------------------------------------------------- schema

def _stage_get(i):
    def get(raw, mode):
        b = raw[RID_DPI]
        return bytes_to_dpi(b[8 + i], b[16 + i], bool(b[6] & (1 << i)))
    return get


def _stage_set(i):
    def set_(raw, mode, v):
        b = raw[RID_DPI]
        x, y, dbl = dpi_to_bytes(v)
        b[8 + i], b[16 + i] = x, y
        b[6] = (b[6] | (1 << i)) if dbl else (b[6] & ~(1 << i) & 0xFF)
        b[7] = b[6]
    return set_


def _stage_enabled(i):
    return lambda raw, mode: bool(raw[RID_DPI][5] & (1 << i))


def _stage_set_enabled(i):
    def set_enabled(raw, mode, on):
        b = raw[RID_DPI]
        if on:
            b[5] |= 1 << i
            if b[8 + i] == 0 and b[16 + i] == 0:
                # Never had a value: give it a sane default, not 0 DPI.
                _stage_set(i)(raw, mode, 800)
            return
        remaining = [j + 1 for j in range(STAGES) if b[5] & (1 << j) and j != i]
        if not remaining:
            raise ValueError("at least one DPI stage must stay enabled")
        b[5] &= ~(1 << i) & 0xFF
        if b[24] == i + 1:
            b[24] = remaining[0]
    return set_enabled


def _stage_suffix(i):
    def suffix(raw, mode):
        return "  <- active" if raw[RID_DPI][24] == i + 1 else ""
    return suffix


def _color_get(i):
    return lambda raw, mode: tuple(raw[RID_DPI][25 + i * 3:28 + i * 3])


def _color_set(i):
    def set_(raw, mode, rgb):
        raw[RID_DPI][25 + i * 3:28 + i * 3] = bytes(rgb)
    return set_


def _active_set(raw, mode, v):
    if not raw[RID_DPI][5] & (1 << (v - 1)):
        raise ValueError(f"stage {v} is not enabled")
    raw[RID_DPI][24] = v


def _nibble(rid, idx, high):
    def get(raw, mode):
        b = raw[rid][idx]
        return b >> 4 if high else b & 0x0F

    def set_(raw, mode, v):
        b = raw[rid]
        b[idx] = ((v & 0x0F) << 4 | (b[idx] & 0x0F)) if high else ((b[idx] & 0xF0) | (v & 0x0F))
    return get, set_


def _flag(rid, idx):
    get, set_ = _nibble(rid, idx, False)
    return (lambda raw, mode: bool(get(raw, mode)),
            lambda raw, mode, v: set_(raw, mode, 1 if v else 0))


def _poll_set(raw, mode, hz):
    raw[RID_POLL][3] = POLL_RATES[hz]
    raw[RID_POLL][4] = 0xFF - POLL_RATES[hz]


def _light_mode_set(raw, mode, name):
    raw[RID_LIGHT][3] = LIGHT_MODES[name] << 4


def _deep_get(raw, mode):
    b = raw[RID_LIGHT]
    return (b[4] & 0xF0) | (b[5] >> 4)


def _deep_set(raw, mode, v):
    b = raw[RID_LIGHT]
    b[4] = (v & 0xF0) | (b[4] & 0x0F)
    b[5] = ((v & 0x0F) << 4) | (b[5] & 0x0F)


def _light_color_set(raw, mode, rgb):
    raw[RID_LIGHT][6:9] = bytes(rgb)


def _debounce_set(raw, mode, ms):
    raw[RID_LIGHT][10] = ms // 2


def _sleep_set(raw, mode, minutes):
    raw[RID_LIGHT][9] = int(minutes * 2)


def _build_settings():
    s = []
    for i in range(STAGES):
        s.append(Setting(
            f"dpi.stage{i + 1}", f"Stage {i + 1}", "DPI", DPI_KIND, (RID_DPI,),
            _stage_get(i), _stage_set(i),
            get_enabled=_stage_enabled(i), set_enabled=_stage_set_enabled(i),
            suffix=_stage_suffix(i), companion=f"dpi.stage{i + 1}.color"))
        s.append(Setting(
            f"dpi.stage{i + 1}.color", f"Stage {i + 1} colour", "DPI", Color(), (RID_DPI,),
            _color_get(i), _color_set(i), hidden=True))
    snap_get, snap_set = _flag(RID_DPI, 3)
    ripple_get, ripple_set = _flag(RID_DPI, 4)
    lod_get, _ = _nibble(RID_DPI, 3, True)
    ms_get, _ = _nibble(RID_DPI, 4, True)
    speed_get, speed_set = _nibble(RID_LIGHT, 4, False)
    bright_get, bright_set = _nibble(RID_LIGHT, 5, False)
    s += [
        Setting("dpi.active", "Active stage", "DPI", Choice(range(1, STAGES + 1)), (RID_DPI,),
                lambda raw, mode: raw[RID_DPI][24], _active_set),
        Setting("polling", "Polling rate", "Performance", Choice(sorted(POLL_RATES), " Hz"), (RID_POLL,),
                lambda raw, mode: POLL_NAMES.get(raw[RID_POLL][3], f"unknown(0x{raw[RID_POLL][3]:02x})"),
                _poll_set),
        Setting("angle_snap", "Angle snap", "Performance", Bool(), (RID_DPI,), snap_get, snap_set,
                help="straight-line correction"),
        Setting("ripple", "Ripple control", "Performance", Bool(), (RID_DPI,), ripple_get, ripple_set,
                help="jitter smoothing"),
        Setting("motion_sync", "Motion sync", "Performance", Bool(), (RID_DPI,),
                lambda raw, mode: bool(ms_get(raw, mode))),
        Setting("lod", "Lift-off distance", "Performance", Number(0, 15), (RID_DPI,), lod_get),
        Setting("light.mode", "Mode", "Lighting", Choice(LIGHT_MODES), (RID_LIGHT,),
                lambda raw, mode: LIGHT_MODE_NAMES.get(raw[RID_LIGHT][3] >> 4, f"0x{raw[RID_LIGHT][3] >> 4:x}"),
                _light_mode_set),
        Setting("light.color", "Colour", "Lighting", Color(), (RID_LIGHT,),
                lambda raw, mode: tuple(raw[RID_LIGHT][6:9]), _light_color_set),
        Setting("light.brightness", "Brightness", "Lighting", Number(1, 8, unit="/8"), (RID_LIGHT,),
                bright_get, bright_set),
        Setting("light.speed", "Speed", "Lighting", Number(1, 5, unit="/5"), (RID_LIGHT,),
                speed_get, speed_set),
        Setting("debounce", "Debounce", "Timing", Number(4, 50, 2, unit=" ms"), (RID_LIGHT,),
                lambda raw, mode: raw[RID_LIGHT][10] * 2, _debounce_set),
        Setting("sleep", "Idle sleep", "Timing", Number(0.5, 30, 0.5, unit=" min"), (RID_LIGHT,),
                lambda raw, mode: raw[RID_LIGHT][9] / 2, _sleep_set),
        Setting("sleep.deep", "Deep sleep", "Timing", Number(1, 60, unit=" min"), (RID_LIGHT,),
                _deep_get, _deep_set),
    ]
    return s


class X11(Driver):
    id = "attackshark-x11"
    name = "Attack Shark X11"
    aliases = ("ashark",)
    links = {(VENDOR, 0xFA55): "wired", (VENDOR, 0xFA60): "dongle"}
    # Wired first: it's the deterministic, always-live link. The dongle
    # only answers config reads while a mouse is actually paired over it,
    # so trying it first can surface a spurious failure when the cable is
    # also plugged in.
    link_order = ("wired", "dongle")
    groups = ["DPI", "Performance", "Lighting", "Timing"]
    units = (RID_DPI, RID_LIGHT, RID_POLL)
    settings = _build_settings()

    def is_config_interface(self, node):
        # The config interface is the one exposing the DPI + read reports.
        lengths = parse_feature_lengths(node.descriptor)
        return RID_DPI in lengths and RID_READ in lengths

    def link_info(self, node):
        return {"lengths": parse_feature_lengths(node.descriptor)}

    def session(self, link):
        return X11Session(link)


DRIVER = X11()
