"""The test suite must run (and skip what it cannot run) without PyQt.

Every UI test module wraps its Qt imports in try/except and marks the tests
`skipIf(QApplication is None)`, so a machine or CI image without a working
PyQt still gets a green run of everything else. Two modules built a helper
class at import time from the names that the failed import never bound
(`class Destination(QWidget)`), so the ImportError path raised NameError
instead and the whole run stopped with "ERROR collecting tests/...".

These tests import those modules in a child process where qt and both PyQt
bindings are blocked, which is the same situation as a host without Qt.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SRC = ROOT / "src"

# Modules that import Qt at module scope and must still be importable.
MODULES = ("test_manage_ui", "test_install_ui",
           "test_setup_ui", "test_theme_ui")

_BLOCKER = '''\
"""Stands in for a host without PyQt: every import of a Qt binding fails."""
raise ImportError("PyQt is blocked for this test")
'''


class HeadlessCollectionTests(unittest.TestCase):
    def test_ui_test_modules_import_without_qt(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp)
            (blocked / "qt.py").write_text(_BLOCKER)
            for binding in ("PyQt5", "PyQt6"):
                package = blocked / binding
                package.mkdir()
                (package / "__init__.py").write_text(_BLOCKER)
            env = dict(os.environ)
            # The blocker comes first so `qt` resolves to it, not to src/qt.py.
            env["PYTHONPATH"] = os.pathsep.join(
                [str(blocked), str(TESTS), str(SRC), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
            env["QT_QPA_PLATFORM"] = "offscreen"
            code = textwrap.dedent(
                """
                import importlib
                for name in %r:
                    module = importlib.import_module(name)
                    # The skip marker has to be in place, or every test in the
                    # module would run and fail against a missing QApplication.
                    assert module.QApplication is None, name
                print("ok")
                """
            ) % (sorted(set(MODULES)),)
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
