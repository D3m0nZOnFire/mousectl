"""Linux hidraw plumbing shared by every driver: ioctl numbers, report
descriptor parsing and enumeration of /sys/class/hidraw."""

import glob
import os
from dataclasses import dataclass


def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def HIDIOCSFEATURE(size):
    return _ioc(3, "H", 0x06, size)


def HIDIOCGFEATURE(size):
    return _ioc(3, "H", 0x07, size)


def HIDIOCGINPUT(size):
    return _ioc(3, "H", 0x0A, size)


def parse_feature_lengths(desc):
    """Walk a HID report descriptor, return {report_id: feature payload bytes}.

    Global items (report ID/size/count) persist across collections, so they
    are tracked as running state exactly as the HID spec requires, including
    Push/Pop. Bits of one report are packed across items, so they are summed
    before rounding up to bytes. A truncated item at the end is ignored.
    """
    bits = {}
    rid = 0
    size = 0
    count = 0
    stack = []
    i = 0
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:  # long item: 0xFE, bDataSize, bLongItemTag, data
            if i + 1 >= len(desc):
                break
            i += 3 + desc[i + 1]
            continue
        tag, blen = b & 0xFC, b & 0x03
        blen = 4 if blen == 3 else blen
        if i + 1 + blen > len(desc):
            break
        data = int.from_bytes(desc[i + 1:i + 1 + blen], "little") if blen else 0
        if tag == 0x84:      # Report ID
            rid = data
        elif tag == 0x74:    # Report Size
            size = data
        elif tag == 0x94:    # Report Count
            count = data
        elif tag == 0xA4:    # Push
            stack.append((rid, size, count))
        elif tag == 0xB4:    # Pop
            if stack:
                rid, size, count = stack.pop()
        elif tag == 0xB0:    # Feature (main item)
            bits[rid] = bits.get(rid, 0) + size * count
        i += 1 + blen
    return {r: (n + 7) // 8 for r, n in bits.items()}


@dataclass
class HidrawNode:
    path: str           # /dev/hidrawN
    vid: int
    pid: int
    descriptor: bytes


def enumerate_hidraw():
    """Every hidraw node with a readable uevent + report descriptor, in
    numeric order (hidraw2 before hidraw10)."""
    nodes = sorted(glob.glob("/sys/class/hidraw/hidraw*"),
                   key=lambda p: int(p.rsplit("hidraw", 1)[1]))
    for node in nodes:
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                props = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
            with open(os.path.join(node, "device", "report_descriptor"), "rb") as f:
                desc = f.read()
        except OSError:
            continue
        parts = props.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        yield HidrawNode("/dev/" + os.path.basename(node),
                         int(parts[1], 16), int(parts[2], 16), desc)
