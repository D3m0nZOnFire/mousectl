"""Pulsar X3 (PX3R21, PAW3395, Nordic).

Talks Pulsar Fusion's 64-byte vendor protocol directly over /dev/hidraw*:
one feature report (no report ID) on USB interface 3, sent with SET_FEATURE
and answered through GET_FEATURE. Protocol references:
github.com/wertyg775/pulsar-x3-software (docs/protocol.md, decompiled Fusion),
github.com/jonkristian/pulsar-x3-python (X3 captures) and
github.com/packerlschupfer/pulsar-mouse-linux (drivers/feinmann8k.py, the
same framing on another Pulsar 8K model).

Frame (device bytes, after the hidraw report-number byte):
  [0] 0 on requests; 1 = answer ready, 2 = busy on replies
  [1] command group  [2] operation (bit 7 = read)  [3] payload length
  [6] profile (0 = "the active one" for global settings)  [7..61] payload
  [62..63] u16le sum of bytes 0..61

Each unit is one read command's answer, stored as exactly the payload its
write command takes, so a write is "send the unit back".
"""

import array
import fcntl
import os
import time

from ..core.driver import DeviceError, Driver, Session
from ..core.hidraw import HIDIOCGFEATURE, HIDIOCSFEATURE
from ..core.settings import Bool, Choice, Number, Setting

VENDOR = 0x3710
WIRED_PIDS = (0x3409, 0x3410)
CONFIG_INTERFACE = 3
FRAME = 64

STATUS_OK = 0x01
STATUS_BUSY = 0x02

STAGES = 6
STAGE_LEN = 5                  # [index, x lo, x hi, y lo, y hi]
DPI_LEN = 2 + STAGES * STAGE_LEN

# name: (group, op, read length, write length, per-profile, read payload,
#        write prefix, reply offset, stored length)
# `write prefix` goes before the stored bytes on writes (LOD's constant 02).
UNITS = {
    "dpi":         (0x05, 0x04, 0x15, 0x21, True,  b"",     b"",     7, DPI_LEN),
    "lod":         (0x07, 0x02, 0x03, 0x03, True,  b"",     b"\x02", 8, 1),
    "polling":     (0x01, 0x09, 0x02, 0x02, False, b"",     b"",     7, 1),
    "angle_snap":  (0x07, 0x04, 0x02, 0x02, False, b"",     b"",     7, 1),
    "ripple":      (0x07, 0x03, 0x02, 0x02, False, b"",     b"",     7, 1),
    "motion_sync": (0x07, 0x05, 0x02, 0x02, False, b"",     b"",     7, 1),
    "debounce":    (0x04, 0x03, 0x03, 0x03, False, b"",     b"",     7, 1),
    "sleep":       (0x08, 0x05, 0x03, 0x03, False, b"\x01", b"",     7, 2),
}

POLL_RATES = {125: 0x01, 250: 0x02, 500: 0x04, 1000: 0x08, 2000: 0x10, 4000: 0x20, 8000: 0x40}
POLL_NAMES = {v: k for k, v in POLL_RATES.items()}


def frame(group, op, length, profile=0, payload=b""):
    f = bytearray(FRAME)
    f[1], f[2], f[3], f[6] = group, op, length, profile
    f[7:7 + len(payload)] = payload
    f[62:64] = (sum(f[:62]) & 0xFFFF).to_bytes(2, "little")
    return f


