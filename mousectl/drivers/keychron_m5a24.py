"""KeyChron M5 8K (M5-A24, PAW3950).

Keychron's "8k" mouse protocol, as spoken by the Keychron Launcher web app
(launcher.keychron.com, WebHID). The config interface is the vendor collection
on usage page 0xFFC1 (USB interface 4 on both cable and Ultra-Link 8K dongle);
the dongle forwards everything to the mouse over the air.

Reports on that interface (report ID first, then the device bytes):
  0xB3 out, 63 bytes  -> answered by 0xB4 in, 63 bytes   (long commands)
  0xB5 out, 20 bytes  -> answered by 0xB6 in, 20 bytes   (short commands)
Writes are acknowledged with [0xE4, status, command] (status 0 = done).

Command 0x06 (on 0xB3) returns the whole config block:
  [1] profile  [2..4] per-slot level byte: DPI stage (low nibble) and
  polling level (high nibble) for slot 0 = cable, 1 = 2.4 GHz, 2 = spare
  [5..14] five u16le DPI stages  [15] bit0-1 LOD index, bit2 ripple,
  bit3 angle snap, bit4 motion sync, bit6 reversed scroll
  [16] stage count  [17] debounce ms  [18] sleep minutes
  [19] battery % (bit 7 = charging)  [52] bit0 20K fps mode
The mouse keeps one value for every slot: a level write uses the byte of
the slot it is connected on and copies it to the others.

Each unit is stored as exactly the payload of its write command (minus
constant parts), so a write is "send the unit back".
"""

import os
import select
import time

from ..core.driver import DeviceError, Driver, Session
from ..core.settings import Bool, Choice, Number, Setting

VENDOR = 0x3434
WIRED_PID = 0xd048                 # cable (confirmed: Keychron M5 8K)
DONGLE_PID = 0xd028                # Keychron Ultra-Link 8K receiver
DESCRIPTOR = b"\x06\xc1\xff\x09\x01"   # usage page 0xFFC1, usage 1

REPORT_LONG, REPLY_LONG, LONG_LEN = 0xB3, 0xB4, 63
REPORT_SHORT, REPLY_SHORT, SHORT_LEN = 0xB5, 0xB6, 20
ACK = 0xE4

CMD_CONFIG = 0x06
CMD_DPI = 0x40
CMD_POLLING = 0x41
CMD_SENSOR = 0x42
CMD_DEBOUNCE = 0x43
CMD_SLEEP = 0x0A

STAGES = 5
SLOT = {"wired": 0, "dongle": 1}

# Polling level index -> Hz (Launcher product JSON for 3434:d048).
POLL_RATES = [125, 500, 1000, 2000, 4000, 8000]
# LOD index -> mm (same JSON: index 3 is 0.7 mm).
LOD_MM = {3: 0.7, 1: 1, 2: 2}
LOD_INDEX = {v: k for k, v in LOD_MM.items()}

# Sensor unit = command 0x42 payload: [lod, ripple, angle, motion, 0,
# scroll, 0, fps20k]. Ripple, angle and motion are 1 = on / 2 = off
# (0 = leave as is); scroll and fps20k are the Launcher's "value + 1":
# scroll 1 = normal / 2 = reversed, fps20k 1 = off / 2 = on.
S_LOD, S_RIPPLE, S_ANGLE, S_MOTION, S_SCROLL, S_FPS = 0, 1, 2, 3, 5, 7
SENSOR_LEN = 8

UNITS = ("dpi", "polling", "sensor", "debounce", "sleep")
UNIT_LEN = {"dpi": 12, "polling": 1, "sensor": SENSOR_LEN, "debounce": 1, "sleep": 1}
DPI_RANGE = (50, 30000)
DEBOUNCE_MAX = 20
SLEEP_RANGE = (1, 240)


def _flag(on):
    return 1 if on else 2


