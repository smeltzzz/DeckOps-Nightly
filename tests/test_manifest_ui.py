"""Mods navigation stays inside the library after first-run setup."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget, QWidget
    import ui_manifest
except ImportError:
    QApplication = None


_APP = None


class Destination(QWidget):
    def __init__(self, name):
        super().__init__()
        self.screen_name = name


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ManifestNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def _open(self, first_run):
        self.config_patch = patch.object(ui_manifest.cfg, "is_first_run", return_value=first_run)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.scan_patch = patch.object(ui_manifest.ManifestModsScreen, "_scan", lambda screen: None)
        self.scan_patch.start()
        self.addCleanup(self.scan_patch.stop)
        stack = QStackedWidget()
        stack.resize(800, 500)
        stack.addWidget(Destination("SetupFlowScreen"))
        stack.addWidget(Destination("WelcomeScreen"))
        stack.addWidget(Destination("ManagementScreen"))
        mods = ui_manifest.ManifestModsScreen(stack)
        stack.addWidget(mods)
        stack.show()
        stack.setCurrentWidget(mods)
        _APP.processEvents()
        self.addCleanup(stack.close)
        return stack, mods

    def test_library_no_manifests_has_guidance_and_returns_to_library(self):
        stack, mods = self._open(False)
        with patch.object(ui_manifest.mm, "get_saved_cdn", return_value=""):
            mods._on_scanned(False)
        self.assertIs(stack.currentWidget(), mods)
        self.assertEqual(mods.status.text(), "No mod manifests found.")
        self.assertFalse(mods.inst.isEnabled())
        mods.skip.click()
        self.assertEqual(stack.currentWidget().screen_name, "ManagementScreen")

    def test_first_run_no_manifests_skips_to_game_scan(self):
        stack, mods = self._open(True)
        mods._on_scanned(False)
        self.assertEqual(stack.currentWidget().screen_name, "WelcomeScreen")
