"""Rapoo VT3 PRO (HSDM, dual-mode, PAW3398).

Talks the vendor protocol directly over /dev/hidraw*: 32-byte A4 (read) /
A5 (write) EEPROM frames on vendor report 0xBA, sent as an output report and
answered through GET_REPORT(Input) -- reverse engineered from Rapoo's Windows
"RapooGameDevDriver" 1.6.29 (class FDeviceVT3PRO_HSDM[_Wireless]_BCut).

Everywhere possible: read the 4/16-byte block the Windows driver itself
reads, modify only the requested bytes, write the block back, then read it
again to verify. Bytes whose meaning isn't confirmed from the driver are
shown read-only (or not at all) and always preserved untouched.

The VT3 PRO Max (rapoo_vt3pro_max.py) speaks the same protocol with the
same EEPROM layout and reuses this session and schema with its own limits.
"""

import fcntl
import os
import time

from ..core.driver import DeviceError, Driver, Session
from ..core.hidraw import HIDIOCGINPUT
from ..core.settings import Bool, Choice, Number, Setting

VENDOR = 0x24AE
CONN_BYTE = {"wired": 0xFF, "dongle": 0xA5}

RID = 0xBA
CMD_READ = 0xA4
CMD_WRITE = 0xA5
CMD_BATTERY = 0xAA
ACK_OK = 0x01
ACK_BUSY = 0x02

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
STAGES = 7

# Correction byte (ADDR_TIMING[3]) stores *disable* flags:
# straightLine on/off -> bit0 clear/set, corrugation on/off -> bit1 clear/set.
FLAG_ANGLE_SNAP_OFF = 0x01
FLAG_RIPPLE_OFF = 0x02


