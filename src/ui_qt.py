"""
ui_qt.py — DeckOps main UI entry point

Slim shell: BootstrapScreen, DeckOpsWindow, and run().
All screen classes are imported from split modules:
    ui_constants  — shared constants, helpers, navigation
    ui_setup      — first-run setup flow (OS → Device → Gyro → ...)
    ui_install    — install pipeline (Select Games with the first-run scan, Install)
    ui_manage     — post-install (Management, Configure, SetupComplete, Update)
"""

import sys, os

from qt import QApplication, QMainWindow, QStackedWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QWidget, QMessageBox, QStatusBar
from qt import Qt, QTimer

import config as cfg
import inhibit
from identity import APP_TITLE

from ui_constants import (
    C_CARD, C_DIM, C_DARK_BTN, C_FOCUS, C_LINE, C_TEXT, font, _lbl, _title_block,
    _load_font, _app_style, _start_audio, _kill_audio,
    _set_audio_enabled, _clean_enabled,
    go_to, get_screen, screen_title, install_controller_nav, _ensure_focus_in,
    FONT_BODY, FOCUS_RING_W, BTN_MIN_H,
)

from ui_setup import SetupFlowScreen
from ui_install import SetupScreen, InstallScreen
from ui_manage import ManagementScreen, ConfigureScreen, SetupCompleteScreen, UpdateScreen


# ── BootstrapScreen ───────────────────────────────────────────────────────────
class BootstrapScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack = stack; self.screen_name = "BootstrapScreen"
        lay = QVBoxLayout(self); lay.setContentsMargins(80,80,80,80); lay.setSpacing(14)
        lay.addStretch()
        _title_block(lay)
        lay.addStretch()
        self.status = _lbl("Opening DeckOps...", 13, C_DIM)
        lay.addWidget(self.status)
        self.bar = QProgressBar(); self.bar.setMaximum(100); self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        bw = QHBoxLayout(); bw.addStretch(); bw.addWidget(self.bar,6); bw.addStretch()
        lay.addLayout(bw); lay.addSpacing(50)

    def showEvent(self, e):
        super().showEvent(e)
        # Window restored / re-mapped: the bootstrap is already running
        # (a second _bootstrap.run thread would race it)
        if e.spontaneous():
            return
        _start_audio()
        # Game artwork is optional and ManagementCard fetches it as needed.
        # Waiting for every remote image here kept offline users on the splash
        # screen through repeated network timeouts before they could use DeckOps.
        QTimer.singleShot(300, self._proceed)

    def _proceed(self):
        # Load the bundled font if available, then enter the app without
        # waiting for optional artwork or a network connection.
        try:
            _load_font()
            QApplication.instance().setStyleSheet(_app_style())
        except FileNotFoundError:
            pass  # Font missing — fall back gracefully, app still works

        first_run = cfg.is_first_run()      # loads deckops.json: moves a damaged one aside
        damaged = cfg.unnoticed_corrupt_files()
        if damaged:
            # It used to only reach the log: the player just saw setup start
            # again and every game shown as not set up, with no reason given.
            QMessageBox.warning(
                self, APP_TITLE,
                "DeckOps's settings file was damaged (for example by a power cut), so DeckOps "
                "started with fresh settings.\n\nYour games, game files and saves were not touched. "
                "Run Set Up again for your games to continue.\n\nThe damaged file was kept as:\n"
                + "\n".join(damaged))
            cfg.mark_corrupt_noticed(damaged)
        if first_run:
            go_to(self.stack, "SetupFlowScreen")
        else:
            go_to(self.stack, "ManagementScreen")


