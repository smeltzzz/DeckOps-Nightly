"""
ui_welcome.py — WelcomeScreen, the first screen of the install flow

Moved out of ui_install.py.
"""

import threading

from qt import (
    QWidget, QVBoxLayout, QHBoxLayout, QProgressBar,
)
from qt import Qt

from detect_games import find_steam_root, find_all_games
import config as cfg

from ui_constants import (
    C_IW, C_TREY, C_DIM, C_DARK_BTN, _btn, _lbl, _title_block, _log_to_file, _Sigs,
    ALL_GAMES, go_to, get_screen,
)


# ── WelcomeScreen ─────────────────────────────────────────────────────────────
class WelcomeScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.installed={}; self.screen_name = "WelcomeScreen"
        self.steam_installed={}; self.own_installed={}; self.steam_root=""
        lay = QVBoxLayout(self); lay.setContentsMargins(60,30,60,30); lay.setSpacing(12)
        # Smaller title than other screens: the results + notice need the vertical room at 800px
        _title_block(lay, main_size=40)
        lay.addSpacing(6)
        self.status = _lbl("Scanning for games...", 14, C_DIM)
        lay.addWidget(self.status)
        self.bar = QProgressBar(); self.bar.setMaximum(100); self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        bw = QHBoxLayout(); bw.addStretch(); bw.addWidget(self.bar,6); bw.addStretch()
        lay.addLayout(bw)
        lay.addSpacing(10)
        self.results = _lbl("", 13, C_IW)
        self.results.setTextFormat(Qt.TextFormat.RichText)
        self.notice = _lbl(
            "Before you continue:\n"
            "•  Downgrading a game (MW3, Ghosts, AW) can take a very long time and needs "
            "at least that game's full size free on the drive during the install.\n"
            "•  On a handheld, plug it in. DeckOps keeps the device awake while it "
            "installs, so you don't need to block sleep yourself.\n"
            "•  Use a good, stable internet connection.\n"
            "•  Keep your device somewhere it can breathe. Don't leave it on a blanket or "
            "anywhere it could overheat.",
            12, C_TREY, align=Qt.AlignmentFlag.AlignLeft)
        self.notice.setStyleSheet(
            f"color:{C_TREY};background:#2A1A08;border:1px solid {C_TREY};"
            "border-radius:8px;padding:10px 16px;")
        self.notice.setVisible(False)
        # Games list and notice side by side so neither gets squeezed
        mid = QHBoxLayout(); mid.setSpacing(30)
        mid.addWidget(self.results, 1, Qt.AlignmentFlag.AlignTop)
        mid.addWidget(self.notice, 1, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(mid)
        lay.addStretch()
        self._scanning = False
        self.back = _btn("<< Back", C_DARK_BTN, h=52); self.back.setFixedWidth(180)
        self.back.clicked.connect(self._go_back)
        self.retry = _btn("Scan Again", C_DARK_BTN, h=52); self.retry.setFixedWidth(200)
        self.retry.clicked.connect(self._start_scan)
        self.cont = _btn("Continue >>", C_IW, h=52)
        self.cont.setFixedWidth(260); self.cont.setVisible(False)
        self.cont.clicked.connect(self._go_next)
        cw = QHBoxLayout(); cw.setSpacing(16); cw.addStretch()
        cw.addWidget(self.back); cw.addWidget(self.retry); cw.addWidget(self.cont); cw.addStretch()
        lay.addLayout(cw)

    def showEvent(self, e):
        super().showEvent(e)
        if e.spontaneous():     # window restored, not a screen change
            return
        self._start_scan()

    def _go_back(self):
        go_to(self.stack, "SetupFlowScreen" if cfg.is_first_run() else "ManagementScreen")

    def _start_scan(self):
        if self._scanning: return
        self._scanning = True
        self.bar.setValue(20); self.results.setText(""); self.notice.setVisible(False)
        self.cont.setVisible(False); self.retry.setVisible(False); self.back.setVisible(False)
        self.status.setText("Scanning for Steam and games...")
        self.status.setStyleSheet(f"color:{C_DIM};background:transparent;")
        self._s = _Sigs()
        self._s.done.connect(self._on_scanned)
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        root, merged = "", {}
        try:
            root = find_steam_root() or ""
            if root: merged = find_all_games(root)
        except Exception as ex:
            _log_to_file(f"[WelcomeScreen] scan failed: {ex}")
        self.steam_root, self._merged = root, merged
        self._s.done.emit(bool(root))

    def _on_scanned(self, steam_found):
        self._scanning = False
        self.back.setVisible(True)
        if not steam_found:
            self.bar.setValue(100); self.retry.setVisible(True)
            self.status.setText("Steam not found. Install Steam and open it once, then scan again.")
            self.status.setStyleSheet(f"color:{C_TREY};background:transparent;")
            return
        self.bar.setValue(70)
        merged = self._merged
        self.steam_installed = {k: v for k, v in merged.items() if v.get("source") != "own"}
        self.own_installed = {k: v for k, v in merged.items() if v.get("source") == "own"}
        self.installed = merged
        if cfg.is_lcd():
            lcd_allowed = set()
            for g in ALL_GAMES:
                lcd_allowed.update(g.get("lcd_keys", g["keys"]))
            self.installed = {k:v for k,v in self.installed.items() if k in lcd_allowed}
            self.steam_installed = {k:v for k,v in self.steam_installed.items() if k in lcd_allowed}
            self.own_installed = {k:v for k,v in self.own_installed.items() if k in lcd_allowed}
        self._show_results()

    def _show_results(self):
        self.bar.setValue(100)
        self.cont.setText("Continue >>")
        if not self.installed:
            self.retry.setVisible(True)
            self.status.setStyleSheet(f"color:{C_TREY};background:transparent;")
            if cfg.is_first_run():
                self.status.setText(
                    "No supported games found. If your games are in a custom folder, "
                    "continue and use Choose Folder.")
                self.cont.setText("Choose Folder >>"); self.cont.setVisible(True)
            else:
                self.status.setText("No supported games found.")
            return
        unique = len({g["name"].split(" - ")[0].split(" (")[0] for g in self.installed.values()})
        self.status.setText(f"Found {unique} supported game(s)!")
        self.status.setStyleSheet(f"color:{C_IW};background:transparent;")
        lines = []
        seen_steam = set()
        for g in sorted(self.steam_installed.values(), key=lambda x: x.get("order",99)):
            base = g["name"].split(" - ")[0].split(" (")[0]
            if base not in seen_steam:
                seen_steam.add(base)
                lines.append(f'<span style="color:{C_IW}">{base} (Steam)</span>')
        seen_own = set()
        for g in sorted(self.own_installed.values(), key=lambda x: x.get("order",99)):
            base = g["name"].split(" - ")[0].split(" (")[0]
            if base not in seen_own:
                seen_own.add(base)
                lines.append(f'<span style="color:{C_TREY}">{base} (Non-Steam)</span>')
        self.results.setText("<br>".join(lines)); self.cont.setVisible(True)
        self.notice.setVisible(cfg.is_first_run())

    def _go_next(self):
        if cfg.is_first_run():
            s = get_screen(self.stack, "SetupScreen")
            s.steam_installed = self.steam_installed
            # Keep the games the user added with Choose Folder: this scan
            # doesn't know their folders, and Back -> Continue used to replace
            # the list with it, dropping those games.
            folder_found = {k: v for k, v in (getattr(s, "_folder_found", None) or {}).items()
                            if k not in self.steam_installed}
            s.own_installed   = {**self.own_installed, **folder_found}
            s.steam_root      = self.steam_root
            go_to(self.stack, "SetupScreen")
        else:
            get_screen(self.stack, "ManagementScreen").set_installed(self.installed)
            go_to(self.stack, "ManagementScreen")