class VT3ProSession(Session):
    """One VT3 PRO config interface."""

    GAP_S = 0.050          # Windows driver sleeps 50ms after every exchange
    ACCEPT_S = 0.100       # busy shows up within ~4ms; none by now = frame lost
    SENDS = 4
    STALE_RETRIES = 4
    SETTLE_S = 0.300
    QUIET_S = 0.050        # see _settle
    FAST = False           # see _request_fast; the VT3 PRO Max sets it

    def __init__(self, link):
        super().__init__(link)
        self.fd = None
        self._last_done = 0.0
        self._last_resp = None

    def __enter__(self):
        try:
            self.fd = os.open(self.link.path, os.O_RDWR)
        except PermissionError:
            raise DeviceError(f"permission denied on {self.link.path} -- run 'mousectl install-udev'", code="permission")
        except OSError as e:
            raise DeviceError(f"cannot open {self.link.path}: {e}")
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
        if self.FAST:
            return self._request_fast(frame, timeout)
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
        raise DeviceError("no answer from the mouse (asleep? move it and retry)", code="no-answer")

    def _kind(self, resp):
        """'battery' or 'block': battery answers carry the charge state
        (1/2) in byte 1, where every block answer (read or write) has 0."""
        return "battery" if resp[1] else "block"

    def _settle(self):
        """(FAST timing uses time.monotonic(): a wall-clock adjustment must
        not shorten the quiet window or a deadline.)

        The input report once nothing is in flight or queued: not busy
        and unchanged for QUIET_S. Measured on the Max's cable, frames sent
        back to back are all answered within ~5 ms of the last write, so a
        queued exchange shows up well inside the window. Returns the busy
        report if busy lasts SETTLE_S: a freshly plugged-in cable reports
        02 00 00 ... until its first command, but so would a slow exchange,
        so the caller must sync before trusting anything (_request_fast)."""
        start = changed = time.monotonic()
        last = None
        while True:
            try:
                held = self._get_input()
            except OSError as e:
                raise DeviceError(f"read failed: {e}")
            now = time.monotonic()
            if held != last:
                last, changed = held, now
            if held[0] != ACK_BUSY and now - changed >= self.QUIET_S:
                return held
            if now - start >= self.SETTLE_S:
                if held[0] == ACK_BUSY:
                    return held
                raise DeviceError("mouse keeps answering earlier commands -- retry", code="no-answer")
            time.sleep(0.002)

    def _request_fast(self, frame, timeout=2.0):
        """request() for devices that may answer before busy is visible.

        Freshness can't come from seeing busy, so it comes from contents.
        The mouse handles one exchange at a time and answers in order, so:
        - nothing may be in flight or queued when the frame is sent
          (_settle: quiet for QUIET_S). A busy
          that doesn't clear is either the idle state of a freshly plugged
          cable or a slow earlier exchange; elapsed time can't tell them
          apart, so it never authorises the caller's frame. A battery query
          is answered first instead (sync), and its answer, possibly the
          earlier exchange's, is thrown away. If the mouse is busy again
          after that (more left over from a timed-out exchange), the call
          fails with no-answer instead of sending the caller's frame.
        - the report held when the frame is sent must be of the other kind
          than the expected answer; if it isn't, a query of the other kind
          goes first.
        A fresh answer then always differs from what was held, an unchanged
        report always means "not answered yet", and a frame that is never
        answered ends in no-answer.
        """
        kind = "battery" if frame[1] == CMD_BATTERY else "block"
        held = self._settle()
        if held[0] == ACK_BUSY:
            held = self._exchange(self._frame(CMD_BATTERY, 0, length=0), "battery", held, timeout)
            held = self._settle()
        if held[0] == ACK_OK and self._kind(held) == kind:
            other = (self._frame(CMD_BATTERY, 0, length=0) if kind == "block"
                     else self._frame(CMD_READ, ADDR_DPI_CUR, length=BLOCKS[ADDR_DPI_CUR]))
            self._request_fast(other, timeout)
            held = self._settle()
        if held[0] == ACK_BUSY:
            # still busy after the sync/priming answer: that answer may have
            # been left over from an earlier, timed-out exchange, and more
            # are queued. Never send the caller's frame from here.
            raise DeviceError("mouse still busy with an earlier command -- retry", code="no-answer")
        return self._exchange(frame, kind, held, timeout)

    def _exchange(self, frame, kind, held, timeout):
        """Send `frame` (resending while nothing happens) until an OK
        answer of `kind` that differs from `held` arrives."""
        data = bytes([RID]) + bytes(frame).ljust(32, b"\0")[:32]
        deadline = time.monotonic() + timeout
        for _ in range(self.SENDS):
            wait = self._last_done + self.GAP_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                os.write(self.fd, data)
            except OSError as e:
                raise DeviceError(f"write failed: {e}")
            sent = time.monotonic()
            busy = False
            while time.monotonic() < deadline:
                time.sleep(0.002)
                try:
                    resp = self._get_input()
                except OSError as e:
                    raise DeviceError(f"read failed: {e}")
                if resp[0] == ACK_BUSY:
                    busy = True
                elif resp[0] == ACK_OK and resp != held and self._kind(resp) == kind:
                    self._last_done = time.monotonic()
                    self._last_resp = resp
                    return resp
                elif not busy and time.monotonic() - sent >= self.ACCEPT_S:
                    break       # nothing happened: frame lost, send again
            self._last_done = time.monotonic()
            if time.monotonic() >= deadline:
                break
        raise DeviceError("no answer from the mouse (asleep? move it and retry)", code="no-answer")

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
        if self.FAST:       # every answer is proven fresh (_request_fast)
            return bytearray(self.request(frame)[4:4 + length])
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

    def read(self, units=None):
        return {addr: self.read_block(addr) for addr in (units or BLOCKS)}

    def write(self, before, after):
        """Write every block that differs, in address order, verifying each."""
        changed = [a for a in sorted(after) if bytes(before.get(a, b"")) != bytes(after[a])]
        for a in changed:
            self.write_block(a, after[a])
            before[a] = bytearray(after[a])
        return changed

    def battery(self, timeout=None):
        resp = self._battery_resp()
        if resp is None:
            raise DeviceError("no battery answer from the mouse", code="no-answer")
        state = {1: "discharging", 2: "charging"}.get(resp[1], "unknown")
        return min(resp[2], 100), state


# ------------------------------------------------------------------- schema

def _u16(addr, i):
    return lambda raw: int.from_bytes(raw[addr][i * 2:i * 2 + 2], "little")


def _stage_get(i):
    x = _u16(ADDR_DPI_X, i)
    return lambda raw, mode: x(raw)


def _stage_set(i):
    def set_(raw, mode, dpi):
        v = int(dpi).to_bytes(2, "little")
        raw[ADDR_DPI_X][i * 2:i * 2 + 2] = v
        raw[ADDR_DPI_Y][i * 2:i * 2 + 2] = v
    return set_


def _stage_suffix(i):
    y = _u16(ADDR_DPI_Y, i)

    def suffix(raw, mode):
        extra = f" (Y {y(raw)})" if y(raw) != _u16(ADDR_DPI_X, i)(raw) else ""
        return extra + ("  <- active" if raw[ADDR_DPI_CUR][0] == i else "")
    return suffix


def _poll_byte(mode):
    return 2 if mode == "wired" else 0


def _hz(b):
    return POLL_NAMES.get(b, f"unknown(0x{b:02x})")


def _flag(off_bit):
    def get(raw, mode):
        return not (raw[ADDR_TIMING][3] & off_bit)

    def set_(raw, mode, on):
        if on:
            raw[ADDR_TIMING][3] &= ~off_bit & 0xFF
        else:
            raw[ADDR_TIMING][3] |= off_bit
    return get, set_


def _byte(addr, idx):
    def set_(raw, mode, v):
        raw[addr][idx] = v
    return (lambda raw, mode: raw[addr][idx]), set_


