"""Sweep 6: Game Library, Settings, Field Guide and Update screens -- controller
focus, Deck scale, compact library cards, sectioned Settings, and the in-app
update's relaunch (offscreen Qt)."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QDialog, QLabel, QPushButton, QScrollArea, QStackedWidget, QWidget, Qt
    import ui_constants as uc
    import ui_manage
except ImportError:
    QApplication = None
    QWidget = None


_APP = None
_GLYPHS = "\u2713\u2714\u2717\u2718\u26a0\u23f3\u2190"

if QWidget is not None:
    class _Destination(QWidget):
        def __init__(self, name):
            super().__init__()
            self.screen_name = name
else:
    _Destination = None


class _SyncThread:
    """threading.Thread stand-in that runs the target on start()."""

    def __init__(self, target=None, daemon=None, args=(), **_kw):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _installed():
    keys = ("cod4mp", "cod4sp", "iw4mp", "iw4sp", "t4mp", "t4sp", "t5mp", "t5sp", "t6mp", "t6zm", "t6sp")
    return {k: {"source": "steam", "install_dir": f"/games/{k}"} for k in keys}


def _settle(n=4):
    for _ in range(n):
        QApplication.processEvents()


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])
        uc._load_font()

    def setUp(self):
        # In-memory config: nothing here reads or writes the real deckops.json
        self.state = {"steam_root": "/steam", "deck_model": "oled", "player_name": "Ben",
                      "setup_games": {"cod4mp": {"client": "cod4r", "source": "steam"},
                                      "iw4mp": {"client": "iw4x", "source": "steam"}}}

        def save(config):
            self.state.clear()
            self.state.update(config)

        patches = [
            patch.object(ui_manage.cfg, "load", side_effect=lambda: dict(self.state)),
            patch.object(ui_manage.cfg, "save", side_effect=save),
            patch.object(ui_manage.ManagementScreen, "_refresh", lambda _self: None),
            patch.object(ui_manage.ManagementCard, "_fetch", lambda _self, _appid: None),
            patch.object(ui_manage, "_quick_update_check", return_value=None),
            patch.object(ui_manage, "find_steam_root", return_value="/steam"),
            patch.object(ui_manage.inhibit, "busy", return_value=None),
            patch.object(ui_manage.inhibit, "start"),
            patch.object(ui_manage.inhibit, "stop"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.busy, self.inh_start, self.inh_stop = mocks[-3:]

    def stack(self, w=800, h=500):
        stack = QStackedWidget(); stack.resize(w, h)
        self.addCleanup(stack.close)
        library = ui_manage.ManagementScreen(stack)
        library._startup_checked = True
        stack.addWidget(library)
        for name in ("SetupScreen", "InstallScreen"):
            stack.addWidget(_Destination(name))
        settings = ui_manage.ConfigureScreen(stack)
        stack.addWidget(settings)
        update = ui_manage.UpdateScreen(stack)
        stack.addWidget(update)
        guide = ui_manage.SetupCompleteScreen(stack)
        stack.addWidget(guide)
        return stack, library, settings, update, guide

    def window(self):
        """The real main window at 1280x800, with the status bar it reserves."""
        import ui_qt
        for p in (patch.object(ui_qt.BootstrapScreen, "_proceed", lambda *a: None),
                  patch.object(ui_qt, "_start_audio", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)
        prev = _APP.styleSheet(), _APP.font()
        self.addCleanup(_APP.setStyleSheet, prev[0])
        self.addCleanup(_APP.setFont, prev[1])
        _APP.setFont(uc.font(11))
        _APP.setStyleSheet(uc._app_style())
        win = ui_qt.DeckOpsWindow()
        self.addCleanup(win.close)
        win.resize(1280, 800)
        win.show()
        _settle()
        return win


class RelaunchTests(_Base):
    """CORE-1: the in-app update's relaunch must not leak the sleep inhibitor."""

    def test_inhibitor_is_released_before_execv(self):
        _, _, settings, _, _ = self.stack()
        settings._updating = True
        seen = {}

        def execv(path, argv):
            seen["stopped"] = self.inh_stop.call_count
            seen["updating"] = settings._updating
            raise SystemExit("replaced")        # stands in for the process image change

        with patch.object(ui_manage.os, "execv", side_effect=execv), \
             patch.object(ui_manage.shutil, "rmtree"):
            with self.assertRaises(SystemExit):
                settings._relaunch()
        self.assertEqual(seen, {"stopped": 1, "updating": False})

    def test_failed_execv_does_not_stop_the_inhibitor_twice(self):
        _, _, settings, _, _ = self.stack()
        settings._updating = True
        settings._update_pd = MagicMock()
        with patch.object(ui_manage.os, "execv", side_effect=OSError("gone")), \
             patch.object(ui_manage.shutil, "rmtree"):
            settings._relaunch()
        self.inh_stop.assert_called_once()
        self.assertIn("could not restart", settings._update_status.text())


