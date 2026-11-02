"""Rapoo VT3 PRO Max (PAW3950, 8K).

Same vendor protocol, EEPROM layout and schema as the VT3 PRO
(rapoo_vt3pro.py): 32-byte A4/A5 frames on report 0xBA. Read from a real
Max, every block decodes to the published factory defaults (stages
400/800/1200/1600/3200/6400/30000), and every setting round-trips through
the same addresses over both the cable and the receiver.

What differs:
- DPI goes to 30000 in steps of 10 (PAW3950).
- The cable takes 8000 Hz, byte 0x81 in the Windows driver's setReportRate
  table, which the VT3 PRO (support8k=false) doesn't offer; measured at
  ~8000 reports/s. Over the receiver the mouse refuses 0x81 (it keeps the
  old rate), so the 2.4GHz slot stays at up to 4000 Hz until that is
  understood. Writes, `restore` included, are checked for this up front.
- On the cable it answers within ~4 ms, often before busy is visible, so
  the session runs VT3ProSession in FAST mode (see _request_fast).

Its own driver id keeps its snapshots, auto-backup and restore checks
apart from the VT3 PRO's.
"""

from ..core.driver import DeviceError
from .rapoo_vt3pro import ADDR_PERF, POLL_RATES, VENDOR, VT3Pro, VT3ProSession, build_settings

POLL_RATES_MAX = {**POLL_RATES, 8000: 0x81}
SLOT_BYTES = {0: set(POLL_RATES.values()),          # 2.4GHz slot
              2: set(POLL_RATES_MAX.values())}      # cable slot


def check_perf(held, after):
    """Refuse a polling byte its slot doesn't take, unless the mouse
    already holds it (raw restores bypass the setters' checks). `held` is
    the block as read from the mouse, or None when it isn't known."""
    for i, allowed in SLOT_BYTES.items():
        b = after[i]
        if b not in allowed and (held is None or b != held[i]):
            raise DeviceError(f"refusing to write polling byte 0x{b:02x} to the "
                              f"{'cable' if i else '2.4GHz'} slot: not a rate that slot takes")


class VT3ProMaxSession(VT3ProSession):
    FAST = True

    def write(self, before, after):
        held = before.get(ADDR_PERF)
        if ADDR_PERF in after and (held is None or bytes(held) != bytes(after[ADDR_PERF])):
            check_perf(held, after[ADDR_PERF])
        return super().write(before, after)


class VT3ProMax(VT3Pro):
    id = "rapoo-vt3pro-max"
    name = "Rapoo VT3 PRO Max"
    aliases = ("vt3promax",)
    # Confirmed on a real Max: plugging the cable in adds 4444 while the
    # receiver stays 1244.
    links = {(VENDOR, 0x4444): "wired", (VENDOR, 0x1244): "dongle"}
    link_order = ("wired", "dongle")
    settings = build_settings(dpi_max=30000, dpi_step=10, poll_rates=POLL_RATES_MAX,
                              wireless_rates=POLL_RATES)

    def session(self, link):
        return VT3ProMaxSession(link)


DRIVER = VT3ProMax()
