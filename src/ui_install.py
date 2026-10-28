"""
ui_install.py — Install pipeline screens for DeckOps

Screens: WelcomeScreen, SetupScreen, InstallScreen
Extracted from ui_qt.py — all hardcoded stack indices replaced with named lookups.
"""

import html, os, subprocess, threading, time

from qt import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLayout,
    QLabel, QCheckBox, QProgressBar, QPushButton, QButtonGroup,
    QPlainTextEdit, QFileDialog, QMessageBox,
)
from qt import Qt, QTimer, QStorageInfo, QUrl

from detect_games import find_steam_root, find_all_games
import config as cfg
import inhibit
from net import DownloadError

from ui_constants import (
    C_BG, C_CARD, C_SURFACE, C_LINE, C_TEXT, C_IW, C_TREY, C_DIM, C_DARK_BTN,
    font, _btn, _lbl, _title_block, _header_bar, _log_to_file, _log_html, _copy_log_to_clipboard, _Sigs,
    ALL_GAMES, KEY_CLIENT, KEY_MODE_LABEL,
    _active_keys, _active_client,
    _show_preflight,
    go_to, get_screen,
)


# ── WelcomeScreen ─────────────────────────────────────────────────────────────
class WelcomeScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.installed={}; self.screen_name = "WelcomeScreen"
        self.steam_installed={}; self.own_installed={}; self.steam_root=""
        lay = QVBoxLayout(self); lay.setContentsMargins(24,12,24,16); lay.setSpacing(8)
        _title_block(lay, main_size=30)
        self.status = _lbl("Scanning for games...", 14, C_DIM)
        lay.addWidget(self.status)
        self.bar = QProgressBar(); self.bar.setMaximum(100); self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        bw = QHBoxLayout(); bw.addStretch(); bw.addWidget(self.bar,6); bw.addStretch()
        lay.addLayout(bw)
        self.results = _lbl("", 13, C_IW, align=Qt.AlignmentFlag.AlignLeft)
        self.results.setTextFormat(Qt.TextFormat.RichText)
        self.notice = _lbl(
            "Before you continue:\n"
            "•  Downgrading a game (MW3) can take a long time and needs at least "
            "that game's full size free on the drive.\n"
            "•  On a handheld, plug it in. DeckOps keeps the device awake during the install.\n"
            "•  Use a stable internet connection.\n"
            "•  Keep the device somewhere it can cool down, not on a blanket.",
            12, C_TREY, align=Qt.AlignmentFlag.AlignLeft)
        self.notice.setStyleSheet(
            f"color:{C_TREY};background:{C_SURFACE};border:1px solid {C_TREY};"
            "border-radius:8px;padding:10px 16px;")
        self.notice.setVisible(False)
        # Results and safety advice can scroll independently of the navigation.
        self.details = QScrollArea(); self.details.setWidgetResizable(True)
        self.details.setFrameShape(QScrollArea.Shape.NoFrame)
        details_body = QWidget(); mid = QVBoxLayout(details_body)
        mid.setContentsMargins(4, 4, 4, 4); mid.setSpacing(8)
        mid.addWidget(self.results); mid.addWidget(self.notice); mid.addStretch()
        self.details.setWidget(details_body)
        lay.addWidget(self.details, 1)
        self._scanning = False
        self.back = _btn("Back", C_DARK_BTN, size=11, h=42)
        self.back.clicked.connect(self._go_back)
        self.retry = _btn("Scan Again", C_DARK_BTN, size=11, h=42)
        self.retry.clicked.connect(self._start_scan)
        self.cont = _btn("Continue >>", C_IW, size=11, h=42)
        self.cont.setVisible(False)
        self.cont.clicked.connect(self._go_next)
        cw = QHBoxLayout(); cw.setSpacing(8)
        cw.addWidget(self.back); cw.addWidget(self.retry); cw.addWidget(self.cont, 1)
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
        self._scan_error = False
        try:
            root = find_steam_root() or ""
            if root: merged = find_all_games(root)
        except Exception as ex:
            self._scan_error = True
            _log_to_file(f"[WelcomeScreen] scan failed: {ex}")
        self.steam_root, self._merged = root, merged
        self._s.done.emit(bool(root))

    def _on_scanned(self, steam_found):
        self._scanning = False
        self.back.setVisible(True)
        if getattr(self, "_scan_error", False):
            self.bar.setValue(100); self.retry.setVisible(True)
            self.status.setText("Game scan couldn't finish. Try again" +
                                (", or choose a game folder manually." if self.steam_root else
                                 " after opening Steam once."))
            self.status.setStyleSheet(f"color:{C_TREY};background:transparent;")
            if cfg.is_first_run() and self.steam_root:
                self.cont.setText("Choose Folder >>"); self.cont.setVisible(True)
            return
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
        unique = len({gd["base"] for gd in ALL_GAMES
                      if any(k in self.installed for k in _active_keys(gd))})
        self.status.setText(f"Found {unique} supported game(s)!")
        self.status.setStyleSheet(f"color:{C_IW};background:transparent;")
        lines = []
        for source, games, color in (("Steam", self.steam_installed, C_IW),
                                     ("Non-Steam", self.own_installed, C_TREY)):
            for key, game in sorted(games.items(), key=lambda item: item[1].get("order", 99)):
                gd = next((g for g in ALL_GAMES if key in _active_keys(g)), None)
                name = gd["base"] if gd else game.get("name", key)
                mode = SetupScreen._mode_name(key)
                lines.append(f'<span style="color:{color}">'
                             f'{html.escape(name)} · {html.escape(mode)} · {source}</span>')
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
            s._from_manage = False
            go_to(self.stack, "SetupScreen")
        else:
            get_screen(self.stack, "ManagementScreen").set_installed(self.installed)
            go_to(self.stack, "ManagementScreen")


