"""KeyChron M5 8K (M5-A24, PAW3950).

Talks Pulsar Fusion's 64-byte vendor protocol directly over /dev/hidraw*:
one feature report (no report ID) on USB interface 3, sent with SET_FEATURE
and answered through GET_FEATURE. Protocol is identical to Pulsar X3.

Frame (device bytes, after the hidraw report-number byte):
  [0] 0 on requests; 1 = answer ready, 2 = busy on replies
  [1] command group  [2] operation (bit 7 = read)  [3] payload length
  [6] profile (0 = "the active one" for global settings)  [7..61] payload
  [62..63] u16le sum of bytes 0..61

Each unit is one read command's answer, stored as exactly the payload its
write command takes, so a write is "send the unit back".
"""

import fcntl

from ..core.driver import DeviceError, Driver, Session
from ..core.settings import Bool, Choice, Number, Setting
from .pulsar_x3 import X3Session as _X3Session
from .pulsar_x3 import (
    DPI_LEN, FRAME, POLL_NAMES, POLL_RATES, STAGES, STAGE_LEN, UNITS,
    STATUS_OK, STATUS_BUSY,
    _stage_get, _stage_set, _stage_enabled, _stage_set_enabled, _stage_suffix,
    _active_set, _byte, _bool, _sleep_get, _sleep_set,
)

VENDOR = 0x3434
WIRED_PIDS = (0xd027, 0xd029)     # 0xd027 = wired, 0xd029 = 8K wired mode (verify with lsusb)
DONGLE_PID = 0xd028               # wireless dongle (Ultra-Link 8K)
CONFIG_INTERFACE = 3
DESCRIPTOR = b"\x06\xff\xff\x09\x01"  # same as Pulsar X3 (vendor class)


class M5Session(_X3Session):
    """One M5 config interface (cable or dongle)."""


def _build_settings():
    """M5 has the same settings as Pulsar X3 (same protocol)."""
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


class KeychronM5(Driver):
    id = "keychron-m5a24"
    name = "KeyChron M5 8K"
    aliases = ("m5a24", "m5")
    links = {**{(VENDOR, pid): "wired" for pid in WIRED_PIDS},
             (VENDOR, DONGLE_PID): "dongle"}
    link_order = ("wired", "dongle")
    groups = ["DPI", "Performance", "Timing"]
    units = tuple(UNITS)
    settings = _build_settings()

    def is_config_interface(self, node):
        if DESCRIPTOR not in node.descriptor:
            return False
        dev = f"/sys/class/hidraw/{node.path.split('/')[-1]}/device/.."
        try:
            with open(f"{dev}/bInterfaceNumber") as f:
                return int(f.read().strip(), 16) == CONFIG_INTERFACE
        except (OSError, ValueError):
            return False

    def unit_name(self, unit):
        return unit

    def parse_unit(self, name):
        return name

    def session(self, link):
        return M5Session(link)


DRIVER = KeychronM5()
