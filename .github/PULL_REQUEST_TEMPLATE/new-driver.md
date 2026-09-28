<!-- new-driver -->
<!--
Title: "Add <Vendor Model> driver". Fill in every field; the `pr-checklist` check
fails while a field is empty or a box is unticked. If a check doesn't apply
(e.g. no battery on a wired-only mouse), tick it and write "n/a: <why>" after it.
Walkthrough: docs/ADDING_A_DRIVER.md
-->

## Mouse

- **Vendor / model:**
- **Driver id / aliases:**
- **USB ids (`lsusb`), one per link:**
- **Firmware version (vendor software, or "unknown"):**
- **Protocol source (own USB capture, another project + licence, ...):**

## Files

- [ ] `mousectl/drivers/<module>.py` exporting `DRIVER`, listed in `mousectl/drivers/__init__.py`
- [ ] `tests/fixtures/<driver-id>.json` from `mousectl save` on the real mouse
- [ ] Decode/encode tests for this model in `tests/test_drivers.py`
- [ ] Row in the README's supported table (+ Credits if the protocol came from another project)

## Hardware tests (on my own mouse)

- [ ] H1 `mousectl list` finds the mouse on every link it has
- [ ] H2 Works as my normal user after `mousectl install-udev`
- [ ] H3 `mousectl status` matches the vendor software / the mouse's own indicators
- [ ] H4 `python3 -m tests.hardware_check -m <driver-id>` passes on every link (report below)
- [ ] H5 Changes can be felt/seen (DPI, polling rate, lighting, ...) and survive an unplug/replug
- [ ] H6 A value set over one link reads back over the other
- [ ] H7 Bad values are refused without writing (`mousectl set` off-grid DPI, unknown key)
- [ ] H8 `mousectl restore` brings back a `mousectl save` snapshot byte for byte
- [ ] H9 `mousectl battery` is plausible and shows charging when plugged in
- [ ] H10 The TUI shows every group and applies an edit in each
- [ ] H11 `python3 -m unittest` passes

## Reports

<details><summary>hardware_check report</summary>

<!-- paste the markdown printed by tests/hardware_check.py here -->

</details>

<details><summary><code>mousectl dump</code> and <code>mousectl status</code></summary>

```
paste here
```

</details>

## Not supported / known issues

<!-- settings the vendor software has that this driver doesn't, quirks, "none" -->
