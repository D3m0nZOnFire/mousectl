"""On-disk state: settings snapshots and the one-time auto-backup.

~/.config/mousectl/<driver-id>/auto-backup.json is written once, from the
untouched device, right before mousectl first writes to that mouse -- so
there is always a way back to how the mouse came.
"""

import json
import os
import shutil
import time

from .settings import copy_raw

CONFIG_DIR = os.path.expanduser(os.environ.get("MOUSECTL_CONFIG", "~/.config/mousectl"))

# Backups left by the old single-model tools, adopted on first use.
LEGACY_BACKUPS = {
    "rapoo-vt3pro": os.path.expanduser("~/.config/rapoo-vt3pro/auto-backup.json"),
}


def driver_dir(driver):
    return os.path.join(CONFIG_DIR, driver.id)


def auto_backup_path(driver):
    return os.path.join(driver_dir(driver), "auto-backup.json")


def snapshot(driver, mode, raw):
    return {
        "driver": driver.id,
        "mode": mode,
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blocks": {driver.unit_name(u): bytes(b).hex() for u, b in raw.items()},
        "decoded": {s.key: _jsonable(s.value(raw, mode)) for s in driver.settings},
    }


def _jsonable(v):
    return "#%02x%02x%02x" % v if isinstance(v, tuple) else v


def write_snapshot(path, driver, mode, raw):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(snapshot(driver, mode, raw), f, indent=2)


def load_snapshot(path, driver):
    """Raw units from a snapshot file, validated against `driver`.
    Files from the old vt3pro tool (no "driver" field) are accepted."""
    with open(path) as f:
        saved = json.load(f)
    if saved.get("driver", driver.id) != driver.id:
        raise ValueError(f"{path} is a {saved['driver']} snapshot, not {driver.id}")
    raw = {driver.parse_unit(u): bytearray.fromhex(h) for u, h in saved["blocks"].items()}
    if set(raw) != set(driver.units):
        raise ValueError(f"{path} does not contain the {driver.name} settings blocks")
    return raw


def ensure_backup(session, raw):
    """Write the auto-backup once, before the first write to this mouse.
    `raw` may be partial; missing units are read from the device."""
    driver = session.link.driver
    path = auto_backup_path(driver)
    if os.path.exists(path):
        return
    legacy = LEGACY_BACKUPS.get(driver.id)
    if legacy and os.path.exists(legacy):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(legacy, path)
        return
    full = copy_raw(raw)
    missing = [u for u in driver.units if u not in full]
    if missing:
        full.update(session.read(missing))
    write_snapshot(path, driver, session.mode, full)


def apply(session, before, after):
    """Back up if needed, then write what changed. Returns written units."""
    if any(bytes(before[u]) != bytes(after[u]) for u in after):
        ensure_backup(session, before)
    return session.write(before, after)
