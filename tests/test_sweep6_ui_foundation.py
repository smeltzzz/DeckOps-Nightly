"""Sweep 6 shared UI foundations: controller navigation, Deck scale, labels,
the header build badge and font-safe status marks."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import (
        QApplication, QCheckBox, QEvent, QKeyEvent, QLabel, QPushButton, QRawFont,
        QScrollArea, QStackedWidget, QVBoxLayout, QWidget, Qt,
    )
    import ui_constants as uc
except ImportError:
    QApplication = None


_APP = None
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"


def _key(widget, key, release=True):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))
    if release:
        QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyRelease, key, Qt.KeyboardModifier.NoModifier))
    QApplication.processEvents()


def _settle():
    for _ in range(3):
        QApplication.processEvents()


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])
        uc._load_font()
        uc.install_controller_nav(_APP)

    def _window(self, *widgets, page=None):
        win = uc._nav_scope(QWidget()); lay = QVBoxLayout(win)
        for w in widgets:
            lay.addWidget(w)
        win.resize(400, 300)
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.close)
        win.show(); win.activateWindow(); _settle()
        return win

    def _focus(self, w):
        w.setFocus(Qt.FocusReason.OtherFocusReason); _settle()
        if QApplication.focusWidget() is not w:
            self.skipTest("offscreen platform did not give the test window focus")


class ControllerNavTests(_QtCase):
    def test_btn_is_auto_default(self):
        b = uc._btn("Go", uc.C_DARK_BTN)
        self.assertTrue(b.autoDefault())
        self.assertGreaterEqual(b.height(), uc.BTN_MIN_H)
        self.assertGreaterEqual(b.font().pointSize(), uc.FONT_ACTION)

    def test_return_clicks_focused_plain_button(self):
        b = QPushButton("Plain")      # not autoDefault: the filter still clicks it
        hits = []
        b.clicked.connect(lambda: hits.append(1))
        self._window(b); self._focus(b)
        _key(b, Qt.Key.Key_Return)
        self.assertEqual(hits, [1])

    def test_return_toggles_focused_checkbox(self):
        cb = QCheckBox("Multiplayer")
        self._window(cb); self._focus(cb)
        _key(cb, Qt.Key.Key_Return)
        self.assertTrue(cb.isChecked())
        _key(cb, Qt.Key.Key_Enter)
        self.assertFalse(cb.isChecked())

    def test_autorepeat_return_does_not_click_again(self):
        b = uc._btn("Once", uc.C_DARK_BTN)
        hits = []
        b.clicked.connect(lambda: hits.append(1))
        self._window(b); self._focus(b)
        QApplication.sendEvent(b, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                            Qt.KeyboardModifier.NoModifier, "", True))
        _settle()
        self.assertEqual(hits, [])

    def test_down_and_up_move_focus_between_buttons(self):
        a, b = uc._btn("A", uc.C_DARK_BTN), uc._btn("B", uc.C_DARK_BTN)
        self._window(a, b); self._focus(a)
        _key(a, Qt.Key.Key_Down)
        self.assertIs(QApplication.focusWidget(), b)
        _key(b, Qt.Key.Key_Up)
        self.assertIs(QApplication.focusWidget(), a)
        _key(a, Qt.Key.Key_Right)
        self.assertIs(QApplication.focusWidget(), b)

    def test_windows_outside_the_scope_keep_plain_qt_keys(self):
        cb = QCheckBox("Not ours")
        win = self._window(cb)
        uc._nav_scope(win, False)
        self._focus(cb)
        _key(cb, Qt.Key.Key_Return)
        self.assertFalse(cb.isChecked())
        win.setProperty("deckopsNav", None)
        self.assertFalse(uc._nav_enabled(cb))

    def test_arrows_skip_a_scroll_area_of_buttons(self):
        top = uc._btn("Top", uc.C_DARK_BTN)
        inner = QWidget(); il = QVBoxLayout(inner)
        inside = uc._btn("Inside", uc.C_DARK_BTN); il.addWidget(inside)
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(inner)   # no helper
        self._window(top, sa); self._focus(top)
        _key(top, Qt.Key.Key_Down)
        self.assertIs(QApplication.focusWidget(), inside)

    def test_escape_runs_screen_back_action_and_respects_opt_out(self):
        class Screen(QWidget):
            def __init__(self):
                super().__init__(); self.backs = 0; self.allowed = True
                QVBoxLayout(self).addWidget(uc._btn("X", uc.C_DARK_BTN))

            def back_action(self):
                self.backs += 1

            def escape_allowed(self):
                return self.allowed

        win = uc._nav_scope(QWidget()); win.stack = QStackedWidget(); QVBoxLayout(win).addWidget(win.stack)
        screen = Screen(); win.stack.addWidget(screen)
        self.addCleanup(win.deleteLater); self.addCleanup(win.close)
        win.show(); win.activateWindow(); _settle()
        btn = screen.findChildren(QPushButton)[0]; self._focus(btn)
        _key(btn, Qt.Key.Key_Escape)
        self.assertEqual(screen.backs, 1)
        screen.allowed = False
        _key(btn, Qt.Key.Key_Escape)
        self.assertEqual(screen.backs, 1)

    def test_scroll_area_helpers_take_no_focus(self):
        sa = QScrollArea()
        self.assertIs(uc._no_focus_scroll(sa), sa)
        self.assertEqual(sa.focusPolicy(), Qt.FocusPolicy.NoFocus)
        sa2 = uc._scroll_area(QWidget())
        self.assertEqual(sa2.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertTrue(sa2.widgetResizable())

    def test_focused_child_is_scrolled_into_view(self):
        inner = QWidget(); il = QVBoxLayout(inner)
        buttons = [uc._btn(f"B{i}", uc.C_DARK_BTN) for i in range(20)]
        for b in buttons:
            il.addWidget(b)
        sa = uc._scroll_area(inner)
        win = self._window(sa); win.resize(300, 150); _settle()
        self._focus(buttons[0])
        buttons[-1].setFocus(Qt.FocusReason.TabFocusReason); _settle()
        self.assertGreater(sa.verticalScrollBar().value(), 0)

    def test_chain_focus_and_focus_first(self):
        a, b, c = (uc._btn(t, uc.C_DARK_BTN) for t in "abc")
        hidden = uc._btn("hidden", uc.C_DARK_BTN)
        win = self._window(a, b, c, hidden)
        hidden.hide()
        chain = uc._chain_focus([c, None, a, b])
        self.assertEqual(chain, [c, a, b])
        self.assertIs(c.nextInFocusChain(), a)
        uc._focus_first([hidden, b, a]); _settle()
        if QApplication.focusWidget() is None:
            self.skipTest("offscreen platform did not give the test window focus")
        self.assertIs(QApplication.focusWidget(), b)
        self.assertIs(uc._first_focusable(win), uc._first_focusable([c, a, b]))

    def test_focus_ring_in_styles(self):
        style = uc._app_style()
        self.assertIn(f"QCheckBox:focus, QRadioButton:focus {{ outline:none; border:{uc.FOCUS_RING_W}px solid",
                      style)
        self.assertIn("QMessageBox QPushButton, QDialogButtonBox QPushButton", style)
        self.assertIn(f"min-height:{uc.BTN_MIN_H}px", style)
        self.assertIn(f"width:{uc.CHECK_INDICATOR}px", style)
        self.assertIn("QSlider::handle:horizontal:focus", style)
        for color in (uc.C_DARK_BTN, uc.C_IW, uc.C_TREY, uc.C_RED_BTN):
            self.assertIn(f"QPushButton:focus{{border:{uc.FOCUS_RING_W}px solid {uc.C_FOCUS};}}",
                          uc._btn("x", color).styleSheet())


class LayoutAndLabelTests(_QtCase):
    def test_header_badge_is_not_stretched(self):
        prev = _APP.styleSheet(); self.addCleanup(_APP.setStyleSheet, prev)
        _APP.setStyleSheet(uc._app_style())
        hdr, _ = uc._header_bar()
        win = self._window(hdr); win.resize(1280, 800); _settle()
        badge = hdr.findChild(QLabel, "deckopsBuildBadge")
        self.assertIsNotNone(badge)
        self.assertLess(badge.height(), 30)
        mid = badge.geometry().center().y()
        self.assertLessEqual(abs(mid - hdr.height() // 2), 3)

    def test_status_bar_uses_screen_titles(self):
        import ui_qt
        import ui_manage
        with patch.object(ui_manage.ManagementCard, "_fetch", lambda *a: None), \
             patch.object(ui_manage.ManagementScreen, "_refresh", lambda *a: None), \
             patch.object(ui_qt.BootstrapScreen, "_proceed", lambda *a: None):
            win = ui_qt.DeckOpsWindow()
            self.addCleanup(win.close)
            for name, title in uc.SCREEN_TITLES.items():
                screen = uc.get_screen(win.stack, name)
                if screen is None:
                    continue
                win.stack.setCurrentWidget(screen)
                self.assertEqual(win._screen_label.text(), title.upper())
            self.assertEqual(uc.screen_title("ManagementScreen"), "Game Library")
            self.assertEqual(win._mute_btn.focusPolicy(), Qt.FocusPolicy.TabFocus)
            self.assertGreaterEqual(win._mute_btn.height(), 40)
            self.assertGreaterEqual(win._screen_label.font().pointSize(), 11)

    def test_label_table(self):
        self.assertEqual(uc.LABELS["back"], "Back")
        self.assertEqual(uc.LABELS["continue"], "Continue")
        self.assertEqual(uc.LABELS["back_to_library"], "Back to Game Library")
        self.assertNotIn("My Games", " ".join(uc.LABELS.values()) + " ".join(uc.SCREEN_TITLES.values()))
        offline = [g for g in uc.ALL_GAMES if g["client"] == "lan"][0]
        self.assertEqual(offline["badge"].lower(), "offline")


class MarkTests(_QtCase):
    def _supported(self, text):
        missing = {}
        for fn in ("AtkinsonHyperlegible-Regular.ttf", "AtkinsonHyperlegible-Bold.ttf", "RussoOne-Regular.ttf"):
            raw = QRawFont(str(FONTS / fn), 12)
            self.assertTrue(raw.isValid(), fn)
            bad = [ch for ch in text if ch.strip() and not raw.supportsCharacter(ch)]
            if bad:
                missing[fn] = bad
        return missing

    def test_marks_use_bundled_font_glyphs(self):
        words = uc.MARK_OK + uc.MARK_FAIL + uc.MARK_WARN + uc.MARK_WAIT
        self.assertEqual(self._supported(words), {})

    def test_plain_marks_replace_unsupported_glyphs(self):
        cases = {
            "\u2713  Plutonium ready.": "OK  Plutonium ready.",
            "  \u2717  Skipped by user.": "  FAILED  Skipped by user.",
            "\u26a0\ufe0f Low space": "WARNING Low space",
            "\u23f3  HGL is launching": "WAIT  HGL is launching",
            "I've closed Plutonium  \u2713": "I've closed Plutonium",
            "\u2190 Back": "Back",
            "Nothing special": "Nothing special",
        }
        for raw, want in cases.items():
            got = uc._plain_marks(raw)
            self.assertEqual(got, want)
            self.assertEqual(self._supported(got), {})

    def test_log_html_has_no_unsupported_glyphs(self):
        line = uc._log_html("\u2717  Depot download failed \u2190 retry")
        self.assertIn(uc.MARK_FAIL, line)
        self.assertIn(uc.C_FAIL_TEXT, line)
        for g in "\u2713\u2717\u26a0\u23f3\u2190":
            self.assertNotIn(g, line)
        self.assertIn(uc.C_IW, uc._log_html("\u2713  done"))
        self.assertIn(uc.C_DIM, uc._log_html("plain <b>"))
        self.assertIn("&lt;b&gt;", uc._log_html("plain <b>"))
        self.assertIn(uc.MARK_OK, uc._mark("ok"))


if __name__ == "__main__":
    unittest.main()
