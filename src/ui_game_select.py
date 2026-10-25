"""
ui_game_select.py — SetupScreen, the unified game selection screen

Steam and non-Steam games in one list. Moved out of ui_install.py.
"""

import os, threading

from qt import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QCheckBox, QPushButton, QButtonGroup,
    QFileDialog, QMessageBox,
)
from qt import Qt, QStorageInfo, QUrl

import config as cfg

from ui_constants import (
    C_IW, C_TREY, C_DIM, C_DARK_BTN, C_BLUE_BTN, C_CARD,
    font, _btn, _lbl, _header_bar, _badge, _log_to_file, _Sigs,
    ALL_GAMES, KEY_CLIENT, KEY_MODE_LABEL,
    _active_keys, _active_client,
    _ask_bo3_client, _show_preflight,
    go_to, get_screen,
)


# ── SetupScreen ───────────────────────────────────────────────────────────────
class SetupScreen(QWidget):
    """
    Unified game selection. Shows Steam and non-Steam games in one list.
    """
    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.screen_name = "SetupScreen"
        self.steam_installed={}; self.own_installed={}; self.steam_root=""
        self._checks={}
        self._extra_paths = []
        self._folder_found = {}
        self._cod4_choice = "cod4r"
        self._iw4x_dlc_cb = None; self._iw4x_dlc_present = False
        self._zd_cb = None; self._lan_cb = None

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        lay.addWidget(_header_bar()[0])

        # ── Content area ───────────────────────────────────────────────
        content = QWidget()
        clay = QVBoxLayout(content); clay.setContentsMargins(60,20,60,40); clay.setSpacing(14)
        clay.addWidget(_lbl(
            "Choose which games to set up. "
            "DeckOps will create Proton prefixes automatically.", 13, C_DIM))
        selrow = QHBoxLayout(); selrow.setSpacing(8); selrow.addStretch()
        for _txt, _on in (("Select All", True), ("Select None", False)):
            _b = _btn(_txt, C_DARK_BTN, size=10, h=30); _b.setFixedWidth(110)
            _b.clicked.connect(lambda _c, v=_on: self._set_all(v))
            selrow.addWidget(_b)
        clay.addLayout(selrow)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Two columns (IW/SHG | Treyarch) so the whole list fits at 1280x800 without scrolling
        self._lw = QWidget(); cols = QHBoxLayout(self._lw)
        cols.setContentsMargins(0,0,0,0); cols.setSpacing(24)
        self._cols = {}
        for dev, title, color in (("iw", "INFINITY WARD / SLEDGEHAMMER", C_IW), ("trey", "TREYARCH", C_TREY)):
            col = QVBoxLayout(); col.setSpacing(0)
            col.addWidget(_lbl(title, 11, color, bold=True, align=Qt.AlignmentFlag.AlignLeft, wrap=False))
            col.addStretch()
            cols.addLayout(col, 1); self._cols[dev] = col
        scroll.setWidget(self._lw); clay.addWidget(scroll, stretch=1)
        # Extras: optional add-ons as tiles under both columns, filled by _build_extras
        self._extras = QWidget(); ex = QVBoxLayout(self._extras); ex.setContentsMargins(0,0,0,0); ex.setSpacing(8)
        ex.addWidget(_lbl("EXTRAS", 11, C_DIM, bold=True, align=Qt.AlignmentFlag.AlignLeft, wrap=False))
        self._tiles = QHBoxLayout(); self._tiles.setSpacing(12); ex.addLayout(self._tiles)
        clay.addWidget(self._extras)

        self.warning = _lbl("", 12, C_TREY, align=Qt.AlignmentFlag.AlignLeft)
        self.warning.setVisible(False); clay.addWidget(self.warning)
        brow = QHBoxLayout(); brow.setSpacing(16)
        back = _btn("<< Back", C_DARK_BTN, h=52); back.setFixedWidth(180)
        back.clicked.connect(lambda: go_to(self.stack, "WelcomeScreen"))
        self._folder_btn = _btn("Choose Folder", C_DARK_BTN, h=52)
        self._folder_btn.setFixedWidth(200)
        self._folder_btn.clicked.connect(self._pick_folder)
        self.inst_btn = _btn("Install Selected >>", C_IW, h=52)
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

    def _set_all(self, on):
        """Only touches rows the user could tick anyway."""
        for cb, _gd, _src in self._checks.values():
            if cb.isEnabled():
                cb.setChecked(on)

    def _add_row(self, gd, widget):
        col = self._cols["iw" if gd["dev"] == "iw" else "trey"]
        col.insertWidget(col.count() - 1, widget)

    @staticmethod
    def _short_name(base):
        return base.replace("Call of Duty 4: Modern Warfare", "Modern Warfare 1").replace("Call of Duty: ", "")

    def _build(self):
        for col in self._cols.values():
            # keep the column title (first) and trailing stretch (last)
            while col.count() > 2:
                item = col.takeAt(1)
                if item.widget(): item.widget().deleteLater()
        self._checks.clear()
        self._iw4x_dlc_cb = None; self._iw4x_dlc_present = False
        self._zd_cb = None; self._lan_cb = None

        MAX_SLOTS  = 3
        SLOT_W     = 28
        SLOT_GAP   = 8
        CHECKS_W   = MAX_SLOTS * SLOT_W + (MAX_SLOTS - 1) * SLOT_GAP

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
                    self._add_mw3_free_row(gd, CHECKS_W)
                    continue
            # Games that aren't found still show, greyed out, so users know DeckOps supports them

            color  = C_IW if gd["dev"] == "iw" else C_TREY
            client = _active_client(gd)

            cw = QWidget()
            outer = QVBoxLayout(cw); outer.setContentsMargins(6, 6, 6, 6); outer.setSpacing(4)
            row = QHBoxLayout(); row.setSpacing(10)

            checks_widget = QWidget()
            checks_widget.setFixedWidth(CHECKS_W)
            checks_layout = QHBoxLayout(checks_widget)
            checks_layout.setContentsMargins(0, 0, 0, 0)
            checks_layout.setSpacing(SLOT_GAP)

            for key in keys:
                is_steam = key in self.steam_installed
                is_own   = key in self.own_installed
                installed = is_steam or is_own
                source = "own" if is_own and not is_steam else "steam"
                already_done = cfg.is_game_setup_for_source(key, source)

                slot = QWidget()
                slot.setFixedWidth(SLOT_W)
                slot_lay = QVBoxLayout(slot)
                slot_lay.setContentsMargins(0, 0, 0, 0)
                slot_lay.setSpacing(2)
                slot_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

                cb = QCheckBox()
                if not installed:
                    cb.setChecked(False); cb.setEnabled(False)
                elif already_done:
                    cb.setChecked(False)
                else:
                    cb.setChecked(True)

                mode_color = C_TREY if not installed else "#666677"
                mode_lbl = _lbl(KEY_MODE_LABEL.get(key, key), 10, mode_color,
                                 align=Qt.AlignmentFlag.AlignHCenter, wrap=False)

                slot_lay.addWidget(cb, alignment=Qt.AlignmentFlag.AlignHCenter)
                slot_lay.addWidget(mode_lbl)
                checks_layout.addWidget(slot)
                self._checks[key] = (cb, gd, source)

            for _ in range(MAX_SLOTS - len(keys)):
                spacer = QWidget()
                spacer.setFixedSize(SLOT_W, 38)
                spacer.setStyleSheet("background: transparent;")
                checks_layout.addWidget(spacer)

            row.addWidget(checks_widget)
            row.addWidget(_lbl(self._short_name(gd["base"]), 13, "#FFF" if ik else "#555566",
                               align=Qt.AlignmentFlag.AlignLeft, wrap=False), stretch=1)

            row_has_own = any(k in self.own_installed and k not in self.steam_installed for k in ik)
            row_has_steam = any(k in self.steam_installed for k in ik)
            if not ik:
                src_text, src_color = "NOT FOUND", C_DARK_BTN
            elif row_has_own and row_has_steam:
                src_text, src_color = "BOTH", C_TREY
            elif row_has_own:
                src_text, src_color = "OWN", C_TREY
            else:
                src_text, src_color = "STEAM", C_IW
            row.addWidget(_badge(src_text, src_color, 84, h=24, size=9, radius=4))
            row.addWidget(_badge(client.upper(), color, 170, h=28, size=9))
            outer.addLayout(row)

            # Options sit under the row, indented past the checkbox column
            opts = QVBoxLayout(); opts.setContentsMargins(CHECKS_W + 10, 0, 0, 0); opts.setSpacing(4)
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
                cb = QCheckBox(title); cb.setFont(font(12, True)); cb.setChecked(bool(checked) and not missing)
                cb.setStyleSheet("QCheckBox{color:#FFF;background:transparent;} QCheckBox:disabled{color:#666677;}")
                v.addWidget(cb)
            n = _lbl(f"{note} \u00b7 {missing}" if missing else note, 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft); v.addWidget(n)
            if missing:
                cb.setEnabled(False); self._tiles.addWidget(w, 1)
                return None
            gcb = self._checks.get(game, (None,))[0]
            if cb and gcb:
                def sync(on): cb.setEnabled(on); n.setText(note if on else f"{note} \u00b7 {needs}")
                sync(gcb.isChecked()); gcb.toggled.connect(sync)
            self._tiles.addWidget(w, 1)
            return cb

        # Offline Mode: one setting for every Plutonium game, so never greyed out
        from plutonium import GAME_META
        lan_note = ("Offline launcher, Plutonium Offline in each game's Steam launch menu, "
                    "bot matches in WaW / BO1 / BO2 / MW3 MP.")
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
                "cod4r": "Native controller support, server browser, bots and quality-of-life fixes. Built for controllers.",
                "cod4x": "Established community client. No native controller support, so you'll set up controls manually.",
            }
            w, v = _opt_box()
            h = QHBoxLayout(); h.setSpacing(6)
            h.addWidget(_lbl("MP client:", 11, C_DIM, wrap=False))
            note = _lbl(notes[self._cod4_choice], 10, "#777788", align=Qt.AlignmentFlag.AlignLeft)
            grp = QButtonGroup(w)
            for val, text in (("cod4r", "CoD4R (recommended)"), ("cod4x", "CoD4x")):
                b = QPushButton(text); b.setCheckable(True); b.setFont(font(10, True)); b.setFixedHeight(34)
                b.setStyleSheet(
                    f"QPushButton{{background:{C_DARK_BTN};color:#AAA;border:none;border-radius:6px;padding:0 12px;}}"
                    f"QPushButton:checked{{background:{C_IW};color:#FFF;}}")
                grp.addButton(b); b.setChecked(self._cod4_choice == val)
                b.toggled.connect(lambda on, v_=val: on and (setattr(self, "_cod4_choice", v_), note.setText(notes[v_])))
                h.addWidget(b)
            h.addStretch(); v.addLayout(h); v.addWidget(note)
            lay.addWidget(w); _bind("cod4mp", w)

    def _add_mw3_free_row(self, gd, checks_w):
        cw = QWidget()
        row = QHBoxLayout(cw); row.setSpacing(10); row.setContentsMargins(6, 6, 6, 6)
        btn = _btn("Get Free", C_BLUE_BTN, size=11, h=40); btn.setFixedWidth(checks_w)
        btn.clicked.connect(self._add_mw3_ds)
        row.addWidget(btn)
        row.addWidget(_lbl(self._short_name(gd["base"]), 13, "#555566", align=Qt.AlignmentFlag.AlignLeft, wrap=False), stretch=1)
        color = C_IW if gd["dev"] == "iw" else C_TREY
        row.addWidget(_badge(_active_client(gd).upper(), color, 170, h=28, size=9))
        self._add_row(gd, cw)

    def _add_mw3_ds(self):
        from depot_downgrade import open_steam_install
        open_steam_install(42750)
        QMessageBox.information(
            self, "MW3 Multiplayer",
            "DeckOps is adding the free MW3 Dedicated Server to your "
            "Steam account. This includes the full multiplayer files.\n\n"
            "Let Steam finish the download, then close and reopen "
            "DeckOps. MW3 will appear as a detected game."
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
        self._folder_found = own        # WelcomeScreen merges these back after Back -> Continue
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

        s = get_screen(self.stack, "InstallScreen")
        s.steam_selected = steam_selected
        s.own_selected = own_selected
        s.steam_root = self.steam_root
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
        s.bo3_client = _ask_bo3_client(self, all_tuples)
        go_to(self.stack, "InstallScreen")
