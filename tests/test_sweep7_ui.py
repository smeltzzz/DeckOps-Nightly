"""Sweep 7: recoverable UI failures, shared busy ownership, and dynamic labels.

All workers, dialogs, Steam calls and config writes are replaced. Screens are
kept offscreen unless a test explicitly needs a visible controller focus.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QCheckBox, QFileDialog, QMessageBox, QPushButton, QStackedWidget
    import ui_constants as uc
    import ui_install
    import ui_manage
except ImportError:
    QApplication = None

_APP = None


class _SyncThread:
    def __init__(self, target=None, args=(), **_kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class _NoSpawn:
    def __init__(self, *_a, **_kw):
        pass

    def start(self):
        raise RuntimeError("no worker threads available")


class _CapturedThread:
    def __init__(self, target=None, args=(), **_kw):
        self.target, self.args = target, args

    def start(self):
        pass


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])
        uc._load_font()

    def setUp(self):
        self.state = {"steam_root": "/steam", "deck_model": "oled", "player_name": "Old",
                      "setup_games": {"cod4mp": {"client": "cod4r", "source": "steam"}}}
        patches = [
            patch.object(ui_manage.cfg, "load", side_effect=lambda: dict(self.state)),
            patch.object(ui_manage.cfg, "save", side_effect=lambda c: self.state.update(c)),
            patch.object(ui_manage.cfg, "is_game_setup_for_source", return_value=False),
            patch.object(ui_manage.ManagementCard, "_fetch", lambda *_a: None),
            patch.object(ui_manage, "_quick_update_check", return_value=None),
            patch.object(ui_manage, "find_steam_root", return_value="/steam"),
            patch.object(ui_install, "find_steam_root", return_value="/steam"),
            patch.object(ui_manage.inhibit, "busy", return_value=None),
            patch.object(ui_manage.inhibit, "start"),
            patch.object(ui_manage.inhibit, "stop"),
            patch.object(ui_install, "_log_to_file"),
            patch.object(ui_manage, "_log_to_file"),
            patch("save_backup.restore_offer_pending", return_value=False),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.busy, self.inh_start, self.inh_stop = mocks[7:10]
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.addCleanup(self.stack.close)

    def add(self, widget):
        self.stack.addWidget(widget)
        return widget

    def management(self):
        library = self.add(ui_manage.ManagementScreen(self.stack))
        library._startup_checked = True
        return library

    def settings(self):
        return self.add(ui_manage.ConfigureScreen(self.stack))

    def guide(self):
        return self.add(ui_manage.SetupCompleteScreen(self.stack))

    def selection(self):
        return self.add(ui_install.SetupScreen(self.stack))

    def install(self):
        return self.add(ui_install.InstallScreen(self.stack))


class SettingsFailureTests(_Base):
    def test_busy_operation_refuses_a_settings_task_without_reserving_it(self):
        s = self.settings()
        self.busy.return_value = "DeckOps is installing."
        with patch.object(ui_manage.threading, "Thread") as thread:
            s._apply_deadzone()
        thread.assert_not_called()
        self.assertFalse(getattr(s, "_settings_running", False))
        self.assertTrue(s.escape_allowed())
        self.assertTrue(all(b.isEnabled() for b in s._task_buttons))
        self.assertIn("installing", s.status.text())
        self.inh_start.assert_not_called()

    def test_worker_spawn_failure_releases_buttons_focus_lock_and_status(self):
        s = self.settings()
        with patch.object(ui_manage.threading, "Thread", _NoSpawn):
            s._apply_deadzone()
        self.assertFalse(s._settings_running)
        self.assertTrue(s.escape_allowed())
        self.assertTrue(all(b.isEnabled() for b in s._task_buttons))
        self.assertIn("Could not start task", s.status.text())
        self.inh_start.assert_called_once()
        self.inh_stop.assert_called_once()

    def test_name_save_failure_does_not_rename_game_configs_or_report_success(self):
        s = self.settings()
        s._name_input.setText("New Name")
        with patch.object(ui_manage.cfg, "set_player_name", side_effect=OSError("read-only")), \
             patch("game_config.rename_player") as rename, \
             patch.object(ui_manage.threading, "Thread") as thread:
            s._save_name()
        rename.assert_not_called()
        thread.assert_not_called()
        self.assertIn("Could not save player name", s.status.text())
        self.assertIn("read-only", s.status.text())
        self.assertFalse(s._settings_running)
        self.inh_stop.assert_called_once()

    def test_name_worker_spawn_failure_releases_the_reserved_task(self):
        s = self.settings()
        s._name_input.setText("New Name")
        with patch.object(ui_manage.cfg, "set_player_name") as save, \
             patch.object(ui_manage.threading, "Thread", _NoSpawn):
            s._save_name()
        save.assert_called_once_with("New Name")
        self.assertEqual(s._name_input.text(), "New Name")
        self.assertFalse(s._settings_running)
        self.inh_stop.assert_called_once()


class RestoreAndResetTests(_Base):
    def test_restore_refuses_to_share_prefixes_with_another_operation(self):
        g = self.guide()
        self.busy.return_value = "DeckOps is updating."
        with patch.object(ui_manage.threading, "Thread") as thread:
            g._do_restore()
        thread.assert_not_called()
        self.assertFalse(getattr(g, "_restoring", False))
        self.assertTrue(g._continue_btn.isEnabled())
        self.assertIn("updating", g._restore_status.text())
        self.inh_start.assert_not_called()

    def test_restore_spawn_failure_keeps_the_offer_and_releases_the_lock(self):
        g = self.guide()
        with patch.object(ui_manage.threading, "Thread", _NoSpawn), \
             patch.object(g, "_mark_offer_handled") as handled:
            g._do_restore()
        handled.assert_not_called()
        self.assertFalse(g._restoring)
        self.assertTrue(g._restore_btn.isEnabled())
        self.assertTrue(g._restore_skip.isEnabled())
        self.assertTrue(g._continue_btn.isEnabled())
        self.assertIn("failed to start", g._restore_status.text())
        self.inh_stop.assert_called_once()

    def test_successful_restore_hides_the_whole_section_not_just_its_actions(self):
        g = self.guide()
        for w in g._restore_widgets:
            w.setVisible(True)
        with patch.object(g, "_mark_offer_handled"):
            g._restore_finished("DONE|Restored saves.")
        self.assertTrue(all(not w.isVisibleTo(g) for w in g._restore_widgets))
        self.assertTrue(g._restore_status.isVisibleTo(g))

    def test_failed_reset_does_not_restart_setup_or_claim_success(self):
        s = self.settings()
        with patch.object(ui_manage.cfg, "reset", side_effect=OSError("permission denied")), \
             patch.object(ui_manage.QTimer, "singleShot") as timer, \
             patch.object(s, "_reload_saved_settings") as reload:
            s._reset_deckops()
        timer.assert_not_called()
        reload.assert_not_called()
        self.assertIn("Could not reset config", s.status.text())
        self.assertIn("permission denied", s.status.text())


class UpdateCheckTests(_Base):
    def test_repeated_check_clicks_start_one_worker_until_its_result_arrives(self):
        s = self.settings()
        with patch.object(ui_manage.threading, "Thread", wraps=_CapturedThread) as thread:
            s._update_btn.click()
            s._update_btn.click()
            self.assertEqual(thread.call_count, 1)
            self.assertFalse(s._update_btn.isEnabled())
            s._on_update_signal("FAIL|Offline")
            self.assertTrue(s._update_btn.isEnabled())
            s._update_btn.click()
            self.assertEqual(thread.call_count, 2)

    def test_failed_recheck_cannot_apply_the_previously_advertised_update(self):
        s = self.settings()
        with patch.object(s, "_apply_update") as apply, patch.object(s, "_check_for_updates") as check:
            s._update_result("Update available", True)
            self.assertEqual(s._update_btn.accessibleName(), "Update & Restart")
            s._update_result("Update check failed. Offline.", False)
            s._update_btn.click()
        apply.assert_not_called()
        check.assert_called_once()
        self.assertEqual(s._update_btn.text(), "Check for Updates")
        self.assertEqual(s._update_btn.accessibleName(), "Check for Updates")

    def test_check_spawn_failure_is_retryable(self):
        s = self.settings()
        with patch.object(ui_manage.threading, "Thread", _NoSpawn):
            s._check_for_updates()
        self.assertFalse(s._checking_update)
        self.assertTrue(s._update_btn.isEnabled())
        self.assertIn("Update check failed", s._update_status.text())
        with patch.object(ui_manage.threading, "Thread", wraps=_CapturedThread) as thread:
            s._update_btn.click()
        thread.assert_called_once()


class WorkerStartTests(_Base):
    def test_library_scan_spawn_failure_leaves_a_visible_retry(self):
        library = self.management()
        with patch.object(ui_manage.threading, "Thread", _NoSpawn):
            library._refresh()
        self.assertFalse(library._scanning)
        self.assertTrue(library._game_update_btn.isEnabled())
        self.assertTrue(library._scan_again_btn.isVisibleTo(library))
        self.assertIn("Couldn't scan", library._status.text())
        self.assertNotEqual(library._status.text(), "Loading games...")

    def test_deadzone_pass_owns_the_busy_lock_until_it_finishes(self):
        library = self.management()
        library._scanned = {"cod4mp": {"source": "steam", "install_dir": "/games/cod4"}}
        with patch.object(ui_manage.threading, "Thread", _SyncThread), \
             patch("game_config.set_deadzone") as deadzone:
            library._on_scanned(True)
        deadzone.assert_called_once()
        self.assertIn("deadzones", self.inh_start.call_args.args[0])
        self.inh_stop.assert_called_once()
        self.assertFalse(library._deadzone_running)

    def test_deadzone_spawn_failure_releases_its_lock(self):
        library = self.management()
        library._scanned = {"cod4mp": {"source": "steam", "install_dir": "/games/cod4"}}
        with patch.object(ui_manage.threading, "Thread", _NoSpawn):
            library._on_scanned(True)
        self.assertFalse(library._deadzone_running)
        self.inh_stop.assert_called_once()

    def test_update_screen_ignores_a_second_start_and_recovers_from_spawn_failure(self):
        update = self.add(ui_manage.UpdateScreen(self.stack))
        update.selected = [("cod4mp", uc.ALL_GAMES[0], {"source": "steam"})]
        with patch.object(ui_manage.QTimer, "singleShot") as timer:
            update._start_update()
            update._append_log("First attempt")
            update._start_update()
        timer.assert_called_once()
        self.assertIn("First attempt", update.log.toPlainText())
        with patch.object(ui_manage.threading, "Thread", _NoSpawn):
            update._start_run_thread()
        self.assertFalse(update._running)
        self.assertTrue(update.escape_allowed())
        self.assertEqual(update._failed_games, update.selected)
        self.assertTrue(update.retry_btn.isVisibleTo(update))
        self.inh_stop.assert_called_once()

    def test_install_spawn_failure_finishes_with_back_and_no_false_success(self):
        screen = self.install()
        with patch.object(ui_install.QTimer, "singleShot"):
            screen._selection_entry = True
            screen._reset_and_run()
        with patch.object(ui_install.threading, "Thread", _NoSpawn), \
             patch.object(screen, "_run") as run:
            screen._start_run_thread()
        run.assert_not_called()
        self.assertFalse(screen._running)
        self.assertTrue(screen.escape_allowed())
        self.assertTrue(screen.cont_btn.isVisibleTo(screen))
        self.assertFalse(screen._last_complete)
        self.assertIn("Could not start setup", screen.log.toPlainText())
        self.inh_stop.assert_called_once()

    def test_install_second_start_does_not_clear_the_running_screens_results(self):
        screen = self.install()
        with patch.object(ui_install.QTimer, "singleShot") as timer:
            screen._reset_and_run()
            results = screen._results
            screen._reset_and_run()
        timer.assert_called_once()
        self.assertIs(screen._results, results)


class SelectionFailureTests(_Base):
    def test_game_scan_spawn_failure_keeps_scan_again_available(self):
        s = self.selection()
        with patch.object(ui_install.threading, "Thread", _NoSpawn):
            s._start_scan()
        self.assertFalse(s._scanning)
        self.assertTrue(s._scan_error)
        self.assertTrue(s.rescan_btn.isVisibleTo(s))
        self.assertTrue(s.scan_bar.isHidden())
        self.assertIn("couldn't finish", s.scan_status.text())

    def test_folder_scan_spawn_failure_restores_actions_and_keeps_the_list(self):
        s = self.selection()
        s.own_installed = {"cod4mp": {"source": "own", "install_dir": "/games/cod4"}}
        before = dict(s.own_installed)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(QFileDialog, "exec", return_value=1), \
             patch.object(QFileDialog, "selectedFiles", return_value=[folder]), \
             patch.object(ui_install.threading, "Thread", _NoSpawn):
            s._pick_folder()
        self.assertEqual(s.own_installed, before)
        self.assertTrue(s._folder_btn.isEnabled())
        self.assertTrue(s.inst_btn.isEnabled())
        self.assertIn("list is unchanged", s.warning.text())

    def test_install_refuses_to_start_over_another_busy_operation(self):
        s = self.selection()
        gd = uc.ALL_GAMES[0]
        key = gd["keys"][0]
        cb = QCheckBox(s)
        cb.setChecked(True)
        s._checks[key] = (cb, gd, "steam")
        s.steam_installed = {key: {"source": "steam", "install_dir": "/games/cod4"}}
        self.busy.return_value = "DeckOps is changing Steam settings."
        with patch.object(s, "_preflight_ok") as preflight:
            s._go_install()
        preflight.assert_not_called()
        self.assertIn("changing Steam settings", s.warning.text())


class DialogAndLabelTests(_Base):
    def test_removing_offline_mode_defaults_to_no(self):
        library = self.management()
        with patch.object(ui_manage.QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as ask:
            library._offline(False)
            library._offline(True)
        self.assertEqual(ask.call_args_list[0].args[4], QMessageBox.StandardButton.No)
        self.assertEqual(ask.call_args_list[1].args[4], QMessageBox.StandardButton.Yes)

    def test_mw3_verify_dialog_failure_is_logged_without_launching_steam(self):
        screen = self.install()
        with patch.object(ui_install.QMessageBox, "exec", side_effect=RuntimeError("dialog failed")), \
             patch("wrapper.launch_steam") as launch:
            screen._ask_iw5_verify()
        launch.assert_not_called()
        self.assertTrue(any("verify dialog failed" in str(c) for c in ui_install._log_to_file.call_args_list))

    def test_mw3_verify_accept_launches_the_campaign_validation_once(self):
        screen = self.install()
        def clicked(box):
            return next(b for b in box.findChildren(QPushButton) if b.text() == "Verify MW3 Now")
        with patch.object(ui_install.QMessageBox, "exec", return_value=0), \
             patch.object(ui_install.QMessageBox, "clickedButton", clicked), \
             patch("wrapper.launch_steam") as launch:
            screen._ask_iw5_verify()
        launch.assert_called_once_with("steam://validate/42680")

    def test_dynamic_button_labels_keep_their_accessible_names_current(self):
        s = self.settings()
        s._refresh_cap_btn()
        s._sync_music_toggle()
        for b in (s._cap_btn, s._music_toggle):
            self.assertEqual(b.accessibleName(), b.text())
        screen = self.install()
        screen._toggle_details()
        self.assertEqual(screen.details_btn.accessibleName(), "Hide details")
        screen._toggle_details()
        self.assertEqual(screen.details_btn.accessibleName(), "Show details")

    def test_update_notice_and_self_update_states_keep_accessible_names_current(self):
        library = self.management()
        library._on_startup_update_check(True)
        self.assertEqual(library._deckops_update_btn.accessibleName(), "Update Available")
        library._clear_update_notice()
        self.assertEqual(library._deckops_update_btn.accessibleName(), "DeckOps Update")
        s = self.settings()
        with patch.object(ui_manage.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(ui_manage.threading, "Thread", _CapturedThread):
            s._apply_update()
        self.assertEqual(s._update_btn.accessibleName(), "Updating")
        s._on_apply_signal("FAIL|Already up to date.")
        self.assertEqual(s._update_btn.accessibleName(), "Check for Updates")
        s._update_btn.setEnabled(False)
        with patch.object(ui_manage.os, "execv", side_effect=OSError("exec failed")):
            s._relaunch()
        self.assertEqual(s._update_btn.accessibleName(), "Restart needed")


if __name__ == "__main__":
    unittest.main()
