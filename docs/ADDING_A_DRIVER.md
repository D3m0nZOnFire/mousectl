# Adding a driver

A driver is one module in `mousectl/drivers/` that exports `DRIVER`, plus one
line in `mousectl/drivers/__init__.py`. The CLI (`status`, `get`, `set`,
`keys`, `save`/`restore`, `battery`, `dump`), the TUI, auto-backup and the
udev rule all come from what the driver declares.

## 1. The Driver

```python
class MyMouse(Driver):
    id = "vendor-model"            # stable: used by -m and ~/.config/mousectl/<id>/
    name = "Vendor Model"
    aliases = ()                   # optional extra -m names
    links = {(0x1234, 0x0001): "wired", (0x1234, 0x0002): "dongle"}
    link_order = ("wired", "dongle")
    groups = ["DPI", "Performance"] # section order in status / TUI; each is applied on its own
    units = (...)                  # keys of a full raw snapshot
    settings = [...]               # see below

    def is_config_interface(self, node):   # node.descriptor = HID report descriptor
        ...                                # pick the vendor collection among the hidraw nodes
    def session(self, link):
        return MySession(link)
```

`unit_name` / `parse_unit` control how units are written in snapshot files
(default `0x..` hex of an int unit).

## 2. The Session

Subclass `core.driver.Session`; it is opened per operation as a context
manager.

- `read(units=None)` → `{unit: bytearray}`: the raw config blocks/reports.
  Honour `units` so `mousectl get/set` only read what they touch.
- `write(before, after)` → list of written units: write every unit whose bytes
  differ, verifying if the device allows it. Update `before[unit]` as you go.
  Checksums etc. may be fixed up in `after` in place.
- `battery(timeout=None)` → `(percent, state)` or `None`.

Raise `core.driver.DeviceError` for anything link-related; the CLI then tries
the next link.

## 3. The settings

Each `core.settings.Setting` is a pure get/set on the raw snapshot:

```python
Setting("polling", "Polling rate", "Performance", Choice([125, 500, 1000], " Hz"),
        units=(RID_POLL,), get=lambda raw, mode: ..., set=lambda raw, mode, v: ...)
```

- kinds: `Number(lo, hi, step)` (or `steps=((upto, step), ...)` for coarser
  grids), `Choice(values)`, `Bool()`, `Color()`.
- `set=None` makes it read-only (shown dimmed, never written).
- `get_enabled` / `set_enabled` for values that can be switched off (DPI
  stages); `set KEY=off` disables, any value re-enables.
- `suffix(raw, mode)` adds display text; `companion="other.key"` shows another
  (hidden) setting inline, edited with `c` in the TUI.
- `mode` is the link ("wired"/"dongle") for settings that differ per link.

Keep key names consistent with existing drivers (`dpi.stageN`, `dpi.active`,
`polling`, `angle_snap`, `ripple`, `motion_sync`, `lod`, `debounce`, `sleep`,
`light.*`) so scripts and future profiles work across mice.

Because settings only touch their own bits, the core can apply one group at
a time even when groups share a unit: `settings.replay` rebuilds the target
from the device's bytes plus just that group's edits.

## 4. Tests

Capture `mousectl dump` from the real device into `tests/fixtures/` and add
decode/encode tests next to `tests/test_drivers.py`. Run `python3 -m unittest`.
