"""Registered drivers. To support a new mouse, add a module here that
exports DRIVER (see docs/ADDING_A_DRIVER.md) and list it below."""

from . import attackshark_x11, rapoo_vt3pro

DRIVERS = [attackshark_x11.DRIVER, rapoo_vt3pro.DRIVER]


def find(name):
    for d in DRIVERS:
        if name == d.id or name in d.aliases:
            return d
    return None
