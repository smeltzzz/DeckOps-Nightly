"""Controller-only focus exits and partial save-restore feedback (offscreen Qt)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import (QApplication, QDialog, QEvent, QKeyEvent, QLineEdit,
                    QPlainTextEdit, QSlider, QStackedWidget, QVBoxLayout, QWidget, Qt)
    import ui_constants as uc
    import ui_manage
except ImportError:
    QApplication = None

_APP = None


class _SyncThread:
    def __init__(self, target=None, args=(), **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@unittest.skipIf(QApplication is None, "PyQt unavailable")
class ControllerFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])
        uc.install_controller_nav(_APP)

    def setUp(self):
        self.win = uc._nav_scope(QWidget())
        self.layout = QVBoxLayout(self.win)
        self.addCleanup(self.win.deleteLater)
        self.addCleanup(self.win.close)

    def _show(self, widget, other):
        self.layout.addWidget(widget)
        self.layout.addWidget(other)
        self.win.show()
        self.win.activateWindow()
        QApplication.processEvents()
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        QApplication.processEvents()
        if QApplication.focusWidget() is not widget:
            self.skipTest("offscreen platform cannot grant focus")

    @staticmethod
    def _key(widget, key):
        QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key,
                                                 Qt.KeyboardModifier.NoModifier))
        QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyRelease, key,
                                                 Qt.KeyboardModifier.NoModifier))
        QApplication.processEvents()

    def test_name_field_down_exits_but_left_right_edit_cursor(self):
        field = QLineEdit("Player")
        following = uc._btn("Continue", uc.C_IW)
        self._show(field, following)
        field.setCursorPosition(3)
        self._key(field, Qt.Key.Key_Left)
        self.assertIs(QApplication.focusWidget(), field)
        self.assertEqual(field.cursorPosition(), 2)
        self._key(field, Qt.Key.Key_Down)
        self.assertIs(QApplication.focusWidget(), following)

    def test_horizontal_volume_slider_down_exits_but_right_adjusts(self):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(50)
        following = uc._btn("Next", uc.C_IW)
        self._show(slider, following)
        self._key(slider, Qt.Key.Key_Right)
        self.assertIs(QApplication.focusWidget(), slider)
        self.assertGreater(slider.value(), 50)
        self._key(slider, Qt.Key.Key_Down)
        self.assertIs(QApplication.focusWidget(), following)

    def test_deferred_screen_focus_does_not_steal_a_modal_confirmation(self):
        behind = uc._btn("Behind dialog", uc.C_IW)
        self.layout.addWidget(behind)
        self.win.show()
        self.win.activateWindow()
        QApplication.processEvents()
        dialog = QDialog(self.win)
        dialog.setModal(True)
        choice = uc._btn("Cancel", uc.C_DARK_BTN)
        QVBoxLayout(dialog).addWidget(choice)
        self.addCleanup(dialog.close)
        dialog.show()
        QApplication.processEvents()
        choice.setFocus()
        if QApplication.activeModalWidget() is not dialog:
            self.skipTest("offscreen platform cannot activate a modal dialog")
        uc._focus_first(behind)
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), choice)

    def test_read_only_log_right_exits_without_losing_vertical_scrolling(self):
        log = QPlainTextEdit("\n".join(str(i) for i in range(60)))
        log.setReadOnly(True)
        following = uc._btn("Back", uc.C_IW)
        self._show(log, following)
        self.win.resize(240, 120)
        QApplication.processEvents()
        self._key(log, Qt.Key.Key_Down)
        self.assertIs(QApplication.focusWidget(), log)
        self._key(log, Qt.Key.Key_Right)
        self.assertIs(QApplication.focusWidget(), following)


@unittest.skipIf(QApplication is None, "PyQt unavailable")
class RestoreFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def test_partial_restore_stays_retryable_and_does_not_dismiss_backups(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(ui_manage, "find_steam_root", return_value=root), \
             patch.object(ui_manage.inhibit, "busy", return_value=None), \
             patch.object(ui_manage.inhibit, "start"), \
             patch.object(ui_manage.inhibit, "stop") as stop, \
             patch.object(ui_manage.cfg, "is_lcd", return_value=False), \
             patch.object(ui_manage.threading, "Thread", _SyncThread), \
             patch("save_backup.restore_saves") as restore:
            stack = QStackedWidget()
            self.addCleanup(stack.deleteLater)
            self.addCleanup(stack.close)
            guide = ui_manage.SetupCompleteScreen(stack)
            stack.addWidget(guide)
            def partial(*, steam_root, on_progress):
                self.assertEqual(steam_root, root)
                on_progress("⚠  Could not restore BO2 stats")
                return {"MW2": ["saved.cfg"]}
            restore.side_effect = partial
            with patch.object(guide, "_mark_offer_handled") as handled:
                guide._do_restore()
            handled.assert_not_called()
            self.assertTrue(guide._restore_btn.isEnabled())
            self.assertTrue(guide._restore_skip.isEnabled())
            self.assertTrue(guide._continue_btn.isEnabled())
            self.assertIn("could not restore", guide._restore_status.text().lower())
            self.assertFalse(guide._restoring)
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
