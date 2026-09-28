import json
import os
import tempfile
import unittest

from mousectl import cli
from mousectl.core import store
from mousectl.core.driver import Link, Session
from mousectl.core.settings import copy_raw, dirty_settings, replay
from mousectl.drivers import attackshark_x11 as x11
from mousectl.drivers import pulsar_x3 as px3
from mousectl.drivers import rapoo_vt3pro as vt3

GOLDEN = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "golden.json")))


def x11_raw():
    r = GOLDEN["attackshark-x11"]["raw"]
    return {x11.RID_DPI: bytearray.fromhex(r["dpi"]), x11.RID_LIGHT: bytearray.fromhex(r["light"]),
            x11.RID_POLL: bytearray.fromhex(r["poll"])}


def vt3_raw():
    return {int(a, 16): bytearray.fromhex(h) for a, h in GOLDEN["rapoo-vt3pro"]["raw"].items()}


def val(drv, raw, key, mode="dongle"):
    return drv.setting(key).get(raw, mode)


class X11Codec(unittest.TestCase):
    def test_grid_matches_golden(self):
        # Byte encoding for every DPI value offered, pinned by the golden fixture.
        golden = GOLDEN["attackshark-x11"]["dpi_roundtrip"]
        self.assertEqual(len(x11.DPI_KIND.values), 330)
        for dpi in x11.DPI_KIND.values:
            self.assertEqual(list(x11.dpi_to_bytes(dpi)), golden[str(dpi)], dpi)

    def test_roundtrip(self):
        for v in x11.DPI_KIND.values:
            self.assertEqual(x11.bytes_to_dpi(*x11.dpi_to_bytes(v)), v)

    def test_off_grid_rejected(self):
        for bad in (10050, 20300, 22100, 26001, 49):
            with self.assertRaises(ValueError):
                x11.DPI_KIND.parse(str(bad))

    def test_device_checksums(self):
        raw = x11_raw()
        self.assertEqual(x11.seal_dpi(bytearray(raw[x11.RID_DPI])), raw[x11.RID_DPI])
        self.assertEqual(x11.seal_light(bytearray(raw[x11.RID_LIGHT])), raw[x11.RID_LIGHT])


class X11Schema(unittest.TestCase):
    def test_decode_matches_golden(self):
        g = GOLDEN["attackshark-x11"]["decoded"]
        raw, d = x11_raw(), x11.DRIVER
        for i in range(8):
            s = d.setting(f"dpi.stage{i + 1}")
            self.assertEqual(s.enabled(raw, "dongle"), bool(g["dpi"]["active_mask"] & (1 << i)))
            self.assertEqual(s.get(raw, "dongle"), g["dpi"]["stages"][i])
            self.assertEqual(list(val(d, raw, f"dpi.stage{i + 1}.color")), g["dpi"]["colors"][i])
        self.assertEqual(val(d, raw, "dpi.active"), g["dpi"]["current_stage"])
        self.assertEqual(val(d, raw, "angle_snap"), g["dpi"]["angle_snap"])
        self.assertEqual(val(d, raw, "ripple"), g["dpi"]["ripple_control"])
        self.assertEqual(val(d, raw, "motion_sync"), g["dpi"]["motion_sync"])
        self.assertEqual(val(d, raw, "lod"), g["dpi"]["lod"])
        self.assertEqual(val(d, raw, "polling"), g["poll"]["hz"])
        light = g["light"]
        for key, gk in (("light.mode", "mode"), ("light.brightness", "brightness"),
                        ("light.speed", "led_speed"), ("debounce", "debounce_ms"),
                        ("sleep", "sleep_min"), ("sleep.deep", "deep_sleep_min")):
            self.assertEqual(val(d, raw, key), light[gk], key)
        self.assertEqual(list(val(d, raw, "light.color")), light["color"])

    def test_set_then_get(self):
        raw, d = x11_raw(), x11.DRIVER
        for key, text, want in (("dpi.stage3", "12000", 12000), ("light.color", "#123456", (0x12, 0x34, 0x56)),
                                ("sleep", "2.5", 2.5), ("sleep.deep", "37", 37), ("polling", "1000", 1000),
                                ("light.mode", "neon", "neon"), ("debounce", "12", 12), ("ripple", "on", True)):
            d.setting(key).assign(raw, "dongle", text)
            self.assertEqual(val(d, raw, key), want, key)

    def test_stage_enable_disable(self):
        raw, d = x11_raw(), x11.DRIVER
        s2, s5 = d.setting("dpi.stage2"), d.setting("dpi.stage5")
        s2.assign(raw, "dongle", "off")                  # the active stage
        self.assertFalse(s2.enabled(raw, "dongle"))
        self.assertEqual(val(d, raw, "dpi.active"), 3)  # moved to the first remaining one
        s5.assign(raw, "dongle", "4000")                 # a value enables a stage
        self.assertTrue(s5.enabled(raw, "dongle"))
        with self.assertRaises(ValueError):
            d.setting("dpi.active").assign(raw, "dongle", "1")   # disabled
        for k in ("dpi.stage3", "dpi.stage4"):
            d.setting(k).assign(raw, "dongle", "off")
        with self.assertRaises(ValueError):
            s5.assign(raw, "dongle", "off")               # last one standing

    def test_enabling_empty_stage_defaults_to_800(self):
        raw, d = x11_raw(), x11.DRIVER
        d.setting("dpi.stage7").set_enabled(raw, "dongle", True)
        self.assertEqual(val(d, raw, "dpi.stage7"), 800)


