"""Driver / Session base classes and device discovery.

A *driver* knows one mouse model: which hidraw nodes are its config
interfaces, how to read/write its settings, and the Setting schema that
describes them. A *link* is one live connection to it (cable or dongle);
a *mouse* is a driver plus all of its links currently attached.
"""

from dataclasses import dataclass, field

from .hidraw import enumerate_hidraw


class DeviceError(Exception):
    """A problem talking to one specific link (skip it, don't abort)."""


@dataclass
class Link:
    driver: "Driver"
    path: str               # /dev/hidrawN
    mode: str               # "wired" | "dongle" | ...
    info: dict = field(default_factory=dict)   # driver-private probe results

    def open(self):
        return self.driver.session(self)

    def __str__(self):
        return f"{self.driver.name} [{self.mode}] {self.path}"


class Session:
    """An open link. Use as a context manager; subclasses do the I/O."""

    def __init__(self, link):
        self.link = link
        self.mode = link.mode

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, units=None):
        """Raw snapshot {unit: bytearray} of `units` (default: all)."""
        raise NotImplementedError

    def write(self, before, after):
        """Write every unit whose bytes differ, verifying where the device
        allows it. Returns the list of written units. `after` may be
        adjusted in place (e.g. checksums)."""
        raise NotImplementedError

    def battery(self, timeout=None):
        """(percent, state) or None when this link can't report it.
        `timeout` bounds mice that only report periodically."""
        return None


class Driver:
    id = ""                 # stable, used for -m and config paths
    name = ""               # human name
    aliases = ()            # short names for -m (ashark, vt3pro)
    links = {}              # {(vid, pid): mode}
    link_order = ()         # preferred link order, e.g. ("wired", "dongle")
    settings = []           # [Setting]
    groups = []             # display/apply order of Setting.group
    units = ()              # every raw unit a full read returns

    def probe(self, node):
        """Return a Link if `node` is this driver's config interface."""
        mode = self.links.get((node.vid, node.pid))
        if mode and self.is_config_interface(node):
            return Link(self, node.path, mode, self.link_info(node))
        return None

    def is_config_interface(self, node):
        return True

    def link_info(self, node):
        return {}

    def session(self, link):
        raise NotImplementedError

    def setting(self, key):
        for s in self.settings:
            if s.key == key:
                return s
        raise KeyError(key)

    def unit_name(self, unit):
        return f"0x{unit:02x}"

    def parse_unit(self, name):
        return int(name, 16)

    def udev_rules(self, owner=None):
        extra = f', MODE="0660", OWNER="{owner}"' if owner else ""
        return "".join(
            f'SUBSYSTEM=="hidraw", ATTRS{{idVendor}}=="{vid:04x}", '
            f'ATTRS{{idProduct}}=="{pid:04x}"{extra}, TAG+="uaccess"\n'
            for (vid, pid) in sorted(self.links))


@dataclass
class Mouse:
    driver: Driver
    links: list

    @property
    def name(self):
        return self.driver.name


def discover(drivers):
    """Every attached mouse, links ordered by each driver's preference."""
    found = {}
    for node in enumerate_hidraw():
        for drv in drivers:
            link = drv.probe(node)
            if link:
                found.setdefault(drv.id, Mouse(drv, [])).links.append(link)
                break
    mice = []
    for drv in drivers:
        m = found.get(drv.id)
        if m:
            order = list(drv.link_order)
            m.links.sort(key=lambda l: order.index(l.mode) if l.mode in order else len(order))
            mice.append(m)
    return mice