# ── SetupScreen ───────────────────────────────────────────────────────────────
class SetupScreen(QWidget):
    """
    Unified game selection. Shows Steam and non-Steam games in one list.
    """
    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.screen_name = "SetupScreen"
        self.steam_installed={}; self.own_installed={}; self.steam_root=""
        self._from_manage = False  # set by ManagementScreen._add_games on entry
        self._checks={}
        self._choices = {}  # key -> explicit choice, including choices across Back and folder scans
        self._extra_choices = {}
        self._extra_paths = []
        self._folder_found = {}
        self._cod4_choice = "cod4r"
        self._iw4x_dlc_cb = None; self._iw4x_dlc_present = False
        self._zd_cb = None; self._lan_cb = None

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        lay.addWidget(_header_bar()[0])

        # ── Content area ───────────────────────────────────────────────
        content = QWidget()
        clay = QVBoxLayout(content); clay.setContentsMargins(16,10,16,12); clay.setSpacing(6)
        clay.addWidget(_lbl(
            "Choose games and modes. DeckOps creates their Proton prefixes.", 11, C_DIM,
            align=Qt.AlignmentFlag.AlignLeft))
        selrow = QHBoxLayout(); selrow.setSpacing(8)
        selrow.addWidget(_lbl("Scroll for more games", 10, C_DIM,
                              align=Qt.AlignmentFlag.AlignLeft, wrap=False))
        selrow.addStretch()
        for _txt, _on in (("Select All", True), ("Select None", False)):
            _b = _btn(_txt, C_DARK_BTN, size=10, h=32)
            _b.setMinimumWidth(max(110, _b.sizeHint().width()))
            _b.clicked.connect(lambda _c, v=_on: self._set_all(v))
            selrow.addWidget(_b)
        clay.addLayout(selrow)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # One fluid column: the mode labels never compete with fixed-width badges.
        # Extras live in the same scroll area, leaving navigation visible at 800x500.
        self._lw = QWidget(); self._list = QVBoxLayout(self._lw)
        self._list.setContentsMargins(0,0,4,0); self._list.setSpacing(6)
        # QScrollArea otherwise forces this widget down to the viewport height,
        # compressing every game row below its minimum height under the app QSS.
        self._list.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._list.addWidget(_lbl("INFINITY WARD / SLEDGEHAMMER · TREYARCH", 10, C_DIM,
                                  bold=True, align=Qt.AlignmentFlag.AlignLeft))
        self._extras = QWidget(); ex = QVBoxLayout(self._extras)
        ex.setContentsMargins(0,4,0,0); ex.setSpacing(6)
        ex.addWidget(_lbl("OPTIONAL EXTRAS", 10, C_DIM, bold=True,
                          align=Qt.AlignmentFlag.AlignLeft))
        self._tiles = QVBoxLayout(); self._tiles.setSpacing(6); ex.addLayout(self._tiles)
        self._list.addWidget(self._extras)
        self._list.addStretch()
        scroll.setWidget(self._lw); self.scroll = scroll
        clay.addWidget(scroll, stretch=1)

        self.warning = _lbl("", 10, C_TREY, align=Qt.AlignmentFlag.AlignLeft)
        self.warning.setVisible(False); clay.addWidget(self.warning)
        brow = QHBoxLayout(); brow.setSpacing(6)
        back = _btn("Back", C_DARK_BTN, size=10, h=40)
        self.back = back
        back.clicked.connect(self._go_back)
        self._folder_btn = _btn("Choose Folder", C_DARK_BTN, size=10, h=40)
        self._folder_btn.clicked.connect(self._pick_folder)
        self.inst_btn = _btn("Review and install", C_IW, size=10, h=40)
        self.inst_btn.clicked.connect(self._go_install)
        brow.addWidget(back); brow.addWidget(self._folder_btn)
        brow.addWidget(self.inst_btn, stretch=1); clay.addLayout(brow)
        lay.addWidget(content, stretch=1)

    def showEvent(self, e):
        super().showEvent(e)
        if e.spontaneous():     # window restored: keep the user's ticks
            return
        self.warning.setVisible(False)
        self._build()

    def _go_back(self):
        go_to(self.stack, "ManagementScreen" if self._from_manage else "WelcomeScreen")

    def _set_all(self, on):
        """Only touches rows the user could tick anyway."""
        for cb, _gd, _src in self._checks.values():
            if cb.isEnabled():
                cb.setChecked(on)

    def _add_row(self, gd, widget):
        self._list.insertWidget(self._list.indexOf(self._extras), widget)

    @staticmethod
    def _mode_name(key):
        return {"MP": "Multiplayer", "SP": "Singleplayer", "S/Z": "Solo / Zombies",
                "ZM": "Zombies", "DS": "Dedicated Server"}.get(KEY_MODE_LABEL.get(key), key)

    def _build(self):
        while self._list.indexOf(self._extras) > 1:
            item = self._list.takeAt(1)
            if item.widget(): item.widget().deleteLater()
        self._checks.clear()
        self._iw4x_dlc_cb = None; self._iw4x_dlc_present = False
        self._zd_cb = None; self._lan_cb = None

        all_installed = {**self.steam_installed, **self.own_installed}

        for gd in ALL_GAMES:
            keys = _active_keys(gd)
            if not keys: continue
            ik = [k for k in keys if k in all_installed]
            if "iw5mp" in ik and "iw5mp_ds" in ik:
                ik = [k for k in ik if k != "iw5mp_ds"]
                keys = [k for k in keys if k != "iw5mp_ds"]
            if not ik:
                _iw5_keys = {"iw5mp", "iw5mp_ds"}
                if _iw5_keys.intersection(keys) and not _iw5_keys.intersection(all_installed):
                    self._add_mw3_free_row(gd)
                    continue
            # Games that aren't found still show, greyed out, so users know DeckOps supports them

            color = C_IW if gd["dev"] == "iw" else C_TREY

            cw = QWidget(); cw.setObjectName("gameRow")
            cw.setStyleSheet(f"#gameRow{{background:{C_CARD};border-radius:6px;}}")
            outer = QVBoxLayout(cw); outer.setContentsMargins(10, 6, 10, 6); outer.setSpacing(3)
            row_has_own = any(k in self.own_installed and k not in self.steam_installed for k in ik)
            row_has_steam = any(k in self.steam_installed for k in ik)
            source_text = ("Steam + Non-Steam" if row_has_own and row_has_steam else
                           "Non-Steam" if row_has_own else "Steam" if row_has_steam else "Not found")
            title = _lbl(gd["base"], 12, color if ik else C_DIM, bold=True,
                         align=Qt.AlignmentFlag.AlignLeft)
            title.setAccessibleName(gd["base"])
            outer.addWidget(title)
            outer.addWidget(_lbl(f"{source_text} · {_active_client(gd)}", 10, C_DIM,
                                 align=Qt.AlignmentFlag.AlignLeft))
            modes = QHBoxLayout(); modes.setSpacing(12)
            for key in keys:
                is_steam = key in self.steam_installed
                is_own = key in self.own_installed
                installed = is_steam or is_own
                source = "own" if is_own and not is_steam else "steam"
                mode = self._mode_name(key)
                cb = QCheckBox(mode)
                mode_source = "Steam" if is_steam else "Non-Steam" if is_own else "Not found"
                cb.setAccessibleName(f"{gd['base']} · {mode} · {mode_source}")
                cb.setToolTip(cb.accessibleName())
                cb.setFont(font(10))
                cb.setChecked(installed and self._choices.get(
                    key, not cfg.is_game_setup_for_source(key, source)))
                cb.setEnabled(installed)
                if installed:
                    cb.toggled.connect(lambda checked, k=key: self._choices.__setitem__(k, checked))
                modes.addWidget(cb)
                self._checks[key] = (cb, gd, source)
            modes.addStretch()
            outer.addLayout(modes)
            opts = QVBoxLayout(); opts.setContentsMargins(0, 0, 0, 0); opts.setSpacing(3)
            self._row_options(opts, keys, all_installed)
            outer.addLayout(opts)
            self._add_row(gd, cw)

        self._build_extras(all_installed)

    def _build_extras(self, all_installed):
        """Optional add-ons, one tile each. Always shown so users know they exist: a tile greys out
        while its game is unticked or missing (with what to do); Select All never ticks them."""
        while self._tiles.count():
            w = self._tiles.takeAt(0).widget()
            if w: w.deleteLater()
        active = {k for g in ALL_GAMES for k in _active_keys(g)}
        shown = active & set(all_installed)

        def tile(title, note, checked=None, game=None, needs="", missing=""):
            """missing: why it can't be installed yet; the tile shows greyed and returns None."""
            w = QWidget(); w.setObjectName("tile"); w.setStyleSheet(f"#tile{{background:{C_CARD};border-radius:8px;}}")
            v = QVBoxLayout(w); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(2)
            cb = None
            if checked is None and not missing:
                v.addWidget(_lbl(f"{title}: Already Installed", 12, C_DIM, bold=True, align=Qt.AlignmentFlag.AlignLeft))
            else:
                cb = QCheckBox(title); cb.setFont(font(11, True))
                cb.setChecked(self._extra_choices.get(title, bool(checked)) and not missing)
                if not missing:
                    cb.toggled.connect(lambda on, name=title: self._extra_choices.__setitem__(name, on))
                cb.setStyleSheet(f"QCheckBox{{color:{C_TEXT};background:transparent;}} "
                                 f"QCheckBox:disabled{{color:{C_DIM};}}")
                v.addWidget(cb)
            n = _lbl(f"{note} \u00b7 {missing}" if missing else note, 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft); v.addWidget(n)
            if missing:
                cb.setEnabled(False); self._tiles.addWidget(w)
                return None
            gcb = self._checks.get(game, (None,))[0]
            if cb and gcb:
                def sync(on): cb.setEnabled(on); n.setText(note if on else f"{note} \u00b7 {needs}")
                sync(gcb.isChecked()); gcb.toggled.connect(sync)
            self._tiles.addWidget(w)
            return cb

        # Offline Mode: one setting for every Plutonium game, so never greyed out
        from plutonium import GAME_META
        lan_note = ("Offline launcher, an Offline entry in each game's Steam launch menu, and "
                    "bot matches in WaW, BO1, BO2 and MW3 multiplayer.")
        if shown & set(GAME_META):
            self._lan_cb = tile("Plutonium Offline", lan_note, cfg.get_offline_mode())
        elif active & set(GAME_META):
            tile("Plutonium Offline", lan_note, missing="needs WaW, Black Ops, Black Ops II or MW3")
        if "iw4mp" not in shown and "iw4mp" in active:
            tile("IW4x DLC maps", "Free MW2 DLC maps, ~3 GB", missing="needs MW2 Multiplayer")
        elif "iw4mp" in shown:
            from iw4x import is_iw4x_dlc_installed
            idir = all_installed["iw4mp"].get("install_dir", "")
            self._iw4x_dlc_present = bool(idir) and is_iw4x_dlc_installed(idir)
            cb = (tile("IW4x DLC maps", "Free MW2 DLC maps") if self._iw4x_dlc_present else
                  tile("IW4x DLC maps", "Free MW2 DLC maps, ~3 GB, recommended", True, "iw4mp", "tick MW2 Multiplayer"))
            if cb: self._iw4x_dlc_cb = cb
        if "t6zm" in active:
            from zombies_declassified import has_bo2_zm_dlc
            zd_note = "10 classic maps for BO2 Zombies, ~9 GB"
            bdir = all_installed["t6zm"].get("install_dir", "") if "t6zm" in shown else ""
            if cfg.is_zd_installed():
                tile("Zombies Declassified", "10 classic maps for BO2 Zombies")
            elif not bdir:
                tile("Zombies Declassified", zd_note, missing="needs BO2 Zombies with all its DLC")
            elif not has_bo2_zm_dlc(bdir):
                tile("Zombies Declassified", zd_note,
                     missing="needs all BO2 Zombies DLC: install it in Steam or verify the game files")
            else:
                self._zd_cb = tile("Zombies Declassified", zd_note, False, "t6zm", "tick BO2 Zombies")
        self._extras.setVisible(self._tiles.count() > 0)

    def _row_options(self, lay, keys, all_installed):
        """Per-game install choices shown under the row while that mode is ticked (replaces pop-ups)."""
        def _bind(key, w):
            cb = self._checks.get(key, (None,))[0]
            w.setVisible(bool(cb and cb.isChecked()))
            if cb: cb.toggled.connect(w.setVisible)

        def _opt_box():
            w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
            return w, v

        if "cod4mp" in keys and "cod4mp" in all_installed:
            notes = {
                "cod4r": "Built for controllers: native controller support, server browser, bots and quality fixes.",
                "cod4x": "Long-running community client with no native controller support, so you set up the controls yourself.",
            }
            w, v = _opt_box()
            h = QHBoxLayout(); h.setSpacing(6)
            h.addWidget(_lbl("MP client:", 11, C_DIM, wrap=False))
            note = _lbl(notes[self._cod4_choice], 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
            grp = QButtonGroup(w)
            for val, text in (("cod4r", "CoD4R (recommended)"), ("cod4x", "CoD4x")):
                b = QPushButton(text); b.setCheckable(True); b.setFont(font(10, True)); b.setFixedHeight(34)
                b.setStyleSheet(
                    f"QPushButton{{background:{C_DARK_BTN};color:{C_TEXT};border:2px solid transparent;"
                    "border-radius:6px;padding:0 12px;}"
                    f"QPushButton:checked{{background:{C_IW};color:{C_BG};}}"
                    f"QPushButton:focus{{border-color:{C_TEXT};}}")
                grp.addButton(b); b.setChecked(self._cod4_choice == val)
                b.toggled.connect(lambda on, v_=val: on and (setattr(self, "_cod4_choice", v_), note.setText(notes[v_])))
                h.addWidget(b)
            h.addStretch(); v.addLayout(h); v.addWidget(note)
            lay.addWidget(w); _bind("cod4mp", w)

    def _add_mw3_free_row(self, gd):
        cw = QWidget(); row = QHBoxLayout(cw)
        row.setContentsMargins(10, 6, 10, 6)
        row.addWidget(_lbl(f"{gd['base']} · Not found", 11, C_DIM,
                           align=Qt.AlignmentFlag.AlignLeft), 1)
        btn = _btn("Get Free MW3 Server", C_DARK_BTN, size=10, h=36)
        btn.clicked.connect(self._add_mw3_ds)
        row.addWidget(btn)
        self._add_row(gd, cw)

    def _add_mw3_ds(self):
        from depot_downgrade import open_steam_install
        open_steam_install(42750)
        QMessageBox.information(
            self, "MW3 Multiplayer",
            "DeckOps is adding the free MW3 Dedicated Server to your Steam account. "
            "It includes the full multiplayer files.\n\n"
            "Let Steam finish the download, then close and reopen DeckOps. "
            "MW3 will show up as a detected game."
        )

    @staticmethod
    def _removable_mounts():
        # SD cards / USB drives; username-agnostic so it works on Bazzite/CachyOS too
        out = set()
        for v in QStorageInfo.mountedVolumes():
            root = v.rootPath()
            if (v.isValid() and v.isReady() and not v.isReadOnly()
                    and root.startswith(("/run/media/", "/media/", "/mnt/"))):
                out.add(root)
        return sorted(out)

    def _pick_folder(self):
        home = os.path.expanduser("~")
        mounts = self._removable_mounts()
        dlg = QFileDialog(self, "Select your games folder", mounts[0] if len(mounts) == 1 else home)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        # Qt's own dialog honors custom sidebar entries; the KDE/portal one ignores them
        dlg.setOptions(QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks | QFileDialog.Option.DontUseNativeDialog)
        dlg.setSidebarUrls([QUrl.fromLocalFile(home)] + [QUrl.fromLocalFile(m) for m in mounts])
        dlg.resize(1000, 600)
        if not dlg.exec():
            return
        folder = dlg.selectedFiles()[0] if dlg.selectedFiles() else ""
        if not folder:
            return
        # Picking a folder again rescans it (it used to do nothing, silently)
        if folder not in self._extra_paths:
            self._extra_paths.append(folder)
        self._folder_btn.setEnabled(False); self.inst_btn.setEnabled(False)
        self.warning.setText(f"Scanning {folder}...")
        self.warning.setStyleSheet(f"color:{C_DIM};background:transparent;")
        self.warning.setVisible(True)
        self._scan_sigs = _Sigs()
        self._scan_sigs.done.connect(self._on_folder_scanned)
        threading.Thread(target=self._do_folder_scan, daemon=True).start()

    def _do_folder_scan(self):
        from detect_games import find_own_installed
        try:
            found = find_own_installed(extra_paths=self._extra_paths)
        except Exception as ex:
            _log_to_file(f"[SetupScreen] folder scan failed: {ex}")
            self._scanned_own = None        # keep what was found before
            self._scan_sigs.done.emit(False)
            return
        self._scanned_own = {k: v for k, v in found.items() if k not in self.steam_installed}
        self._scan_sigs.done.emit(True)

    def _on_folder_scanned(self, ok):
        if not ok or self._scanned_own is None:
            self._folder_btn.setEnabled(True); self.inst_btn.setEnabled(True)
            self.warning.setText("Couldn't scan that folder. Your list is unchanged; try again.")
            self.warning.setStyleSheet(f"color:{C_TREY};background:transparent;")
            self.warning.setVisible(True)
            return
        own = self._scanned_own
        if cfg.is_lcd():
            lcd_allowed = set()
            for g in ALL_GAMES: lcd_allowed.update(g.get("lcd_keys", g["keys"]))
            own = {k: v for k, v in own.items() if k in lcd_allowed}
        added = len(set(own) - set(self.own_installed))
        self.own_installed = own
        self._folder_found = own  # rescans all saved folders; don't keep removed games
        self._folder_btn.setEnabled(True); self.inst_btn.setEnabled(True)
        self._build()
        self.warning.setText(f"Found {added} new game(s)." if added else "No new supported games in that folder.")
        self.warning.setStyleSheet(f"color:{C_IW if added else C_TREY};background:transparent;")
        self.warning.setVisible(True)

    def _preflight_ok(self, steam_selected, own_selected):
        """Disk space and power. Fast enough to run inline; never blocks the install."""
        import preflight
        rows = [(k, KEY_CLIENT.get(k, ""), g.get("install_dir", ""))
                for k, _gd, g in steam_selected]
        rows += [(k, KEY_CLIENT.get(k, ""), g.get("install_dir", ""))
                 for k, g in own_selected.items()]
        try:
            checks = preflight.check(
                rows,
                iw4x_dlc=bool(self._iw4x_dlc_cb and self._iw4x_dlc_cb.isEnabled() and self._iw4x_dlc_cb.isChecked()),
                zd=bool(self._zd_cb and self._zd_cb.isEnabled() and self._zd_cb.isChecked()),
                downgrade_keys=[k for k, _gd, _g in steam_selected])  # own copies are already 32-bit
        except Exception as ex:
            _log_to_file(f"[SetupScreen] preflight failed: {ex}")
            return True
        return _show_preflight(self, checks)

    def _review_install(self, steam_selected, own_selected):
        rows = [(key, gd, "Steam") for key, gd, _ in steam_selected]
        rows += [(key, gd, "Non-Steam") for key in own_selected for gd in ALL_GAMES
                 if key in _active_keys(gd)]
        msg = QMessageBox(self)
        msg.setWindowTitle("Review installation")
        msg.setTextFormat(Qt.TextFormat.RichText)
        games = "<br>".join(
            f"• {html.escape(gd['base'])} · {html.escape(self._mode_name(key))} "
            f"({source})" for key, gd, source in rows)
        extras = []
        if self._iw4x_dlc_cb and self._iw4x_dlc_cb.isEnabled() and self._iw4x_dlc_cb.isChecked():
            extras.append("IW4x DLC maps (~3 GB)")
        if self._zd_cb and self._zd_cb.isEnabled() and self._zd_cb.isChecked():
            extras.append("Zombies Declassified (~9 GB)")
        if self._lan_cb:
            extras.append("Plutonium Offline: " + ("on" if self._lan_cb.isChecked() else "off"))
        msg.setText("<b>Set up these games and modes?</b><br><br>" + games +
                    ("<br><br><b>Options:</b> " + html.escape(", ".join(extras)) if extras else "") +
                    "<br><br>Downloads and game-file changes may take time. "
                    "You can return to selection without losing your choices.")
        start = msg.addButton("Start Installation", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Back to Selection", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        return msg.clickedButton() is start

    def _go_install(self):
        steam_selected = []
        own_selected = {}
        for key, (cb, gd, source) in self._checks.items():
            if not cb.isChecked(): continue
            if source == "own" and key in self.own_installed:
                own_selected[key] = self.own_installed[key]
            elif key in self.steam_installed:
                steam_selected.append((key, gd, self.steam_installed[key]))

        if not steam_selected and not own_selected:
            self.warning.setText("Select at least one game to continue.")
            self.warning.setStyleSheet(f"color:{C_TREY};background:transparent;")
            self.warning.setVisible(True); return

        if not self._preflight_ok(steam_selected, own_selected):
            return
        if not self._review_install(steam_selected, own_selected):
            return

        s = get_screen(self.stack, "InstallScreen")
        s.steam_selected = steam_selected
        s.own_selected = own_selected
        s.steam_root = self.steam_root
        s._return_to_management = self._from_manage
        s._selection_entry = True
        all_tuples = list(steam_selected)
        for _k, _g in own_selected.items():
            for _gd in ALL_GAMES:
                if _k in _active_keys(_gd):
                    all_tuples.append((_k, _gd, _g)); break
        sel = {k for k, _, _ in all_tuples}
        if "iw4mp" not in sel:
            s.install_iw4x_dlc = ""
        elif self._iw4x_dlc_present:
            s.install_iw4x_dlc = "keep"
        else:
            s.install_iw4x_dlc = "install" if self._iw4x_dlc_cb and self._iw4x_dlc_cb.isChecked() else ""
        s.cod4_client = self._cod4_choice if "cod4mp" in sel else "cod4r"
        s.zd_choice = bool(self._zd_cb and self._zd_cb.isChecked())
        s.offline_mode = self._lan_cb.isChecked() if self._lan_cb else None
        go_to(self.stack, "InstallScreen")

class _SteamStillRunning(RuntimeError):
    """Steam could not be closed, so the install stops before Steam's files."""


# --- _BaseInstallScreen ---
class _BaseInstallScreen(QWidget):
    _DONE_MSG = "Installation complete!"

    def __init__(self, stack, screen_name):
        super().__init__(); self.stack = stack; self.screen_name = screen_name
        self.steam_root = ""
        self._plut_event = threading.Event()
        self._iw5_dg_event = threading.Event()
        self._iw5_method = ""
        self._dg_game_title = ""
        self._iw5_trim_event = threading.Event()
        self._iw5_trim_accept = False
        self._iw5_needs_verify = False
        self._manual_dl_event = threading.Event()
        self._manual_dl_ok = False
        self._retry_dl_event = threading.Event()
        self._retry_dl_choice = ""
        self._zd_event = threading.Event()
        self._zd_accept = False
        self._return_to_management = False
        self._from_manage = False
        self._selection_entry = False
        self._from_setup_selection = False
        self.zd_choice = None
        self.offline_mode = None
        self._results = []
        self._plan = []; self._bands = {}
        self._phase_i = -1; self._phase_id = None
        self._last_pct = 0; self._t0 = 0.0

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        lay.addWidget(_header_bar()[0])

        content = QWidget()
        clay = QVBoxLayout(content); clay.setContentsMargins(20,10,20,12); clay.setSpacing(6)
        self.cur = _lbl("Preparing...", 16, C_TEXT); clay.addWidget(self.cur)
        self.bar = QProgressBar(); self.bar.setMaximum(100); self.bar.setTextVisible(False)
        self.bar.setFixedHeight(22)
        bw = QHBoxLayout(); bw.addWidget(self.bar)
        clay.addLayout(bw)
        self.stat = _lbl("", 13, C_IW); clay.addWidget(self.stat)
        self.iw5_qr_box = QWidget()
        self.iw5_qr_box.setVisible(False)
        qb = QVBoxLayout(self.iw5_qr_box)
        qb.setContentsMargins(0, 0, 0, 0)
        qb.setSpacing(4)
        self.iw5_qr_lbl = QLabel("")
        self.iw5_qr_lbl.setStyleSheet(
            "font-family: monospace; font-size: 8px; line-height: 8px;"
            f"color:{C_BG};background:{C_TEXT};padding:8px;"
            f"border:2px solid {C_LINE};border-radius:8px;"
        )
        self.iw5_qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_row = QHBoxLayout()
        qr_row.addStretch(); qr_row.addWidget(self.iw5_qr_lbl); qr_row.addStretch()
        qb.addLayout(qr_row)
        self.iw5_qr_info = _lbl(
            "Open the Steam app on your phone, tap the shield icon (Steam Guard),\n"
            "then tap \"Approve a sign-in\". Point your camera at the QR code above.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignCenter,
        )
        qb.addWidget(self.iw5_qr_info)
        clay.addWidget(self.iw5_qr_box)

        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFont(font(10))
        self.log.setStyleSheet(f"QPlainTextEdit{{color:{C_DIM};background:transparent;"
                               "border:none;padding:10px;}")
        clay.addWidget(self.log, stretch=1)

        self._log_status = _lbl("", 11, C_DIM, wrap=False)
        log_btn = _btn("Copy Log", C_DARK_BTN, size=11, h=40); log_btn.setFixedWidth(130)
        log_btn.clicked.connect(lambda: _copy_log_to_clipboard(self._log_status))
        lr = QHBoxLayout(); lr.addStretch(); lr.addWidget(log_btn); lr.addWidget(self._log_status); lr.addStretch()
        clay.addLayout(lr)

        self.plut_warn = _lbl(
            "Wait for Plutonium to finish downloading. Log in, close its window, "
            "then confirm below. Do not continue while it is still open.",
            11, C_TREY, align=Qt.AlignmentFlag.AlignCenter,
        )
        self.plut_warn.setStyleSheet(
            f"color:{C_TREY};background:{C_SURFACE};border:1px solid {C_TREY};"
            "border-radius:8px;padding:10px 16px;"
        )
        self.plut_warn.setVisible(False)
        clay.addWidget(self.plut_warn)

        self.plut_btn = _btn("I've closed Plutonium  ✓", C_TREY, size=13, h=52)
        self.plut_btn.setVisible(False)
        self.plut_btn.clicked.connect(self._confirm_plut)
        pw = QHBoxLayout(); pw.addStretch(); pw.addWidget(self.plut_btn); pw.addStretch()
        clay.addLayout(pw)

        self.iw5_dg_btn = _btn("Download complete, continue  ✓", C_IW, size=13, h=52)
        self.iw5_dg_btn.setVisible(False)
        self.iw5_dg_btn.clicked.connect(self._confirm_iw5_dg)
        dw = QHBoxLayout(); dw.addStretch(); dw.addWidget(self.iw5_dg_btn); dw.addStretch()
        clay.addLayout(dw)
        self.manual_step = _lbl("", 10, C_TREY, align=Qt.AlignmentFlag.AlignCenter)
        self.manual_step.setVisible(False)
        clay.addWidget(self.manual_step)

        self.zd_info = _lbl(
            "Zombies Declassified adds 10 classic Zombies maps to BO2 through Plutonium.\n"
            "It's an optional ~9 GB download. You can install it later from Manage.",
            12, C_TEXT, align=Qt.AlignmentFlag.AlignCenter,
        )
        self.zd_info.setStyleSheet(
            f"color:{C_TEXT};background:{C_SURFACE};border:1px solid {C_LINE};"
            "border-radius:8px;padding:10px 16px;"
        )
        self.zd_info.setVisible(False)
        clay.addWidget(self.zd_info)
        self.zd_yes = _btn("Install Zombies Declassified (~9 GB)", C_IW, size=13, h=52)
        self.zd_yes.setVisible(False)
        self.zd_yes.clicked.connect(lambda: self._confirm_zd(True))
        self.zd_skip = _btn("Skip", C_DARK_BTN, size=13, h=52)
        self.zd_skip.setVisible(False)
        self.zd_skip.clicked.connect(lambda: self._confirm_zd(False))
        zw = QHBoxLayout(); zw.addStretch(); zw.addWidget(self.zd_yes); zw.addSpacing(12); zw.addWidget(self.zd_skip); zw.addStretch()
        clay.addLayout(zw)

        self.summary = _lbl("", 12, C_TEXT, align=Qt.AlignmentFlag.AlignLeft)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setStyleSheet(
            f"color:{C_TEXT};background:{C_SURFACE};border:1px solid {C_LINE};"
            "border-radius:8px;padding:8px 12px;")
        self.summary_scroll = QScrollArea(); self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.summary_scroll.setMaximumHeight(132)
        self.summary_scroll.setWidget(self.summary)
        self.summary_scroll.setVisible(False)
        clay.addWidget(self.summary_scroll)

        self.retry_btn = _btn("Retry Failed", C_TREY, size=13, h=52)
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self._retry_failed)

        self.cont_btn = _btn("Continue  >>", C_IW, size=13, h=52)
        self.cont_btn.setVisible(False)
        self.cont_btn.clicked.connect(lambda: go_to(self.stack, "SetupCompleteScreen"))
        cw = QHBoxLayout(); cw.setSpacing(8)
        cw.addWidget(self.retry_btn); cw.addWidget(self.cont_btn, 1)
        clay.addLayout(cw)
        lay.addWidget(content, stretch=1)

        self._s = _Sigs()
        self._s.progress.connect(self._on_progress)
        self._s.log.connect(self._append_log)
        self._s.done.connect(self._on_done)
        self._s.plut_wait.connect(self._show_plut_wait)
        self._s.plut_go.connect(self._hide_plut_wait)
        self._s.iw5_dg_wait.connect(self._show_iw5_dg_wait)
        self._s.iw5_dg_go.connect(self._hide_iw5_dg_wait)
        self._s.iw5_qr_show.connect(self._show_iw5_qr)
        self._s.iw5_qr_hide.connect(self._hide_iw5_qr)
        self._s.iw5_dg_choose.connect(self._ask_iw5_dg_method)
        self._s.iw5_trim_ask.connect(self._ask_iw5_trim)
        self._s.pulse_start.connect(self._start_pulse)
        self._s.pulse_stop.connect(self._stop_pulse)
        self._s.manual_dl.connect(self._show_manual_dl_dialog)
        self._s.retry_dl.connect(self._show_retry_dl_dialog)
        self._s.zd_ask.connect(self._show_zd_ask)
        self._s.zd_go.connect(self._hide_zd_ask)

        self._tick = QTimer()
        self._tick.timeout.connect(self._update_stat)

        self._pulse_timer = QTimer()
        self._pulse_timer.timeout.connect(self._do_pulse)
        self._pulse_msg   = ""
        self._pulse_count = 0

    def _on_progress(self, p, m):
        # Clamp. Phases are ordered, but a stray low value from an installer
        # that reports its own 0-100 must not rewind the bar.
        self._last_pct = min(max(int(p), self._last_pct), 100)
        self.bar.setValue(self._last_pct)
        if m: self.cur.setText(m)
        if self._plan and self._phase_i >= 0:
            self._update_stat()

    def _update_stat(self):
        if not self._plan: return
        el = int(time.time() - self._t0)
        i = min(max(self._phase_i, 0), len(self._plan) - 1)
        self.stat.setText(
            f"Step {i + 1} of {len(self._plan)}  \u00b7  {self._plan[i][1]}"
            f"  \u00b7  {el // 60}:{el % 60:02d} elapsed")

    # Install phases. The pipeline varies with the selection, so the bar's
    # bands are built from the phases that will actually run. Weights are
    # relative cost estimates, not measured times, and are safe to retune.
    # A phase that ends up not running needs no handling: the next _phase()
    # emits its own band start, which is already past the skipped one.
    def _plan_set(self, entries):
        self._plan = [e for e in entries if e[2] > 0]
        total = sum(e[2] for e in self._plan) or 1
        self._bands = {}; acc = 0
        for pid, label, w in self._plan:
            lo = 2 + int(acc / total * 96); acc += w
            self._bands[pid] = (lo, 2 + int(acc / total * 96), label)
        self._phase_i = -1; self._phase_id = None

    def _phase(self, pid):
        """Enter a phase and move the bar to the start of its band."""
        if pid not in self._bands: return
        self._phase_id = pid
        self._phase_i = [p for p, _, _ in self._plan].index(pid)
        lo, _hi, label = self._bands[pid]
        self._s.progress.emit(lo, f"{label}...")

    def _ppct(self, sub, i=0, n=1):
        """Map an installer's 0-100 into the current band, split n ways."""
        if self._phase_id is None: return 0
        lo, hi, _ = self._bands[self._phase_id]
        span = (hi - lo) / max(n, 1)
        return int(lo + span * i + span * min(max(sub, 0), 100) / 100.0)

    def _pmsg(self, msg):
        self._s.progress.emit(self._last_pct, msg)

    def _start_pulse(self, base_msg):
        self._pulse_msg   = base_msg
        self._pulse_count = 0
        self._pulse_timer.start(500)

    def _do_pulse(self):
        dots = "." * (self._pulse_count % 4)
        self.cur.setText(f"{self._pulse_msg}{dots}")
        self._pulse_count += 1

    def _stop_pulse(self):
        self._pulse_timer.stop()

    def _show_plut_wait(self):
        self.plut_warn.setVisible(True)
        self.plut_btn.setVisible(True)

    def _hide_plut_wait(self):
        self.plut_warn.setVisible(False)
        self.plut_btn.setVisible(False)

    def _show_iw5_dg_wait(self, cmd, step_label):
        from depot_downgrade import copy_to_clipboard
        copy_to_clipboard(cmd)
        self.manual_step.setText(f"{step_label}: command copied. Paste it in Steam Console, "
                                 "wait for the download to finish, then continue.")
        self.manual_step.setVisible(True)
        self.iw5_dg_btn.setText(f"{step_label} complete, continue  ✓")
        self.iw5_dg_btn.setVisible(True)

    def _hide_iw5_dg_wait(self):
        self.iw5_dg_btn.setVisible(False)
        self.manual_step.setVisible(False)

    def _confirm_iw5_dg(self):
        self._iw5_dg_event.set()

    def _show_zd_ask(self):
        self.zd_info.setVisible(True)
        self.zd_yes.setVisible(True)
        self.zd_skip.setVisible(True)

    def _hide_zd_ask(self):
        self.zd_info.setVisible(False)
        self.zd_yes.setVisible(False)
        self.zd_skip.setVisible(False)

    def _confirm_zd(self, accepted):
        self._zd_accept = accepted
        self._zd_event.set()

    def _show_iw5_qr(self, qr_text):
        from depot_downgrade import qr_text_to_pixmap
        pm = qr_text_to_pixmap(qr_text, scale=8)
        if pm:
            pm = pm.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
            self.iw5_qr_lbl.setPixmap(pm)
            self.iw5_qr_lbl.setFixedSize(pm.size())
        else:
            self.iw5_qr_lbl.setText(qr_text)
        self.iw5_qr_box.setVisible(True)
        self.log.setMaximumHeight(0)

    def _hide_iw5_qr(self):
        self.iw5_qr_box.setVisible(False)
        self.log.setMaximumHeight(16777215)

    def _ask_iw5_dg_method(self):
        _games = self._dg_game_title or "selected games"
        msg = QMessageBox(self)
        msg.setWindowTitle("Depot Downgrade")
        msg.setText(
            f"These games need older depot files so the community clients work:\n\n"
            f"  {_games}\n\n"
            "This downloads several GB per game (free space was checked). "
            "One QR scan covers all the games above.\n\n"
            "How do you want to sign in for the download?"
        )
        qr_btn = msg.addButton("QR Code Scan (recommended)", QMessageBox.ButtonRole.AcceptRole)
        manual_btn = msg.addButton("Steam Console (manual)", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == qr_btn:
            self._iw5_method = "qr"
        elif clicked == manual_btn:
            self._iw5_method = "manual"
        else:
            self._iw5_method = ""
        self._iw5_dg_event.set()

    def _ask_iw5_trim(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("MW3 Install")
        msg.setText(
            "Trim your MW3 install?\n\n"
            "This removes duplicate 64-bit files that the 32-bit downgrade already "
            "replaces, saving about 5-6 GB.\n\n"
            "Only choose this if you won't play the MW3 campaign through Steam. "
            "(Steam's Verify Integrity restores the files.)"
        )
        trim_btn = msg.addButton("Trim Install", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Keep Both", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        self._iw5_trim_accept = msg.clickedButton() == trim_btn
        self._iw5_trim_event.set()

    def _ask_iw5_verify(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("MW3 Single Player")
        msg.setText(
            "The MW3 campaign needs a quick file check.\n\n"
            "An older DeckOps version replaced its exe with a 32-bit copy that won't "
            "launch through Steam. Steam's Verify Integrity restores the original.\n\n"
            "Multiplayer is not affected."
        )
        verify_btn = msg.addButton("Verify MW3 Now", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        if msg.clickedButton() == verify_btn:
            from wrapper import launch_steam
            launch_steam("steam://validate/42680")

    def _maybe_trim_iw5(self, game_id, install_dir):
        # MP-only MW3: offer to drop the root files duplicated in downgrade/
        if game_id != "iw5" or any(k == "iw5sp" for k, _, _ in self.selected):
            return
        self._iw5_trim_event.clear()
        self._s.iw5_trim_ask.emit()
        self._iw5_trim_event.wait()
        if self._iw5_trim_accept:
            from depot_downgrade import trim_iw5_duplicates
            self._s.log.emit("Trimming duplicate 64-bit files...")
            freed = trim_iw5_duplicates(install_dir, on_progress=lambda m: self._s.log.emit(f"  {m}"))
            self._s.log.emit(f"✓  Trimmed {freed} duplicate files")

    def _run_batch_depot_downgrade(self, dg_jobs):
        """
        Run depot downgrade for multiple games in one session.
        Each job is (game_id, game_name, install_dir, depots, cmds).
        One QR scan / one manual session covers all games.
        Called from the worker thread.
        Returns {game_id: reason} for every game whose downgrade didn't
        finish (cancelled, download failed, merge failed); empty when all
        did. The caller fails those games instead of installing a client
        on the old files.
        """
        from depot_downgrade import (
            ensure_depotdownloader, run_depot_download_qr,
            find_depot_stagings, merge_depots, open_steam_console,
            GAME_CONFIGS, required_space_gb, check_free_space_gb,
        )

        game_names = ", ".join(j[1] for j in dg_jobs)
        self._iw5_dg_event.clear()
        self._iw5_method = ""
        self._dg_game_title = game_names
        self._s.iw5_dg_choose.emit()
        self._iw5_dg_event.wait()

        if not self._iw5_method:
            self._s.log.emit("✗  Depot downgrade cancelled.")
            return {j[0]: "downgrade cancelled" for j in dg_jobs}

        captured_user = None
        failed_ids = {}
        done_ids = set()
        total_depots = sum(len(d) for _, _, _, d, _ in dg_jobs)
        depot_num = 0

        def _merge(game_id, game_name, staging, install_dir):
            self._s.progress.emit(self._ppct(90), f"Merging {game_name} files...")
            self._s.pulse_start.emit(f"Merging {game_name} files")
            merged = False
            try:
                # False = merge_depots' own sanity check found the game
                # still 64-bit (it logs the warning itself)
                merged = merge_depots(
                    game_id, staging, install_dir,
                    on_progress=lambda m: self._s.log.emit(f"  {m}"),
                )
                if not merged:
                    failed_ids[game_id] = "game files still not downgraded after the merge"
            except Exception as ex:
                self._s.log.emit(f"✗  {game_name} merge failed: {ex}")
                failed_ids[game_id] = f"merge failed: {ex}"
            finally:
                self._s.pulse_stop.emit()
            if merged:
                self._s.log.emit(f"✓  {game_name} depot files updated")
                done_ids.add(game_id)
                try:
                    self._maybe_trim_iw5(game_id, install_dir)
                except Exception as ex:
                    self._s.log.emit(f"  MW3 trim skipped: {ex}")

        if self._iw5_method == "qr":
            # DepotDownloader may save a login from here on; the caller removes
            # it (files changed since this time) whether or not this succeeds
            import time as _time
            self._dg_qr_started = _time.time()
            self._s.log.emit(
                f"Downloading depot files for: {game_names}.\n"
                "  Scan the QR code with your Steam mobile app.\n"
                "  The code will refresh if it expires.\n"
                "  Your login credentials will be deleted after the download."
            )
            self._s.progress.emit(self._ppct(2), "Setting up DepotDownloader...")
            try:
                ensure_depotdownloader(
                    on_progress=lambda m: self._s.log.emit(f"  {m}"))

                failed = False
                for game_id, game_name, install_dir, depots, _ in dg_jobs:
                    if failed:
                        # An earlier depot failed; the QR login isn't retried
                        failed_ids[game_id] = "not downloaded (an earlier depot download failed)"
                        continue
                    gcfg = GAME_CONFIGS[game_id]
                    staging = os.path.join(
                        os.path.dirname(install_dir),
                        f".deckops_{game_id}_staging",
                    )
                    for depot in depots:
                        depot_num += 1
                        _dp = int(5 + 80 * (depot_num - 1) / max(total_depots, 1))
                        self._s.progress.emit(
                            self._ppct(_dp), f"Downloading depot {depot_num} of {total_depots}...")
                        result = run_depot_download_qr(
                            staging_dir=staging,
                            depot_info=depot,
                            app_id=gcfg["app_id"],
                            on_qr=lambda qr: self._s.iw5_qr_show.emit(qr),
                            on_auth_success=lambda u: self._s.iw5_qr_hide.emit(),
                            on_progress=lambda m, _p=_dp: self._s.progress.emit(self._ppct(_p), m),
                            on_log=lambda m: self._s.log.emit(f"  {m}"),
                            username=captured_user,
                        )
                        if result is None:
                            self._s.log.emit(f"✗  Depot {depot['depot']} download failed.")
                            failed = True
                            break
                        captured_user = result

                    self._s.iw5_qr_hide.emit()

                    if failed:
                        failed_ids[game_id] = f"depot {depot['depot']} download failed"
                        continue
                    if not os.path.isdir(staging):
                        self._s.log.emit(f"✗  No depot files were downloaded for {game_name}.")
                        failed_ids[game_id] = "no depot files were downloaded"
                        continue
                    _merge(game_id, game_name, staging, install_dir)

            except Exception as ex:
                self._s.log.emit(f"✗  QR downgrade failed: {ex}")
                for j in dg_jobs:
                    if j[0] not in done_ids:
                        failed_ids.setdefault(j[0], f"QR downgrade failed: {ex}")

        elif self._iw5_method == "manual":
            self._s.log.emit(
                f"Downloading depot files for: {game_names}.\n"
                "  DeckOps will open the Steam console. Paste each command\n"
                "  when prompted and wait for \"Depot download complete\"\n"
                "  before clicking continue."
            )
            self._s.progress.emit(self._ppct(2), "Depot download...")
            open_steam_console()

            # Steam saves console downloads under its own steamapps/content,
            # usually the internal drive. The game's drive was checked before;
            # when Steam's is a different one, it needs the room too.
            steam_need = 0.0
            for game_id, game_name, install_dir, depots, cmds in dg_jobs:
                gcfg = GAME_CONFIGS[game_id]
                try:
                    other_drive = os.stat(self.steam_root).st_dev != os.stat(install_dir).st_dev
                except OSError:
                    other_drive = False
                if other_drive:
                    need = required_space_gb(self.steam_root, depots, gcfg["app_id"])
                    free = check_free_space_gb(self.steam_root)
                    if free < steam_need + need:
                        self._s.log.emit(
                            f"✗  {game_name}: the Steam console downloads to {self.steam_root}, "
                            f"which needs about {steam_need + need:.0f} GB free ({free:.0f} GB free).")
                        failed_ids[game_id] = (f"not enough free space on Steam's drive for the console "
                                               f"download (about {steam_need + need:.0f} GB needed)")
                        continue
                    steam_need += need
                for cmd in cmds:
                    depot_num += 1
                    step = f"Depot {depot_num} of {total_depots}"
                    self._s.log.emit(
                        f"\n  Step {depot_num}: Paste this into the Steam console:\n"
                        f"  {cmd}\n"
                        f"  (copied to clipboard)"
                    )
                    self._iw5_dg_event.clear()
                    self._s.iw5_dg_wait.emit(cmd, step)
                    self._iw5_dg_event.wait()
                    self._s.iw5_dg_go.emit()

                # Steam saves each depot under the app it was requested from
                # (MP, DS and DLC depots aren't under the base app)
                staging = find_depot_stagings(
                    self.steam_root, [d.get("app", gcfg["app_id"]) for d in depots])
                if not staging:
                    self._s.log.emit(
                        f"✗  Could not find staging directory for {game_name}.")
                    failed_ids[game_id] = "downloaded depot files not found"
                    continue
                _merge(game_id, game_name, staging, install_dir)

        return failed_ids

    def _seed_results(self):
        """One record per selected key; phases overwrite status as they run."""
        self._results = [{"key": k, "name": gd["base"], "status": "pending", "reason": ""}
                         for k, gd, _g in self.selected]

    def _mark(self, key, status, reason=""):
        for r in self._results:
            if r["key"] == key:
                r["status"] = status; r["reason"] = reason; return

    def _retryable(self):
        return [r for r in self._results if r["status"] in ("failed", "pending", "skipped")]

    def _render_summary(self):
        icons = {"ok": ("✓", C_IW), "failed": ("✗", C_TREY),
                 "skipped": ("–", C_DIM), "pending": ("–", C_DIM)}
        counts = {status: sum(r["status"] == status for r in self._results)
                  for status in ("ok", "failed", "skipped", "pending")}
        rows = [f'<b>{counts["ok"]} ready · {counts["failed"]} failed · '
                f'{counts["skipped"]} skipped · {counts["pending"]} not attempted</b>']
        for r in self._results:
            icon, col = icons.get(r["status"], icons["pending"])
            label = html.escape(f"{r['name']} · {SetupScreen._mode_name(r['key'])}")
            state = {"ok": "ready", "failed": "failed", "skipped": "skipped",
                     "pending": "not attempted"}.get(r["status"], "not attempted")
            txt = f'<span style="color:{col}">{icon}&nbsp;&nbsp;{label} — {state}</span>'
            if r["reason"]:
                txt += f'<span style="color:{C_DIM}"> · {html.escape(r["reason"][:120])}</span>'
            rows.append(txt)
        self.summary.setText("<br>".join(rows))
        self.summary.setVisible(True)
        self.summary_scroll.setVisible(True)

    def _retry_failed(self):
        keys = {r["key"] for r in self._retryable()}
        if not keys: return
        self.steam_selected = [(k, gd, g) for k, gd, g in (self.steam_selected or [])
                               if k in keys]
        self.own_selected = {k: g for k, g in self.own_selected.items() if k in keys}
        # Entry flags were consumed by the first run; preserve them for a retry.
        self._return_to_management = self._from_manage
        self._selection_entry = self._from_setup_selection
        self.zd_choice = self._last_zd_choice
        self.offline_mode = self._last_offline_mode
        self._reset_and_run()

    def _append_log(self, text):
        _log_to_file(text)
        sb = self.log.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.log.appendHtml(_log_html(text))
        if at_bottom: sb.setValue(sb.maximum())

    def _confirm_plut(self):
        self._plut_event.set()

    def _show_retry_dl_dialog(self, label, error_msg):
        msg = QMessageBox(self)
        msg.setWindowTitle(f"{label} — Download Failed")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            f"<b>{html.escape(label)}</b> download failed after multiple attempts.<br><br>"
            f"{html.escape(error_msg)}<br><br>"
            "Retry now, download the file yourself, or skip this game. "
            "A skipped game will appear in the final results and can be retried."
        )
        retry_btn = msg.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        manual_btn = msg.addButton("Download Manually", QMessageBox.ButtonRole.ActionRole)
        msg.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == retry_btn:
            self._retry_dl_choice = "retry"
        elif clicked == manual_btn:
            self._retry_dl_choice = "manual"
        else:
            self._retry_dl_choice = "skip"
        self._retry_dl_event.set()

    def _show_manual_dl_dialog(self, url, dest_folder, filename, label):
        """
        Show a dialog telling the user to manually download a file.
        Runs on the main thread (called via signal from worker).
        """
        os.makedirs(dest_folder, exist_ok=True)
        dest_path = os.path.join(dest_folder, filename)

        msg = QMessageBox(self)
        msg.setWindowTitle(f"{label} — Download Failed")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(
            f"<b>{html.escape(label)}</b> could not be downloaded automatically.<br><br>"
            f'Download it manually here:<br>'
            f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a><br><br>'
            f'Then place <b>{html.escape(filename)}</b> in:<br>'
            f'<code>{html.escape(dest_folder)}</code><br><br>'
            f'Click <b>Open Folder</b> to open the destination, then '
            f'<b>I\'ve Placed It</b> when the file is in place.'
        )
        open_btn = msg.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
        done_btn = msg.addButton("I've Placed It", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Skip", QMessageBox.ButtonRole.RejectRole)

        while True:
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == open_btn:
                subprocess.Popen(["xdg-open", dest_folder])
                continue
            elif clicked == done_btn:
                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                    self._manual_dl_ok = True
                    self._manual_dl_event.set()
                    return
                QMessageBox.warning(
                    self, "File Not Found",
                    f"{filename} was not found in:\n{dest_folder}\n\n"
                    "Make sure the file is downloaded and placed in the correct folder.",
                )
                continue
            else:
                self._manual_dl_ok = False
                self._manual_dl_event.set()
                return

    def _finish_run(self, has_own):
        """Finalize only a fully successful selection; a skip is not completion."""
        complete = bool(self._results) and all(r["status"] == "ok" for r in self._results)
        if complete and cfg.is_first_run():
            cfg.complete_first_run(self.steam_root)
        if has_own and complete:
            self._DONE_MSG = (
                "Installation complete!\n\n"
                "If you enjoy these mods, please consider starring "
                "the original creators' GitHub repositories!")
        self._s.progress.emit(100, "All done!" if complete else "Finished with incomplete games.")
        self._s.done.emit(complete)

    def _on_done(self, ok):
        self._stop_pulse()
        self._tick.stop()
        inhibit.stop()
        el = int(time.time() - self._t0)
        self.stat.setText(f"Finished in {el // 60}:{el % 60:02d}")
        self.zd_choice = None
        self.offline_mode = None
        incomplete = self._retryable()
        complete = ok and bool(self._results) and not incomplete
        if self._results:
            self._render_summary()
        else:
            self.summary_scroll.setVisible(False)
        if complete:
            self.cur.setText(self._DONE_MSG)
            setup = get_screen(self.stack, "SetupScreen")
            if setup is not None:
                setup._choices.clear()
                setup._extra_choices.clear()
        else:
            failed = sum(r["status"] == "failed" for r in self._results)
            skipped = sum(r["status"] == "skipped" for r in self._results)
            pending = sum(r["status"] == "pending" for r in self._results)
            if incomplete:
                message = (f"Setup incomplete: {failed} failed, {skipped} skipped, "
                           f"{pending} not attempted. Review results and log.")
            elif self._results:
                message = "Finalization stopped before completion. Check the log and try again."
            else:
                message = "Setup stopped before game setup. Check the log and try again."
            self.cur.setText(message)
            self.cur.setStyleSheet(f"color:{C_TREY};background:transparent;")
            self.retry_btn.setText(f"Retry Incomplete ({len(incomplete)})")
            self.retry_btn.setVisible(bool(incomplete))
            if self._from_setup_selection:
                try: self.cont_btn.clicked.disconnect()
                except Exception: pass
                self.cont_btn.setText("Back to Game Selection")
                self.cont_btn.clicked.connect(lambda: go_to(self.stack, "SetupScreen"))
        self.cont_btn.setVisible(True)
        # Deferred to the end so Steam isn't killed mid-verify by the install
        if self._iw5_needs_verify:
            self._iw5_needs_verify = False
            self._ask_iw5_verify()

    def _go_management(self):
        go_to(self.stack, "ManagementScreen")

    def showEvent(self, e):
        super().showEvent(e)
        # Spontaneous = the window system re-showing the window (restore from
        # minimised, workspace switch), not a screen change: that used to
        # clear the log and start a second install over the running one
        if e.spontaneous():
            return
        self._reset_and_run()

    def _reset_and_run(self):
        self.bar.setValue(0); self.log.clear()
        self.log.setMaximumHeight(16777215)
        self.cur.setText("Preparing..."); self.cur.setStyleSheet(f"color:{C_TEXT};background:transparent;")
        self.stat.setText("")
        self.plut_btn.setVisible(False)
        self.plut_warn.setVisible(False)
        self.iw5_dg_btn.setVisible(False)
        self.manual_step.setVisible(False)
        self.iw5_qr_box.setVisible(False)
        self.zd_info.setVisible(False); self.zd_yes.setVisible(False); self.zd_skip.setVisible(False)
        self._zd_event.clear(); self._zd_accept = False
        self.summary_scroll.setVisible(False); self.retry_btn.setVisible(False)
        self._results = []
        self._plan = []; self._bands = {}
        self._phase_i = -1; self._phase_id = None
        self._last_pct = 0; self._t0 = time.time()
        self._last_zd_choice = self.zd_choice
        self._last_offline_mode = self.offline_mode
        self._tick.start(1000)
        inhibit.start("DeckOps is installing")
        self.cont_btn.setVisible(False)
        self._plut_event.clear()
        self._iw5_dg_event.clear()
        self._iw5_method = ""
        self._dg_game_title = ""
        self._iw5_needs_verify = False
        self._manual_dl_event.clear()
        self._manual_dl_ok = False
        self._retry_dl_event.clear()
        self._retry_dl_choice = ""
        self._DONE_MSG = _BaseInstallScreen._DONE_MSG
        self._stop_pulse()
        try:
            self.cont_btn.clicked.disconnect()
        except Exception:
            pass
        self._from_manage = self._return_to_management
        self._from_setup_selection = self._selection_entry
        self._selection_entry = False
        if self._return_to_management:
            self.cont_btn.setText("Back to My Games  >>")
            self.cont_btn.clicked.connect(self._go_management)
            self._return_to_management = False
        else:
            self.cont_btn.setText("Continue  >>")
            self.cont_btn.clicked.connect(lambda: go_to(self.stack, "SetupCompleteScreen"))
        _log_to_file(f"── {self.screen_name} started ──")
        QTimer.singleShot(400, lambda: threading.Thread(target=self._run, daemon=True).start())

    def _run(self):
        import traceback as _tb
        try:
            # Before the installs: the Plutonium setup reads it
            if self.offline_mode is not None:
                cfg.set_offline_mode(self.offline_mode)
            _log_to_file(f"Offline Mode: {'on' if cfg.get_offline_mode() else 'off'}")
            self._run_inner()
        except _SteamStillRunning:
            self._s.log.emit("Close Steam yourself (Steam menu → Exit), then run Set Up again. "
                             "Game files already downloaded are kept; Steam's settings were not changed.")
            self._s.progress.emit(100, "Install stopped: Steam is still running.")
            self._s.done.emit(False)
        except Exception:
            err = _tb.format_exc()
            _log_to_file(f"[FATAL] {self.screen_name}._run crashed:\n{err}")
            try:
                self._s.log.emit(f"✗ Install failed with error:\n{err}")
                self._s.progress.emit(100, "Install failed — see log.")
                self._s.done.emit(False)
            except Exception:
                pass

    def _run_inner(self):
        from wrapper import get_proton_path, find_compatdata, kill_steam, set_compat_tool
        from cod4x import install_cod4x
        from cod4r import install_cod4r
        from iw4x import install_iw4x
        from iw3sp import install_iw3sp
        from t6sp_mod import install_t6sp_mod
        from ge_proton import install_ge_proton, resolve_ge_version, MANAGED_APPIDS

        # --- Source awareness setup
        own_selected = self.own_selected
        has_own = bool(own_selected)

        steam_sel = list(self.steam_selected or [])
        own_as_tuples = []
        for k, g in own_selected.items():
            for gd in ALL_GAMES:
                if k in _active_keys(gd):
                    own_as_tuples.append((k, gd, g)); break
        self.selected = steam_sel + own_as_tuples

        if has_own:
            from shortcut import enrich_own_games, write_own_shortcuts

        selected_keys = [key for key, _, _ in self.selected]
        logged_bases  = set()
        has_plut      = any(KEY_CLIENT.get(k) == "plutonium" for k in selected_keys)
        has_cod4      = any(KEY_CLIENT.get(k) in ("cod4r", "cod4x", "iw3sp") for k in selected_keys)
        has_t6sp_mod  = any(KEY_CLIENT.get(k) == "t6sp_mod" for k in selected_keys)
        _sp_keys = [k for k in ("iw4sp", "iw5sp") if k in selected_keys]

        _cod4_client = getattr(self, "cod4_client", "cod4r")
        _steam_killed = False

        # --- Progress plan, built from the phases this selection will run
        def _nk(*clients):
            return len([k for k in selected_keys if KEY_CLIENT.get(k) in clients])
        _DG_CANDIDATES = {"iw5mp", "iw5mp_ds", "iw5sp"}
        _may_downgrade = any(k in _DG_CANDIDATES and k not in own_selected
                             for k in selected_keys)
        self._plan_set([
            ("ge",        "Installing GE-Proton",         4),
            ("own",       "Preparing non-Steam games",    2 if has_own else 0),
            ("prefix",    "Creating Proton prefixes",     8),
            ("cod4",      "Setting up CoD4",              6 * _nk("cod4r", "cod4x", "iw3sp")),
            ("depot",     "Downgrading game files",       24 if _may_downgrade else 0),
            ("plutboot",  "Setting up Plutonium",         10 if has_plut else 0),
            ("plut",      "Installing Plutonium games",   7 * _nk("plutonium")),
            ("t6sp",      "Installing T6SP-MOD",          4 if has_t6sp_mod else 0),
            ("iw4x",      "Installing IW4x",              12 * _nk("iw4x")),
            ("spmod",     "Installing IW-Pad",            4 * len(_sp_keys)),
            ("zd",        "Zombies Declassified",         8 if has_plut and "t6zm" in selected_keys else 0),
            ("finalize",  "Finishing up",                 8),
        ])

        def _kill_steam_once():
            nonlocal _steam_killed
            if not _steam_killed:
                self._pmsg("Closing Steam...")
                self._s.log.emit("Closing Steam...")
                try:
                    kill_steam(on_progress=lambda msg: self._s.log.emit(f"  {msg}"))
                    self._s.log.emit("  ✓ Steam closed.")
                except Exception as ex:
                    # Steam rewrites its config files when it exits: launch
                    # options, compat tools and shortcuts written now would be
                    # lost or mixed. Update Games already stops here; so does install.
                    self._s.log.emit(f"✗  Could not close Steam: {ex}")
                    raise _SteamStillRunning() from ex
                _steam_killed = True

        # --- GE-Proton download (Steam still running)
        ge_version = None
        self._phase("ge")
        try:
            self._s.pulse_start.emit("Installing GE-Proton")
            self._s.log.emit("Installing GE-Proton...")
            ge_version = install_ge_proton(
                on_progress=lambda pct, msg: self._s.progress.emit(self._ppct(pct), msg)
            )
            self._s.pulse_stop.emit()
            self._s.log.emit(f"✓  {ge_version} downloaded")
            cfg.set_ge_proton_version(ge_version)
        except Exception as ex:
            self._s.pulse_stop.emit()
            self._s.log.emit(f"  GE-Proton setup skipped: {ex}")
        # A failed download must not leave games without a compat tool:
        # fall back to a GE-Proton that is already installed.
        if not ge_version:
            ge_version = resolve_ge_version()
            if ge_version:
                self._s.log.emit(f"  Using the installed {ge_version}")
            else:
                self._s.log.emit("✗  No GE-Proton is installed; games may not launch until it is installed. "
                                 "Steam's compatibility settings were left unchanged.")

        proton = get_proton_path(self.steam_root)

        self._seed_results()

        # --- Enrich own game dicts (shortcut_appid, compatdata_path)
        # enrich_own_games computes CRC-based appids, prefix paths, and
        # resolved exe/launch options WITHOUT writing any VDF entries.
        # Must run before ensure_all_prefix_deps. Actual shortcut writing
        # is deferred to write_own_shortcuts() after all mod clients are
        # installed so every target exe exists on disk.
        if has_own:
            self._phase("own")
            self._s.log.emit("Enriching own game data...")
            own_games_dict = dict(own_selected)
            own_games_dict = enrich_own_games(
                own_games=own_games_dict,
                selected_keys=list(own_selected.keys()),
                on_progress=lambda msg: self._s.log.emit(msg),
            )
            _log_to_file("[BREADCRUMB] enrich_own_games returned")
            self.selected = [
                (k, gd, own_games_dict.get(k, g)) for k, gd, g in self.selected
            ]
            own_games = {k: g for k, gd, g in self.selected if g and k in own_selected}
            _log_to_file("[BREADCRUMB] own_games rebuilt, starting prefix init")

        # --- Create prefixes + install deps from GE-Proton default_pfx
        # Every selected game gets its prefix preloaded. Own games use their
        # CRC-based prefix (set by enrich_own_games). Steam games use their
        # Steam appid prefix. ensure_all_prefix_deps handles deduplication
        # and skips prefixes that are already initialized.
        self._phase("prefix")
        self._s.log.emit("Creating Proton prefixes and installing dependencies...")
        self._s.pulse_start.emit("Installing prefix dependencies")
        from ge_proton import ensure_all_prefix_deps
        from detect_games import GAMES as _GAMES_MAP
        dep_targets = []
        for key, gd, game in self.selected:
            if not game:
                continue
            if key in own_selected:
                compat = game.get("compatdata_path", "")
            else:
                appid = _GAMES_MAP[key]["appid"] if key in _GAMES_MAP else gd["appid"]
                compat = find_compatdata(self.steam_root, appid,
                                         game_install_dir=game.get("install_dir"))
                if not compat and game.get("install_dir"):
                    steamapps = os.path.dirname(os.path.dirname(game["install_dir"]))
                    compat = os.path.join(steamapps, "compatdata", str(appid))
            if compat:
                dep_targets.append((key, compat))
        if dep_targets:
            _log_to_file(f"[BREADCRUMB] calling ensure_all_prefix_deps with {len(dep_targets)} targets: {[k for k,_ in dep_targets]}")
            done = ensure_all_prefix_deps(
                ge_version, dep_targets,
                on_progress=lambda msg: self._s.log.emit(msg),
                proton_path=proton,
                steam_root=self.steam_root,
            )
            self._s.log.emit(f"✓  Prefix dependencies: {done}/{len(dep_targets)} ready")
        self._s.pulse_stop.emit()
        _log_to_file("[BREADCRUMB] prefix deps done")

        # --- CoD4 (iw3sp + cod4r/cod4x) -- Steam still running
        # Runs before the Plutonium bootstrapper and before Steam is closed.
        _log_to_file("[BREADCRUMB] starting cod4 install phase")
        if has_cod4:
            cod4_selected = [(k, gd, g) for k, gd, g in self.selected if KEY_CLIENT.get(k) in ("cod4r", "cod4x", "iw3sp")]
            self._phase("cod4")
            _n_c4 = len(cod4_selected)
            for _i_c4, (key, gd, game) in enumerate(cod4_selected):
                base_name = gd["base"]
                self._s.progress.emit(self._ppct(0, _i_c4, _n_c4), f"Setting up {base_name}...")
                def op_cod4(pct, msg, _i=_i_c4):
                    self._s.progress.emit(self._ppct(pct, _i, _n_c4), msg)
                try:
                    source = "own" if key in own_selected else "steam"
                    c = KEY_CLIENT.get(key, gd["client"])
                    if key == "cod4mp":
                        c = _cod4_client
                    if source == "own":
                        compat = game.get("compatdata_path", "")
                        cod4_appid = game.get("shortcut_appid", 7940)
                    else:
                        compat = find_compatdata(self.steam_root, gd["appid"],
                                                  game_install_dir=game.get("install_dir"))
                        cod4_appid = gd["appid"]

                    # One dispatch for the first attempt and every recovery
                    # path, so a retry marks the game the same way the first
                    # attempt would.
                    def _attempt():
                        if c == "cod4r":
                            install_cod4r(game, self.steam_root, proton, compat, op_cod4,
                                          appid=cod4_appid, source=source)
                        elif c == "cod4x":
                            install_cod4x(game, self.steam_root, proton, compat, op_cod4,
                                          appid=cod4_appid)
                        elif c == "iw3sp":
                            install_iw3sp(game, self.steam_root, proton, compat, op_cod4, source=source)

                    def _succeeded(note=""):
                        cfg.mark_game_setup(key, c, source=source)
                        self._mark(key, "ok")
                        if base_name not in logged_bases:
                            self._s.log.emit(f"✓  {base_name} done{note}")
                            logged_bases.add(base_name)

                    _attempt()
                    _succeeded()
                except DownloadError as dl_ex:
                    _dl_resolved = False
                    while not _dl_resolved:
                        self._retry_dl_event.clear()
                        self._retry_dl_choice = ""
                        self._s.retry_dl.emit(dl_ex.label, str(dl_ex))
                        self._retry_dl_event.wait()
                        if self._retry_dl_choice not in ("retry", "manual"):
                            self._s.log.emit(f"  ✗  {base_name} skipped by user.")
                            self._mark(key, "skipped", "skipped by user")
                            _dl_resolved = True
                            continue
                        if self._retry_dl_choice == "manual":
                            self._s.log.emit(f"⚠  {dl_ex.label} — opening manual download dialog")
                            self._manual_dl_event.clear()
                            self._manual_dl_ok = False
                            self._s.manual_dl.emit(
                                dl_ex.url, os.path.dirname(dl_ex.dest),
                                os.path.basename(dl_ex.dest), dl_ex.label,
                            )
                            self._manual_dl_event.wait()
                            if not self._manual_dl_ok:
                                self._s.log.emit(f"  ✗  {base_name} skipped by user.")
                                self._mark(key, "skipped", "skipped by user")
                                _dl_resolved = True
                                continue
                            self._s.log.emit(f"  ✓  {dl_ex.label} placed manually")
                        try:
                            _attempt()
                            _succeeded(" (recovered)")
                            _dl_resolved = True
                        except DownloadError as dl_ex2:
                            dl_ex = dl_ex2
                        except Exception as ex2:
                            self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex2}")
                            self._mark(key, "failed", str(ex2))
                            _dl_resolved = True
                except Exception as ex:
                    self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")
                    self._mark(key, "failed", str(ex))

        # --- Depot downgrades (Steam still running)
        # Detect ALL games that need downgrading, show ONE dialog,
        # run ONE QR scan for everything.
        from depot_downgrade import (
            GAME_CONFIGS as _DG_CONFIGS,
            is_downgrade_needed as _dg_needed,
            detect_dlc_status as _dg_dlc_status,
            check_free_space_gb as _dg_free,
            required_space_gb as _dg_need,
            cleanup_depotdownloader,
            migrate_legacy_iw5 as _dg_migrate_iw5,
        )

        _DG_KEY_MAP = {
            "iw5": ("iw5mp", "iw5mp_ds", "iw5sp"),
        }
        # Each job: (game_id, game_name, install_dir, depots_list, cmds_list)
        _dg_jobs = []
        # Keys whose client needs the downgraded files, per game_id, and the
        # keys whose downgrade didn't happen -> reason. Those are marked failed
        # and their client is skipped: Plutonium on the newer files
        # used to be installed anyway and reported as done.
        _dg_job_keys = {}
        _dg_failed = {}

        def _dg_fail(keys, reason):
            for k in keys:
                _dg_failed[k] = reason

        # Free space: sized from the depots each game will download (a fixed
        # 15 GB was far too little for the older games), added up per drive so two
        # games on one drive both have to fit.
        _dg_reserved = {}

        def _dg_room(game_id, path, depots, keys):
            need = _dg_need(self.steam_root, depots, _DG_CONFIGS[game_id]["app_id"])
            try:
                dev = os.stat(path).st_dev
            except OSError:
                dev = path
            total = _dg_reserved.get(dev, 0.0) + need
            free = _dg_free(path)
            if free < total:
                name = _DG_CONFIGS[game_id]["name"]
                self._s.log.emit(f"✗  {name} downgrade needs about {total:.0f} GB free on that drive "
                                 f"({free:.0f} GB free).")
                _dg_fail(keys, f"not enough free space for the downgrade "
                               f"(about {total:.0f} GB needed, {free:.0f} GB free)")
                return False
            _dg_reserved[dev] = total
            return True

        for _dg_id, _dg_keys in _DG_KEY_MAP.items():
            _matched = [
                (k, gd, g) for k, gd, g in self.selected
                if k in _dg_keys
                and k not in own_selected
                and g.get("install_dir")
            ]
            if not _matched:
                continue
            _gcfg = _DG_CONFIGS[_dg_id]
            _dir = _matched[0][2]["install_dir"]
            _sel = {k for k, _, _ in _matched}

            _forced = _dg_id in self.force_downgrade_ids

            if _dg_id == "iw5":
                # Old in-place downgrades left a 32-bit SP exe in the root
                if _dg_migrate_iw5(_dir):
                    self._iw5_needs_verify = True
                    self._s.log.emit("⚠  MW3 SP still has an old 32-bit exe. Verify MW3 in Steam after install.")
                if cfg.get_setup_games().get("iw5sp", {}).get("client") == "sp_mod":
                    cfg.mark_game_setup("iw5sp", "steam", source="steam")

                # MW3 depot files go into a downgrade/ subfolder so the
                # 64-bit Steam install stays intact. SP never needs
                # downgrade (vanilla 64-bit exe works as-is).
                _has_mp = _sel & {"iw5mp", "iw5mp_ds"}
                if not _has_mp:
                    self._s.log.emit("  MW3 SP does not need downgrade, skipped.")
                    continue

                _is_ds = _sel == {"iw5mp_ds"}
                _needs_base = _forced or _dg_needed("iw5", _dir)
                _dlc_st = _dg_dlc_status("iw5", _dir)
                _dlc_bad = sorted(k for k, v in _dlc_st.items() if v == "wrong")

                if _dlc_bad:
                    _dlc_names = ", ".join(_gcfg["dlc"][k]["name"] for k in _dlc_bad)
                    self._s.log.emit(f"  MW3 DLC needs 32-bit update: {_dlc_names}")

                if not (_needs_base or _dlc_bad):
                    self._s.log.emit("  MW3 is already 32-bit, downgrade skipped.")
                    continue

                self._s.log.emit("Checking MW3... downgrade needed.")

                # MP: base + MP client + language. DS: base + server +
                # language, all requested under the DS app (42750) so a
                # DS-only owner never leans on the MP license.
                if _is_ds:
                    _ds_app = _gcfg["ds_app_id"]
                    _depots = [dict(d, app=_ds_app) for d in _gcfg["depots"]
                               if d["depot"] in _gcfg["ds_depot_ids"]]
                    _dlc_bad = []  # DLC depots only exist under the MP app
                else:
                    _skip = {_gcfg.get("sp_depot_id", 42681), 42751}
                    _depots = [d for d in _gcfg["depots"] if d["depot"] not in _skip]
                _cmds = [f"download_depot {d.get('app', _gcfg['app_id'])} {d['depot']} {d['manifest']}"
                         for d in _depots]

                for dk in _dlc_bad:
                    dlc = _gcfg["dlc"][dk]
                    _depots.append({"depot": dlc["depot"], "manifest": dlc["manifest"], "app": dlc["app"]})
                    _cmds.append(f"download_depot {dlc['app']} {dlc['depot']} {dlc['manifest']}")

                if not _dg_room("iw5", _dir, _depots, _has_mp):
                    continue
                _dg_jobs.append(("iw5", _gcfg["name"], _dir, _depots, _cmds))
                _dg_job_keys["iw5"] = set(_has_mp)

        self.force_downgrade_ids = set()

        # Clean up whenever the QR method ran -- also after a failure or an
        # error, when it used to be skipped (it only ran if a depot succeeded)
        self._dg_qr_started = None
        if _dg_jobs:
            self._phase("depot")
            _dg_res = None
            try:
                _dg_res = self._run_batch_depot_downgrade(_dg_jobs)
            except Exception as ex:
                self._s.log.emit(f"✗  Depot downgrade failed: {ex}")
            finally:
                if self._dg_qr_started is not None:
                    self._s.log.emit("  Removing DepotDownloader and its saved Steam login...")
                    n = cleanup_depotdownloader(since=self._dg_qr_started)
                    if n < 0:
                        self._s.log.emit("✗  Could not remove DepotDownloader's private login data. Check ~/.local/share/deckops/depotdownloader.")
                    elif n:
                        self._s.log.emit("  ✓  DepotDownloader and its private Steam login removed.")
                    else:
                        self._s.log.emit("  ✓  DepotDownloader removed (no private Steam login found).")
            if _dg_res is None:
                _dg_res = {j[0]: "depot downgrade failed" for j in _dg_jobs}
            for _gid, _why in _dg_res.items():
                _dg_fail(_dg_job_keys.get(_gid, ()), f"depot downgrade: {_why}")

        # Their client is skipped below; Retry Failed offers them again
        for _k, _why in _dg_failed.items():
            self._mark(_k, "failed", _why)
        if _dg_failed:
            self._s.log.emit("✗  Skipping the client install for "
                             + ", ".join(sorted(_dg_failed))
                             + " -- the game files were not downgraded")

        # --- Plutonium bootstrapper (Steam still running)
        # Downloads Plutonium and launches it so the user can log in. LCD
        # routes through HGL (shared default prefix); OLED uses the
        # dedicated DeckOps prefix via Proton directly.
        plut_ok = has_plut
        if has_plut:
            from plutonium import (launch_bootstrapper, is_plutonium_ready,
                                   install_plutonium)
            from plutonium import GAME_META as _PLUT_META
            is_lcd = cfg.is_lcd()
            self._phase("plutboot")

            if is_lcd:
                from plutonium_lcd import (launch_bootstrapper_lcd,
                                    is_plutonium_ready_lcd)
                plut_ready = is_plutonium_ready_lcd()
            else:
                plut_ready = is_plutonium_ready()

            # A Plutonium login/download problem only fails the Plutonium games:
            # it used to end the whole batch (done + return), so Steam was never
            # closed and every other game -- CoD4 launch option, IW4x,
            # shortcuts, controller profiles -- was left half set up.
            def _plut_bootstrap():
                """Log in to / update Plutonium. None when ready, else the reason."""
                if not plut_ready:
                    if is_lcd:
                        self._s.progress.emit(self._ppct(0), "Setting up Plutonium through HGL...")
                        self._s.log.emit(
                            "Setting up Plutonium through HGL...\n"
                            "  1. HGL will download and launch Plutonium (this may take a few minutes)\n"
                            "  2. Log in with your Plutonium account\n"
                            "  3. Close the Plutonium window\n"
                            "  4. Click the button below to continue"
                        )
                    else:
                        self._s.progress.emit(self._ppct(0), "Launching Plutonium — please log in...")
                        self._s.log.emit(
                            "Plutonium is launching now.\n"
                            "  1. Wait for it to finish downloading\n"
                            "  2. Log in with your Plutonium account\n"
                            "  3. Close the Plutonium window\n"
                            "  4. Click the button below to continue"
                        )
                    try:
                        if is_lcd:
                            launch_bootstrapper_lcd(
                                on_progress=lambda p, m: self._s.progress.emit(self._ppct(p), m)
                            )
                        else:
                            launch_bootstrapper(
                                proton,
                                on_progress=lambda p, m: self._s.progress.emit(self._ppct(p), m),
                                steam_root=self.steam_root,
                            )
                    except DownloadError as dl_ex:
                        _dl_resolved = False
                        while not _dl_resolved:
                            self._retry_dl_event.clear()
                            self._retry_dl_choice = ""
                            self._s.retry_dl.emit(dl_ex.label, str(dl_ex))
                            self._retry_dl_event.wait()
                            if self._retry_dl_choice == "retry":
                                try:
                                    if is_lcd:
                                        launch_bootstrapper_lcd(
                                            on_progress=lambda p, m: self._s.progress.emit(self._ppct(p), m))
                                    else:
                                        launch_bootstrapper(
                                            proton,
                                            on_progress=lambda p, m: self._s.progress.emit(self._ppct(p), m),
                                            steam_root=self.steam_root)
                                    _dl_resolved = True
                                except DownloadError as dl_ex2:
                                    dl_ex = dl_ex2
                            elif self._retry_dl_choice == "manual":
                                self._manual_dl_event.clear()
                                self._manual_dl_ok = False
                                self._s.manual_dl.emit(
                                    dl_ex.url, os.path.dirname(dl_ex.dest),
                                    os.path.basename(dl_ex.dest), dl_ex.label,
                                )
                                self._manual_dl_event.wait()
                                if not self._manual_dl_ok:
                                    self._s.log.emit("  ✗  Skipped by user.")
                                    return "Plutonium download skipped"
                                self._s.log.emit(f"  ✓  {dl_ex.label} placed manually")
                                _dl_resolved = True
                            else:
                                self._s.log.emit("  ✗  Skipped by user.")
                                return "Plutonium download skipped"
                    except Exception as ex:
                        self._s.log.emit(f"✗  Plutonium launch failed: {ex}")
                        return f"Plutonium launch failed: {ex}"

                    if is_lcd:
                        self._s.log.emit(
                            "⏳  HGL is launching Plutonium. This may take a minute on first run\n"
                            "   while HGL sets up the Wine prefix."
                        )
                        self._s.pulse_start.emit("Waiting for Plutonium login")

                    self._s.plut_wait.emit()
                    self._plut_event.wait()
                    self._s.plut_go.emit()

                    if is_lcd:
                        self._s.pulse_stop.emit()

                    ready_check = is_plutonium_ready_lcd() if is_lcd else is_plutonium_ready()
                    if not ready_check:
                        self._s.log.emit(
                            "✗  Plutonium does not appear to be fully set up.\n"
                            "   Make sure you logged in and let it finish downloading."
                        )
                        return "Plutonium login or download not finished"

                    self._s.log.emit("✓  Plutonium ready.")
                else:
                    self._s.progress.emit(self._ppct(0), "Launching Plutonium to check for updates...")
                    self._s.log.emit(
                        "Plutonium is launching now.\n"
                        "  1. Wait for it to finish updating\n"
                        "  2. Log in if prompted\n"
                        "  3. Close the Plutonium window\n"
                        "  4. Click the button below to continue"
                    )
                    try:
                        if is_lcd:
                            launch_bootstrapper_lcd(
                                on_progress=lambda p, m: self._s.progress.emit(self._ppct(p), m)
                            )
                        else:
                            launch_bootstrapper(
                                proton,
                                on_progress=lambda p, m: self._s.progress.emit(self._ppct(p), m),
                                steam_root=self.steam_root,
                            )
                    except Exception as ex:
                        self._s.log.emit(f"  Plutonium update check skipped: {ex}")

                    if is_lcd:
                        self._s.pulse_start.emit("Waiting for Plutonium update")

                    self._s.plut_wait.emit()
                    self._plut_event.wait()
                    self._s.plut_go.emit()

                    if is_lcd:
                        self._s.pulse_stop.emit()

                    self._s.log.emit("✓  Plutonium ready.")
                return None

            try:
                _plut_reason = _plut_bootstrap()
            except Exception as ex:   # incl. errors raised while retrying a download
                _plut_reason = f"Plutonium setup failed: {ex}"
            if _plut_reason:
                plut_ok = False
                self._s.log.emit(f"✗  {_plut_reason} -- skipping the Plutonium games, "
                                 "continuing with the rest")
                for _k, _gd, _g in self.selected:
                    if KEY_CLIENT.get(_k) == "plutonium":
                        self._mark(_k, "failed", _plut_reason)

        # --- Kill Steam
        _kill_steam_once()

        # --- Clean slate: clear launch options and ALL compat tools
        # Previous installs (LCD->OLED switch, older DeckOps versions) can
        # leave stale launch options and compat tool entries that conflict
        # with the current install. Wipe them first, then re-apply what's
        # needed below. Launch options are only cleared for the Steam games
        # in this run: a per-game Set Up must not wipe the options of games
        # it doesn't reinstall (IW-Pad, IW3SP).
        try:
            from wrapper import clear_launch_options, clear_compat_tool
            from detect_games import GAMES
            run_appids = {GAMES[k]["appid"] for k, _, _ in steam_sel if k in GAMES}
            for appid in MANAGED_APPIDS:
                if appid in run_appids:
                    clear_launch_options(self.steam_root, appid)
            # Only clear mappings that are re-applied below (GE-Proton found
            # and Steam games selected); otherwise earlier setups would lose them.
            if ge_version and steam_sel:
                clear_compat_tool(MANAGED_APPIDS)
            self._s.log.emit("✓  Cleared launch options" +
                             (" and compat tools" if ge_version and steam_sel else ""))
            # The CoD4 phase ran before Steam closed, so its launch option
            # (IW3SP swap, CoD4R pre-launch update) has to be set now.
            if "7940" in run_appids:
                from iw3sp import apply_cod4_launch_option
                if apply_cod4_launch_option(self.steam_root):
                    self._s.log.emit("✓  CoD4 launch option set")
        except Exception as ex:
            self._s.log.emit(f"  Launch option / compat tool cleanup skipped: {ex}")

        # --- Set GE-Proton compat for MANAGED_APPIDS
        # Must run AFTER kill_steam -- Steam overwrites config.vdf on exit.
        # Only write for Steam appids if the user actually has Steam games
        # selected; otherwise this may trigger Steam to re-download games.
        if ge_version and steam_sel:
            try:
                set_compat_tool(MANAGED_APPIDS, ge_version)
                self._s.log.emit(f"✓  {ge_version} set for Steam game appids")
            except Exception as ex:
                self._s.log.emit(f"  CompatToolMapping for Steam appids skipped: {ex}")

        # --- Plutonium games (skipped if the Plutonium login failed)
        if plut_ok:
            try:
                if cfg.get_offline_mode():
                    from shortcut import create_launcher_shortcut
                    create_launcher_shortcut(
                        on_progress=lambda m: self._s.log.emit(m)
                    )
            except Exception as ex:
                self._s.log.emit(f"  Launcher shortcut failed: {ex}")

            # LCD: prepare Heroic's shared prefix
            if cfg.is_lcd():
                try:
                    from ge_proton import ensure_prefix_deps as _epd
                    from plutonium_lcd import HEROIC_DEFAULT_WINE_PREFIX
                    if os.path.isdir(HEROIC_DEFAULT_WINE_PREFIX):
                        _epd(
                            ge_version, HEROIC_DEFAULT_WINE_PREFIX,
                            on_progress=lambda msg: self._s.log.emit(msg),
                            proton_path=proton,
                            steam_root=self.steam_root,
                        )
                        self._s.log.emit("✓  HGL shared prefix ready")
                except Exception as ex:
                    self._s.log.emit(f"  HGL prefix deps skipped: {ex}")

            plut_selected = [(k, gd, g) for k, gd, g in self.selected
                             if KEY_CLIENT.get(k) == "plutonium" and k not in _dg_failed]
            installed_for_plut = {k: g for k, gd, g in self.selected if g}
            total_plut = len(plut_selected)
            self._phase("plut")
            for idx, (key, gd, game) in enumerate(plut_selected):
                base_name = gd["base"]
                if base_name not in logged_bases:
                    self._s.progress.emit(self._ppct(0, idx, total_plut), f"Setting up {base_name}...")
                def op_plut(pct, msg, _i=idx):
                    self._s.progress.emit(self._ppct(pct, _i, total_plut), msg)
                try:
                    source = "own" if key in own_selected else "steam"
                    if source == "own":
                        compat = game.get("compatdata_path", "")
                    else:
                        _plut_appid = _PLUT_META[key][0] if key in _PLUT_META else gd["appid"]
                        compat = find_compatdata(self.steam_root, _plut_appid,
                                                  game_install_dir=game.get("install_dir"))
                    install_plutonium(game, key, self.steam_root, proton, compat,
                                     on_progress=op_plut,
                                     installed_games=installed_for_plut,
                                     source=source)
                    self._mark(key, "ok")
                    if base_name not in logged_bases:
                        self._s.log.emit(f"✓  {base_name} done")
                        logged_bases.add(base_name)
                except Exception as ex:
                    self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")
                    self._mark(key, "failed", str(ex))

        # --- T6SP-MOD (BO2 Singleplayer)
        _log_to_file("[BREADCRUMB] starting t6sp_mod install phase")
        if has_t6sp_mod:
            self._phase("t6sp")
            for key, gd, game in [(k, gd, g) for k, gd, g in self.selected if KEY_CLIENT.get(k) == "t6sp_mod"]:
                base_name = gd["base"]
                self._s.progress.emit(self._ppct(0), "Installing Rattpak's T6SP-MOD (Beta)...")
                def op_t6sp(pct, msg): self._s.progress.emit(self._ppct(pct), msg)
                try:
                    source = "own" if key in own_selected else "steam"
                    if source == "own":
                        compat = game.get("compatdata_path", "")
                    else:
                        compat = find_compatdata(self.steam_root, gd["appid"],
                                                  game_install_dir=game.get("install_dir"))
                    install_t6sp_mod(game, self.steam_root, proton, compat, op_t6sp, source=source)
                    cfg.mark_game_setup(key, "t6sp_mod", source=source)
                    self._mark(key, "ok")
                    self._s.log.emit(f"✓  {base_name} (T6SP-MOD) done")
                    logged_bases.add(base_name)
                except Exception as ex:
                    self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")
                    self._mark(key, "failed", str(ex))

        # --- IW4x
        _log_to_file("[BREADCRUMB] starting iw4x install phase")
        has_iw4x = any(KEY_CLIENT.get(k) == "iw4x" for k in selected_keys)
        if has_iw4x:
            _iw4x_sel = [(k, gd, g) for k, gd, g in self.selected if KEY_CLIENT.get(k) == "iw4x"]
            self._phase("iw4x")
            _n_i4 = len(_iw4x_sel)
            for _i_i4, (key, gd, game) in enumerate(_iw4x_sel):
                base_name = gd["base"]
                self._s.progress.emit(self._ppct(0, _i_i4, _n_i4), f"Setting up {base_name}...")
                def op_iw4x(pct, msg, _i=_i_i4):
                    self._s.progress.emit(self._ppct(pct, _i, _n_i4), msg)
                try:
                    source = "own" if key in own_selected else "steam"
                    if source == "own":
                        compat = game.get("compatdata_path", "")
                    else:
                        compat = find_compatdata(self.steam_root, gd["appid"],
                                                  game_install_dir=game.get("install_dir"))
                    _dlc_action = getattr(self, 'install_iw4x_dlc', '')
                    install_iw4x(game, self.steam_root, proton, compat, op_iw4x, source=source,
                                 install_dlc=(_dlc_action == "install"),
                                 remove_dlc=(_dlc_action == "remove"))
                    cfg.mark_game_setup(key, "iw4x", source=source)
                    self._mark(key, "ok")
                    self._s.log.emit(f"✓  {base_name} done")
                    logged_bases.add(base_name)
                except Exception as ex:
                    self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")
                    self._mark(key, "failed", str(ex))

        # --- MW2/MW3 SP: IW-Pad. Steam (x64) gets IW-Pad + launch option; own (32-bit)
        # also gets the AlterWare SP client (MW2/MW3 only). On failure the game stays vanilla.
        if _sp_keys:
            from sp_mod import install_sp_mod
            self._phase("spmod")
            _sp_games = {k: g for k, _gd, g in self.selected if k in _sp_keys}
            for _i_sp, _sp_key in enumerate(_sp_keys):
                _g = _sp_games.get(_sp_key) or {}
                source = "own" if _sp_key in own_selected else "steam"
                def op_sp(pct, msg, _i=_i_sp):
                    self._s.progress.emit(self._ppct(pct, _i, len(_sp_keys)), msg)
                try:
                    install_sp_mod(_sp_key, _g["install_dir"], op_sp,
                                   source=source, steam_root=self.steam_root)
                    cfg.mark_game_setup(_sp_key, "iw-pad", source=source)
                    self._mark(_sp_key, "ok")
                    self._s.log.emit(f"✓  IW-Pad installed ({_sp_key}, {source})")
                except Exception as ex:
                    _g["_own_launch_options"] = ""
                    self._s.log.emit(f"✗  IW-Pad install failed ({_sp_key}): {ex}")
                    self._mark(_sp_key, "failed", f"IW-Pad: {ex}")

        # --- Zombies Declassified (optional DLC5 for BO2 Zombies via Plutonium)
        if plut_ok and "t6zm" in selected_keys:
            self._phase("zd")
            _zd_requested = self.zd_choice is True

            def _zd_incomplete(reason, status="skipped"):
                if _zd_requested and any(r["key"] == "t6zm" and r["status"] == "ok"
                                         for r in self._results):
                    self._mark("t6zm", status, f"Zombies Declassified: {reason}")

            try:
                # Check BO2 Zombies DLC prerequisite
                _bo2_dir = None
                for _k, _gd, _g in self.selected:
                    if _k == "t6zm" and _g:
                        _bo2_dir = _g.get("install_dir", "")
                        break

                from zombies_declassified import has_bo2_zm_dlc
                if not _bo2_dir or not has_bo2_zm_dlc(_bo2_dir):
                    self._s.log.emit("Zombies Declassified skipped (requires all BO2 Zombies DLC)")
                    _zd_incomplete("requires all BO2 Zombies DLC")
                else:
                    from zombies_declassified import resolve_zd_storage
                    _zd_storage = resolve_zd_storage({k: g for k, gd, g in self.selected}.get("t6zm"))

                    if _zd_storage and os.path.isdir(_zd_storage):
                        # Choice comes from the game-selection row; None means ask now (~9 GB)
                        _zd_yes = self.zd_choice
                        if _zd_yes is None:
                            self._s.progress.emit(self._ppct(0), "Zombies Declassified available")
                            self._s.log.emit("Zombies Declassified (DLC5) is available for BO2 Zombies.")
                            self._zd_event.clear()
                            self._zd_accept = False
                            self._s.zd_ask.emit()
                            self._zd_event.wait()
                            self._s.zd_go.emit()
                            _zd_yes = self._zd_accept
                        _zd_requested = bool(_zd_yes)

                        if _zd_yes:
                            self._s.progress.emit(self._ppct(5), "Installing Zombies Declassified...")
                            self._s.log.emit("Installing Zombies Declassified (DLC5 map pack)...")
                            from zombies_declassified import install_zd
                            def op_zd(pct, msg): self._s.progress.emit(self._ppct(pct), msg)
                            errors = install_zd(_zd_storage, op_zd)
                            from zombies_declassified import get_zd_info as _zd_info
                            info = _zd_info(_zd_storage)
                            if info.get("manifest_hash"):
                                cfg.mark_zd_installed(info["manifest_hash"])
                            if errors:
                                self._s.log.emit(f"⚠  Zombies Declassified installed with {len(errors)} error(s)")
                                _zd_incomplete(f"{len(errors)} install error(s)", "failed")
                            else:
                                self._s.log.emit("✓  Zombies Declassified installed")
                        else:
                            self._s.log.emit("Zombies Declassified skipped (can install later from Manage)")
                    else:
                        self._s.log.emit("⚠  Zombies Declassified skipped (paths not resolved)")
                        _zd_incomplete("game paths not resolved")
            except Exception as ex:
                self._s.log.emit(f"⚠  Zombies Declassified skipped: {ex}")
                _zd_incomplete(str(ex), "failed")

        # --- Clean up ZD files leaked into non-t6zm prefixes
        if has_plut and not cfg.is_lcd():
            try:
                from plutonium import cleanup_zd_from_other_prefixes
                n = cleanup_zd_from_other_prefixes(
                    {k: g for k, gd, g in self.selected if g},
                    self.steam_root,
                    on_progress=lambda msg: self._s.log.emit(msg),
                )
                if n:
                    self._s.log.emit(f"✓  Cleaned ZD files from {n} prefix(es)")
            except Exception as ex:
                self._s.log.emit(f"  ZD cleanup skipped: {ex}")

        # --- Clean up Plutonium dirs leaked into non-Plutonium prefixes
        if has_plut and not cfg.is_lcd():
            try:
                from plutonium import cleanup_plut_from_non_plut_prefixes
                n = cleanup_plut_from_non_plut_prefixes(
                    self.steam_root,
                    on_progress=lambda msg: self._s.log.emit(msg),
                )
                if n:
                    self._s.log.emit(f"✓  Cleaned Plutonium from {n} non-Plutonium prefix(es)")
            except Exception as ex:
                self._s.log.emit(f"  Plutonium cleanup skipped: {ex}")

        # --- Mark vanilla games
        for key, gd, game in self.selected:
            c = KEY_CLIENT.get(key, "")
            source = "own" if key in own_selected else "steam"
            if c == "steam":
                if not cfg.is_game_setup_for_source(key, source):
                    cfg.mark_game_setup(key, "steam", source=source)
                    self._s.log.emit(f"✓  {gd['base']} ({key}) ready")
                self._mark(key, "ok")

        # --- Game display configs
        self._phase("finalize")
        self._s.progress.emit(self._ppct(0), "Applying game configs...")
        try:
            from game_config import apply_game_configs
            applied, skipped, failed = apply_game_configs(
                selected_keys=selected_keys,
                installed_games={k: g for k, gd, g in self.selected if g},
                steam_root=self.steam_root,
                deck_model=cfg.get_deck_model() or "oled",
                on_progress=lambda msg: self._s.log.emit(msg),
            )
            if applied > 0:
                self._s.log.emit(f"✓  Game display configs: {applied} written"
                                 + (f", {skipped} skipped" if skipped else "")
                                 + (f", {failed} failed" if failed else ""))
            elif skipped > 0:
                self._s.log.emit(f"⚠  Game display configs: none applied ({skipped} skipped)")
            else:
                self._s.log.emit("⚠  Game display configs: no eligible configs found")
        except Exception as ex:
            self._s.log.emit(f"  Game configs skipped: {ex}")

        # --- Controller templates
        self._s.progress.emit(self._ppct(30), "Installing controller templates...")
        try:
            from controller_profiles import install_controller_templates, assign_controller_profiles, assign_external_controller_profiles
            install_controller_templates(
                on_progress=lambda msg: self._s.log.emit(f"  {msg}")
            )
            gyro_mode = cfg.get_gyro_mode() or "on"
            assign_controller_profiles(
                gyro_mode,
                on_progress=lambda msg: self._s.log.emit(f"  {msg}")
            )
            self._s.log.emit(f"✓  Neptune controller profiles assigned ({gyro_mode} mode)")
            if cfg.uses_external_controller():
                controller_type = cfg.get_external_controller() or "playstation"
                assign_external_controller_profiles(
                    controller_type,
                    gyro_mode,
                    on_progress=lambda msg: self._s.log.emit(f"  {msg}")
                )
                self._s.log.emit(f"✓  External controller profiles assigned ({controller_type})")
        except Exception as ex:
            self._s.log.emit(f"  Templates skipped: {ex}")

        try:
            from wrapper import set_steam_input_enabled
            set_steam_input_enabled(self.steam_root)
            self._s.log.emit("✓  Steam Input enabled for all games")
        except Exception as ex:
            self._s.log.emit(f"  Steam Input setup skipped: {ex}")

        # --- Write own game shortcuts (VDF, artwork, controllers, compat)
        # All mod client exes now exist on disk. write_own_shortcuts reads
        # the enrichment data set by enrich_own_games() earlier and writes
        # the actual VDF entries, downloads artwork, assigns controller
        # configs, and sets GE-Proton compat tool per shortcut.
        # Plutonium games whose login failed weren't installed: no shortcuts for them
        def _installable(k):
            return plut_ok or KEY_CLIENT.get(k) != "plutonium"

        if has_own:
            gyro_mode = cfg.get_gyro_mode() or "on"
            self._s.progress.emit(self._ppct(60), "Writing own game shortcuts...")
            try:
                write_own_shortcuts(
                    own_games=own_games,
                    selected_keys=[k for k in own_selected.keys() if _installable(k)],
                    gyro_mode=gyro_mode,
                    on_progress=lambda msg: self._s.log.emit(msg),
                )
            except Exception as ex:
                self._s.log.emit(f"  Own shortcuts failed: {ex}")

        # --- Non-Steam shortcuts for Steam games
        steam_keys = [k for k, _, _ in steam_sel]
        # CoD4 MP and WaW MP shortcuts are written every run, so a game set up
        # by an earlier run that stopped before this step still gets one.
        # (Not CoD4R MP, or WaW MP off LCD: those run from the main Steam tile,
        # and create_shortcuts removes their old shortcuts instead.)
        _setup = cfg.get_setup_games()
        sc_extra = [k for k in ("cod4mp", "t4mp") if k not in steam_keys
                    and _setup.get(k, {}).get("source") == "steam"]
        if steam_sel or sc_extra:
            try:
                from shortcut import create_shortcuts
                self._s.log.emit("Creating non-Steam shortcuts...")
                steam_installed = {k: g for k, gd, g in steam_sel if g}
                if sc_extra:
                    from detect_games import parse_library_folders, find_installed_games
                    found = find_installed_games(parse_library_folders(self.steam_root), self.steam_root)
                    steam_installed.update({k: found[k] for k in sc_extra if k in found})
                gyro_mode = cfg.get_gyro_mode() or "on"
                create_shortcuts(
                    installed_games=steam_installed,
                    selected_keys=[k for k in steam_keys if _installable(k)] + sc_extra,
                    gyro_mode=gyro_mode,
                    on_progress=lambda msg: self._s.log.emit(msg),
                    steam_root=self.steam_root,
                )
            except Exception as ex:
                self._s.log.emit(f"  Steam shortcuts skipped: {ex}")

            # Set default launch option so Steam Deck skips the mode picker
            has_cod4_steam = any(KEY_CLIENT.get(k) in ("cod4r", "cod4x", "iw3sp")
                                for k in steam_keys)
            has_waw_steam = any(k in ("t4sp", "t4mp") for k in steam_keys)
            defaults = {}
            if has_cod4_steam:
                # CoD4 -> Singleplayer. With CoD4R, MP is the main menu's
                # Multiplayer button on this same tile.
                defaults["7940"] = ("7a722f97", "1")
            # WaW keeps Steam's picker for its Plutonium entries
            if defaults:
                try:
                    from wrapper import set_default_launch_option
                    set_default_launch_option(self.steam_root, defaults)
                    self._s.log.emit("✓  Default launch options set (SP mode)")
                except Exception as ex:
                    self._s.log.emit(f"  Launch options skipped: {ex}")
            if has_waw_steam:
                try:
                    from wrapper import clear_default_launch_option
                    clear_default_launch_option(self.steam_root, ["10090"])
                except Exception as ex:
                    self._s.log.emit(f"  WaW launch picker reset skipped: {ex}")

        # --- Steam artwork
        if steam_sel:
            self._s.progress.emit(self._ppct(85), "Applying Steam artwork...")
            try:
                from shortcut import apply_steam_artwork
                self._s.log.emit("Applying custom artwork for multiplayer games...")
                apply_steam_artwork(
                    selected_keys=steam_keys,
                    on_progress=lambda msg: self._s.log.emit(msg)
                )
            except Exception as ex:
                self._s.log.emit(f"  Steam artwork skipped: {ex}")

        # --- Done
        _log_to_file("[BREADCRUMB] all phases complete, finishing up")
        self._finish_run(has_own)



# --- InstallScreen ---
class InstallScreen(_BaseInstallScreen):
    def __init__(self, stack):
        super().__init__(stack, "InstallScreen")
        self.selected = []
        self.own_selected = {}
        self.steam_selected = None
        self.force_downgrade_ids = set()



