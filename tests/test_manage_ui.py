"""Game library layout and management preflight regressions (offscreen Qt)."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget, QWidget, QPushButton
    import ui_manage
except ImportError:
    QApplication = None


_APP = None


class Destination(QWidget):
    def __init__(self, name):
        super().__init__()
        self.screen_name = name


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ManagementUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.ready = set()
        patches = [
            patch.object(ui_manage.ManagementScreen, "_refresh", lambda _self: None),
            patch.object(ui_manage.ManagementCard, "_fetch", lambda _self, _appid: None),
            patch.object(ui_manage.cfg, "is_game_setup_for_source",
                         side_effect=lambda k, source: k in self.ready),
            patch.object(ui_manage.cfg, "get_offline_mode", return_value=False),
            patch.object(ui_manage, "find_steam_root", return_value="/steam"),
            patch.object(ui_manage, "_ask_iw4x_dlc", return_value=""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.screen = ui_manage.ManagementScreen(self.stack)
        self.screen._startup_checked = True
        self.stack.addWidget(self.screen)
        for name in ("SetupScreen", "InstallScreen", "ManifestModsScreen", "ConfigureScreen"):
            self.stack.addWidget(Destination(name))
        self.stack.show()
        _APP.processEvents()
        self.addCleanup(self.stack.close)

    @staticmethod
    def buttons(widget):
        return [b for b in widget.findChildren(QPushButton) if b.isEnabled()]

    def cap_settings(self, state):
        """Settings with an in-memory config and a deterministic repair worker."""
        def save(config):
            state.clear()
            state.update(config)

        patches = [
            patch.object(ui_manage.cfg, "load", side_effect=lambda: dict(state)),
            patch.object(ui_manage.cfg, "save", side_effect=save),
            patch.object(ui_manage.cfg, "get_dxvk_frame_cap",
                         side_effect=lambda: bool(state.get("dxvk_frame_cap", True))),
            patch.object(ui_manage.threading, "Thread",
                         side_effect=lambda target, daemon: SimpleNamespace(start=target)),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        settings = ui_manage.ConfigureScreen(self.stack)
        self.stack.addWidget(settings)
        return settings, mocks[1]

    def test_initial_library_geometry_with_app_style(self):
        from ui_constants import _app_style, _load_font
        _load_font()
        old_style = _APP.styleSheet()
        _APP.setStyleSheet(_app_style())
        self.addCleanup(lambda: _APP.setStyleSheet(old_style))

        for width, height, expected_columns in ((1280, 800, 5), (800, 500, 3)):
            with self.subTest(window=f"{width}x{height}"), \
                 patch.object(ui_manage, "_header_path", return_value="/missing/deckops_art.jpg"):
                # A fresh window at each size, not a resized window: this
                # caught the initial zero-height cover in the 1280 screenshot.
                stack = QStackedWidget()
                stack.resize(width, height)
                screen = ui_manage.ManagementScreen(stack)
                screen._startup_checked = True
                stack.addWidget(screen)
                stack.show()
                screen.set_installed({})
                _APP.processEvents()
                self.assertEqual((stack.width(), stack.height()), (width, height))
                self.assertEqual(screen._columns, expected_columns)
                self.assertFalse(stack.grab().isNull())
                output = os.environ.get("DECKOPS_LIBRARY_SNAPSHOT_DIR")
                if output:
                    stack.grab().save(os.path.join(output, f"library_fixed_{width}.png"))
                cards = screen._cards
                self.assertEqual(len(cards), 7)
                for card in cards:
                    with self.subTest(card=card._appid):
                        self.assertEqual(card.width(), ui_manage._LIBRARY_CARD_W)
                        self.assertEqual(card._img_container.height(), ui_manage._LIBRARY_COVER_H)
                        badge_bottom = card._client_badge.mapTo(
                            card, card._client_badge.rect().bottomLeft()).y()
                        cover_bottom = card._img_container.mapTo(
                            card, card._img_container.rect().bottomLeft()).y()
                        title_top = card._title_label.mapTo(
                            card, card._title_label.rect().topLeft()).y()
                        title_bottom = card._title_label.mapTo(
                            card, card._title_label.rect().bottomLeft()).y()
                        status_top = card._state_label.mapTo(
                            card, card._state_label.rect().topLeft()).y()
                        status_bottom = card._state_label.mapTo(
                            card, card._state_label.rect().bottomLeft()).y()
                        summary_top = card._summary_label.mapTo(
                            card, card._summary_label.rect().topLeft()).y()
                        summary_bottom = card._summary_label.mapTo(
                            card, card._summary_label.rect().bottomLeft()).y()
                        actions_top = card._btn_bar.mapTo(
                            card, card._btn_bar.rect().topLeft()).y()
                        self.assertLess(badge_bottom, cover_bottom)
                        self.assertGreater(title_top, cover_bottom, (card.height(), card.sizeHint().height(), card._img_container.geometry(), card._title_label.geometry(), card._btn_bar.geometry()))
                        self.assertGreater(status_top, title_bottom)
                        self.assertGreater(summary_top, status_bottom)
                        self.assertGreaterEqual(actions_top, summary_bottom)
                        if card._appid is not None:
                            self.assertEqual(card._img.text(), "NO ARTWORK\nAVAILABLE")
                        else:
                            art = card._img.pixmap()
                            self.assertIsNotNone(art)
                            self.assertFalse(art.isNull())
                            self.assertEqual((art.width(), art.height()),
                                             (ui_manage._LIBRARY_CARD_W,
                                              ui_manage._LIBRARY_COVER_H))
                for i, card in enumerate(cards):
                    for other in cards[i + 1:]:
                        self.assertFalse(card.geometry().intersects(other.geometry()),
                                         (card._appid, card.geometry(), other._appid, other.geometry()))
                if width == 1280:
                    stack.resize(800, 500)
                    _APP.processEvents()
                    self.assertEqual(screen._columns, 3)
                    self.assertFalse(cards[0].geometry().intersects(cards[3].geometry()))
                    stack.resize(1280, 800)
                    _APP.processEvents()
                    self.assertEqual(screen._columns, 5)
                    self.assertFalse(cards[0].geometry().intersects(cards[5].geometry()))
                stack.close()

    def test_columns_and_actions_fit_both_window_sizes(self):
        self.assertLessEqual(self.stack.width(), 800)
        self.screen.set_installed({})
        _APP.processEvents()
        self.assertEqual(self.screen._columns, 3, (self.stack.width(), self.screen.width(), self.screen._scroll.viewport().width(), self.screen._scroll.widget().width()))
        self.assertEqual(len(self.screen._cards), 7)
        self.assertEqual(self.screen._cards[0]._state_label.text(), "NOT FOUND")
        self.assertIn("Add Games", [b.text() for b in self.buttons(self.screen._cards[0])])
        for button in self.buttons(self.screen):
            if button.parent() == self.screen._cards[0]._btn_bar:
                continue
            self.assertLessEqual(button.mapTo(self.screen, button.rect().bottomRight()).x(),
                                 self.screen.width())
        self.assertLessEqual(self.screen._scroll.widget().width(),
                             self.screen._scroll.viewport().width())
        for card in self.screen._cards:
            with self.subTest(card=card._appid):
                self.assertLessEqual(card._client_badge.width() + 4, card.width())
                for button in card._btn_bar.findChildren(QPushButton):
                    self.assertLessEqual(button.minimumSizeHint().width(), card.width() - 20)
        self.stack.resize(1280, 800)
        _APP.processEvents()
        self.assertEqual(self.screen._columns, 5)
        self.assertLessEqual(self.screen._scroll.widget().width(),
                             self.screen._scroll.viewport().width())
        self.stack.resize(800, 500)
        _APP.processEvents()
        self.assertEqual(self.screen._columns, 3)
        self.assertLessEqual(self.screen._scroll.widget().width(),
                             self.screen._scroll.viewport().width())

    def test_partial_card_sets_up_only_missing_detected_mode(self):
        gd = ui_manage.ALL_GAMES[0]
        self.ready.add("cod4mp")
        self.screen.set_installed({
            "cod4mp": {"source": "steam", "install_dir": "/game"},
            "cod4sp": {"source": "steam", "install_dir": "/game"},
        })
        card = self.screen._cards[0]
        self.assertEqual(card._state_label.text(), "PARTIAL")
        labels = [b.text() for b in self.buttons(card)]
        self.assertIn("Set Up Modes", labels)
        self.assertIn("Game Options", labels)
        for button in self.buttons(card):
            self.assertLessEqual(button.minimumSizeHint().width(), card.width() - 20)
        with patch("preflight.check", return_value=[]), \
             patch.object(ui_manage, "_show_preflight", return_value=True):
            self.assertTrue(self.screen._setup(gd))
        install = ui_manage.get_screen(self.stack, "InstallScreen")
        self.assertEqual([k for k, _, _ in install.steam_selected], ["cod4sp"])
        self.assertEqual(self.stack.currentWidget(), install)
        self.assertIn("cod4mp", self.ready)

    def test_missing_non_steam_mode_uses_own_selection_without_downgrade(self):
        gd = ui_manage.ALL_GAMES[0]
        self.ready.add("cod4mp")
        self.screen.set_installed({
            "cod4mp": {"source": "steam", "install_dir": "/steam/cod4"},
            "cod4sp": {"source": "own", "install_dir": "/games/cod4"},
        })
        with patch("preflight.check", return_value=[]) as preflight_check, \
             patch.object(ui_manage, "_show_preflight", return_value=True):
            self.assertTrue(self.screen._setup(gd))
        install = ui_manage.get_screen(self.stack, "InstallScreen")
        self.assertEqual(list(install.own_selected), ["cod4sp"])
        self.assertEqual(install.steam_selected, [])
        self.assertEqual(preflight_check.call_args.kwargs["downgrade_keys"], [])

    def test_partial_missing_files_offers_add_games_not_reinstall(self):
        self.ready.add("t6zm")
        self.screen.set_installed({"t6zm": {"source": "steam", "install_dir": "/bo2"}})
        card = self.screen._cards[5]
        self.assertEqual(card._state_label.text(), "PARTIAL")
        labels = [b.text() for b in self.buttons(card)]
        self.assertIn("Find Modes", labels)
        self.assertIn("Game Options", labels)
        self.assertNotIn("Set Up", labels)

    def test_cancel_reinstall_keeps_setup_and_install_screen_unchanged(self):
        gd = ui_manage.ALL_GAMES[0]
        self.ready.update(("cod4mp", "cod4sp"))
        self.screen.set_installed({
            "cod4mp": {"source": "steam", "install_dir": "/game"},
            "cod4sp": {"source": "steam", "install_dir": "/game"},
        })
        with patch.object(ui_manage.cfg, "get_setup_games", return_value={
                 "cod4mp": {"client": "cod4x"}}), \
             patch.object(ui_manage.cfg, "unmark_game_setup") as unmark, \
             patch("preflight.check", return_value=[object()]), \
             patch.object(ui_manage, "_show_preflight", return_value=False):
            self.screen._reinstall(gd)
        unmark.assert_not_called()
        self.assertEqual(self.stack.currentWidget(), self.screen)
        self.assertIn("canceled", self.screen._status.text())

    def test_approved_reinstall_commits_only_after_preflight(self):
        gd = ui_manage.ALL_GAMES[0]
        self.ready.update(("cod4mp", "cod4sp"))
        self.screen.set_installed({
            "cod4mp": {"source": "steam", "install_dir": "/game"},
            "cod4sp": {"source": "steam", "install_dir": "/game"},
        })
        self.assertEqual(self.screen._cards[0]._state_label.text(), "READY")
        with patch.object(ui_manage.cfg, "get_setup_games", return_value={
                 "cod4mp": {"client": "cod4x"}}), \
             patch.object(ui_manage.cfg, "unmark_game_setup") as unmark, \
             patch("preflight.check", return_value=[]), \
             patch.object(ui_manage, "_show_preflight", return_value=True):
            self.screen._reinstall(gd)
        unmark.assert_called_once_with(["cod4mp", "cod4sp"])
        install = ui_manage.get_screen(self.stack, "InstallScreen")
        self.assertEqual(install.cod4_client, "cod4x")
        self.assertEqual([k for k, _, _ in install.steam_selected],
                         ["cod4mp", "cod4sp"])

    def test_preflight_error_does_not_clear_setup_or_start_install(self):
        gd = ui_manage.ALL_GAMES[0]
        self.screen.set_installed({"cod4mp": {"source": "steam", "install_dir": "/game"}})
        with patch.object(ui_manage.cfg, "unmark_game_setup") as unmark, \
             patch("preflight.check", side_effect=RuntimeError("space check failed")), \
             patch.object(ui_manage, "_show_preflight") as show:
            self.screen._reinstall(gd)
        unmark.assert_not_called()
        show.assert_not_called()
        self.assertEqual(self.stack.currentWidget(), self.screen)
        self.assertIn("No setup was changed", self.screen._status.text())

    def test_no_modes_to_setup_explains_missing_files(self):
        gd = ui_manage.ALL_GAMES[5]
        self.ready.add("t6zm")
        self.screen.set_installed({"t6zm": {"source": "steam", "install_dir": "/bo2"}})
        self.assertFalse(self.screen._setup(gd))
        self.assertIn("missing game files", self.screen._status.text())

    def test_cancel_downgrade_keeps_receipt_and_setup(self):
        gd = ui_manage.ALL_GAMES[2]
        self.screen.set_installed({"iw5mp": {"source": "steam", "install_dir": "/mw3"}})
        with patch("depot_downgrade.is_downgrade_needed", return_value=True), \
             patch("depot_downgrade.clear_receipt") as clear_receipt, \
             patch.object(ui_manage.cfg, "unmark_game_setup") as unmark, \
             patch("preflight.check", return_value=[object()]), \
             patch.object(ui_manage, "_show_preflight", return_value=False):
            self.screen._depot_downgrade("iw5", gd, ["iw5mp"])
        clear_receipt.assert_not_called()
        unmark.assert_not_called()
        self.assertEqual(self.stack.currentWidget(), self.screen)

    def test_approved_downgrade_clears_receipt_after_preflight(self):
        gd = ui_manage.ALL_GAMES[2]
        self.screen.set_installed({"iw5mp": {"source": "steam", "install_dir": "/mw3"}})
        with patch("depot_downgrade.is_downgrade_needed", return_value=True), \
             patch("depot_downgrade.clear_receipt") as clear_receipt, \
             patch.object(ui_manage.cfg, "unmark_game_setup") as unmark, \
             patch("preflight.check", return_value=[]), \
             patch.object(ui_manage, "_show_preflight", return_value=True):
            self.screen._depot_downgrade("iw5", gd, ["iw5mp"])
        clear_receipt.assert_called_once_with("iw5", "/mw3")
        unmark.assert_called_once_with(["iw5mp"])
        install = ui_manage.get_screen(self.stack, "InstallScreen")
        self.assertEqual(install.force_downgrade_ids, {"iw5"})

    def test_game_options_scroll_with_cancel_and_separated_advanced_actions(self):
        gd = ui_manage.ALL_GAMES[0]
        self.screen.set_installed({"cod4mp": {"source": "steam", "install_dir": "/game"}})
        seen = {}

        def inspect(dialog):
            dialog.show()
            _APP.processEvents()
            seen["buttons"] = [b.text() for b in dialog.findChildren(QPushButton)]
            seen["scroll"] = dialog.findChild(ui_manage.QScrollArea).height()
            seen["height"] = dialog.height()
            dialog.reject()

        with patch.object(ui_manage.QDialog, "exec", inspect):
            self.screen._configure(gd, ["cod4mp"])
        self.assertIn("Open Mod Folders", seen["buttons"])
        self.assertIn("Update Client", seen["buttons"])
        self.assertIn("Reinstall Game...", seen["buttons"])
        self.assertIn("Cancel", seen["buttons"])
        self.assertLess(seen["scroll"], seen["height"])
        self.assertEqual(self.stack.currentWidget(), self.screen)

    def test_settings_reflow_and_danger_zone(self):
        settings = ui_manage.ConfigureScreen(self.stack)
        self.stack.addWidget(settings)
        self.stack.setCurrentWidget(settings)
        self.stack.resize(800, 500)
        _APP.processEvents()
        self.assertLessEqual(self.stack.width(), 800)
        self.assertTrue(settings._settings_narrow)
        self.assertLessEqual(settings._settings_scroll.widget().width(),
                             settings._settings_scroll.viewport().width())
        self.assertEqual(settings._settings_cols.itemAtPosition(1, 0).widget(),
                         settings._right_panel)
        danger = settings.findChild(ui_manage.QFrame, "DangerZone")
        self.assertIsNotNone(danger)
        self.assertIn("Full Uninstall...", [b.text() for b in self.buttons(danger)])
        self.stack.resize(1280, 800)
        _APP.processEvents()
        self.assertFalse(settings._settings_narrow)
        self.assertEqual(settings._settings_cols.itemAtPosition(0, 1).widget(),
                         settings._right_panel)

    def test_cap_toggle_while_settings_busy_leaves_config_and_ui_unchanged(self):
        state = {"dxvk_frame_cap": True, "steam_root": "/steam"}
        settings, save = self.cap_settings(state)
        settings._settings_running = True
        settings._cap_btn.click()
        self.assertTrue(state["dxvk_frame_cap"])
        self.assertIn("On", settings._cap_btn.text())
        self.assertIn("Please wait", settings.status.text())
        save.assert_not_called()

    def test_cap_toggle_repair_failure_restores_config_and_button(self):
        state = {"steam_root": "/steam"}  # default is On, no explicit cap key
        settings, save = self.cap_settings(state)
        with patch("wrapper.kill_steam"), \
             patch("shortcut.repair_shortcuts", side_effect=RuntimeError("shortcuts locked")):
            settings._cap_btn.click()
        self.assertEqual(state, {"steam_root": "/steam"})
        self.assertEqual(save.call_count, 2)
        self.assertIn("On", settings._cap_btn.text())
        self.assertIn("shortcuts locked", settings.status.text())
        self.assertIn("restored", settings.status.text())
        self.assertFalse(settings._settings_running)

    def test_cap_toggle_detects_sp_option_failure_and_rolls_back(self):
        state = {"dxvk_frame_cap": True, "steam_root": "/steam"}
        settings, save = self.cap_settings(state)
        with patch("wrapper.kill_steam"), \
             patch("shortcut.repair_shortcuts", return_value=[]), \
             patch("sp_mod.repair_sp_launch_options", side_effect=OSError("launch option locked")):
            settings._cap_btn.click()
        self.assertTrue(state["dxvk_frame_cap"])
        self.assertIn("On", settings._cap_btn.text())
        self.assertIn("launch option locked", settings.status.text())
        self.assertEqual(save.call_count, 2)
        self.assertFalse(settings._settings_running)

    def test_cap_toggle_worker_start_failure_restores_setting(self):
        state = {"dxvk_frame_cap": True, "steam_root": "/steam"}
        settings, save = self.cap_settings(state)
        with patch.object(ui_manage.threading, "Thread", side_effect=RuntimeError("thread unavailable")):
            settings._cap_btn.click()
        self.assertTrue(state["dxvk_frame_cap"])
        self.assertIn("On", settings._cap_btn.text())
        self.assertIn("thread unavailable", settings.status.text())
        self.assertEqual(save.call_count, 2)
        self.assertFalse(settings._settings_running)

    def test_successful_cap_toggle_and_regular_repair_results(self):
        state = {"dxvk_frame_cap": True, "steam_root": "/steam"}
        settings, save = self.cap_settings(state)
        with patch("wrapper.kill_steam"), \
             patch("shortcut.repair_shortcuts", return_value=[]), \
             patch("sp_mod.repair_sp_launch_options", return_value=[]) as sp_repair:
            settings._cap_btn.click()
        self.assertFalse(state["dxvk_frame_cap"])
        self.assertIn("Off", settings._cap_btn.text())
        self.assertIn("updated", settings.status.text())
        self.assertEqual(save.call_count, 1)
        sp_repair.assert_called_once()
        self.assertFalse(settings._settings_running)

        results = []
        reserve = settings._settings_task
        def capture(s):
            s.done.connect(results.append)
            return reserve(s)
        with patch.object(settings, "_settings_task", side_effect=capture), \
             patch("wrapper.kill_steam"), \
             patch("shortcut.repair_shortcuts", side_effect=RuntimeError("repair failed")):
            settings._repair_shortcuts()
        self.assertEqual(results, [False])
        self.assertIn("Repair failed", settings.status.text())
        self.assertFalse(settings._settings_running)
        with patch("wrapper.kill_steam"), \
             patch("shortcut.repair_shortcuts", return_value=[]):
            settings._repair_shortcuts()
        self.assertIn("All shortcuts present", settings.status.text())
        self.assertFalse(settings._settings_running)

    def test_field_guide_has_scrollable_notes_and_pinned_return_at_both_sizes(self):
        from ui_constants import _app_style, _load_font
        _load_font()
        previous_style = _APP.styleSheet()
        _APP.setStyleSheet(_app_style())
        self.addCleanup(lambda: _APP.setStyleSheet(previous_style))
        setup = {k: {"source": "steam", "client": "plutonium"}
                 for gd in ui_manage.ALL_GAMES for k in gd["keys"]}
        setup["cod4mp"]["client"] = "cod4r"
        setup["iw4mp"]["source"] = "own"
        for width, height in ((800, 500), (1280, 800)):
            with self.subTest(size=(width, height)), \
                 patch.object(ui_manage.cfg, "is_lcd", return_value=True), \
                 patch.object(ui_manage.cfg, "get_setup_games", return_value=setup), \
                 patch.object(ui_manage.cfg, "is_game_setup_for_source", return_value=True), \
                 patch("save_backup.has_backups", return_value=True):
                stack = QStackedWidget(); stack.resize(width, height)
                guide = ui_manage.SetupCompleteScreen(stack)
                guide._from_guide = True
                stack.addWidget(guide)
                destination = Destination("ManagementScreen"); stack.addWidget(destination)
                stack.show(); _APP.processEvents()
                scroll = guide._guide_scroll
                self.assertEqual((stack.width(), stack.height()), (width, height))
                self.assertEqual(guide._heading.text(), "HOW TO PLAY")
                self.assertEqual(guide._continue_btn.text(), "Back to My Games")
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())
                self.assertTrue(guide._restore_btn.isVisible())
                self.assertLess(guide._continue_btn.mapTo(guide,
                                guide._continue_btn.rect().bottomRight()).y(), guide.height())
                self.assertGreaterEqual(guide._continue_btn.mapTo(guide,
                                        guide._continue_btn.rect().topLeft()).y(),
                                        scroll.mapTo(guide, scroll.rect().bottomLeft()).y())
                output = os.environ.get("DECKOPS_LIBRARY_SNAPSHOT_DIR")
                if output:
                    stack.grab().save(os.path.join(output, f"guide_initial_{width}.png"))
                for widget in (guide._restore_btn, guide._mw1waw_body, guide._lcd_body):
                    scroll.ensureWidgetVisible(widget)
                    _APP.processEvents()
                    if isinstance(widget, ui_manage.QLabel):
                        self.assertGreaterEqual(widget.height(), widget.heightForWidth(widget.width()))
                    self.assertGreaterEqual(widget.mapTo(scroll.viewport(),
                                            widget.rect().topLeft()).x(), 0)
                    self.assertLess(widget.mapTo(scroll.viewport(),
                                    widget.rect().bottomRight()).x(), scroll.viewport().width())
                    self.assertLess(widget.mapTo(scroll.viewport(),
                                    widget.rect().topLeft()).y(), scroll.viewport().height())
                self.assertFalse(stack.grab().isNull())
                output = os.environ.get("DECKOPS_LIBRARY_SNAPSHOT_DIR")
                if output:
                    stack.grab().save(os.path.join(output, f"guide_fixed_{width}.png"))
                guide._continue_btn.click()
                self.assertIs(stack.currentWidget(), destination)
                stack.setCurrentWidget(guide)
                _APP.processEvents()
                self.assertEqual(guide._heading.text(), "SETUP COMPLETE")
                self.assertEqual(guide._continue_btn.text(), "Continue to My Games")
                stack.close()

    def test_update_progress_log_and_result_fit_both_sizes(self):
        from ui_constants import _app_style, _load_font
        _load_font()
        previous_style = _APP.styleSheet()
        _APP.setStyleSheet(_app_style())
        self.addCleanup(lambda: _APP.setStyleSheet(previous_style))
        gd = ui_manage.ALL_GAMES[0]
        for width, height in ((800, 500), (1280, 800)):
            with self.subTest(size=(width, height)), \
                 patch.object(ui_manage.QTimer, "singleShot"), \
                 patch.object(ui_manage.inhibit, "start"), \
                 patch.object(ui_manage.inhibit, "stop"), \
                 patch.object(ui_manage, "_log_to_file"):
                stack = QStackedWidget(); stack.resize(width, height)
                update = ui_manage.UpdateScreen(stack)
                update.selected = [("cod4mp", gd, {"install_dir": "/game"})]
                stack.addWidget(update)
                stack.show(); _APP.processEvents()
                update._s.progress.emit(35, "Checking Modern Warfare client and launch options...")
                update._s.log.emit("Checking game files and Steam launch options")
                update._s.plut_wait.emit()
                _APP.processEvents()
                self.assertEqual((stack.width(), stack.height()), (width, height))
                self.assertEqual(update.bar.value(), 35)
                self.assertIn("Checking game files", update.log.toPlainText())
                self.assertTrue(update.steam_btn.isVisible())
                self.assertIn("Close the Plutonium window", update.cur.text())
                self.assertFalse(update.back_btn.isVisible())
                self.assertGreaterEqual(update.log.height(), 100)
                self.assertLess(update.log.mapTo(update, update.log.rect().bottomLeft()).y(),
                                update._footer.mapTo(update, update._footer.rect().topLeft()).y())
                self.assertLess(update._footer.mapTo(update,
                                update._footer.rect().bottomLeft()).y(), update.height())
                self.assertFalse(stack.grab().isNull())
                output = os.environ.get("DECKOPS_LIBRARY_SNAPSHOT_DIR")
                if output:
                    stack.grab().save(os.path.join(output, f"update_progress_{width}.png"))

                update._s.plut_go.emit()
                update._failed_games = [("cod4mp", gd, {})]
                update._on_done(False)
                _APP.processEvents()
                self.assertTrue(update.back_btn.isVisible())
                self.assertTrue(update.retry_btn.isVisible())
                self.assertFalse(update.steam_btn.isVisible())
                self.assertIn("Retry", update.cur.text())
                self.assertLess(update.cur.mapTo(update, update.cur.rect().bottomLeft()).y(),
                                update.bar.mapTo(update, update.bar.rect().topLeft()).y())
                self.assertLess(update.retry_btn.mapTo(update,
                                update.retry_btn.rect().bottomRight()).x(), update.width())
                if output:
                    stack.grab().save(os.path.join(output, f"update_result_{width}.png"))
                update._failed_games = [(k, game, {}) for game in ui_manage.ALL_GAMES[:6]
                                        for k in game["keys"][:1]]
                update._zd_missing = True
                update._on_done(False)
                _APP.processEvents()
                self.assertGreaterEqual(update.log.height(), 100)
                self.assertGreaterEqual(update.cur.height(),
                                        update.cur.heightForWidth(update.cur.width()))
                self.assertLess(update.cur.mapTo(update, update.cur.rect().bottomLeft()).y(),
                                update.bar.mapTo(update, update.bar.rect().topLeft()).y())
                self.assertLess(update._footer.mapTo(update,
                                update._footer.rect().bottomLeft()).y(), update.height())
                self.assertIn("BO2 Options", update.cur.text())
                update.task = ("REPAIR OFFLINE LAUNCHER", lambda sigs: None)
                update._start_update(); _APP.processEvents()
                self.assertEqual(update.title.text(), "REPAIR OFFLINE LAUNCHER")
                self.assertFalse(update.back_btn.isVisible())
                update._s.progress.emit(62, "Rebuilding launcher prefix...")
                update._s.log.emit("Re-adding Steam shortcut")
                _APP.processEvents()
                self.assertEqual(update.bar.value(), 62)
                self.assertIn("Rebuilding launcher prefix", update.cur.text())
                self.assertIn("Re-adding Steam shortcut", update.log.toPlainText())
                update._on_done(True)
                _APP.processEvents()
                self.assertEqual(update.bar.value(), 100)
                self.assertTrue(update.back_btn.isVisible())
                self.assertIn("Complete", update.cur.text())
                self.assertLess(update._footer.mapTo(update,
                                update._footer.rect().bottomLeft()).y(), update.height())
                stack.close()

    def test_add_games_opens_selection_with_existing_sources(self):
        installed = {"t6zm": {"source": "own"}, "t6mp": {"source": "steam"}}
        self.screen.set_installed(installed)
        self.screen._add_games()
        setup = ui_manage.get_screen(self.stack, "SetupScreen")
        self.assertEqual(setup.own_installed, {"t6zm": installed["t6zm"]})
        self.assertEqual(setup.steam_installed, {"t6mp": installed["t6mp"]})
        self.assertEqual(self.stack.currentWidget(), setup)
        self.assertTrue(setup._from_manage)
        self.assertTrue(ui_manage.get_screen(self.stack, "InstallScreen")._return_to_management)


if __name__ == "__main__":
    unittest.main()
