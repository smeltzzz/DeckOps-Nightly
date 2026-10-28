"""Mods navigation stays inside the library after first-run setup."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget, QWidget, QScrollArea
    import ui_manifest
    import ui_theme
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

    def test_mod_choices_use_warm_palette_and_stay_reachable_at_800x500(self):
        stack, mods = self._open(True)
        mod = {"id": "sample", "name": "Sample Mod", "version": "1", "description": "Details",
               "mode": "options", "options": [{"id": "base", "name": "Base", "description": "Choice",
                                               "files": [{"size": 1024}]}]}
        mods._found = [{"manifest": {**mod, "id": f"sample-{i}"}, "path": f"/games/sample-{i}.json"}
                       for i in range(4)]
        mods._build_roots()
        mods._build_rows()
        _APP.processEvents()
        self.assertEqual(len(mods._rows), 4)
        scroll = mods.findChild(QScrollArea, "")
        self.assertGreater(mods._lw.sizeHint().height(), scroll.viewport().height())
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        for item in mods._rows:
            self.assertGreaterEqual(item["card"].height(), item["card"].minimumSizeHint().height())
        row = mods._rows[0]
        self.assertIn(f"background:{ui_theme.C_CARD}", row["card"].styleSheet())
        option = row["card"].findChild(QWidget, "mmoption")
        self.assertIn(f"background:{ui_theme.C_SURFACE}", option.styleSheet())
        self.assertIn(f"background:{ui_theme.C_CARD}", mods.cdn.styleSheet())
        self.assertIn(f"selection-color:{ui_theme.C_BG}", mods.root_choice.styleSheet())
        row["cb"].setChecked(True)
        self.assertTrue(mods.inst.isEnabled())
        self.assertEqual(mods._selected()[0][1], "base")
        mods._set_state(row, "INSTALLED", ui_theme.C_IW)
        self.assertIn(f"background:{ui_theme.C_IW};color:{ui_theme.C_BG}",
                      row["state"].styleSheet())
        for button in (mods.back, mods.skip, mods.inst):
            corner = button.mapTo(mods, button.rect().bottomRight())
            self.assertLess(corner.x(), mods.width())
            self.assertLess(corner.y(), mods.height())
        self.assertGreater(mods._lw.height(), 0)

    def test_first_run_no_manifests_skips_to_game_scan(self):
        stack, mods = self._open(True)
        mods._on_scanned(False)
        self.assertEqual(stack.currentWidget().screen_name, "WelcomeScreen")