def decode_config(cfg, mode):
    """Split a command 0x06 answer (device bytes, cfg[0] == 6) into units."""
    if len(cfg) < LONG_LEN or cfg[0] != CMD_CONFIG or not 1 <= cfg[16] <= STAGES:
        raise DeviceError(f"odd config answer {bytes(cfg).hex()}")
    slot = cfg[2 + SLOT.get(mode, 1)]
    f = cfg[15]
    sensor = bytearray(SENSOR_LEN)
    sensor[S_LOD] = f & 3
    sensor[S_RIPPLE] = _flag(f >> 2 & 1)
    sensor[S_ANGLE] = _flag(f >> 3 & 1)
    sensor[S_MOTION] = _flag(f >> 4 & 1)
    sensor[S_SCROLL] = (f >> 6 & 1) + 1
    sensor[S_FPS] = (cfg[52] & 1) + 1
    return {
        "dpi": bytearray([slot & 0x0F]) + bytearray(cfg[5:15]) + bytearray([cfg[16]]),
        "polling": bytearray([slot >> 4]),
        "sensor": sensor,
        "debounce": bytearray([cfg[17]]),
        "sleep": bytearray([cfg[18]]),
    }


def _fields(unit, data):
    """(label, value, valid) for every field of a unit."""
    if unit == "dpi":
        count = data[11]
        f = [("stage count", count, 1 <= count <= STAGES),
             ("active stage", data[0], data[0] < count)]
        for i in range(min(count, STAGES)):
            v = int.from_bytes(data[1 + 2 * i:3 + 2 * i], "little")
            f.append((f"stage {i + 1} DPI", v, DPI_RANGE[0] <= v <= DPI_RANGE[1]))
        return f
    if unit == "polling":
        return [("polling level", data[0], data[0] < len(POLL_RATES))]
    if unit == "sensor":
        return ([("LOD", data[S_LOD], data[S_LOD] in LOD_MM),
                 ("reserved byte 4", data[4], data[4] == 0),
                 ("reserved byte 6", data[6], data[6] == 0)]
                + [(f"switch byte {p}", data[p], data[p] in (1, 2))
                   for p in (S_RIPPLE, S_ANGLE, S_MOTION, S_SCROLL, S_FPS)])
    if unit == "debounce":
        return [("debounce", data[0], data[0] <= DEBOUNCE_MAX)]
    return [("sleep", data[0], SLEEP_RANGE[0] <= data[0] <= SLEEP_RANGE[1])]


def check_unit(unit, data, before=None):
    """Refuse bytes the mouse must never get, e.g. from a hand-edited or
    foreign snapshot passed to `restore`. A value the mouse already holds
    (`before`) passes even if it is outside the known range, so an odd
    value reported by newer firmware doesn't block unrelated edits."""
    if len(data) != UNIT_LEN[unit]:
        raise DeviceError(f"refusing to write {unit} {bytes(data).hex()}: expected {UNIT_LEN[unit]} bytes")
    held = {}
    if before is not None and len(before) == UNIT_LEN[unit]:
        held = {label: v for label, v, _ in _fields(unit, before)}
    for label, v, ok in _fields(unit, data):
        if not ok and held.get(label) != v:
            raise DeviceError(f"refusing to write {unit} {bytes(data).hex()}: {label} {v} is out of range")


def encode_unit(unit, data):
    """(report, command payload) that writes `data` back."""
    if unit == "dpi":
        a = data[0]
        return REPORT_SHORT, bytes([CMD_DPI, a, a, a]) + bytes(data[1:12])
    if unit == "polling":
        lv = data[0]
        # [level per slot x2, level -> rate table, table size]: what the
        # Launcher sends for mice without configurable polling "gears".
        return REPORT_SHORT, bytes([CMD_POLLING, lv, lv, 0, 1, 2, 3, 4, 5, 6])
    if unit == "sensor":
        return REPORT_SHORT, bytes([CMD_SENSOR]) + bytes(data)
    if unit == "debounce":
        return REPORT_SHORT, bytes([CMD_DEBOUNCE, data[0]])
    if unit == "sleep":
        return REPORT_SHORT, bytes([CMD_SLEEP, 1, data[0]])
    raise KeyError(unit)