class VT3Schema(unittest.TestCase):
    def test_decode_matches_golden(self):
        raw, d = vt3_raw(), vt3.DRIVER
        for mode in ("dongle", "wired"):
            g = GOLDEN["rapoo-vt3pro"]["decoded"][mode]
            for i in range(7):
                self.assertEqual(val(d, raw, f"dpi.stage{i + 1}", mode), g["dpi_x"][i])
            for key, gk in (("dpi.active", "active_stage"), ("polling", "polling_hz"),
                            ("polling.wireless", "polling_wireless_hz"), ("polling.wired", "polling_wired_hz"),
                            ("motion_sync", "motion_sync"), ("lod", "lod_index"),
                            ("debounce.press", "debounce_press_ms"), ("debounce.release", "debounce_release_ms"),
                            ("sleep", "sleep_min"), ("angle_snap", "angle_snap"),
                            ("ripple", "ripple_control"), ("sensor_angle", "sensor_angle")):
                self.assertEqual(val(d, raw, key, mode), g[gk], (mode, key))

    def test_polling_writes_the_links_slot(self):
        d = vt3.DRIVER
        for mode, idx in (("dongle", 0), ("wired", 2)):
            raw = vt3_raw()
            d.setting("polling").assign(raw, mode, "4000")
            self.assertEqual(raw[vt3.ADDR_PERF][idx], vt3.POLL_RATES[4000])
            self.assertEqual(raw[vt3.ADDR_PERF][2 - idx], vt3_raw()[vt3.ADDR_PERF][2 - idx])

    def test_stage_sets_x_and_y(self):
        raw, d = vt3_raw(), vt3.DRIVER
        d.setting("dpi.stage2").assign(raw, "dongle", "950")
        self.assertEqual(raw[vt3.ADDR_DPI_X][2:4], raw[vt3.ADDR_DPI_Y][2:4])
        with self.assertRaises(ValueError):
            d.setting("dpi.stage2").assign(raw, "dongle", "975")

    def test_replay_keeps_other_groups_bytes_in_shared_block(self):
        # ripple (Performance) and sleep (Timing) live in the same 0x8C0 block.
        d, device = vt3.DRIVER, vt3_raw()
        pending = copy_raw(device)
        d.setting("ripple").assign(pending, "dongle", "on")
        d.setting("sleep").assign(pending, "dongle", "20")
        target = replay(d.settings, device, pending, "dongle", ["Timing"])
        self.assertEqual(val(d, target, "sleep"), 20)
        self.assertEqual(val(d, target, "ripple"), False)
        self.assertEqual([s.key for s in dirty_settings(d.settings, target, pending, "dongle")], ["ripple"])


def x3_raw():
    return {u: bytearray.fromhex(h) for u, h in GOLDEN["pulsar-x3"]["raw"].items()}