class X3Session(Session):
    """One X3 config interface (cable or dongle)."""

    POLL_S = 0.02
    TIMEOUT_S = 1.5
    SETTLE_S = 0.3        # the dongle holds one in-flight command; a write
                          # followed too fast by anything else gets dropped

    def __init__(self, link):
        super().__init__(link)
        self.fd = None
        self._profile = None

    def __enter__(self):
        try:
            self.fd = os.open(self.link.path, os.O_RDWR)
        except PermissionError:
            raise DeviceError(f"no permission to open {self.link.path} -- run 'mousectl install-udev'")
        except OSError as e:
            raise DeviceError(f"cannot open {self.link.path}: {e}")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    # ------------------------------------------------------------ transport

    def _set(self, f):
        buf = array.array("B", b"\0" + bytes(f))      # report number 0 + frame
        try:
            fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)
        except OSError as e:
            raise DeviceError(f"write failed: {e}")

    def _get(self):
        buf = array.array("B", bytes(FRAME + 1))
        try:
            fcntl.ioctl(self.fd, HIDIOCGFEATURE(len(buf)), buf, True)
        except OSError as e:
            raise DeviceError(f"read failed: {e}")
        return bytes(buf[1:])

    def query(self, group, op, length, profile=0, payload=b""):
        """Send a read command and poll until its own answer is ready.

        The answer needs a radio round trip to the mouse (~15-250ms) and is
        busy until then. The reply register is shared, so an answer only
        counts when it echoes our group/op (and profile, when one was given).
        """
        self._set(frame(group, op | 0x80, length, profile, payload))
        time.sleep(0.05)
        deadline = time.time() + self.TIMEOUT_S
        while time.time() < deadline:
            r = self._get()
            if (r[0] == STATUS_OK and r[1] == group and r[2] == op | 0x80
                    and (not profile or r[6] == profile)):
                return r
            time.sleep(self.POLL_S)
        raise DeviceError(f"no answer for {group:02x}/{op:02x} "
                          f"(mouse asleep or out of range? move it and retry)")

    def command(self, group, op, length, profile=0, payload=b""):
        self._set(frame(group, op, length, profile, payload))
        self._get()                 # the dongle only acts once it's polled
        time.sleep(self.SETTLE_S)

    # --------------------------------------------------------------- units

    def profile(self):
        if self._profile is None:
            r = self.query(0x02, 0x06, 0x01)
            self._profile = r[7] or r[6]
            if not 1 <= self._profile <= 6:
                raise DeviceError(f"odd active profile {self._profile}")
        return self._profile

    def read_unit(self, name):
        group, op, rlen, _, per_profile, rpay, _, off, n = UNITS[name]
        r = self.query(group, op, rlen, self.profile() if per_profile else 0, rpay)
        return bytearray(r[off:off + n])

    def write_unit(self, name, data):
        group, op, _, wlen, per_profile, _, prefix, _, n = UNITS[name]
        payload = prefix + bytes(data)
        if name == "dpi":
            payload = payload[:2 + data[1] * STAGE_LEN]   # only the live stages
        self.command(group, op, wlen, self.profile() if per_profile else 0, payload)

    def read(self, units=None):
        return {u: self.read_unit(u) for u in (units or UNITS)}

    def write(self, before, after):
        written = []
        for u in UNITS:
            if u not in after or bytes(before.get(u, b"")) == bytes(after[u]):
                continue
            for attempt in range(3):
                self.write_unit(u, after[u])
                back = self.read_unit(u)
                if _same(u, back, after[u]):
                    break
                time.sleep(0.5)
            else:
                raise DeviceError(f"verify failed for {u}: wrote {bytes(after[u]).hex()} "
                                  f"read back {bytes(back).hex()}")
            before[u] = bytearray(after[u])
            written.append(u)
        return written

    def battery(self, timeout=None):
        pct = self.query(0x08, 0x01, 0x01)[6]
        charging = self.query(0x08, 0x03, 0x01)[6]
        return min(pct, 100), ("charging" if charging else "discharging")


def _same(unit, a, b):
    if unit == "dpi":     # slots past the stage count aren't stored
        n = 2 + b[1] * STAGE_LEN
        return bytes(a[:n]) == bytes(b[:n])
    return bytes(a) == bytes(b)


# ------------------------------------------------------------------- schema

def _xy(raw, i):
    o = 2 + i * STAGE_LEN
    b = raw["dpi"]
    return int.from_bytes(b[o + 1:o + 3], "little"), int.from_bytes(b[o + 3:o + 5], "little")


def _stage_get(i):
    return lambda raw, mode: _xy(raw, i)[0]


def _stage_set(i):
    def set_(raw, mode, dpi):
        o = 2 + i * STAGE_LEN
        v = int(dpi).to_bytes(2, "little")
        raw["dpi"][o:o + STAGE_LEN] = bytes([i + 1]) + v + v
    return set_


def _stage_enabled(i):
    return lambda raw, mode: i < raw["dpi"][1]


def _stage_set_enabled(i):
    """Stages are a list: count N means stages 1..N. Enabling a later stage
    enables the ones before it; only the last stage can be switched off."""
    def set_enabled(raw, mode, on):
        b = raw["dpi"]
        count = b[1]
        if on:
            for j in range(count, i + 1):
                if _xy(raw, j)[0] == 0:
                    _stage_set(j)(raw, mode, 800)
                else:
                    b[2 + j * STAGE_LEN] = j + 1
            b[1] = max(count, i + 1)
            return
        if i != count - 1:
            raise ValueError(f"only the last stage (stage {count}) can be turned off")
        if count == 1:
            raise ValueError("at least one DPI stage must stay enabled")
        b[1] = count - 1
        if b[0] > b[1]:
            b[0] = b[1]
    return set_enabled


