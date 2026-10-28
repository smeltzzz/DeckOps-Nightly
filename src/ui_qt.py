"""
ui_qt.py — DeckOps main UI entry point

Slim shell: BootstrapScreen, DeckOpsWindow, and run().
All screen classes are imported from split modules:
    ui_constants  — shared constants, helpers, navigation
    ui_setup      — first-run setup flow (OS → Device → Gyro → ...)
    ui_install    — install pipeline (Welcome, Setup, Install)
    ui_manage     — post-install (Management, Configure, SetupComplete, Update)
"""

import sys, os

from qt import QApplication, QMainWindow, QStackedWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QWidget, QMessageBox, QStatusBar
from qt import Qt, QTimer

import config as cfg
import inhibit
from identity import APP_TITLE

from ui_constants import (
    C_CARD, C_DIM, C_DARK_BTN, C_LINE, C_TEXT, font, _lbl, _title_block,
    _load_font, _app_style, _start_audio, _kill_audio,
    _set_audio_enabled,
    go_to, get_screen,
)

from ui_setup import SetupFlowScreen
from ui_install import WelcomeScreen, SetupScreen, InstallScreen
from ui_manifest import ManifestModsScreen
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
            ManifestModsScreen,
            WelcomeScreen,
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
        strip.setFixedHeight(38)
        strip.setSizeGripEnabled(False)
        strip.setStyleSheet(
            f"QStatusBar{{background:{C_CARD};border-top:1px solid {C_LINE};}}"
            f"QStatusBar QLabel{{background:transparent;color:{C_DIM};padding:0 10px;}}")
        self.setStatusBar(strip)
        self._screen_label = QLabel()
        self._screen_label.setFont(font(9, bold=True))
        strip.addWidget(self._screen_label)
        self._dbg_label = QLabel()
        self._dbg_label.setFont(font(9))
        strip.addPermanentWidget(self._dbg_label)
        self._dbg_label.setVisible(os.environ.get("DECKOPS_DEBUG") == "1")

        self._muted = not cfg.get_music_enabled()
        self._mute_btn = QPushButton(self)
        self._mute_btn.setFixedSize(112, 30)
        self._mute_btn.setFont(font(10, bold=True))
        self._mute_btn.setStyleSheet(
            f"QPushButton{{background:{C_DARK_BTN};color:{C_TEXT};border:1px solid {C_LINE};"
            "border-radius:6px;padding:0 8px;}"
            "QPushButton:hover,QPushButton:focus{border:2px solid #FFFFFF;}")
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        strip.addPermanentWidget(self._mute_btn)
        self._sync_mute_button()
        self.stack.currentChanged.connect(self._update_dbg_label)
        self._update_dbg_label(0)

    def _update_dbg_label(self, idx):
        w = self.stack.widget(idx)
        name = getattr(w, "screen_name", w.__class__.__name__)
        titles = {
            "BootstrapScreen": "OPENING", "SetupFlowScreen": "DEVICE SETUP",
            "ManifestModsScreen": "OPTIONAL MODS", "WelcomeScreen": "FIND GAMES",
            "SetupScreen": "SELECT GAMES", "InstallScreen": "INSTALLATION",
            "ManagementScreen": "GAME LIBRARY", "ConfigureScreen": "SETTINGS",
            "SetupCompleteScreen": "GUIDE", "UpdateScreen": "GAME UPDATES",
        }
        self._screen_label.setText(titles.get(name, name.upper()))
        self._dbg_label.setText(f"[{idx}] {name}")
        self._muted = not cfg.get_music_enabled()
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
    _load_font()
    app.setFont(font(11))
    app.setStyleSheet(_app_style())
    win = DeckOpsWindow()
    # With a sys.excepthook set, PyQt no longer aborts on an error in a slot;
    # this adds the dialog so the user knows something failed
    crash.install_qt(lambda: win)
    win.showMaximized()
    sys.exit(app.exec())

if __name__ == "__main__":
    run()