class X3Schema(unittest.TestCase):
    def test_decode_real_dump(self):
        raw, d, g = x3_raw(), px3.DRIVER, GOLDEN["pulsar-x3"]["decoded"]
        for i, dpi in enumerate(g["stages"]):
            self.assertEqual(val(d, raw, f"dpi.stage{i + 1}"), dpi)
        self.assertEqual(val(d, raw, "dpi.active"), g["active"])
        for key in ("lod", "polling", "angle_snap", "ripple", "motion_sync", "debounce", "sleep"):
            self.assertEqual(val(d, raw, key), g[key], key)

    def test_set_then_get(self):
        raw, d = x3_raw(), px3.DRIVER
        for key, text, want in (("dpi.stage2", "950", 950), ("dpi.active", "6", 6), ("polling", "500", 500),
                                ("lod", "0.7", 0.7), ("lod", "2", 2), ("debounce", "0", 0),
                                ("sleep", "2", 2), ("sleep", "1.5", 1.5), ("motion_sync", "on", True)):
            d.setting(key).assign(raw, "dongle", text)
            self.assertEqual(val(d, raw, key), want, key)
        self.assertEqual(raw["polling"], bytearray([0x04]))
        self.assertEqual(raw["sleep"], bytearray((90).to_bytes(2, "little")))

    def test_stage_sets_x_and_y(self):
        raw, d = x3_raw(), px3.DRIVER
        d.setting("dpi.stage4").assign(raw, "dongle", "1230")
        self.assertEqual(raw["dpi"][17:22], bytes([4]) + (1230).to_bytes(2, "little") * 2)

    def test_stages_are_a_list(self):
        raw, d = x3_raw(), px3.DRIVER
        with self.assertRaises(ValueError):
            d.setting("dpi.stage3").assign(raw, "dongle", "off")     # not the last
        d.setting("dpi.stage6").assign(raw, "dongle", "off")
        d.setting("dpi.stage5").assign(raw, "dongle", "off")
        self.assertEqual(raw["dpi"][1], 4)
        with self.assertRaises(ValueError):
            d.setting("dpi.active").assign(raw, "dongle", "5")
        d.setting("dpi.stage6").assign(raw, "dongle", "20000")       # re-enables 5 too
        self.assertEqual(raw["dpi"][1], 6)
        self.assertEqual(val(d, raw, "dpi.stage5"), 6400)             # old value kept

    def test_disabling_active_stage_moves_it(self):
        raw, d = x3_raw(), px3.DRIVER
        d.setting("dpi.active").assign(raw, "dongle", "6")
        d.setting("dpi.stage6").assign(raw, "dongle", "off")
        self.assertEqual(val(d, raw, "dpi.active"), 5)

    def test_frame_checksum(self):
        f = px3.frame(0x05, 0x84, 0x15, 1)
        self.assertEqual(len(f), 64)
        self.assertEqual(f[62:64], (0x05 + 0x84 + 0x15 + 1).to_bytes(2, "little"))


class FakeSession(Session):
    def __init__(self, link, raw):
        super().__init__(link)
        self.dev = copy_raw(raw)
        self.writes = []

    def read(self, units=None):
        return {u: bytearray(self.dev[u]) for u in (units or self.dev)}

    def write(self, before, after):
        changed = [u for u in after if bytes(before[u]) != bytes(after[u])]
        for u in changed:
            self.dev[u] = bytearray(after[u])
        self.writes += changed
        return changed


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cfg, store.CONFIG_DIR = store.CONFIG_DIR, self.tmp.name

    def tearDown(self):
        store.CONFIG_DIR = self._cfg
        self.tmp.cleanup()

    def test_backup_before_first_write_then_restore(self):
        d = x11.DRIVER
        s = FakeSession(Link(d, "/dev/null", "dongle"), x11_raw())
        before = s.read([x11.RID_LIGHT])        # partial read
        after = copy_raw(before)
        d.setting("light.mode").assign(after, "dongle", "static")
        self.assertEqual(store.apply(s, before, after), [x11.RID_LIGHT])
        backup = store.load_snapshot(store.auto_backup_path(d), d)
        self.assertEqual(backup, x11_raw())     # full, untouched device state
        store.apply(s, s.read(), backup)
        self.assertEqual(s.dev, x11_raw())

    def test_rejects_other_drivers_snapshot(self):
        path = os.path.join(self.tmp.name, "v.json")
        store.write_snapshot(path, vt3.DRIVER, "dongle", vt3_raw())
        with self.assertRaises(ValueError):
            store.load_snapshot(path, x11.DRIVER)
        self.assertEqual(store.load_snapshot(path, vt3.DRIVER), vt3_raw())


class InstallUdev(unittest.TestCase):
    def test_rejects_injected_owner(self):
        for owner in ['me", RUN+="/some/script', "no such user", "nosuchuser-mousectl"]:
            with self.assertRaises(SystemExit):
                cli.cmd_install_udev(cli.argparse.Namespace(owner=owner))


class FakeScreen:
    """Just enough of a curses window for the waiting screen."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.text = []

    def getmaxyx(self):
        return 24, 80

    def addstr(self, y, x, text, attr=0):
        self.text.append(text)

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    def erase(self): pass
    def refresh(self): pass
    def timeout(self, ms): pass


class TuiNoMouse(unittest.TestCase):
    def test_tui_opens_without_a_mouse(self):
        from unittest import mock
        with mock.patch.object(cli, "discover", return_value=[]), \
                mock.patch("mousectl.tui.app.run") as run:
            cli.cmd_tui(cli.argparse.Namespace(mouse=None, link=None))
        self.assertEqual(run.call_args.args[0], [])

    def test_waiting_screen_quits_on_q(self):
        from mousectl.tui import app
        scr = FakeScreen([ord("q")])
        self.assertIsNone(app.wait_for_mice(scr, lambda: [], "supported mouse"))
        self.assertTrue(any("No supported mouse connected" in t for t in scr.text), scr.text)

    def test_waiting_screen_returns_once_a_mouse_appears(self):
        from mousectl.tui import app
        scans = iter([[], [], ["x11"]])
        self.assertEqual(app.wait_for_mice(FakeScreen([]), lambda: next(scans), "supported mouse"), ["x11"])


if __name__ == "__main__":
    unittest.main()
