"""Checks every registered driver must pass, without the mouse attached.

These run in CI on every pull request. A new driver only needs its
`mousectl save` snapshot in tests/fixtures/<driver-id>.json; the
model-specific decode/encode tests still go in test_drivers.py.
"""

import glob
import importlib
import os
import re
import tempfile
import unittest

from mousectl.core import store
from mousectl.core.driver import Link
from mousectl.core.settings import copy_raw, dirty_settings, replay
from mousectl.drivers import DRIVERS
from tests.hardware_check import check_session, other_value
from tests.test_drivers import FakeSession

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
KEY_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*")
ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)+")


def fixture_path(drv):
    return os.path.join(HERE, "fixtures", f"{drv.id}.json")


def modes(drv):
    return sorted(set(drv.links.values()))


class Registration(unittest.TestCase):
    def test_every_driver_module_is_registered(self):
        for path in glob.glob(os.path.join(ROOT, "mousectl", "drivers", "*.py")):
            name = os.path.basename(path)[:-3]
            if name == "__init__":
                continue
            mod = importlib.import_module(f"mousectl.drivers.{name}")
            self.assertTrue(hasattr(mod, "DRIVER"), f"{name}.py does not export DRIVER")
            self.assertTrue(any(mod.DRIVER is d for d in DRIVERS), f"{name}.DRIVER is not listed in drivers/__init__.py")

    def test_ids_and_aliases_are_unique(self):
        seen = {}
        for d in DRIVERS:
            self.assertRegex(d.id, ID_RE, "id should be vendor-model, lowercase")
            for name in (d.id, *d.aliases):
                self.assertNotIn(name, seen, f"{d.id}: '{name}' already used by {seen.get(name)}")
                seen[name] = d.id

    def test_usb_ids_are_claimed_once(self):
        seen = {}
        for d in DRIVERS:
            for vid, pid in d.links:
                self.assertTrue(0 <= vid <= 0xFFFF and 0 <= pid <= 0xFFFF, (d.id, vid, pid))
                self.assertNotIn((vid, pid), seen, f"{d.id}: {vid:04x}:{pid:04x} already claimed by {seen.get((vid, pid))}")
                seen[(vid, pid)] = d.id

    def test_listed_in_readme(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for d in DRIVERS:
            self.assertIn(f"`{d.id}`", readme, f"{d.id} missing from the README's supported table")


class Schema(unittest.TestCase):
    def test_declarations(self):
        for d in DRIVERS:
            with self.subTest(driver=d.id):
                self.assertTrue(d.name)
                self.assertTrue(d.links, "no USB ids")
                self.assertEqual(set(d.link_order), set(d.links.values()), "link_order must list every link mode")
                self.assertTrue(d.units, "units is empty")
                self.assertEqual(len(d.udev_rules().splitlines()), len(d.links))

    def test_settings(self):
        for d in DRIVERS:
            keys = [s.key for s in d.settings]
            with self.subTest(driver=d.id):
                self.assertTrue(d.settings, "no settings")
                self.assertEqual(len(keys), len(set(keys)), "duplicate setting keys")
                self.assertEqual({s.group for s in d.settings}, set(d.groups), "groups and settings' groups differ")
            for s in d.settings:
                with self.subTest(driver=d.id, key=s.key):
                    self.assertRegex(s.key, KEY_RE)
                    self.assertTrue(s.label)
                    self.assertTrue(s.units, "a setting must name the units it lives in")
                    self.assertLessEqual(set(s.units), set(d.units))
                    self.assertEqual(s.get_enabled is None, s.set_enabled is None,
                                     "get_enabled and set_enabled go together")
                    if s.companion:
                        self.assertIn(s.companion, keys)


class Fixture(unittest.TestCase):
    """Uses the snapshot captured from the real mouse with `mousectl save`."""

    def raw(self, d):
        path = fixture_path(d)
        self.assertTrue(os.path.exists(path),
                        f"missing {os.path.relpath(path, ROOT)}: run `mousectl save -m {d.id} <that path>`")
        return store.load_snapshot(path, d)

    def test_snapshot_roundtrip(self):
        for d in DRIVERS:
            with self.subTest(driver=d.id):
                raw = self.raw(d)
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "s.json")
                    store.write_snapshot(path, d, modes(d)[0], raw)
                    self.assertEqual(store.load_snapshot(path, d), raw)

    def test_decoded_values_unchanged(self):
        # The snapshot's "decoded" was checked against the vendor software by
        # the contributor; a decoding change has to update it on purpose.
        import json
        for d in DRIVERS:
            with self.subTest(driver=d.id):
                raw = self.raw(d)
                with open(fixture_path(d)) as f:
                    saved = json.load(f)
                self.assertEqual(store.snapshot(d, saved["mode"], raw)["decoded"], saved["decoded"])

    def test_every_setting_decodes_to_a_valid_value(self):
        for d in DRIVERS:
            raw = self.raw(d)
            for mode in modes(d):
                for s in d.settings:
                    with self.subTest(driver=d.id, mode=mode, key=s.key):
                        if s.enabled(raw, mode):    # a disabled one shows "off", whatever it holds
                            s.kind.check(s.get(raw, mode))
                        self.assertIsInstance(s.format(raw, mode), str)

    def test_writing_the_current_value_changes_nothing(self):
        for d in DRIVERS:
            raw = self.raw(d)
            for mode in modes(d):
                for s in d.settings:
                    if s.read_only or not s.enabled(raw, mode):
                        continue
                    with self.subTest(driver=d.id, mode=mode, key=s.key):
                        after = copy_raw(raw)
                        s.assign(after, mode, s.get(raw, mode))
                        self.assertEqual(after, raw)

    def test_set_then_get_stays_in_its_units(self):
        for d in DRIVERS:
            raw = self.raw(d)
            for mode in modes(d):
                for s in d.settings:
                    if s.read_only or not s.enabled(raw, mode):
                        continue
                    v = other_value(s, raw, mode)
                    if v is None:
                        continue
                    with self.subTest(driver=d.id, mode=mode, key=s.key, value=v):
                        after = copy_raw(raw)
                        s.assign(after, mode, v)
                        self.assertEqual(s.get(after, mode), v)
                        for u in d.units:
                            if u not in s.units:
                                self.assertEqual(after[u], raw[u], f"wrote to {d.unit_name(u)}, not in its units")

    def test_group_apply_leaves_other_groups_pending(self):
        for d in DRIVERS:
            raw = self.raw(d)
            for mode in modes(d):
                pending = copy_raw(raw)
                edited = {}
                for s in d.settings:
                    if s.group in edited or s.read_only or s.hidden or not s.enabled(raw, mode):
                        continue
                    v = other_value(s, pending, mode)
                    if v is not None:
                        s.assign(pending, mode, v)
                        edited[s.group] = s.key
                for group in edited:
                    with self.subTest(driver=d.id, mode=mode, group=group):
                        target = replay(d.settings, raw, pending, mode, [group])
                        still = {x.group for x in dirty_settings(d.settings, target, pending, mode)}
                        self.assertNotIn(group, still)
                        self.assertEqual(still, set(edited) - {group})

    def test_hardware_check_passes_on_a_fake_mouse(self):
        # What tests/hardware_check.py does on the real mouse, minus the mouse.
        cfg = store.CONFIG_DIR
        for d in DRIVERS:
            raw = self.raw(d)
            for mode in modes(d):
                with self.subTest(driver=d.id, mode=mode), tempfile.TemporaryDirectory() as tmp:
                    store.CONFIG_DIR = tmp
                    try:
                        s = FakeSession(Link(d, "/dev/null", mode), raw)
                        self.assertEqual([r for r in check_session(s) if not r.ok], [])
                    finally:
                        store.CONFIG_DIR = cfg


if __name__ == "__main__":
    unittest.main()