class M5Session(Session):
    """One M5 config interface (cable or dongle)."""

    TIMEOUT_S = 1.0       # the Launcher waits 1 s per command, then resends
    TRIES = 3             # a sleeping mouse needs a resend or two to wake

    def __init__(self, link):
        super().__init__(link)
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.link.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(f"no permission to open {self.link.path} -- run 'mousectl install-udev'", code="permission")
        except OSError as e:
            raise DeviceError(f"cannot open {self.link.path}: {e}")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    # ------------------------------------------------------------ transport

    def _drain(self):
        try:
            while select.select([self.fd], [], [], 0)[0]:
                os.read(self.fd, 256)
        except OSError:
            pass

    def _send(self, report, payload):
        n = LONG_LEN if report == REPORT_LONG else SHORT_LEN
        buf = bytes([report]) + bytes(payload) + bytes(n - len(payload))
        try:
            os.write(self.fd, buf)
        except OSError as e:
            raise DeviceError(f"write failed: {e}")

    def _wait(self, reply, match):
        """First full-length `reply` input report (without its report ID)
        that `match` accepts."""
        n = LONG_LEN if reply == REPLY_LONG else SHORT_LEN
        deadline = time.monotonic() + self.TIMEOUT_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                if not select.select([self.fd], [], [], left)[0]:
                    continue
                r = os.read(self.fd, 256)
            except OSError as e:
                raise DeviceError(f"read failed: {e}")
            if len(r) > n and r[0] == reply and match(r[1:]):
                return r[1:]

    def transact(self, report, payload, reply, match):
        for _ in range(self.TRIES):
            self._drain()
            self._send(report, payload)
            r = self._wait(reply, match)
            if r is not None:
                return r
        raise DeviceError(f"no answer to command 0x{payload[0]:02x} "
                          f"(mouse asleep or out of range? move it and retry)", code="no-answer")

    def config(self):
        return self.transact(REPORT_LONG, bytes([CMD_CONFIG]), REPLY_LONG, lambda r: r[0] == CMD_CONFIG)

    def command(self, report, payload):
        cmd = payload[0]
        reply = REPLY_LONG if report == REPORT_LONG else REPLY_SHORT
        r = self.transact(report, payload, reply, lambda r: r[0] == ACK and r[2] == cmd)
        if r[1] != 0:
            raise DeviceError(f"mouse refused command 0x{cmd:02x} (status {r[1]})")

    # --------------------------------------------------------------- units

    def read(self, units=None):
        all_units = decode_config(self.config(), self.mode)
        return {u: all_units[u] for u in (units or UNITS)}

    def write(self, before, after):
        changed = [u for u in UNITS
                   if u in after and bytes(before.get(u, b"")) != bytes(after[u])]
        for u in changed:               # all or nothing: check before any write
            check_unit(u, after[u], before.get(u))
        written = []
        for u in changed:
            for attempt in range(3):
                self.command(*encode_unit(u, after[u]))
                back = self.read([u])[u]
                if _same(u, back, after[u]):
                    break
                time.sleep(0.3)
            else:
                raise DeviceError(f"verify failed for {u}: wrote {bytes(after[u]).hex()} "
                                  f"read back {bytes(back).hex()}")
            before[u] = bytearray(after[u])
            written.append(u)
        return written

    def battery(self, timeout=None):
        b = self.config()[19]
        return min(b & 0x7F, 100), ("charging" if b & 0x80 else "discharging")


def _same(unit, a, b):
    if unit == "dpi":     # the mouse refills slots past the stage count
        n = 1 + 2 * b[11]
        return bytes(a[:n]) == bytes(b[:n]) and a[11] == b[11]
    return bytes(a) == bytes(b)


# ------------------------------------------------------------------- schema

def _stage(raw, i):
    return int.from_bytes(raw["dpi"][1 + 2 * i:3 + 2 * i], "little")


def _stage_get(i):
    return lambda raw, mode: _stage(raw, i)


def _stage_set(i):
    def set_(raw, mode, dpi):
        raw["dpi"][1 + 2 * i:3 + 2 * i] = int(dpi).to_bytes(2, "little")
    return set_


def _stage_enabled(i):
    return lambda raw, mode: i < raw["dpi"][11]