class DialogTests(_Base):
    def test_view_log_has_close_and_confirms_the_copy_inside(self):
        _, _, settings, _, _ = self.stack()
        seen = {}

        def run(dialog):
            dialog.show(); _settle()
            buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
            self.assertIn("Close", buttons)
            seen["focus"] = dialog.focusWidget()
            buttons["Copy to Clipboard"].click()
            seen["confirm"] = dialog.findChild(QLabel, "viewLogStatus").text()
            seen["copy"] = buttons["Copy to Clipboard"]
            buttons["Close"].click()
            seen["result"] = dialog.result()

        with patch.object(ui_manage.QDialog, "exec", run), \
             patch.object(uc, "LOG_PATH", "/nonexistent/install.log"):
            settings._view_log()
        self.assertIs(seen["focus"], seen["copy"])
        self.assertIn("copied", seen["confirm"])
        self.assertEqual(seen["result"], QDialog.DialogCode.Rejected)
        self.assertEqual(settings.status.text(), "")       # not reported behind the dialog

    def test_check_setup_opens_on_its_first_action_and_has_no_glyphs(self):
        _, _, settings, _, _ = self.stack()
        results = [{"key": "cod4mp", "name": "MW1", "problems": [], "fixable": False},
                   {"key": "iw4mp", "name": "MW2", "problems": ["iw4x.dll is missing"], "fixable": True}]
        seen = {}

        def run(dialog):
            dialog.show(); _settle()
            seen["focus"] = dialog.focusWidget()
            seen["scroll"] = dialog.findChild(QScrollArea).focusPolicy()
            seen["texts"] = [w.text() for w in dialog.findChildren(QLabel)]
            seen["fix"] = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Fix")

        with patch.object(ui_manage.QDialog, "exec", run):
            settings._show_setup_report(results, {})
        self.assertIs(seen["focus"], seen["fix"])
        self.assertEqual(seen["scroll"], Qt.FocusPolicy.NoFocus)
        self.assertGreaterEqual(seen["fix"].height(), uc.BTN_MIN_H)
        joined = " ".join(seen["texts"])
        self.assertIn("OK  MW1", joined)
        self.assertIn("WARNING  MW2", joined)
        self.assertFalse(set(joined) & set(_GLYPHS))

    def test_game_options_opens_on_its_first_action(self):
        _, library, _, _, _ = self.stack()
        library.installed = {"cod4mp": {"source": "steam", "install_dir": "/game"}}
        seen = {}

        def run(dialog):
            dialog.show(); _settle()
            seen["focus"] = dialog.focusWidget()
            seen["scroll"] = dialog.findChild(QScrollArea).focusPolicy()
            seen["buttons"] = [b for b in dialog.findChildren(QPushButton)]

        with patch.object(ui_manage.QDialog, "exec", run):
            library._configure(ui_manage.ALL_GAMES[0], ["cod4mp"])
        self.assertIs(seen["focus"], seen["buttons"][0])
        self.assertNotEqual(seen["focus"].text(), "Cancel")
        self.assertEqual(seen["scroll"], Qt.FocusPolicy.NoFocus)
        for b in seen["buttons"]:
            self.assertGreaterEqual(b.height(), uc.BTN_MIN_H)
            self.assertGreaterEqual(b.font().pointSize(), uc.FONT_ACTION)


