"""Third sweep: library/settings/shell regressions (offscreen Qt)."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget, QWidget, QLabel
    import ui_constants
    import ui_manage
except ImportError:
    # No usable PyQt: the tests below skip (the class body still has to import).
    QApplication = None
    QWidget = None
    ui_constants = None


_APP = None

if QWidget is not None:
    class Destination(QWidget):
        def __init__(self, name):
            super().__init__()
            self.screen_name = name
else:
    Destination = None


class VolumeSanitizingTests(unittest.TestCase):
    @unittest.skipIf(ui_constants is None, "PyQt is not installed")
    def test_unusable_saved_volumes_fall_back_and_usable_ones_are_clamped(self):
        clean = ui_constants._clean_volume
        for bad in (None, "loud", "", [], {}, float("nan"), True):
            with self.subTest(bad=bad):
                self.assertEqual(clean(bad), 0.4)
        self.assertEqual(clean(7), 1.0)
        self.assertEqual(clean(-3), 0.0)
        self.assertEqual(clean("0.25"), 0.25)
        self.assertEqual(clean(0.6), 0.6)

    @unittest.skipIf(ui_constants is None, "PyQt is not installed")
    def test_saved_music_flag_must_be_a_real_boolean(self):
        self.assertTrue(ui_constants._clean_enabled(None))
        self.assertTrue(ui_constants._clean_enabled("yes"))
        self.assertFalse(ui_constants._clean_enabled(False))


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ManageSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.state = {"steam_root": "/steam"}
        patches = [
            patch.object(ui_manage.ManagementScreen, "_refresh", lambda _self: None),
            patch.object(ui_manage.ManagementCard, "_fetch", lambda _self, _appid: None),
            patch.object(ui_manage.cfg, "get_offline_mode", return_value=False),
            patch.object(ui_manage, "find_steam_root", return_value="/steam"),
            patch.object(ui_manage.cfg, "load", side_effect=lambda: dict(self.state)),
            patch.object(ui_manage.inhibit, "busy", return_value=None),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.busy = self.mocks[-1]
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.library = ui_manage.ManagementScreen(self.stack)
        self.library._startup_checked = True
        self.stack.addWidget(self.library)
        for name in ("SetupScreen", "InstallScreen", "UpdateScreen"):
            self.stack.addWidget(Destination(name))
        self.settings = ui_manage.ConfigureScreen(self.stack)
        self.stack.addWidget(self.settings)
        self.addCleanup(self.stack.close)

    # ── settings read from deckops.json ────────────────────────────────────

    def test_settings_screen_survives_a_damaged_saved_volume(self):
        for bad in (None, "loud", float("nan")):
            with self.subTest(bad=bad), \
                 patch.object(ui_manage.cfg, "get_music_volume", return_value=bad), \
                 patch.object(ui_manage.cfg, "get_music_enabled", return_value="maybe"):
                screen = ui_manage.ConfigureScreen(self.stack)
                self.assertEqual(screen._vol_slider.value(), 40)
                self.assertEqual(screen._vol_label.text(), "40%")
                self.assertTrue(screen._music_on)

    # ── Reset Config and the music/FPS controls ───────────────────────────

    def test_reset_config_resyncs_music_and_fps_controls_with_the_wiped_config(self):
        old = (ui_constants._music_enabled, ui_constants._music_volume)
        self.addCleanup(lambda: setattr(ui_constants, "_music_enabled", old[0]))
        self.addCleanup(lambda: setattr(ui_constants, "_music_volume", old[1]))
        ui_constants._music_enabled = False          # the player had turned music off
        self.settings._music_on = False
        self.settings._vol_slider.setValue(90)
        mute_sync = MagicMock()
        self.stack._sync_mute_button = mute_sync
        self.state.update({"music_enabled": True, "music_volume": 0.7})   # what Reset leaves

        with patch.object(ui_manage.cfg, "reset"), \
             patch.object(ui_manage.cfg, "set_music_volume") as save_volume, \
             patch.object(ui_manage.QTimer, "singleShot"):
            self.settings._reset_deckops()

        self.assertTrue(ui_constants._music_enabled)     # _start_audio would play again
        self.assertAlmostEqual(ui_constants._music_volume, 0.7)
        self.assertEqual(self.settings._vol_slider.value(), 70)
        self.assertEqual(self.settings._vol_label.text(), "70%")
        self.assertEqual(self.settings._music_toggle.text(), "Music: ON")
        self.assertFalse(self.stack._muted)
        mute_sync.assert_called()
        # re-showing the saved value is not a user change: it must not be saved back
        save_volume.assert_not_called()
        self.assertFalse(self.settings._vol_save.isActive())

    def test_reset_config_is_refused_while_a_task_is_running(self):
        self.busy.return_value = "DeckOps is changing Steam settings"
        with patch.object(ui_manage.QMessageBox, "warning") as ask, \
             patch.object(self.settings, "_reset_deckops") as reset:
            self.settings._confirm_reset()
        ask.assert_not_called()
        reset.assert_not_called()
        self.assertIn("changing Steam settings", self.settings.status.text())

    # ── library actions while a Settings task still runs ──────────────────

    def test_library_actions_wait_for_a_running_settings_task(self):
        self.busy.return_value = "DeckOps is changing Steam settings"
        gd = next(g for g in ui_manage.ALL_GAMES if "t5mp" in g["keys"])
        self.library.installed = {"t5mp": {"install_dir": "/g", "source": "steam"}}
        actions = {
            "update all": lambda: self.library._update_all_games(),
            "card update": lambda: self.library._update(gd, ["t5mp"]),
            "offline": lambda: self.library._offline(True),
            "repair offline": lambda: self.library._readd(gd),
            "setup": lambda: self.library._setup(gd),
        }
        with patch.object(ui_manage, "_confirm_game_update") as confirm, \
             patch.object(ui_manage.QMessageBox, "question") as ask, \
             patch.object(ui_manage, "_ask_iw4x_dlc") as dlc, \
             patch.object(ui_manage, "go_to") as go:
            for name, act in actions.items():
                with self.subTest(action=name):
                    self.library._status.setText("")
                    act()
                    self.assertIn("changing Steam settings", self.library._status.text())
        confirm.assert_not_called()
        ask.assert_not_called()
        dlc.assert_not_called()
        go.assert_not_called()

    def test_library_actions_still_run_when_nothing_else_is_busy(self):
        with patch.object(ui_manage.QMessageBox, "question",
                          return_value=ui_manage.QMessageBox.StandardButton.No) as ask:
            self.library._offline(True)
        ask.assert_called_once()

    # ── Check My Setup ────────────────────────────────────────────────────

    def test_fix_with_nothing_to_update_says_so_on_the_settings_screen(self):
        self.library.installed = {"cod4mp": {"install_dir": "/g", "source": "steam"}}
        setup = {"cod4mp": {"client": "cod4x", "source": "steam"}}      # legacy: not updatable
        with patch.object(ui_manage.cfg, "get_setup_games", return_value=setup), \
             patch.object(ui_manage, "_confirm_game_update") as confirm, \
             patch.object(ui_manage, "go_to") as go:
            self.settings._fix_game("cod4mp")
        confirm.assert_not_called()
        go.assert_not_called()
        self.assertIn("No configured modes", self.settings.status.text())

    def test_fix_runs_the_games_update_when_it_applies(self):
        self.library.installed = {"cod4mp": {"install_dir": "/g", "source": "steam"}}
        setup = {"cod4mp": {"client": "cod4r", "source": "steam"}}
        with patch.object(ui_manage.cfg, "get_setup_games", return_value=setup), \
             patch.object(self.library, "_update") as update:
            self.settings._fix_game("cod4mp")
        update.assert_called_once()

    def test_fix_is_refused_while_another_operation_runs(self):
        self.busy.return_value = "DeckOps is installing."
        self.library.installed = {"cod4mp": {"install_dir": "/g", "source": "steam"}}
        setup = {"cod4mp": {"client": "cod4r", "source": "steam"}}
        with patch.object(ui_manage.cfg, "get_setup_games", return_value=setup), \
             patch.object(ui_manage, "_confirm_game_update") as confirm:
            self.settings._fix_game("cod4mp")
        confirm.assert_not_called()
        self.assertIn("installing", self.settings.status.text())

    def test_setup_check_worker_does_not_touch_the_widget_stack(self):
        main = threading.get_ident()
        seen = []
        real = ui_manage.get_screen

        def spy(stack, name):
            seen.append(threading.get_ident())
            return real(stack, name)

        import setup_check
        with patch.object(ui_manage, "get_screen", side_effect=spy), \
             patch.object(setup_check, "check_setup", return_value=[]), \
             patch.object(setup_check, "check_shortcuts", return_value=None), \
             patch.object(ui_manage.cfg, "get_setup_games", return_value={}), \
             patch.object(self.settings, "_show_setup_report"):
            self.library.installed = {}
            self.settings._check_setup()
            for _ in range(200):
                _APP.processEvents()
                if not self.settings._settings_running:
                    break
                threading.Event().wait(0.02)
        self.assertFalse(self.settings._settings_running)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {main})

    # ── relaunch after an update ──────────────────────────────────────────

    def test_failed_relaunch_does_not_leave_the_modal_progress_dialog_up(self):
        pd = MagicMock()
        self.settings._update_pd = pd
        with patch.object(ui_manage.os, "execv", side_effect=OSError("no such python")), \
             patch.object(ui_manage.shutil, "rmtree"):
            self.settings._relaunch()          # must not raise out of the timer slot
        pd.close.assert_called_once()
        self.assertIn("could not restart", self.settings._update_status.text())
        self.assertEqual(self.settings._update_btn.text(), "Restart needed")

    # ── copying the log ───────────────────────────────────────────────────

    def test_copy_log_puts_the_log_on_the_clipboard_even_without_wl_copy_or_xclip(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "install.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write("line one\nline two\n")
            label = QLabel()
            # depot_downgrade.copy_to_clipboard returns quietly when no helper exists
            with patch.object(ui_constants, "LOG_PATH", log), \
                 patch("depot_downgrade.copy_to_clipboard", return_value=None):
                ui_constants._copy_log_to_clipboard(label)
        self.assertIn("line two", QApplication.clipboard().text())
        self.assertIn("copied", label.text())

    def test_copy_log_still_reports_success_when_the_helper_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "install.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write("only line\n")
            label = QLabel()
            with patch.object(ui_constants, "LOG_PATH", log), \
                 patch("depot_downgrade.copy_to_clipboard", side_effect=RuntimeError("boom")):
                ui_constants._copy_log_to_clipboard(label)
        self.assertIn("only line", QApplication.clipboard().text())
        self.assertIn("copied", label.text())


if __name__ == "__main__":
    unittest.main()
