"""The machine-readable interface (docs/INTERFACE.md) that external tools --
bar widgets, GUIs, scripts -- build on. These pin its shape: change one and
you break someone's widget, so bump mousectl.interface.VERSION instead."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from mousectl import cli, interface
from mousectl.core import store
from mousectl.core.driver import DeviceError, Link, Mouse
from mousectl.drivers import DRIVERS
from mousectl.drivers import attackshark_x11 as x11
from mousectl.drivers import pulsar_x3 as px3
from mousectl.drivers import rapoo_vt3pro as vt3
from tests.test_driver_contract import fixture_path
from tests.test_drivers import FakeSession, FakeX11

X11 = x11.DRIVER


class Dev(FakeSession):
    """A fake mouse behind one link: settings from the driver's fixture, a
    canned battery answer, and optionally a DeviceError on open/battery."""

    def __init__(self, link, bat=None, fail=None, bat_fail=None):
        super().__init__(link, store.load_snapshot(fixture_path(link.driver), link.driver))
        self.bat, self.fail, self.bat_fail = bat, fail, bat_fail

    def __enter__(self):
        if self.fail:
            raise self.fail
        return self

    def battery(self, timeout=None):
        if self.bat_fail:
            raise self.bat_fail
        return self.bat


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(store, "CONFIG_DIR", tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.devs = {}

    def connect(self, drv=X11, **links):
        """connect(dongle=dict(bat=...), wired=dict()) -> the Mouse, links
        in the order given, each backed by a Dev."""
        mouse = Mouse(drv, [])
        for mode, cfg in links.items():
            link = Link(drv, f"/dev/hidraw{len(self.devs)}", mode)
            mouse.links.append(link)
            self.devs[link.path] = Dev(link, **cfg)
        return mouse

    def run_cli(self, argv, mice=()):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(cli, "discover", return_value=list(mice)), \
                contextlib.ExitStack() as stack:
            for d in DRIVERS:
                stack.enter_context(mock.patch.object(d, "session", side_effect=lambda l: self.devs[l.path]))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    cli.main(argv)
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, argv, mice=(), code=0):
        rc, out, err = self.run_cli(argv, mice)
        self.assertEqual(rc, code, err)
        doc = json.loads(out)
        self.assertEqual(doc["interface"], interface.VERSION)
        return doc


class Envelope(Harness):
    def test_every_json_command_is_versioned(self):
        m = self.connect(dongle=dict(bat=(80, "discharging")))
        for argv in (["list", "--json"], ["status", "--json"], ["battery", "--json"],
                     ["keys", "--json"], ["get", "--json", "polling"], ["set", "--json", "polling=500"]):
            with self.subTest(argv=argv):
                self.json_cli(argv, [m])

    def test_no_mouse_is_an_empty_list_not_an_error(self):
        # A bar polls whether or not the dongle is plugged in.
        for argv in (["list", "--json"], ["status", "--json"], ["battery", "--json"], ["keys", "--json"],
                     ["battery", "--json", "-m", "ashark"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.json_cli(argv, []), {"interface": interface.VERSION, "mice": []})

    def test_unknown_mouse_is_still_a_usage_error(self):
        rc, out, err = self.run_cli(["battery", "--json", "-m", "nope"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("unknown mouse", err)


class ListJson(Harness):
    def test_mice_and_their_links(self):
        m = self.connect(wired={}, dongle={})
        self.assertEqual(self.json_cli(["list", "--json"], [m])["mice"], [{
            "mouse": "attackshark-x11", "name": "Attack Shark X11",
            "links": [{"mode": "wired", "path": m.links[0].path},
                      {"mode": "dongle", "path": m.links[1].path}]}])


class BatteryJson(Harness):
    def battery(self, mouse):
        mice = self.json_cli(["battery", "--json"], [mouse])["mice"]
        self.assertEqual(len(mice), 1)
        return mice[0]

    def test_reading(self):
        m = self.connect(dongle=dict(bat=(80, "discharging")))
        self.assertEqual(self.battery(m), {
            "mouse": "attackshark-x11", "name": "Attack Shark X11",
            "link": "dongle", "path": m.links[0].path,
            "battery": {"percent": 80, "state": "discharging"}, "error": None})

    def test_unrecognised_state_reads_unknown(self):
        m = self.connect(dongle=dict(bat=(50, "0x07")))
        self.assertEqual(self.battery(m)["battery"], {"percent": 50, "state": "unknown"})

    def test_asleep_is_an_entry_with_an_error(self):
        m = self.connect(dongle=dict(bat=None))
        b = self.battery(m)
        self.assertIsNone(b["battery"])
        self.assertEqual(b["error"]["code"], "no-answer")
        self.assertTrue(b["error"]["message"])

    def test_device_error_codes_pass_through(self):
        for code in ("permission", "no-answer", "io"):
            with self.subTest(code=code):
                m = self.connect(dongle=dict(fail=DeviceError("boom", code=code)))
                self.assertEqual(self.battery(m)["error"], {"code": code, "message": "boom"})

    def test_wired_without_report_is_no_battery_and_no_error(self):
        m = self.connect(wired=dict(bat=None))
        b = self.battery(m)
        self.assertEqual((b["link"], b["battery"], b["error"]), ("wired", None, None))

    def test_falls_through_to_the_link_that_reports(self):
        m = self.connect(wired=dict(bat=None), dongle=dict(bat=(42, "charging")))
        b = self.battery(m)
        self.assertEqual((b["link"], b["battery"]["percent"], b["error"]), ("dongle", 42, None))

    def test_battery_errors_are_not_on_stderr_in_json_mode(self):
        m = self.connect(dongle=dict(bat_fail=DeviceError("no battery answer", code="no-answer")))
        rc, out, err = self.run_cli(["battery", "--json"], [m])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["mice"][0]["error"]["code"], "no-answer")

    def test_text_mode_still_fails_without_a_reading(self):
        m = self.connect(dongle=dict(bat=None))
        rc, out, err = self.run_cli(["battery"], [m])
        self.assertEqual(rc, 1)
        self.assertIn("no battery report", err)


class StatusJson(Harness):
    def test_settings_and_battery(self):
        m = self.connect(dongle=dict(bat=(80, "charging")))
        e = self.json_cli(["status", "--json"], [m])["mice"][0]
        self.assertEqual(set(e), {"mouse", "name", "link", "path", "settings", "battery", "error"})
        raw = self.devs[m.links[0].path].dev
        self.assertEqual(e["settings"], {s.key: interface.value(s.value(raw, "dongle")) for s in X11.settings})
        self.assertRegex(e["settings"]["light.color"], r"^#[0-9a-f]{6}$")
        self.assertEqual(e["battery"], {"percent": 80, "state": "charging"})
        self.assertEqual((e["link"], e["error"]), ("dongle", None))

    def test_unreadable_mouse_is_listed_with_its_error(self):
        m = self.connect(dongle=dict(fail=DeviceError("no permission", code="permission")))
        rc, out, err = self.run_cli(["status", "--json"], [m])
        self.assertEqual((rc, err), (0, ""))
        e = json.loads(out)["mice"][0]
        self.assertEqual((e["mouse"], e["link"], e["path"], e["settings"], e["battery"]),
                         ("attackshark-x11", None, None, None, None))
        self.assertEqual(e["error"], {"code": "permission", "message": "no permission"})

    def test_battery_failure_does_not_fail_the_settings(self):
        m = self.connect(dongle=dict(bat_fail=DeviceError("asleep", code="no-answer")))
        e = self.json_cli(["status", "--json"], [m])["mice"][0]
        self.assertTrue(e["settings"])
        self.assertIsNone(e["battery"])
        self.assertIsNone(e["error"])

    def test_no_battery_flag(self):
        m = self.connect(dongle=dict(bat=(80, "charging")))
        self.assertIsNone(self.json_cli(["status", "--json", "--no-battery"], [m])["mice"][0]["battery"])


class KeysJson(Harness):
    def test_schema_of_every_driver(self):
        for d in DRIVERS:
            with self.subTest(driver=d.id):
                m = self.connect(d, dongle={})
                e = self.json_cli(["keys", "--json"], [m])["mice"][0]
                self.assertEqual((e["mouse"], e["name"]), (d.id, d.name))
                self.assertEqual([s["key"] for s in e["settings"]], [s.key for s in d.settings])
                for s, j in zip(d.settings, e["settings"]):
                    self.check_setting(s, j)

    def check_setting(self, s, j):
        common = {"key", "label", "group", "kind", "unit", "read_only", "can_disable", "help"}
        self.assertLessEqual(common, set(j))
        self.assertEqual((j["label"], j["group"], j["read_only"], j["can_disable"], j["help"]),
                         (s.label, s.group, s.read_only, s.set_enabled is not None, s.help))
        self.assertEqual(j["unit"], j["unit"].strip())
        if j["kind"] == "number":
            self.assertEqual(j["values"], s.kind.values)
            self.assertEqual((j["min"], j["max"]), (s.kind.lo, s.kind.hi))
        elif j["kind"] == "choice":
            self.assertEqual(j["values"], list(s.kind.values))
        else:
            self.assertIn(j["kind"], ("bool", "color"))

    def test_units_are_trimmed(self):
        m = self.connect(dongle={})
        keys = {j["key"]: j for j in self.json_cli(["keys", "--json"], [m])["mice"][0]["settings"]}
        self.assertEqual(keys["polling"]["unit"], "Hz")
        self.assertEqual(keys["dpi.stage1"]["unit"], "DPI")
        self.assertEqual(keys["light.mode"]["unit"], "")


class GetSetJson(Harness):
    def test_get(self):
        m = self.connect(dongle={})
        raw = self.devs[m.links[0].path].dev
        self.assertEqual(self.json_cli(["get", "--json", "polling", "dpi.active"], [m]), {
            "interface": interface.VERSION, "mouse": "attackshark-x11", "name": "Attack Shark X11",
            "link": "dongle", "path": m.links[0].path, "error": None,
            "settings": {"polling": X11.setting("polling").value(raw, "dongle"),
                         "dpi.active": X11.setting("dpi.active").value(raw, "dongle")}})

    def test_set_reports_values_and_whether_it_wrote(self):
        m = self.connect(dongle={})
        cur = self.json_cli(["get", "--json", "polling"], [m])["settings"]["polling"]
        other = next(v for v in X11.setting("polling").kind.values if v != cur)
        doc = self.json_cli(["set", "--json", f"polling={other}"], [m])
        self.assertEqual((doc["settings"], doc["changed"], doc["error"]), ({"polling": other}, True, None))
        self.assertEqual(self.json_cli(["get", "--json", "polling"], [m])["settings"]["polling"], other)
        self.assertFalse(self.json_cli(["set", "--json", f"polling={other}"], [m])["changed"])

    def test_device_failure_is_a_json_error_and_exit_1(self):
        for argv in (["get", "--json", "polling"], ["set", "--json", "polling=500"]):
            with self.subTest(argv=argv):
                m = self.connect(dongle=dict(fail=DeviceError("asleep", code="no-answer")))
                doc = self.json_cli(argv, [m], code=1)
                self.assertEqual(doc["error"], {"code": "no-answer", "message": "asleep"})
                self.assertIsNone(doc["settings"])

    def test_bad_input_is_a_usage_error(self):
        m = self.connect(dongle={})
        for argv in (["get", "--json", "nope"], ["set", "--json", "polling=7"], ["set", "--json", "nope=1"]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_cli(argv, [m])
                self.assertNotEqual(rc, 0)
                self.assertEqual(out, "")
                self.assertTrue(err)


class Values(unittest.TestCase):
    def test_every_json_value_can_be_passed_back_to_set(self):
        # A tool reads a value from status/get --json and later sends it back
        # as KEY=VALUE: strings as-is, everything else as JSON text.
        for d in DRIVERS:
            raw = store.load_snapshot(fixture_path(d), d)
            mode = sorted(set(d.links.values()))[0]
            for s in d.settings:
                if s.read_only:
                    continue
                v = interface.value(s.value(raw, mode))
                text = v if isinstance(v, str) else json.dumps(v)
                with self.subTest(driver=d.id, key=s.key, text=text):
                    s.assign(raw, mode, text)
                    self.assertEqual(interface.value(s.value(raw, mode)), v)

    def test_color_as_hex(self):
        self.assertEqual(interface.value((255, 136, 0)), "#ff8800")


class DeviceErrors(unittest.TestCase):
    def test_default_code(self):
        self.assertEqual(DeviceError("x").code, "io")
        self.assertEqual(DeviceError("x", code="permission").code, "permission")
        self.assertEqual(str(DeviceError("x", code="permission")), "x")

    def test_codes_are_the_documented_ones(self):
        self.assertEqual(set(interface.ERROR_CODES), {"permission", "no-answer", "io"})

    def test_drivers_flag_permission_denied(self):
        sessions = [x11.X11Session(Link(X11, "/dev/null", "dongle", {"lengths": {}})),
                    vt3.VT3ProSession(Link(vt3.DRIVER, "/dev/null", "dongle")),
                    px3.X3Session(Link(px3.DRIVER, "/dev/null", "dongle"))]
        for s in sessions:
            with self.subTest(driver=s.link.driver.id), \
                    mock.patch("os.open", side_effect=PermissionError(13, "denied")), \
                    self.assertRaises(DeviceError) as cm:
                s.__enter__()
            self.assertEqual(cm.exception.code, "permission")


class BatteryStates(unittest.TestCase):
    def test_vocabulary(self):
        self.assertEqual(set(interface.BATTERY_STATES), {"charging", "discharging", "full", "unknown"})

    def test_x11_states(self):
        for byte, state in ((1, "discharging"), (2, "full"), (3, "charging"), (7, "unknown")):
            with self.subTest(byte=byte):
                s = FakeX11()
                s.drain_events = lambda timeout: [bytes([0x03, 0, 0x40, byte, 77])]
                self.assertEqual(s.battery(), (77, state))

    def test_vt3pro_states(self):
        for byte, state in ((1, "discharging"), (2, "charging"), (7, "unknown")):
            with self.subTest(byte=byte):
                s = vt3.VT3ProSession(Link(vt3.DRIVER, "/dev/null", "dongle"))
                s._battery_resp = lambda: bytes([0, byte, 64])
                self.assertEqual(s.battery(), (64, state))


class Documented(unittest.TestCase):
    def doc(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "INTERFACE.md")) as f:
            return f.read()

    def test_version_codes_and_states_are_documented(self):
        doc = self.doc()
        self.assertIn(f'"interface": {interface.VERSION}', doc)
        for word in (*interface.ERROR_CODES, *interface.BATTERY_STATES):
            self.assertIn(f"`{word}`", doc)

    def test_every_json_command_is_documented(self):
        doc = self.doc()
        for cmd in ("list", "status", "battery", "keys", "get", "set"):
            self.assertIn(f"mousectl {cmd} --json", doc)


if __name__ == "__main__":
    unittest.main()