def _active_set(raw, mode, v):
    raw[ADDR_DPI_CUR][0] = v - 1


def _motion_set(raw, mode, on):
    raw[ADDR_SENSOR][1] = 1 if on else 0


def build_settings(dpi_max=26000, dpi_step=50, poll_rates=POLL_RATES, wireless_rates=None):
    """The VT3 PRO schema; the Max calls it with its own sensor/polling
    limits. `wireless_rates` limits what the 2.4GHz link accepts when it
    is narrower than `poll_rates`."""
    dpi = Number(50, dpi_max, dpi_step, unit=" DPI")
    rates = sorted(poll_rates)

    def poll_set(raw, mode, hz):
        if mode == "dongle" and wireless_rates is not None and hz not in wireless_rates:
            raise ValueError(f"{hz} Hz is only accepted over the cable")
        raw[ADDR_PERF][_poll_byte(mode)] = poll_rates[hz]

    s = [Setting(f"dpi.stage{i + 1}", f"Stage {i + 1}", "DPI", dpi, (ADDR_DPI_X, ADDR_DPI_Y),
                 _stage_get(i), _stage_set(i), suffix=_stage_suffix(i))
         for i in range(STAGES)]
    snap_get, snap_set = _flag(FLAG_ANGLE_SNAP_OFF)
    ripple_get, ripple_set = _flag(FLAG_RIPPLE_OFF)
    press_get, press_set = _byte(ADDR_TIMING, 0)
    release_get, release_set = _byte(ADDR_TIMING, 1)
    sleep_get, sleep_set = _byte(ADDR_TIMING, 2)
    s += [
        Setting("dpi.active", "Active stage", "DPI", Choice(range(1, STAGES + 1)), (ADDR_DPI_CUR,),
                lambda raw, mode: raw[ADDR_DPI_CUR][0] + 1, _active_set),
        Setting("polling", "Polling rate", "Performance", Choice(rates, " Hz"), (ADDR_PERF,),
                lambda raw, mode: _hz(raw[ADDR_PERF][_poll_byte(mode)]), poll_set,
                suffix=lambda raw, mode: f"  ({'cable' if mode == 'wired' else '2.4GHz'} link)",
                help="rate of the link you're connected over"),
        Setting("motion_sync", "Motion sync", "Performance", Bool(), (ADDR_SENSOR,),
                lambda raw, mode: bool(raw[ADDR_SENSOR][1]), _motion_set),
        Setting("angle_snap", "Angle snap", "Performance", Bool(), (ADDR_TIMING,), snap_get, snap_set,
                help="straight-line correction"),
        Setting("ripple", "Ripple control", "Performance", Bool(), (ADDR_TIMING,), ripple_get, ripple_set,
                help="jitter smoothing"),
        Setting("polling.wireless", "Polling (2.4GHz slot)", "Performance", Choice(rates, " Hz"),
                (ADDR_PERF,), lambda raw, mode: _hz(raw[ADDR_PERF][0])),
        Setting("polling.wired", "Polling (cable slot)", "Performance", Choice(rates, " Hz"),
                (ADDR_PERF,), lambda raw, mode: _hz(raw[ADDR_PERF][2])),
        Setting("lod", "Lift-off level", "Performance", Number(0, 255), (ADDR_SENSOR,),
                lambda raw, mode: raw[ADDR_SENSOR][0]),
        Setting("sensor_angle", "Sensor angle", "Performance", Number(-128, 127), (ADDR_ANGLE,),
                lambda raw, mode: int.from_bytes(raw[ADDR_ANGLE][0:1], "little", signed=True)),
        Setting("debounce.press", "Debounce (press)", "Timing", Choice(DEBOUNCE_MS, " ms"), (ADDR_TIMING,),
                press_get, press_set),
        Setting("debounce.release", "Debounce (release)", "Timing", Choice(DEBOUNCE_MS, " ms"), (ADDR_TIMING,),
                release_get, release_set),
        Setting("sleep", "Sleep after", "Timing", Number(1, 60, unit=" min"), (ADDR_TIMING,),
                sleep_get, sleep_set),
    ]
    return s


class VT3Pro(Driver):
    id = "rapoo-vt3pro"
    name = "Rapoo VT3 PRO"
    aliases = ("vt3pro",)
    links = {(VENDOR, 0x4431): "wired", (VENDOR, 0x1231): "dongle"}
    link_order = ("wired", "dongle")
    groups = ["DPI", "Performance", "Timing"]
    units = tuple(BLOCKS)
    settings = build_settings()

    def is_config_interface(self, node):
        return bytes([0x85, RID]) in node.descriptor

    def unit_name(self, unit):
        return f"0x{unit:03x}"

    def session(self, link):
        return VT3ProSession(link)


DRIVER = VT3Pro()