def _stage_suffix(i):
    def suffix(raw, mode):
        x, y = _xy(raw, i)
        return (f" (Y {y})" if y != x else "") + ("  <- active" if raw["dpi"][0] == i + 1 else "")
    return suffix


def _active_set(raw, mode, v):
    if v > raw["dpi"][1]:
        raise ValueError(f"stage {v} is not enabled")
    raw["dpi"][0] = v


def _byte(unit, conv_get=lambda v: v, conv_set=lambda v: v):
    def set_(raw, mode, v):
        raw[unit][0] = conv_set(v)
    return (lambda raw, mode: conv_get(raw[unit][0])), set_


def _bool(unit):
    return _byte(unit, bool, lambda v: 1 if v else 0)


def _sleep_get(raw, mode):
    s = int.from_bytes(raw["sleep"][0:2], "little")
    return s // 60 if s % 60 == 0 else s / 60


def _sleep_set(raw, mode, minutes):
    raw["sleep"][0:2] = int(round(minutes * 60)).to_bytes(2, "little")


def _build_settings():
    dpi = Number(50, 26000, 10, unit=" DPI")
    s = [Setting(f"dpi.stage{i + 1}", f"Stage {i + 1}", "DPI", dpi, ("dpi",),
                 _stage_get(i), _stage_set(i),
                 get_enabled=_stage_enabled(i), set_enabled=_stage_set_enabled(i),
                 suffix=_stage_suffix(i))
         for i in range(STAGES)]
    poll_get, poll_set = _byte("polling", lambda b: POLL_NAMES.get(b, f"unknown(0x{b:02x})"),
                               POLL_RATES.__getitem__)
    lod_get, lod_set = _byte("lod", lambda b: b / 10 if b % 10 else b // 10, lambda mm: round(mm * 10))
    deb_get, deb_set = _byte("debounce")
    s += [
        Setting("dpi.active", "Active stage", "DPI", Choice(range(1, STAGES + 1)), ("dpi",),
                lambda raw, mode: raw["dpi"][0], _active_set),
        Setting("polling", "Polling rate", "Performance", Choice(sorted(POLL_RATES), " Hz"), ("polling",),
                poll_get, poll_set, help="2000 Hz and up need the 8K dongle"),
        Setting("motion_sync", "Motion sync", "Performance", Bool(), ("motion_sync",), *_bool("motion_sync")),
        Setting("angle_snap", "Angle snap", "Performance", Bool(), ("angle_snap",), *_bool("angle_snap"),
                help="straight-line correction"),
        Setting("ripple", "Ripple control", "Performance", Bool(), ("ripple",), *_bool("ripple"),
                help="jitter smoothing"),
        Setting("lod", "Lift-off distance", "Performance", Choice([0.7, 1, 2], " mm"), ("lod",),
                lod_get, lod_set),
        Setting("debounce", "Debounce", "Timing", Number(0, 15, unit=" ms"), ("debounce",),
                deb_get, deb_set),
        Setting("sleep", "Idle sleep", "Timing", Number(0.5, 15, 0.5, unit=" min"), ("sleep",),
                _sleep_get, _sleep_set),
    ]
    return s


class X3(Driver):
    id = "pulsar-x3"
    name = "Pulsar X3"
    aliases = ("x3",)
    links = {**{(VENDOR, pid): "wired" for pid in WIRED_PIDS},
             (VENDOR, 0x5402): "dongle", (VENDOR, 0x5403): "dongle"}
    link_order = ("wired", "dongle")
    groups = ["DPI", "Performance", "Timing"]
    units = tuple(UNITS)
    settings = _build_settings()

    def is_config_interface(self, node):
        # Interfaces 2 and 3 carry identical vendor descriptors; Fusion
        # talks to interface 3 (MI03).
        if b"\x06\xff\xff\x09\x01" not in node.descriptor:
            return False
        dev = os.path.realpath(f"/sys/class/hidraw/{os.path.basename(node.path)}/device/..")
        try:
            with open(os.path.join(dev, "bInterfaceNumber")) as f:
                return int(f.read().strip(), 16) == CONFIG_INTERFACE
        except (OSError, ValueError):
            return False

    def unit_name(self, unit):
        return unit

    def parse_unit(self, name):
        return name

    def session(self, link):
        return X3Session(link)


DRIVER = X3()
