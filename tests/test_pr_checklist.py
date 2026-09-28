import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
TEMPLATE = os.path.join(ROOT, ".github", "PULL_REQUEST_TEMPLATE", "new-driver.md")
_spec = importlib.util.spec_from_file_location("pr_checklist", os.path.join(ROOT, ".github", "scripts", "pr_checklist.py"))
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

REPORT = """### Foo Bar (`foo-bar`) over dongle

| check | result | detail |
|---|---|---|
| polling | pass | 500 Hz -> 1000 Hz |
"""


def template():
    with open(TEMPLATE) as f:
        return f.read()


def filled():
    body = template().replace("- [ ]", "- [x]")
    for field in ("Vendor / model", "Driver id / aliases", "USB ids (`lsusb`), one per link",
                  "Firmware version (vendor software, or \"unknown\")",
                  "Protocol source (own USB capture, another project + licence, ...)"):
        body = body.replace(f"- **{field}:**", f"- **{field}:** something")
    return body.replace("<!-- paste the markdown printed by tests/hardware_check.py here -->", REPORT)


class PrChecklist(unittest.TestCase):
    def test_filled_template_passes(self):
        self.assertEqual(pr.problems("Add Foo Bar driver", filled(), []), [])

    def test_empty_template_lists_everything_missing(self):
        errs = "\n".join(pr.problems("Add Foo Bar driver", template(), []))
        self.assertIn("Vendor / model", errs)
        self.assertIn("H4", errs)
        self.assertIn("hardware_check report", errs)

    def test_unticked_box(self):
        body = filled().replace("- [x] H8", "- [ ] H8")
        self.assertEqual(len(pr.problems("Add Foo Bar driver", body, [])), 1)

    def test_failed_hardware_check(self):
        body = filled().replace("| pass |", "| **FAIL** |")
        self.assertTrue(any("FAIL" in e for e in pr.problems("Add Foo Bar driver", body, [])))

    def test_title(self):
        self.assertTrue(pr.problems("new mouse", filled(), []))

    def test_new_driver_file_needs_the_template(self):
        errs = pr.problems("Fix things", "## What and why\nstuff", ["mousectl/drivers/foo_bar.py"])
        self.assertTrue(any("new-driver" in e for e in errs))

    def test_other_prs_are_left_alone(self):
        self.assertEqual(pr.problems("tui: tweak", "## What and why\n- [ ] later", ["mousectl/tui/app.py"]), [])


if __name__ == "__main__":
    unittest.main()
