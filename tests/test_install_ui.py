"""Install selection and completion UX regressions (offscreen, no worker runs)."""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

    def is_lcd(self):
        return False

    def is_first_run(self):
        return not self.completed

    def complete_first_run(self, root):
        self.completed.append(root)

    def is_game_setup_for_source(self, key, source):
        return False

    def get_offline_mode(self):
        return False

    def is_zd_installed(self):
        return False


_APP = None


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

    def test_warm_selection_and_install_status_styles(self):
        self.setup.steam_installed = {"cod4mp": self.game("CoD4 - Multiplayer")}
        self.setup._build()
        self.assertIn(f"background:{ui_theme.C_CARD}",
                      self.setup._lw.findChildren(QWidget, "gameRow")[0].styleSheet())
        buttons = {b.text(): b for b in self.setup.findChildren(QPushButton)}
        client = buttons["CoD4R (recommended)"]
        self.assertIn(f"background:{ui_theme.C_IW};color:{ui_theme.C_BG}", client.styleSheet())
        self.assertIn(f"border-color:{ui_theme.C_TEXT}", client.styleSheet())
        self.assertIn(f"color:{ui_theme.C_DIM}", self.install.log.styleSheet())
        self.assertIn(f"background:{ui_theme.C_SURFACE}", self.install.summary.styleSheet())
        self.assertIn(f"color:{ui_theme.C_TEXT}", self.install.summary.styleSheet())
        self.assertIn(f"background:{ui_theme.C_SURFACE}", self.install.plut_warn.styleSheet())
        self.assertIn(f"color:{ui_theme.C_BG}", self.install.plut_btn.styleSheet())

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

    def test_retry_keeps_addon_choice_and_only_incomplete_modes(self):
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
        self.install._last_zd_choice = True
        self.install._last_offline_mode = True
        with patch.object(self.install, "_reset_and_run") as restart:
            self.install._retry_failed()
        restart.assert_called_once()
        self.assertEqual([k for k, _, _ in self.install.steam_selected], ["t6zm"])
        self.assertTrue(self.install.zd_choice)
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
