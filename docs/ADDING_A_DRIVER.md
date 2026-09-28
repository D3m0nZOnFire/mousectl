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

The maintainers can't test mice they don't own, so testing is split in two:
you run the hardware tests on your mouse, and GitHub runs everything that
works without one on every push.

### 4.1 Snapshot fixture

Set distinctive values first (e.g. in the vendor software), check
`mousectl status` shows them correctly (H3 below), then:

```bash
mousectl save -m <driver-id> tests/fixtures/<driver-id>.json
```

The file holds the raw blocks and the values your driver decoded from them.
The contract tests use it as the stand-in for your mouse, and fail if a later
change decodes it differently.

### 4.2 Model tests

In `tests/test_drivers.py`, add a class for your mouse like `X3Schema`:
decode the fixture and compare with what the vendor software showed, set →
get round trips, and whatever is special about the protocol (checksums, DPI
grids, X/Y written together, stages that can't be disabled, ...).

### 4.3 Contract tests (run by GitHub)

`tests/test_driver_contract.py` runs on every registered driver, so your
driver is covered as soon as it is in `DRIVERS`. It checks that:

- every module in `mousectl/drivers/` is registered; ids, aliases and USB
  ids aren't already taken; the driver is in the README table;
- `link_order`, `groups`, `units` and every setting's `units`/`companion`
  agree with each other; keys are lowercase and dotted;
- the fixture exists, loads, and still decodes to its saved values;
- every setting decodes to a valid value on every link;
- writing a setting's current value changes no bytes, and writing another
  value reads back and only touches that setting's `units`;
- applying one group leaves the other groups' edits pending;
- `tests/hardware_check.py` passes against a fake mouse built from the fixture.

`python3 -m unittest` runs all of it locally. On GitHub the `tests` workflow
runs it on Python 3.9, 3.11 and 3.14.

### 4.4 Hardware tests (on your mouse)

Do these on every link the mouse has (cable, dongle). Save a snapshot first so
you can always go back: `mousectl save -m <id> ~/before.json`. The first
write also leaves `~/.config/mousectl/<id>/auto-backup.json`.

| # | Test | How |
|---|------|-----|
| H1 | Detected on every link | Plug in only the cable, then only the dongle: `mousectl list` shows the right link each time. |
| H2 | Works without root | `mousectl install-udev`, run the printed commands, replug; all commands work without `sudo`. |
| H3 | Reads are right | Set distinctive values in the vendor software (a Windows VM with USB passthrough works), then compare with `mousectl status`. |
| H4 | Write, read back, restore | `python3 -m tests.hardware_check -m <id>` writes a different value to each setting, reads it back and puts the original back. Paste the report it prints in the PR. |
| H5 | Real effect, persistent | `mousectl set` a few settings you can notice (DPI, polling rate with `evhz`, lighting, sleep). Unplug and replug (or switch the mouse off and on) and check `mousectl status`. |
| H6 | Links share settings | `mousectl set --link dongle ...`, then `mousectl get --link wired ...` shows the same value. |
| H7 | Bad values refused | `mousectl set -m <id> dpi.stage1=7` and `mousectl set -m <id> nosuch=1` both fail, and `mousectl dump` is unchanged. |
| H8 | Restore is exact | `mousectl save a.json`, change things, `mousectl restore a.json`, `mousectl save b.json`: the `"blocks"` of both files are identical. |
| H9 | Battery | `mousectl battery` gives a plausible %, and "charging" while the cable is in. Write n/a for wired-only mice. |
| H10 | TUI | `mousectl tui`: every group shows up, an edit in each group applies, and unplugging the mouse doesn't crash it. |
| H11 | Unit tests | `python3 -m unittest` passes. |

A test that can't pass on your mouse isn't a blocker. Say why in the PR.

## 5. Opening the pull request

1. Fork the repository on GitHub and clone your fork.
2. Branch: `git switch -c add-<driver-id>`.
3. Make one commit (or a few) titled like the existing ones:
   `Add <Vendor Model> driver`.
4. `git push -u origin add-<driver-id>`.
5. Open the PR **with the new-driver template**:
   - `gh pr create --template new-driver.md --title "Add <Vendor Model> driver"`, or
   - on github.com, open the "Compare & pull request" page and add
     `?template=new-driver.md` to its URL (`&template=new-driver.md` if the
     URL already has a `?`).
6. Fill in every field:
   - **Mouse**: vendor/model, driver id and aliases, the `lsusb` id of each
     link, the firmware version, and where the protocol came from (your own USB
     capture, or another project; give its licence and add it to Credits).
   - **Files**: tick each file once it's in the PR.
   - **Hardware tests**: tick H1–H11. For one that doesn't apply, tick it and
     write `n/a: <why>` after it.
   - **Reports**: the `hardware_check` markdown, plus `mousectl dump` and
     `mousectl status` output.
   - **Not supported / known issues**: vendor features the driver doesn't cover,
     quirks, or "none".
7. Wait for the checks:
   - `tests`: the unit and contract tests. On a failure, open the log and fix
     the code (the fixture and the contract tests describe what the mouse does).
   - `pr-checklist`: fails while the title isn't `Add … driver`, a field is
     empty, a box is unticked, or the `hardware_check` report is missing or has
     `FAIL` rows. Editing the PR description re-runs it. A PR that adds a file
     in `mousectl/drivers/` without this template fails here too.

On a first-time contributor's PR the checks only start after a maintainer
approves the workflow run, so a short wait is normal.