# ── DeckOpsWindow ─────────────────────────────────────────────────────────────
class DeckOpsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 800)
        self.setMinimumSize(800, 500)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Register all screens — order doesn't matter, navigation is by name
        for cls in [
            BootstrapScreen,
            SetupFlowScreen,
            SetupScreen,
            InstallScreen,
            ManagementScreen,
            ConfigureScreen,
            SetupCompleteScreen,
            UpdateScreen,
        ]:
            self.stack.addWidget(cls(self.stack))

        self.stack.setCurrentIndex(0)

        # A compact status strip owns the persistent music control. Unlike the
        # old floating button it never covers a wizard action or a result card.
        strip = QStatusBar(self)
        strip.setFixedHeight(BTN_MIN_H + 4)     # room for the music button
        strip.setSizeGripEnabled(False)
        strip.setStyleSheet(
            f"QStatusBar{{background:{C_CARD};border-top:1px solid {C_LINE};}}"
            f"QStatusBar QLabel{{background:transparent;color:{C_DIM};padding:0 10px;}}")
        self.setStatusBar(strip)
        self._screen_label = QLabel()
        self._screen_label.setFont(font(FONT_BODY, bold=True))
        strip.addWidget(self._screen_label)
        self._dbg_label = QLabel()
        self._dbg_label.setFont(font(FONT_BODY))
        strip.addPermanentWidget(self._dbg_label)
        self._dbg_label.setVisible(os.environ.get("DECKOPS_DEBUG") == "1")

        self._muted = not _clean_enabled(cfg.get_music_enabled())
        self._mute_btn = QPushButton(self)
        self._mute_btn.setFixedSize(124, BTN_MIN_H)
        self._mute_btn.setFont(font(FONT_BODY, bold=True))
        self._mute_btn.setStyleSheet(
            f"QPushButton{{background:{C_DARK_BTN};color:{C_TEXT};"
            f"border:{FOCUS_RING_W}px solid {C_LINE};border-radius:6px;padding:0 8px;}}"
            f"QPushButton:hover{{border-color:{C_DIM};}}"
            f"QPushButton:focus{{border:{FOCUS_RING_W}px solid {C_FOCUS};}}")
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Reachable with Tab / D-pad, but a click or tap must not park focus on
        # it: it used to be the first focus on every screen, so A toggled music.
        self._mute_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._mute_btn.setAutoDefault(True)
        self._mute_btn.clicked.connect(self._toggle_mute)
        strip.addPermanentWidget(self._mute_btn)
        self._sync_mute_button()
        self.stack.currentChanged.connect(self._update_dbg_label)
        self.stack.currentChanged.connect(self._land_focus)
        self._update_dbg_label(0)
        # Controller (D-pad / A / B) navigation for the whole app
        install_controller_nav(QApplication.instance(), scope=self)
        QApplication.instance().focusChanged.connect(self._focus_moved)

    def _focus_moved(self, old, new):
        # When the focused widget is hidden (a setup step swapped out, a button
        # hidden after use) Qt passes focus to the next widget in the chain, and
        # that was usually the music button, so A then toggled music. Send that
        # fallback focus back into the current screen instead; a deliberate Tab /
        # D-pad move onto the button (old still visible) is left alone.
        try:
            if new is self._mute_btn and (old is None or not old.isVisible()):
                _ensure_focus_in(self.stack.currentWidget())
        except RuntimeError:
            pass

    def _land_focus(self, idx):
        # Screens may set their own focus (focus_target / _focus_first in showEvent);
        # this only fills in when the new screen still has nothing focused.
        _ensure_focus_in(self.stack.widget(idx))

    def _update_dbg_label(self, idx):
        w = self.stack.widget(idx)
        name = getattr(w, "screen_name", w.__class__.__name__)
        self._screen_label.setText(screen_title(name).upper())
        self._dbg_label.setText(f"[{idx}] {name}")
        self._muted = not _clean_enabled(cfg.get_music_enabled())
        self._sync_mute_button()

    def _sync_mute_button(self):
        self._mute_btn.setText("Music: Off" if self._muted else "Music: On")
        label = "Unmute music" if self._muted else "Mute music"
        self._mute_btn.setToolTip(label)
        self._mute_btn.setAccessibleName(label)

    def _toggle_mute(self):
        self._muted = not self._muted
        if self._muted:
            _kill_audio()
            _set_audio_enabled(False)
        else:
            _set_audio_enabled(True)
            _start_audio()
        self._sync_mute_button()
        settings = get_screen(self.stack, "ConfigureScreen")
        if settings is not None:
            settings._sync_music_toggle()

    def closeEvent(self, e):
        # Workers are daemon threads: quitting mid-install killed them in the
        # middle of writes (half-built prefixes, half-copied files, unsaved
        # setup state) without a word. Ask while a long operation runs.
        why = inhibit.busy()
        if why and QMessageBox.question(
                self, "Quit DeckOps?",
                f"{why.rstrip('.')}.\n\nQuitting now stops it part way and can leave "
                "games half set up.\n\nQuit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        _kill_audio()
        inhibit.stop()
        super().closeEvent(e)


# ── Entry point ───────────────────────────────────────────────────────────────
def run():
    import crash
    crash.install()     # no-op if main.py already did
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        _load_font()
    except FileNotFoundError as ex:
        # Same fallback as BootstrapScreen._proceed: the app works without the
        # bundled font (it uses the system one), so don't die before the window opens
        from log import get_logger
        get_logger(__name__).warning("bundled font missing, using the system font: %s", ex)
    app.setFont(font(11))
    app.setStyleSheet(_app_style())
    install_controller_nav(app)     # DeckOpsWindow also does this; idempotent
    win = DeckOpsWindow()
    # With a sys.excepthook set, PyQt no longer aborts on an error in a slot;
    # this adds the dialog so the user knows something failed
    crash.install_qt(lambda: win)
    win.showMaximized()
    sys.exit(app.exec())

if __name__ == "__main__":
    run()
