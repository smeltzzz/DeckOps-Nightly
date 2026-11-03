"""Install selection and completion UX regressions (offscreen, no worker runs)."""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import guide_text

try:
    from qt import QApplication, QStackedWidget, QWidget, QPushButton, QLabel, QCheckBox
    import ui_install
    import ui_theme
    import ui_constants
    import ui_qt
except ImportError:
    QApplication = None


class Settings:
    def __init__(self):
        self.completed = []
        self.setup_for_source = set()

    def is_lcd(self):
        return False

    def is_first_run(self):
        return not self.completed

    def complete_first_run(self, root):
        self.completed.append(root)

    def is_game_setup_for_source(self, key, source):
        return (key, source) in self.setup_for_source

    def get_offline_mode(self):
        return False



_APP = None


class MW1GuideTextTests(unittest.TestCase):
    def test_new_cod4r_uses_main_menu(self):
        text = guide_text.how_to_start_text({"cod4mp": {"client": "cod4r"}}, lcd=False)
        self.assertIn("starts CoD4R", text)
        self.assertNotIn("CoD4x", text)

    def test_legacy_cod4x_keeps_separate_shortcut_advice(self):
        text = guide_text.how_to_start_text({"cod4mp": {"client": "cod4x"}}, lcd=False)
        self.assertIn("Multiplayer (CoD4x) is the separate DeckOps entry", text)

    def test_non_steam_and_unknown_clients_do_not_claim_cod4x(self):
        for entry in ({"client": "cod4r", "source": "own"}, {"client": "cod4x", "source": "own"}, {}):
            with self.subTest(entry=entry):
                text = guide_text.how_to_start_text({"cod4mp": entry}, lcd=False)
                self.assertNotIn("Multiplayer (CoD4x)", text)


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class InstallUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = Settings()
        for target in (ui_install, ui_constants):
            p = patch.object(target, "cfg", self.settings)
            p.start(); self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.placeholder = QWidget()
        self.stack.addWidget(self.placeholder)
        self.setup = ui_install.SetupScreen(self.stack)
        self.stack.addWidget(self.setup)
        self.install = ui_install.InstallScreen(self.stack)
        self.stack.addWidget(self.install)
        self.stack.show()
        self.stack.setCurrentWidget(self.setup)
        _APP.processEvents()
        self.addCleanup(self.stack.close)

    @staticmethod
    def game(name):
        return {"name": name, "install_dir": "/games/example", "source": "steam"}

    def test_compact_list_full_labels_and_reachable_navigation(self):
        self.setup.steam_installed = {
            "t4mp": self.game("Call of Duty: World at War - Multiplayer"),
            "t4sp": self.game("Call of Duty: World at War"),
        }
        self.setup._build()
        _APP.processEvents()
        self.assertGreater(self.setup.scroll.viewport().height(), 100)
        self.assertGreater(self.setup._lw.sizeHint().height(), self.setup.scroll.viewport().height())
        self.assertEqual(self.setup.scroll.horizontalScrollBar().maximum(), 0)
        for button in (self.setup._folder_btn, self.setup.inst_btn):
            self.assertLess(button.mapTo(self.setup, button.rect().bottomRight()).x(), self.setup.width())
            self.assertLess(button.mapTo(self.setup, button.rect().bottomRight()).y(), self.setup.height())
        mp = self.setup._checks["t4mp"][0]
        self.assertEqual(mp.text(), "Multiplayer")
        self.assertIn("World at War · Multiplayer", mp.accessibleName())
        self.assertEqual(self.setup._checks["t4sp"][0].text(), "Solo / Zombies")
        self.assertFalse(self.setup._checks["t6mp"][0].isEnabled())
        self.assertIn("Black Ops II · Multiplayer", self.setup._checks["t6mp"][0].accessibleName())

    def test_already_configured_modes_are_shown_but_not_selectable(self):
        self.settings.setup_for_source.add(("t4mp", "steam"))
        self.setup.steam_installed = {
            "t4mp": self.game("World at War - Multiplayer"),
            "t4sp": self.game("World at War"),
        }
        self.setup._build()
        mp = self.setup._checks["t4mp"][0]
        self.assertFalse(mp.isEnabled())
        self.assertFalse(mp.isChecked())
        self.assertIn("set up", mp.text())
        self.assertIn("Already set up", mp.accessibleName())
        # The tunable mode next to it still works, and Select All skips the
        # configured one instead of quietly re-queuing it.
        sp = self.setup._checks["t4sp"][0]
        self.assertTrue(sp.isEnabled())
        self.setup._set_all(True)
        self.assertTrue(sp.isChecked())
        self.assertFalse(mp.isChecked())
        self.setup._set_all(False)
        self.assertFalse(sp.isChecked())

    def test_configured_only_selection_never_reaches_the_installer(self):
        self.settings.setup_for_source.add(("t4mp", "steam"))
        self.setup.steam_installed = {"t4mp": self.game("World at War - Multiplayer")}
        self.setup._build()
        mp = self.setup._checks["t4mp"][0]
        # Even a stale forced tick cannot start a second setup of a configured game.
        mp.setChecked(True)
        with patch.object(self.setup, "_preflight_ok") as preflight:
            self.setup._go_install()
        preflight.assert_not_called()
        self.assertIn("Select at least one game", self.setup.warning.text())
        self.assertIs(self.stack.currentWidget(), self.setup)
        self.assertIsNone(self.install.steam_selected)

    def test_configured_game_does_not_block_setting_up_a_fresh_one(self):
        self.settings.setup_for_source.add(("t4mp", "steam"))
        self.setup.steam_installed = {
            "t4mp": self.game("World at War - Multiplayer"),
            "t4sp": self.game("World at War"),
        }
        self.setup._build()
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=True), \
             patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.setup._go_install()
        self.assertEqual([k for k, _, _ in self.install.steam_selected], ["t4sp"])
        self.assertIs(self.stack.currentWidget(), self.install)

    def test_warm_selection_and_install_status_styles(self):
        self.setup.steam_installed = {"cod4mp": self.game("CoD4 - Multiplayer")}
        self.setup._build()
        self.assertIn(f"background:{ui_theme.C_CARD}",
                      self.setup._lw.findChildren(QWidget, "gameRow")[0].styleSheet())
        buttons = {b.text(): b for b in self.setup.findChildren(QPushButton)}
        self.assertNotIn("CoD4x", buttons)
        self.assertNotIn("CoD4R (recommended)", buttons)
        self.assertEqual(self.setup._checks["cod4mp"][0].text(), "Multiplayer")
        self.assertTrue(any("cod4r" in label.text() for label in
                            self.setup._lw.findChildren(QWidget, "gameRow")[0].findChildren(QLabel)))
        self.assertIn(f"color:{ui_theme.C_DIM}", self.install.log.styleSheet())
        self.assertIn(f"background:{ui_theme.C_SURFACE}", self.install.summary.styleSheet())
        self.assertIn(f"color:{ui_theme.C_TEXT}", self.install.summary.styleSheet())
        self.assertIn(f"background:{ui_theme.C_SURFACE}", self.install.plut_warn.styleSheet())
        self.assertIn(f"color:{ui_theme.C_BG}", self.install.plut_btn.styleSheet())

    def test_new_cod4_mp_setup_only_requests_cod4r(self):
        self.install.cod4_client = "cod4x"  # stale value from an older management run
        self.setup.steam_installed = {"cod4mp": self.game("CoD4 - Multiplayer")}
        self.setup._build()
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=True), \
             patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.setup._go_install()
        self.assertEqual([k for k, _, _ in self.install.steam_selected], ["cod4mp"])
        self.assertEqual(self.install.cod4_client, "cod4r")
        self.assertIs(self.stack.currentWidget(), self.install)

    def test_preflight_exception_blocks_first_run_install(self):
        selected = [("t4mp", ui_install.ALL_GAMES[0], self.game("WaW MP"))]
        with patch("preflight.check", side_effect=OSError("disk unavailable")), \
             patch.object(ui_install.QMessageBox, "warning") as warn:
            self.assertFalse(self.setup._preflight_ok(selected, {}))
        warn.assert_called_once()

    def test_choices_survive_rescan_and_rebuild(self):
        self.setup.steam_installed = {"t4mp": self.game("World at War - Multiplayer")}
        self.setup._build()
        self.setup._checks["t4mp"][0].setChecked(False)
        self.setup._lan_cb.setChecked(True)
        self.setup._scanned_own = {"cod4mp": self.game("CoD4 - Multiplayer")}
        self.setup._on_folder_scanned(True)
        self.assertFalse(self.setup._checks["t4mp"][0].isChecked())
        self.assertTrue(self.setup._checks["cod4mp"][0].isChecked())
        self.assertTrue(self.setup._lan_cb.isChecked())
        self.setup._checks["cod4mp"][0].setChecked(False)
        self.setup._scanned_own = {
            "cod4mp": self.game("CoD4 - Multiplayer"),
            "iw4sp": self.game("MW2 - Singleplayer"),
        }
        self.setup._on_folder_scanned(True)
        self.assertIn("cod4mp", self.setup.own_installed)
        self.setup._build()  # same rebuild as Back -> Continue
        self.assertFalse(self.setup._checks["cod4mp"][0].isChecked())
        self.assertFalse(self.setup._checks["t4mp"][0].isChecked())
        self.assertTrue(self.setup._lan_cb.isChecked())

    def test_welcome_scan_shows_full_modes_and_safe_custom_folder_path(self):
        welcome = ui_install.WelcomeScreen(self.stack)
        self.stack.addWidget(welcome)
        with patch.object(welcome, "_start_scan"):
            self.stack.setCurrentWidget(welcome)
        welcome._merged = {"t4mp": self.game("World at War - Multiplayer")}
        welcome._on_scanned(True)
        _APP.processEvents()
        self.assertIn("Call of Duty: World at War · Multiplayer · Steam", welcome.results.text())
        self.assertLess(welcome.cont.mapTo(welcome, welcome.cont.rect().bottomRight()).y(), welcome.height())
        welcome._merged = {}
        welcome._on_scanned(True)
        self.assertIn("Choose Folder", welcome.cont.text())
        self.assertFalse(welcome.cont.isHidden())

    def screen(self, name):
        widget = QWidget()
        widget.screen_name = name
        self.stack.addWidget(widget)
        return widget

    def test_back_uses_entry_screen_without_looping(self):
        welcome = self.screen("WelcomeScreen")
        manage = self.screen("ManagementScreen")
        self.setup._go_back()
        self.assertIs(self.stack.currentWidget(), welcome)
        # ManagementScreen._add_games sets this flag before opening SetupScreen.
        self.setup._from_manage = True
        self.stack.setCurrentWidget(self.setup)
        self.setup.back.click()
        self.assertIs(self.stack.currentWidget(), manage)
        self.stack.removeWidget(welcome)
        # First-run entry from Welcome explicitly resets a prior management flag.
        first_run = ui_install.WelcomeScreen(self.stack)
        self.stack.addWidget(first_run)
        with patch.object(first_run, "_start_scan"):
            self.stack.setCurrentWidget(first_run)
        first_run.steam_installed = {}
        first_run.own_installed = {}
        first_run._go_next()
        self.assertFalse(self.setup._from_manage)
        self.assertIs(self.stack.currentWidget(), self.setup)
        self.setup.back.click()
        self.assertIs(self.stack.currentWidget(), first_run)

    def test_management_add_games_returns_to_selection_on_failure_and_manage_on_success(self):
        self.settings.completed.append("already complete")
        manage = self.screen("ManagementScreen")
        self.setup._from_manage = True
        self.setup.steam_installed = {"t4mp": self.game("World at War - Multiplayer")}
        self.setup._build()
        self.setup._checks["t4mp"][0].setChecked(False)
        self.setup._checks["t4mp"][0].setChecked(True)
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=True), \
             patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.setup._go_install()
        self.assertIs(self.stack.currentWidget(), self.install)
        self.assertTrue(self.install._from_manage)
        self.assertTrue(self.install._from_setup_selection)
        self.install._t0 = time.time()
        self.install._results = [{"key": "t4mp", "name": "World at War",
                                  "status": "failed", "reason": "download"}]
        with patch.object(ui_install.inhibit, "stop"):
            self.install._on_done(False)
        self.assertIn("Game Selection", self.install.cont_btn.text())
        self.install.cont_btn.click()
        self.assertIs(self.stack.currentWidget(), self.setup)
        self.assertTrue(self.setup._checks["t4mp"][0].isChecked())
        self.setup.back.click()
        self.assertIs(self.stack.currentWidget(), manage)
        # A subsequent successful add-games run returns directly to My Games.
        self.stack.setCurrentWidget(self.setup)
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=True), \
             patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.setup._go_install()
        self.install._t0 = time.time()
        self.install._results = [{"key": "t4mp", "name": "World at War",
                                  "status": "ok", "reason": ""}]
        with patch.object(ui_install.inhibit, "stop"):
            self.install._on_done(True)
        self.assertEqual(self.setup._choices, {})
        self.assertEqual(self.setup._extra_choices, {})
        self.assertIn("My Games", self.install.cont_btn.text())
        self.install.cont_btn.click()
        self.assertIs(self.stack.currentWidget(), manage)

    def test_first_run_install_returns_to_setup_on_failure_and_completion_on_success(self):
        welcome = self.screen("WelcomeScreen")
        complete = self.screen("SetupCompleteScreen")
        self.setup._from_manage = False
        self.setup.steam_installed = {"t4mp": self.game("World at War - Multiplayer")}
        self.setup._build()
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=True), \
             patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.setup._go_install()
        self.assertFalse(self.install._from_manage)
        self.assertTrue(self.install._from_setup_selection)
        self.install._t0 = time.time()
        self.install._results = [{"key": "t4mp", "name": "World at War",
                                  "status": "skipped", "reason": "declined"}]
        with patch.object(ui_install.inhibit, "stop"):
            self.install._on_done(False)
        self.install.cont_btn.click()
        self.assertIs(self.stack.currentWidget(), self.setup)
        self.setup.back.click()
        self.assertIs(self.stack.currentWidget(), welcome)
        self.stack.setCurrentWidget(self.setup)
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=True), \
             patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.setup._go_install()
        self.install._t0 = time.time()
        self.install._results = [{"key": "t4mp", "name": "World at War",
                                  "status": "ok", "reason": ""}]
        with patch.object(ui_install.inhibit, "stop"):
            self.install._on_done(True)
        self.install.cont_btn.click()
        self.assertIs(self.stack.currentWidget(), complete)

    def test_direct_management_install_failure_does_not_loop_through_setup(self):
        manage = self.screen("ManagementScreen")
        self.install._return_to_management = True
        with patch.object(ui_install.QTimer, "singleShot"), \
             patch.object(ui_install.inhibit, "start"):
            self.stack.setCurrentWidget(self.install)
        self.install._t0 = time.time()
        with patch.object(ui_install.inhibit, "stop"):
            self.install._on_done(False)
        self.assertIn("My Games", self.install.cont_btn.text())
        self.install.cont_btn.click()
        self.assertIs(self.stack.currentWidget(), manage)

    def test_review_cancel_does_not_start_install(self):
        self.setup.steam_installed = {"t4mp": self.game("World at War - Multiplayer")}
        self.setup._build()
        with patch.object(self.setup, "_preflight_ok", return_value=True), \
             patch.object(self.setup, "_review_install", return_value=False) as review:
            self.setup._go_install()
        review.assert_called_once()
        self.assertIs(self.stack.currentWidget(), self.setup)
        self.assertIsNone(self.install.steam_selected)

    def test_incomplete_statuses_are_visible_and_retryable(self):
        with patch.object(self.install, "_reset_and_run"):
            self.stack.setCurrentWidget(self.install)
        self.install._from_manage = False
        self.install._t0 = time.time()
        self.install._results = [
            {"key": "t4mp", "name": "World at War", "status": "ok", "reason": ""},
            {"key": "t6zm", "name": "Black Ops II", "status": "skipped", "reason": "user skipped download"},
            {"key": "iw4sp", "name": "Modern Warfare 2", "status": "pending", "reason": ""},
        ]
        with patch.object(ui_install.inhibit, "stop"):
            self.install._on_done(True)  # even a mistaken worker True cannot show success
        self.assertIn("incomplete", self.install.cur.text())
        self.assertIn("skipped", self.install.summary.text())
        self.assertIn("Zombies", self.install.summary.text())
        self.assertIn("not attempted", self.install.summary.text())
        self.assertFalse(self.install.retry_btn.isHidden())
        _APP.processEvents()
        self.assertGreater(self.install.summary_scroll.viewport().height(), 0)
        self.assertLess(self.install.cont_btn.mapTo(self.install, self.install.cont_btn.rect().bottomRight()).y(), 500)
        self.assertEqual(len(self.install._retryable()), 2)

    def test_review_names_modes_sources_and_optional_choices(self):
        gd = next(g for g in ui_install.ALL_GAMES if "t4sp" in g["keys"])
        self.setup.steam_installed = {"t4sp": self.game("World at War")}
        self.setup._build()
        self.setup._lan_cb.setChecked(True)
        with patch.object(ui_install, "QMessageBox") as dialog:
            start, back = object(), object()
            box = dialog.return_value
            box.addButton.side_effect = [start, back]
            box.clickedButton.return_value = start
            self.assertTrue(self.setup._review_install([("t4sp", gd, self.game("World at War"))], {}))
            text = box.setText.call_args.args[0]
            self.assertIn("World at War · Solo / Zombies (Steam)", text)
            self.assertIn("Plutonium Offline: on", text)

    def test_progress_shows_current_step_without_rewinding(self):
        self.install._t0 = time.time()
        self.install._plan_set([("prefix", "Creating Proton prefixes", 2),
                                ("finalize", "Finishing up", 1)])
        self.install._phase("prefix")
        self.install._on_progress(70, "Downloading dependencies")
        self.install._on_progress(5, "Still downloading")
        self.assertEqual(self.install.bar.value(), 70)
        self.assertIn("Step 1 of 2", self.install.stat.text())
        self.install._phase("finalize")
        self.assertIn("Finishing up", self.install.stat.text())

    def test_retry_keeps_offline_choice_and_only_incomplete_modes(self):
        gd = next(g for g in ui_install.ALL_GAMES if "t6zm" in g["keys"])
        self.install.steam_selected = [
            ("t6zm", gd, self.game("Black Ops II")),
            ("t6mp", gd, self.game("Black Ops II")),
        ]
        self.install.own_selected = {}
        self.install._results = [
            {"key": "t6zm", "status": "skipped"},
            {"key": "t6mp", "status": "ok"},
        ]
        self.install._last_offline_mode = True
        with patch.object(self.install, "_reset_and_run") as restart:
            self.install._retry_failed()
        restart.assert_called_once()
        self.assertEqual([k for k, _, _ in self.install.steam_selected], ["t6zm"])
        self.assertTrue(self.install.offline_mode)

    def test_first_run_only_completed_when_all_selected_modes_ready(self):
        self.install.steam_root = "/steam"
        self.install._t0 = time.time()
        self.install._results = [{"key": "t4mp", "name": "World at War", "status": "skipped", "reason": "declined"}]
        with patch.object(ui_install.inhibit, "stop"):
            self.install._finish_run(False)
        self.assertEqual(self.settings.completed, [])
        self.assertIn("incomplete", self.install.cur.text())
        self.install._results[0]["status"] = "ok"
        with patch.object(ui_install.inhibit, "stop"):
            self.install._finish_run(False)
        self.assertEqual(self.settings.completed, ["/steam"])
        self.assertIn("complete", self.install.cur.text())
        self.assertIn("ready", self.install.summary.text())

    def test_a_dialog_slot_that_raises_still_releases_the_waiting_worker(self):
        """The worker blocks on these events with no timeout: a slot that raised
        before setting its event hung the whole install (and the sleep inhibitor)."""
        boom = patch.object(ui_install, "QMessageBox", side_effect=RuntimeError("no display"))
        inst = self.install
        with boom:
            for slot, args, event, check in (
                    (inst._ask_iw5_dg_method, (), inst._iw5_dg_event, lambda: inst._iw5_method == ""),
                    (inst._ask_iw5_trim, (), inst._iw5_trim_event, lambda: inst._iw5_trim_accept is False),
                    (inst._show_retry_dl_dialog, ("Game", "failed"), inst._retry_dl_event,
                     lambda: inst._retry_dl_choice == "skip"),
                    (inst._show_manual_dl_dialog, ("http://x", "/tmp/none", "f.bin", "Game"),
                     inst._manual_dl_event, lambda: inst._manual_dl_ok is False)):
                with self.subTest(slot=slot.__name__):
                    event.clear()
                    slot(*args)
                    self.assertTrue(event.is_set(), "the worker would wait forever")
                    self.assertTrue(check())

    def test_retry_asks_for_the_same_forced_downgrade(self):
        inst = self.install
        inst._last_force_downgrade_ids = {"iw5"}
        inst.force_downgrade_ids = set()            # consumed by the first run
        inst._results = [{"key": "iw5mp", "name": "MW3", "status": "failed", "reason": ""}]
        inst.steam_selected = [("iw5mp", {"base": "MW3"}, {})]
        inst.own_selected = {}
        inst._last_offline_mode = None              # set by a real run
        with patch.object(inst, "_reset_and_run"):
            inst._retry_failed()
        self.assertEqual(inst.force_downgrade_ids, {"iw5"})

    def test_setup_screen_forgets_the_old_session_on_reset(self):
        setup = self.setup
        setup._choices = {"t4mp": True}; setup._extra_paths = ["/old/folder"]
        setup._folder_found = {"t4mp": {}}; setup.own_installed = {"t4mp": {}}
        setup.reset_state()
        self.assertEqual((setup._choices, setup._extra_paths, setup._folder_found, setup.own_installed),
                         ({}, [], {}, {}))

    def test_manual_download_dialog_survives_an_unwritable_destination(self):
        inst = self.install
        inst._manual_dl_event.clear()
        with patch.object(ui_install.os, "makedirs", side_effect=PermissionError("denied")), \
             patch.object(ui_install, "QMessageBox", side_effect=RuntimeError("stop here")):
            inst._show_manual_dl_dialog("http://x", "/root/none", "f.bin", "Game")
        self.assertTrue(inst._manual_dl_event.is_set())

    def test_iw5_wait_screen_still_shows_continue_when_the_clipboard_fails(self):
        inst = self.install
        with patch("depot_downgrade.copy_to_clipboard", side_effect=OSError("no clipboard")):
            inst._show_iw5_dg_wait("download_depot 1 2 3", "Step 1")
        self.assertFalse(inst.iw5_dg_btn.isHidden())
        self.assertIn("download_depot 1 2 3", inst.manual_step.text())

    def test_iw5_wait_screen_shows_the_command_when_no_clipboard_helper_took_it(self):
        inst = self.install
        # copy_to_clipboard() returns False (no exception) without wl-copy/xclip
        with patch("depot_downgrade.copy_to_clipboard", return_value=False):
            inst._show_iw5_dg_wait("download_depot 1 2 3", "Step 1")
        self.assertIn("download_depot 1 2 3", inst.manual_step.text())
        self.assertNotIn("command copied", inst.manual_step.text())

    def test_iw5_wait_screen_says_copied_when_a_helper_took_it(self):
        inst = self.install
        with patch("depot_downgrade.copy_to_clipboard", return_value=True):
            inst._show_iw5_dg_wait("download_depot 1 2 3", "Step 1")
        self.assertIn("command copied", inst.manual_step.text())


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class StyledSelectionUITests(unittest.TestCase):
    """Exercise the real app shell and stylesheet, not bare Qt widget defaults."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.old_style = cls.app.style().objectName()
        cls.old_font = cls.app.font()
        cls.old_sheet = cls.app.styleSheet()
        cls.app.setStyle("Fusion")
        ui_constants._load_font()
        cls.app.setFont(ui_constants.font(11))
        cls.app.setStyleSheet(ui_constants._app_style())

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls.old_sheet)
        cls.app.setFont(cls.old_font)
        if cls.old_style:
            cls.app.setStyle(cls.old_style)

    def test_real_window_800x500_selection_is_not_clipped(self):
        settings = Settings()
        with patch.object(ui_install, "cfg", settings), \
             patch.object(ui_constants, "cfg", settings), \
             patch.object(ui_qt.BootstrapScreen, "showEvent"), \
             patch.object(ui_qt, "_start_audio"):
            window = ui_qt.DeckOpsWindow()
            self.addCleanup(window.close)
            setup = ui_constants.get_screen(window.stack, "SetupScreen")
            setup.steam_installed = {
                "cod4mp": {"name": "CoD4 - Multiplayer", "source": "steam"},
                "t4mp": {"name": "World at War - Multiplayer", "source": "steam"},
                "t4sp": {"name": "World at War", "source": "steam"},
            }
            window.stack.setCurrentWidget(setup)
            window.resize(800, 500)
            window.show()
            self.app.processEvents()
            screenshot = os.environ.get("DECKOPS_UI_SCREENSHOT")
            if screenshot:
                window.grab().save(screenshot)
            self.assertEqual(window.size().width(), 800)
            self.assertEqual(window.size().height(), 500)
            self.assertGreater(setup._lw.height(), setup.scroll.viewport().height())
            self.assertGreater(setup.scroll.verticalScrollBar().maximum(), 0)
            for row in setup._lw.findChildren(QWidget, "gameRow"):
                self.assertGreaterEqual(row.height(), row.minimumSizeHint().height())
                for child in row.findChildren(QWidget):
                    if not isinstance(child, (QLabel, QCheckBox, QPushButton)) or child.isHidden():
                        continue
                    corner = child.mapTo(row, child.rect().bottomRight())
                    name = getattr(child, "text", lambda: "")()
                    self.assertLess(corner.y(), row.height(), (row.objectName(), name))
                    self.assertLess(corner.x(), row.width(), (row.objectName(), name))
            for button in setup.findChildren(QPushButton):
                if button.text() in ("Select All", "Select None"):
                    self.assertGreaterEqual(button.width(), button.sizeHint().width())
                    self.assertLess(button.mapTo(setup, button.rect().bottomRight()).x(), setup.width())
            for button in (setup.back, setup._folder_btn, setup.inst_btn):
                self.assertLess(button.mapTo(setup, button.rect().bottomRight()).y(), setup.height())
            selectors = {b.text(): b for b in setup.findChildren(QPushButton)}
            selectors["Select None"].click()
            self.app.processEvents()
            self.assertFalse(setup._checks["cod4mp"][0].isChecked())
            selectors["Select All"].click()
            self.app.processEvents()
            self.assertTrue(setup._checks["cod4mp"][0].isChecked())
            self.assertGreaterEqual(setup._lw.height(), setup._lw.minimumSizeHint().height())
            setup._scanned_own = {"iw4sp": {"name": "MW2 - Singleplayer", "source": "own"}}
            setup._on_folder_scanned(True)
            self.app.processEvents()
            self.assertGreaterEqual(setup._lw.height(), setup._lw.minimumSizeHint().height())
            for row in setup._lw.findChildren(QWidget, "gameRow"):
                if not row.isHidden():
                    self.assertGreaterEqual(row.height(), row.minimumSizeHint().height())


if __name__ == "__main__":
    unittest.main()
