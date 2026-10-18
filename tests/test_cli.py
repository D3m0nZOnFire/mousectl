import contextlib
import io
import os
import re
import subprocess
import sys
import unittest

import mousectl
from mousectl.cli import main

ROOT = os.path.dirname(os.path.dirname(__file__))


class VersionTest(unittest.TestCase):
    def test_version_flag_prints_version_and_exits_0(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"mousectl {mousectl.__version__}")

    def test_version_is_semver(self):
        self.assertRegex(mousectl.__version__, r"^\d+\.\d+\.\d+$")

    def test_pyproject_takes_version_from_package(self):
        # One source of truth: the package, which also works from a checkout
        # (bin/mousectl) where no package metadata is installed.
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            toml = f.read()
        self.assertRegex(toml, r'(?m)^dynamic = \["version"\]$')
        self.assertRegex(toml, r'(?m)^version = \{ ?attr = "mousectl.__version__" ?\}$')
        self.assertIsNone(re.search(r'(?m)^version = "', toml))

    def test_launcher_prints_version_from_checkout(self):
        env = dict(os.environ, PYTHON=sys.executable)
        r = subprocess.run([os.path.join(ROOT, "bin", "mousectl"), "--version"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"mousectl {mousectl.__version__}")


if __name__ == "__main__":
    unittest.main()