class LibraryTests(_Base):
    def test_scan_failure_offers_scan_again_on_the_library(self):
        _, library, _, _, _ = self.stack()
        library._on_scanned(False)
        self.assertEqual(library._status.text(),
                         "Couldn't scan for games. Check that Steam is installed, then try again.")
        self.assertNotIn("Return to Game Library", library._status.text())
        self.assertFalse(library._scan_again_btn.isHidden())
        with patch.object(library, "_refresh") as refresh:
            library._scan_again_btn.click()
        refresh.assert_called_once()
        self.assertTrue(library._scan_again_btn.isHidden())

    def test_scroll_areas_take_no_focus(self):
        _, library, settings, _, guide = self.stack()
        for sa in (library._scroll, settings._settings_scroll, guide._guide_scroll):
            self.assertEqual(sa.focusPolicy(), Qt.FocusPolicy.NoFocus)
        for screen in (library, settings, guide):
            for sa in screen.findChildren(QScrollArea):
                self.assertEqual(sa.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_focus_goes_toolbar_then_cards_in_grid_order_then_footer(self):
        stack, library, _, _, _ = self.stack(1280, 800)
        stack.show()
        library.set_installed(_installed()); _settle()
        order = library._focus_order()
        card_buttons = [b for card in library._cards for b in card._buttons]
        self.assertEqual(order, library._toolbar_buttons + card_buttons + [library._scan_again_btn])
        def next_stop(w):
            # Tab skips widgets that never take focus (the NoFocus scroll area)
            w = w.nextInFocusChain()
            while w.focusPolicy() == Qt.FocusPolicy.NoFocus:
                w = w.nextInFocusChain()
            return w
        for a, b in zip(order, order[1:]):
            self.assertIs(next_stop(a), b, (a.text(), b.text()))
        # Row-major: card i sits at (i // columns, i % columns)
        cols = library._columns
        for i, card in enumerate(library._cards):
            r, c = divmod(i, cols)
            item = library._grid.itemAtPosition(r, c)
            self.assertIs(item.widget(), card)

    def test_library_focus_target_and_rescan_keeps_the_focused_card(self):
        stack, library, _, _, _ = self.stack(1280, 800)
        stack.show(); stack.activateWindow()
        library.set_installed(_installed()); _settle()
        self.assertIs(library.focus_target(), library._cards[0]._primary)
        target = library._cards[2]._buttons[0]
        target.setFocus(); _settle()
        if QApplication.focusWidget() is not target:
            self.skipTest("offscreen platform did not give the window focus")
        library.set_installed(_installed()); _settle()
        self.assertIs(QApplication.focusWidget(), library._cards[2]._buttons[0])
        self.assertIsNone(getattr(library, "back_action", None))

    def test_partial_card_has_one_primary_action(self):
        _, library, _, _, _ = self.stack()
        self.state["setup_games"] = {"cod4mp": {"client": "cod4r", "source": "steam"}}
        with patch.object(ui_manage.cfg, "is_game_setup_for_source", side_effect=lambda k, s: k == "cod4mp"):
            library.set_installed(_installed())
        card = library._cards[0]
        self.assertEqual([b.text() for b in card._buttons], ["Set Up 1 Mode", "Options"])
        self.assertIs(card._primary, card._buttons[0])

    def test_all_cards_fit_one_1280x800_screen(self):
        win = self.window()
        library = uc.get_screen(win.stack, "ManagementScreen")
        library._startup_checked = True
        win.stack.setCurrentWidget(library)
        library.set_installed(_installed())
        library._on_startup_update_check(True)          # the notice line takes room too
        _settle()
        self.assertEqual(library._columns, 4)
        self.assertEqual(len(library._cards), 7)
        self.assertEqual(library._scroll.verticalScrollBar().maximum(), 0)
        viewport = library._scroll.viewport()
        for card in library._cards:
            top_left = card.mapTo(viewport, card.rect().topLeft())
            bottom_right = card.mapTo(viewport, card.rect().bottomRight())
            self.assertGreaterEqual(top_left.y(), 0)
            self.assertLess(bottom_right.y(), viewport.height())
            self.assertLess(bottom_right.x(), viewport.width())
        # Deck scale: actions 44px / 12pt, text at least 10pt
        for b in library.findChildren(QPushButton):
            if b.isVisible() and b.isEnabled():
                self.assertGreaterEqual(b.height(), uc.BTN_MIN_H, b.text())
                self.assertGreaterEqual(b.font().pointSize(), uc.FONT_ACTION, b.text())
        for lbl in library.findChildren(QLabel):
            if lbl.isVisible() and lbl.text() and lbl.objectName() != "deckopsBuildBadge":
                self.assertGreaterEqual(lbl.font().pointSize(), 10, lbl.text())

    def test_update_notice_leads_to_the_settings_check(self):
        stack, library, settings, _, _ = self.stack()
        library._on_startup_update_check(True)
        self.assertTrue(library._deckops_update_btn.isVisibleTo(library))
        with patch.object(settings, "_check_for_updates") as check, \
             patch.object(ui_manage.QTimer, "singleShot", lambda _ms, fn: fn()):
            library._deckops_update_btn.click()
        check.assert_called_once()
        self.assertIs(stack.currentWidget(), settings)
        self.assertFalse(library._deckops_update_btn.isVisibleTo(library))


class SettingsTests(_Base):
    def test_settings_fit_1280x800_without_scrolling(self):
        self.state["deck_model"] = "lcd"         # the LCD-only shader cache action shows too
        win = self.window()
        settings = uc.get_screen(win.stack, "ConfigureScreen")
        win.stack.setCurrentWidget(settings); _settle()
        for section in settings._SECTIONS:
            settings._status_labels[section].setText("\u26a0  A status that is long enough to wrap onto "
                                                     "a second line in its column")
        _settle()
        self.assertFalse(settings._settings_narrow)
        self.assertEqual(settings._settings_scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(settings._shader_row.isVisible())
        viewport = settings._settings_scroll.viewport()
        for panel in (settings._maint_panel, settings._tweaks_panel, settings._support_col):
            bottom = panel.mapTo(viewport, panel.rect().bottomRight())
            self.assertLessEqual(bottom.y(), viewport.height())
            self.assertLessEqual(bottom.x(), viewport.width())
        for b in settings.findChildren(QPushButton):
            if b.isVisible():
                self.assertGreaterEqual(b.height(), uc.BTN_MIN_H, b.text())
                self.assertGreaterEqual(b.font().pointSize(), uc.FONT_ACTION, b.text())
        self.assertNotIn("font-size", settings._name_input.styleSheet())
        self.assertGreaterEqual(settings._name_input.font().pointSize(), uc.FONT_ACTION)

    def test_task_buttons_disabled_while_running_and_back_after_failure(self):
        _, _, settings, _, _ = self.stack()
        with patch.object(ui_manage.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: None)):
            settings._apply_deadzone()
        self.assertTrue(settings._settings_running)
        self.assertTrue(all(not b.isEnabled() for b in settings._task_buttons))
        self.assertFalse(settings._name_input.isEnabled())
        self.assertFalse(settings.escape_allowed())
        settings._settings_sigs.done.emit(False)
        self.assertTrue(all(b.isEnabled() for b in settings._task_buttons))
        self.assertTrue(settings.escape_allowed())

        # A worker that fails for real re-enables them too
        with patch.object(ui_manage.threading, "Thread", _SyncThread), \
             patch.object(ui_manage, "find_all_games", side_effect=RuntimeError("scan broke")):
            settings._apply_deadzone()
        self.assertFalse(settings._settings_running)
        self.assertTrue(all(b.isEnabled() for b in settings._task_buttons))
        self.assertEqual(settings.status.text(), "FAILED  Failed: scan broke")

    def test_status_shows_in_the_section_of_the_pressed_button(self):
        _, _, settings, _, _ = self.stack()
        labels = settings._status_labels
        with patch.object(ui_manage.threading, "Thread", _SyncThread), \
             patch.object(ui_manage, "find_all_games", side_effect=RuntimeError("scan broke")):
            settings._deadzone_btn.click()
        self.assertIn("scan broke", labels["tweaks"].text())
        self.assertEqual(labels["maintenance"].text(), "")
        with patch.object(ui_manage.threading, "Thread", _SyncThread), \
             patch("wrapper.kill_steam"), patch("shortcut.repair_shortcuts", return_value=[]):
            settings._repair_btn.click()
        self.assertIn("All shortcuts present", labels["maintenance"].text())
        self.assertIn("scan broke", labels["tweaks"].text())
        self.busy.return_value = "DeckOps is installing."
        settings._confirm_reset()
        self.assertIn("installing", labels["danger"].text())
        # Each label sits inside its own section panel
        self.assertTrue(settings._maint_panel.isAncestorOf(labels["maintenance"]))
        self.assertTrue(settings._tweaks_panel.isAncestorOf(labels["tweaks"]))
        self.assertTrue(settings._danger_panel.isAncestorOf(labels["danger"]))

    def test_settings_controller_protocol(self):
        stack, _, settings, _, _ = self.stack()
        self.assertIs(settings.focus_target(), settings._repair_btn)
        stack.setCurrentWidget(settings)
        settings.back_action()
        self.assertIs(stack.currentWidget(), uc.get_screen(stack, "ManagementScreen"))
        settings._updating = True
        self.assertFalse(settings.escape_allowed())
        settings._updating = False
        self.assertTrue(settings.escape_allowed())
        self.assertEqual(settings._back_btn.text(), uc.LABELS["back"])


class UpdateAndGuideTests(_Base):
    def test_update_screen_protocol_and_labels(self):
        stack, _, _, update, _ = self.stack()
        texts = [lbl.text() for lbl in update.findChildren(QLabel)]
        self.assertIn(uc.screen_title("UpdateScreen").upper(), texts)
        self.assertNotIn("GAME MAINTENANCE", texts)
        stack.show(); _settle()
        with patch.object(ui_manage.QTimer, "singleShot"), patch.object(ui_manage, "_log_to_file"):
            stack.setCurrentWidget(update); _settle()
        self.assertTrue(update._running)
        self.assertFalse(update.escape_allowed())
        self.assertIs(update.focus_target(), update.stop_btn)
        update.back_action()                         # ignored while it runs
        self.assertIs(stack.currentWidget(), update)
        update._failed_games = [("t4mp", ui_manage.ALL_GAMES[3], {})]
        update._on_done(False)
        self.assertTrue(update.escape_allowed())
        self.assertIs(update.focus_target(), update.retry_btn)
        self.assertEqual(update.back_btn.text(), uc.LABELS["back_to_library"])
        self.assertNotIn("My Games", update.cur.text())
        update.back_action()
        self.assertIs(stack.currentWidget(), uc.get_screen(stack, "ManagementScreen"))

    def test_operation_log_row_is_vertically_centred(self):
        stack, _, _, update, _ = self.stack(1280, 800)
        with patch.object(ui_manage.QTimer, "singleShot"), patch.object(ui_manage, "_log_to_file"):
            stack.setCurrentWidget(update); stack.show(); _settle()
        heading, button = update._log_heading, update._copy_log_btn
        self.assertGreaterEqual(button.height(), uc.BTN_MIN_H)
        mid = lambda w: w.mapTo(update, w.rect().center()).y()
        self.assertLessEqual(abs(mid(heading) - mid(button)), 2)

    def test_field_guide_protocol(self):
        stack, _, _, _, guide = self.stack()
        self.assertIs(guide.focus_target(), guide._continue_btn)
        self.assertEqual(guide._continue_btn.text(), uc.LABELS["continue_to_library"])
        guide._restoring = True
        self.assertFalse(guide.escape_allowed())
        guide._restoring = False
        stack.setCurrentWidget(guide)
        guide.back_action()
        self.assertIs(stack.currentWidget(), uc.get_screen(stack, "ManagementScreen"))

    def test_no_unsupported_glyphs_in_visible_texts(self):
        stack, library, settings, update, guide = self.stack()
        library._status.setText("\u2713  Opened")
        library._on_startup_update_check(True)
        for section in settings._SECTIONS:
            settings._status_labels[section].setText("\u2717  Failed: boom")
        settings._update_status.setText("\u26a0  Wait")
        settings._log_status.setText("\u23f3  Copying")
        update._on_progress(10, "\u2713  Steam closed")
        guide._restore_finished("DONE|\u2713  Restored saves for 1 group(s): cod4.")
        for screen in (library, settings, update, guide):
            for w in screen.findChildren(QLabel) + screen.findChildren(QPushButton):
                self.assertFalse(set(w.text()) & set(_GLYPHS), (type(screen).__name__, w.text()))
        self.assertEqual(settings._status_labels["maintenance"].text(), "FAILED  Failed: boom")
        self.assertTrue(guide._restore_status.text().startswith("OK"))


if __name__ == "__main__":
    unittest.main()
