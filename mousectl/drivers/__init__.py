"""Registered drivers. To support a new mouse, add a module here that
exports DRIVER (see docs/ADDING_A_DRIVER.md) and list it below."""

from . import attackshark_x11, pulsar_x3, rapoo_vt3pro, rapoo_vt3pro_max

DRIVERS = [attackshark_x11.DRIVER, rapoo_vt3pro.DRIVER, rapoo_vt3pro_max.DRIVER, pulsar_x3.DRIVER]


def find(name):
    for d in DRIVERS:
        if name == d.id or name in d.aliases:
            return d
    return None