def _stage_set_enabled(i):
    """Stages are a list: count N means stages 1..N. Enabling a later stage
    enables the ones before it; only the last stage can be switched off."""
    def set_enabled(raw, mode, on):
        b = raw["dpi"]
        count = b[11]
        if on:
            for j in range(count, i + 1):
                if _stage(raw, j) == 0:
                    _stage_set(j)(raw, mode, 800)
            b[11] = max(count, i + 1)
            return
        if i != count - 1:
            raise ValueError(f"only the last stage (stage {count}) can be turned off")
        if count == 1:
            raise ValueError("at least one DPI stage must stay enabled")
        b[11] = count - 1
        if b[0] >= b[11]:
            b[0] = b[11] - 1
    return set_enabled


def _stage_suffix(i):
    return lambda raw, mode: "  <- active" if raw["dpi"][0] == i else ""


def _active_set(raw, mode, v):
    if v > raw["dpi"][11]:
        raise ValueError(f"stage {v} is not enabled")
    raw["dpi"][0] = v - 1


def _poll_get(raw, mode):
    lv = raw["polling"][0]
    return POLL_RATES[lv] if lv < len(POLL_RATES) else f"unknown({lv})"


def _poll_set(raw, mode, hz):
    raw["polling"][0] = POLL_RATES.index(hz)


def _lod_get(raw, mode):
    i = raw["sensor"][S_LOD]
    return LOD_MM.get(i, f"unknown({i})")


def _lod_set(raw, mode, mm):
    raw["sensor"][S_LOD] = LOD_INDEX[mm]


def _switch(pos, on_value=1):
    def get(raw, mode):
        return raw["sensor"][pos] == on_value

    def set_(raw, mode, on):
        raw["sensor"][pos] = on_value if on else 3 - on_value
    return get, set_


def _byte(unit):
    def set_(raw, mode, v):
        raw[unit][0] = int(v)
    return (lambda raw, mode: raw[unit][0]), set_


def _build_settings():
    dpi = Number(*DPI_RANGE, 50, unit=" DPI")
    s = [Setting(f"dpi.stage{i + 1}", f"Stage {i + 1}", "DPI", dpi, ("dpi",),
                 _stage_get(i), _stage_set(i),
                 get_enabled=_stage_enabled(i), set_enabled=_stage_set_enabled(i),
                 suffix=_stage_suffix(i))
         for i in range(STAGES)]
    s += [
        Setting("dpi.active", "Active stage", "DPI", Choice(range(1, STAGES + 1)), ("dpi",),
                lambda raw, mode: raw["dpi"][0] + 1, _active_set),
        Setting("polling", "Polling rate", "Performance", Choice(POLL_RATES, " Hz"), ("polling",),
                _poll_get, _poll_set, help="shared by cable and dongle"),
        Setting("motion_sync", "Motion sync", "Performance", Bool(), ("sensor",), *_switch(S_MOTION)),
        Setting("angle_snap", "Angle snap", "Performance", Bool(), ("sensor",), *_switch(S_ANGLE),
                help="straight-line correction"),
        Setting("ripple", "Ripple control", "Performance", Bool(), ("sensor",), *_switch(S_RIPPLE),
                help="jitter smoothing"),
        Setting("lod", "Lift-off distance", "Performance", Choice(sorted(LOD_INDEX), " mm"), ("sensor",),
                _lod_get, _lod_set),
        Setting("scroll_reverse", "Reverse scroll", "Performance", Bool(), ("sensor",),
                *_switch(S_SCROLL, on_value=2)),
        Setting("debounce", "Debounce", "Timing", Number(0, DEBOUNCE_MAX, unit=" ms"), ("debounce",),
                *_byte("debounce")),
        Setting("sleep", "Idle sleep", "Timing", Number(*SLEEP_RANGE, unit=" min"), ("sleep",),
                *_byte("sleep")),
    ]
    return s


class KeychronM5(Driver):
    id = "keychron-m5a24"
    name = "KeyChron M5 8K"
    aliases = ("m5a24", "m5")
    links = {(VENDOR, WIRED_PID): "wired", (VENDOR, DONGLE_PID): "dongle"}
    link_order = ("wired", "dongle")
    groups = ["DPI", "Performance", "Timing"]
    units = UNITS
    settings = _build_settings()

    def is_config_interface(self, node):
        return DESCRIPTOR in node.descriptor

    def unit_name(self, unit):
        return unit

    def parse_unit(self, name):
        return name

    def session(self, link):
        return M5Session(link)


DRIVER = KeychronM5()
