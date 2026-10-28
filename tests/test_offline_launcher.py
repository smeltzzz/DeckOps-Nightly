"""Windows offline-launcher regressions; run with QT_QPA_PLATFORM=offscreen."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    import launcher_plut_win as launcher
    import ui_theme
except ImportError:
    launcher = None


@unittest.skipIf(launcher is None, "PyQt5 is not installed")
class OfflineLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        launcher._load_font()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.intent = self.root / ".lan_intent.json"
        for name, value in (("_LINUX_PROJECT_ROOT", str(self.root)),
                            ("_INTENT_FILE", str(self.intent)),
                            ("_LAN_LOG", str(self.root / "lan.log")),
                            ("_ON_LAUNCH", None), ("_ON_ERROR", None),
                            ("_LAUNCH_FIRED", False)):
            p = patch.object(launcher, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_shared_theme_and_light_actions_have_dark_contrasting_text(self):
        self.assertEqual((launcher.C_BG, launcher.C_CARD, launcher.C_IW,
                          launcher.C_TREY, launcher.C_DIM, launcher.C_DARK_BTN),
                         (ui_theme.C_BG, ui_theme.C_CARD, ui_theme.C_IW,
                          ui_theme.C_TREY, ui_theme.C_DIM, ui_theme.C_DARK_BTN))

        def luminance(value):
            channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                      for c in channels]
            return sum(weight * c for weight, c in zip((0.2126, 0.7152, 0.0722), linear))

        for game in launcher.PLUT_GAMES:
            key = game["modes"][0][0]
            row = launcher.GameRow(game, {key: {"lan_wrapper_path": "/game.sh"}})
            self.addCleanup(row.close)
            button = row._mode_buttons[0][0]
            self.assertIn(f"background:{row._color};color:{ui_theme.C_BG};", button.styleSheet())
            light, dark = sorted((luminance(row._color), luminance(ui_theme.C_BG)), reverse=True)
            self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 4.5)
            row.set_focused_button(0)
            self.assertIn(f"color:{ui_theme.C_BG};", button.styleSheet())

    def test_card_shadows_use_warm_palette_at_existing_opacities(self):
        for alpha in (195, 235, 65):
            shade = launcher._card_shade(alpha)
            self.assertEqual(shade.name(), ui_theme.C_BG.lower())
            self.assertEqual(shade.alpha(), alpha)

    def test_intent_commit_keeps_payload_and_cleans_pending_file(self):
        calls = []
        with patch.object(launcher, "_fadeout_audio"), \
             patch.object(launcher.QTimer, "singleShot") as timer, \
             patch.object(launcher, "_ON_LAUNCH", calls.append):
            self.assertTrue(launcher._launch_lan_lcd("/games/launch-lan.sh", "Game"))
            self.assertFalse(launcher._launch_lan_lcd("/another", "Other"))
            timer.assert_called_once()
        self.assertEqual(json.loads(self.intent.read_text()),
                         {"lan_wrapper_path": "/games/launch-lan.sh"})
        self.assertEqual(calls, ["Game"])
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_failed_replace_stays_and_can_retry_without_false_success(self):
        errors, starts = [], []
        with patch.object(launcher, "_ON_ERROR", errors.append), \
             patch.object(launcher, "_ON_LAUNCH", starts.append), \
             patch.object(launcher, "_fadeout_audio"), \
             patch.object(launcher.QTimer, "singleShot") as timer:
            with patch.object(launcher.os, "replace", side_effect=OSError("disk full")):
                self.assertFalse(launcher._dispatch_launch({"lan_wrapper_path": "/game.sh"}, "Game"))
            self.assertIn("disk full", errors[0])
            self.assertFalse(self.intent.exists())
            self.assertFalse(launcher._LAUNCH_FIRED)
            self.assertEqual(list(self.root.glob("*.tmp")), [])
            self.assertEqual(starts, [])
            timer.assert_not_called()
            self.assertTrue(launcher._dispatch_launch({"lan_wrapper_path": "/game.sh"}, "Game"))
        self.assertEqual(starts, ["Game"])
        self.assertEqual(json.loads(self.intent.read_text())["lan_wrapper_path"], "/game.sh")

    def test_cards_are_reachable_and_missing_art_has_no_network_dependency(self):
        wrappers = {}
        for game in launcher.PLUT_GAMES:
            for key, _ in game["modes"]:
                wrappers[key] = {"lan_wrapper_path": "/game.sh"}
        with patch.object(launcher, "_get_setup_plut_keys", return_value=wrappers), \
             patch.object(launcher, "HEROES_DIR", str(self.root / "no-art")):
            win = launcher.LauncherWindow()
            self.addCleanup(win.close)
            self.assertTrue(all(row._bg_pixmap is None for row in win._focus_rows))
            self.assertEqual(win._focus_rows[0]._mode_buttons[0][0].text(), "Multiplayer")
            self.assertIn("Solo / Zombies", win._focus_rows[0]._mode_buttons[1][0].accessibleName())
            for width, height in ((1280, 800), (800, 600), (640, 480)):
                win.resize(width, height)
                win.show()
                self.app.processEvents()
                self.assertEqual(win._rows_scroll.viewport().width() > 0, True)
                for _ in range(3):
                    QTest.keyClick(win, Qt.Key_Down)
                self.app.processEvents()
                self.assertEqual(win._row_idx, 3)
                self.assertTrue(win._focus_rows[3]._row_focused)
                self.assertGreater(win._rows_scroll.verticalScrollBar().value(), 0 if height < 800 else -1)
                for _ in range(3):
                    QTest.keyClick(win, Qt.Key_Up)
                self.assertEqual(win._row_idx, 0)
                win.hide()

    def test_mode_buttons_keep_their_own_wrapper_and_name(self):
        wrappers = {"t4mp": {"lan_wrapper_path": "/mp.sh"},
                    "t4sp": {"lan_wrapper_path": "/solo.sh"}}
        row = launcher.GameRow(launcher.PLUT_GAMES[0], wrappers)
        self.addCleanup(row.close)
        with patch.object(launcher, "_dispatch_launch") as dispatch:
            row._mode_buttons[0][0].click()
            row._mode_buttons[1][0].click()
        self.assertEqual(dispatch.call_args_list[0].args,
                         (wrappers["t4mp"], "Call of Duty: World at War (Multiplayer)"))
        self.assertEqual(dispatch.call_args_list[1].args,
                         (wrappers["t4sp"], "Call of Duty: World at War (Solo / Zombies)"))

    def test_write_failure_is_visible_and_selection_remains_live(self):
        with patch.object(launcher, "_get_setup_plut_keys", return_value={
                "t6mp": {"lan_wrapper_path": "/game.sh"}}):
            win = launcher.LauncherWindow()
            self.addCleanup(win.close)
            win.resize(800, 600)
            win.show()
            self.app.processEvents()
            with patch.object(launcher.os, "replace", side_effect=OSError("read-only disk")):
                QTest.keyClick(win, Qt.Key_Return)
            self.assertIn("read-only disk", win._status.text())
            self.assertFalse(launcher._LAUNCH_FIRED)
            self.assertFalse(win._starting.isVisible())
            self.assertTrue(win._rows_scroll.isVisible())
            self.assertTrue(win._focus_rows[0]._row_focused)
            win._gamepad.stop()


if __name__ == "__main__":
    unittest.main()
