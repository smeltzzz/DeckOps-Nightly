"""Shared type hierarchy and the unobstructed main-window music control."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QFontInfo
    import ui_qt
    import ui_manage
    from ui_constants import _load_font, _app_style, font, get_screen
except ImportError:
    QApplication = None


_APP = None


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ThemeUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])
        _load_font()

    def test_body_font_is_not_the_display_face(self):
        self.assertEqual(QFontInfo(font(11)).family(), "Atkinson Hyperlegible")
        self.assertEqual(QFontInfo(font(22, display=True)).family(), "Russo One")
        self.assertIn("check.svg", _app_style())

    def test_music_controls_stay_in_sync_without_navigation(self):
        state = {"on": True}
        with patch.object(ui_qt.cfg, "get_music_enabled", side_effect=lambda: state["on"]), \
             patch.object(ui_qt, "_set_audio_enabled", side_effect=lambda on: state.__setitem__("on", on)), \
             patch.object(ui_manage, "_set_audio_enabled", side_effect=lambda on: state.__setitem__("on", on)), \
             patch.object(ui_qt, "_kill_audio"), patch.object(ui_manage, "_kill_audio"), \
             patch.object(ui_qt, "_start_audio"), patch.object(ui_manage, "_start_audio"), \
             patch.object(ui_manage.ManagementScreen, "_refresh", lambda *args: None):
            win = ui_qt.DeckOpsWindow()
            self.addCleanup(win.close)
            win.stack.setCurrentWidget(get_screen(win.stack, "ConfigureScreen"))
            self.assertEqual(win._mute_btn.text(), "Music: On")
            win._mute_btn.click()
            settings = get_screen(win.stack, "ConfigureScreen")
            self.assertEqual(settings._music_toggle.text(), "Music: OFF")
            settings._music_toggle.click()
            self.assertEqual(win._mute_btn.text(), "Music: On")
            self.assertIn("color:#10151C", settings._music_toggle.styleSheet())

    def test_music_control_occupies_its_own_strip(self):
        previous_style, previous_font = _APP.styleSheet(), _APP.font()
        self.addCleanup(_APP.setStyleSheet, previous_style)
        self.addCleanup(_APP.setFont, previous_font)
        _APP.setFont(font(11))
        _APP.setStyleSheet(_app_style())
        with patch.object(ui_manage.ManagementCard, "_fetch", lambda *args: None), \
             patch.object(ui_manage.ManagementScreen, "_refresh", lambda *args: None):
            win = ui_qt.DeckOpsWindow()
            self.addCleanup(win.close)
            win.resize(800, 500)
            win.show()
            win.stack.setCurrentWidget(get_screen(win.stack, "SetupScreen"))
            _APP.processEvents()
            self.assertEqual(win._screen_label.text(), "SELECT GAMES")
            self.assertIs(win._mute_btn.parent(), win.statusBar())
            self.assertGreaterEqual(win.statusBar().geometry().top(),
                                    win.stack.geometry().bottom())
            self.assertLessEqual(win._mute_btn.geometry().right(), win.width())
