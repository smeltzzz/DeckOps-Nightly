"""
ui_manage.py — Post-install management screens for DeckOps

Screens: ManagementCard, ManagementScreen, SetupCompleteScreen.
ConfigureScreen is in ui_configure.py and UpdateScreen in ui_update.py; the update
check itself is in updater.py.
Extracted from ui_qt.py — all hardcoded stack indices replaced with named lookups.
"""

import os, shutil, threading

from qt import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea,
    QLabel, QPushButton, QCheckBox, QFrame, QSizePolicy, QMessageBox,
)
from qt import Qt, QTimer, QObject, pyqtSignal
from qt import QPixmap
from qt import QGraphicsOpacityEffect

from detect_games import find_steam_root, find_all_games
import config as cfg
import inhibit

from ui_constants import (
    C_CARD, C_IW, C_TREY, C_DIM, C_DARK_BTN, C_RED_BTN, font, _btn, _lbl, _hdiv, _log_to_file, _Sigs, _detached_open, _header_bar, _header_path, _ask_iw4x_dlc,
    _show_preflight,
    ALL_GAMES, KEY_CLIENT,
    _active_keys, _active_client, _active_appid,
    SP_IMAGE_URLS, IMG_RATIO, CARD_COLS, CARD_MAX_W,
    HEROES_DIR, PROJECT_ROOT,
    go_to, get_screen,
)

from identity import GITHUB_USER, GITHUB_REPO
from log import get_logger
from updater import check_update_status

_log = get_logger(__name__)


# ── ManagementCard ────────────────────────────────────────────────────────────
class ManagementCard(QFrame):
    def __init__(self, gd, installed, on_setup, on_configure,
                 on_readd=None, on_add_ds=None, on_offline=None, parent=None):
        super().__init__(parent)
        color = C_IW if gd["dev"] == "iw" else C_TREY
        self._color  = color
        self._appid  = _active_appid(gd)
        self._is_lan = gd.get("client") == "lan"
        self.setObjectName("MC")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMaximumWidth(CARD_MAX_W)

        keys       = _active_keys(gd)
        client     = _active_client(gd)

        if self._is_lan:
            # Offline Mode card: dimmed until added, like a game that isn't set up
            ik         = []
            is_setup   = cfg.get_offline_mode()
            is_present = True
        else:
            ik         = [k for k in keys if k in installed]
            is_setup   = any(cfg.is_game_setup(k) for k in keys)
            is_present = len(ik) > 0

        border_color = color if is_setup else ("#445544" if is_present else "#333344")
        self.setStyleSheet(
            f"QFrame#MC{{background:{C_CARD};border-top:3px solid {border_color};border-radius:8px;}}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Image container with overlaid badge and buttons ───────────────
        img_container = QWidget()
        img_container.setStyleSheet("background:#0A0A10;border:none;")
        img_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._img = QLabel(img_container)
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setStyleSheet("background:transparent;border:none;")
        if not is_setup:
            effect = QGraphicsOpacityEffect()
            effect.setOpacity(0.45 if is_present else 0.25)
            self._img.setGraphicsEffect(effect)

        # Client badge — overlaid top-left on the image
        self._client_badge = QPushButton(client.upper(), img_container)
        self._client_badge.setFont(font(9, True)); self._client_badge.setEnabled(False)
        self._client_badge.setStyleSheet(
            f"QPushButton{{background:{color};color:#FFF;border:none;border-radius:3px;padding:2px 6px;}}"
            f"QPushButton:disabled{{background:{color};color:#FFF;}}"
        )
        self._client_badge.adjustSize()
        self._client_badge.raise_()

        # Buttons — overlaid bottom-center on the image
        self._btn_bar = QWidget(img_container)
        self._btn_bar.setStyleSheet("background:transparent;border:none;")
        bb = QHBoxLayout(self._btn_bar)
        bb.setContentsMargins(6, 0, 6, 0); bb.setSpacing(4)

        def _card_btn(text, bg, fg="#FFF"):
            b = QPushButton(text); b.setFont(font(11, True)); b.setFixedHeight(40)
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:{fg};border:none;border-radius:4px;padding:0 12px;}}"
                f"QPushButton:disabled{{background:{bg};color:{fg};}}")
            return b

        if self._is_lan and not is_setup:
            b = _card_btn("Add", C_IW)
            if on_offline: b.clicked.connect(lambda: on_offline(True))
        elif self._is_lan:
            b = _card_btn("Repair", C_TREY)
            if on_readd: b.clicked.connect(lambda: on_readd(gd))
            rm = _card_btn("Remove", C_RED_BTN)
            if on_offline: rm.clicked.connect(lambda: on_offline(False))
            bb.addStretch(); bb.addWidget(b); bb.addWidget(rm); b = None
        elif not is_setup and is_present:
            b = _card_btn("Set Up", C_IW)
            b.clicked.connect(lambda: on_setup(gd))
        elif is_setup:
            b = _card_btn("Options", "rgba(51,51,63,200)", "#CCC")
            b.clicked.connect(lambda: on_configure(gd, ik))
        elif "iw5mp_ds" in keys and on_add_ds:
            b = _card_btn("Set Up Free Game", C_IW)
            b.clicked.connect(lambda: on_add_ds(gd))
        else:
            b = _card_btn("Set Up", "rgba(37,37,53,200)", "#555568")
            b.setEnabled(False)
        if b: bb.addStretch(); bb.addWidget(b)
        bb.addStretch()

        self._btn_bar.adjustSize()
        self._btn_bar.raise_()

        self._img_container = img_container
        lay.addWidget(img_container)
        self._raw_pixmap = None

        if self._is_lan:
            local_img = os.path.join(HEROES_DIR, "deckops_grid.png")
            if os.path.exists(local_img):
                self._raw_pixmap = QPixmap(local_img)
        else:
            cached = _header_path(self._appid)
            if os.path.exists(cached):
                self._raw_pixmap = QPixmap(cached)
            else:
                self._img_sigs = _Sigs()
                self._img_sigs.log.connect(self._on_image_fetched)
                threading.Thread(target=self._fetch, args=(self._appid,), daemon=True).start()

    def _on_image_fetched(self, path):
        try:
            pix = QPixmap(path)
            if not pix.isNull():
                self._raw_pixmap = pix
                self._scale_image()
        except RuntimeError:
            pass  # card was rebuilt while the download ran

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._scale_image()

    def _scale_image(self):
        w = self.width()
        h = int(w * IMG_RATIO)
        self._img_container.setFixedSize(w, h)
        self._img.setFixedSize(w, h)
        if self._raw_pixmap:
            self._img.setPixmap(
                self._raw_pixmap.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            )
        # Position client badge — top-left with 4px margin
        self._client_badge.move(4, 4)
        # Position button bar — bottom, full width
        self._btn_bar.setFixedWidth(w)
        bh = self._btn_bar.sizeHint().height()
        self._btn_bar.move(0, h - bh - 4)

    def _fetch(self, appid):
        url = SP_IMAGE_URLS.get(appid)
        if not url:
            return
        try:
            from net import download
            dest = _header_path(appid)
            download(url, dest, timeout=10)
            self._img_sigs.log.emit(dest)
        except Exception:
            _log.debug("image load failed", exc_info=True)


# ── ManagementScreen ──────────────────────────────────────────────────────────
class ManagementScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.installed={}; self.screen_name = "ManagementScreen"
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        hdr, hl = _header_bar()
        self._deckops_update_btn = _btn("Update DeckOps", C_DARK_BTN, size=11, h=40)
        self._deckops_update_btn.setFixedWidth(170)
        self._deckops_update_btn.clicked.connect(self._check_deckops_update)
        hl.addWidget(self._deckops_update_btn)
        hl.addSpacing(8)
        self._game_update_btn = _btn("Update Games", C_DARK_BTN, size=11, h=40)
        self._game_update_btn.setFixedWidth(160)
        self._game_update_btn.clicked.connect(self._update_all_games)
        hl.addWidget(self._game_update_btn)
        hl.addSpacing(8)
        guide_btn = _btn("📋  Guide", C_DARK_BTN, size=11, h=40); guide_btn.setFixedWidth(120)
        guide_btn.clicked.connect(self._open_guide)
        hl.addWidget(guide_btn)
        hl.addSpacing(8)
        cfg_btn = _btn("⚙  Settings", C_DARK_BTN, size=11, h=40); cfg_btn.setFixedWidth(140)
        cfg_btn.clicked.connect(lambda: go_to(self.stack, "ConfigureScreen"))
        hl.addWidget(cfg_btn)
        lay.addWidget(hdr)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); self._grid = QGridLayout(inner)
        self._grid.setContentsMargins(16,16,16,16); self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(inner); lay.addWidget(scroll, stretch=1)

        self._deckops_update_status = _lbl("", 10, C_DIM, wrap=False)
        self._deckops_update_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._deckops_update_status.setContentsMargins(16,0,16,4)
        lay.addWidget(self._deckops_update_status)

        self._status = _lbl("", 12, C_DIM)
        self._status.setContentsMargins(16,4,16,4)
        lay.addWidget(self._status)

    def _open_guide(self):
        get_screen(self.stack, "SetupCompleteScreen")._from_guide = True
        go_to(self.stack, "SetupCompleteScreen")

    def set_installed(self, installed):
        self.installed = installed
        self._rebuild()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh()

    def _refresh(self):
        if getattr(self, "_scanning", False): return
        self._scanning = True
        self._game_update_btn.setEnabled(False)
        if not self.installed: self._status.setText("Loading games...")
        self._scan_sigs = _Sigs()
        self._scan_sigs.done.connect(self._on_scanned)
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            self._scanned = find_all_games()
        except Exception:
            _log.warning("game scan failed", exc_info=True)
            self._scanned = None
        self._scan_sigs.done.emit(self._scanned is not None)

    def _on_scanned(self, ok):
        self._scanning = False
        self._game_update_btn.setEnabled(True)
        if self._status.text() == "Loading games...": self._status.setText("")
        if ok:
            self.installed = self._scanned
            self._rebuild()

    def _rebuild(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        games = [g for g in ALL_GAMES
                 if _active_keys(g) or g.get("client") == "lan"]

        for idx, gd in enumerate(games):
            row = idx // CARD_COLS
            col = idx  % CARD_COLS
            card = ManagementCard(
                gd, self.installed,
                on_setup     = self._setup,
                on_configure = self._configure,
                on_readd     = self._readd,
                on_add_ds    = self._add_mw3_ds,
                on_offline   = self._offline,
            )
            self._grid.addWidget(card, row, col)

        total = len(games)
        remainder = total % CARD_COLS
        if remainder:
            for col in range(remainder, CARD_COLS):
                self._grid.addWidget(QWidget(), total // CARD_COLS, col)

    # ── Update DeckOps (from ManagementScreen) ──────────────────────────────
    _GITHUB_USER = GITHUB_USER
    _GITHUB_REPO = GITHUB_REPO

    def _check_deckops_update(self):
        self._deckops_update_btn.setEnabled(False)
        self._deckops_update_status.setText("Checking...")
        self._deckops_update_status.setStyleSheet(f"color:{C_DIM};")
        self._du_sig = _Sigs()
        self._du_sig.log.connect(self._on_deckops_update_signal)
        threading.Thread(target=self._deckops_update_worker, daemon=True).start()

    def _deckops_update_worker(self):
        self._du_sig.log.emit(check_update_status())

    def _on_deckops_update_signal(self, msg):
        kind, text = msg.split("|", 1)
        self._deckops_update_btn.setEnabled(True)
        self._deckops_update_status.setText(text)
        if kind == "UPDATE":
            self._deckops_update_status.setStyleSheet(f"color:{C_IW};")
            self._deckops_update_btn.setText("Update && Restart")
            try:
                self._deckops_update_btn.clicked.disconnect()
            except TypeError:
                pass
            self._deckops_update_btn.clicked.connect(self._apply_deckops_update)
        else:
            self._deckops_update_status.setStyleSheet(f"color:{C_DIM};")

    def _apply_deckops_update(self):
        # Apply runs on Settings; show it so download progress is visible
        s = get_screen(self.stack, "ConfigureScreen")
        go_to(self.stack, "ConfigureScreen")
        QTimer.singleShot(0, s._apply_update)

    # ── Update all games (batch) ──────────────────────────────────────────
    def _update_all_games(self):
        root = find_steam_root()
        all_installed = self.installed

        # Build selected list: all set-up games with a mod client
        _gd_by_key = {}
        for gd_entry in ALL_GAMES:
            for k in _active_keys(gd_entry):
                _gd_by_key[k] = gd_entry

        selected = []
        for key, game in all_installed.items():
            c = KEY_CLIENT.get(key, "")
            if c in ("steam", "", None):
                continue
            if not cfg.is_game_setup(key):
                continue
            gd_entry = _gd_by_key.get(key)
            if gd_entry:
                selected.append((key, gd_entry, game))

        if not selected:
            self._status.setText("No installed mod-client games to update.")
            return

        names = ", ".join(s[1]["base"] for s in selected)
        msg = QMessageBox(self)
        msg.setWindowTitle("Update All Games")
        msg.setText(f"This will update {len(selected)} game(s):\n\n{names}\n\nContinue?")
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)
        zd_cb = None
        if cfg.is_zd_installed() and any(k == "t6zm" for k, _, _ in selected):
            zd_cb = QCheckBox("Also check Zombies Declassified for updates (up to ~9 GB)")
            msg.setCheckBox(zd_cb)
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return

        us = get_screen(self.stack, "UpdateScreen")
        us.update_zd = bool(zd_cb and zd_cb.isChecked())
        us.selected = selected
        us.steam_root = root
        go_to(self.stack, "UpdateScreen")

    def _offline(self, on):
        """Add or remove Offline Mode (offline launcher, Steam menu Offline entries, bot matches)."""
        text = ("Add Offline Mode? Adds the offline launcher, Plutonium Offline in each game's Steam "
                "launch menu, and bot matches in WaW / BO1 / BO2 / MW3 MP. Steam closes while it is set up."
                if on else
                "Remove Offline Mode? Removes the offline launcher, the Plutonium Offline launch menu "
                "entries and the bot match files. Steam closes while it is removed.")
        if QMessageBox.question(self, "Offline Mode", text) != QMessageBox.StandardButton.Yes:
            return
        def task(sigs):
            from wrapper import kill_steam
            from plutonium import apply_offline_mode
            sigs.progress.emit(2, "Closing Steam...")
            kill_steam(on_progress=sigs.log.emit)
            sr = cfg.load().get("steam_root", "") or find_steam_root()
            apply_offline_mode(on, sr, lambda p, m: (sigs.progress.emit(p, m.strip()), sigs.log.emit(m.strip())))
            sigs.log.emit("✓  Offline Mode added." if on else "✓  Offline Mode removed.")
        us = get_screen(self.stack, "UpdateScreen")
        us.task = ("OFFLINE MODE", task)
        go_to(self.stack, "UpdateScreen")

    def _readd(self, gd):
        """Repair the offline launcher: its files, its prefix and its shortcut."""
        if QMessageBox.question(self, "Offline Mode", "Repair the offline launcher? Checks its files, "
                                "rebuilds its prefix and re-adds the shortcut. Steam closes while it runs.") \
                != QMessageBox.StandardButton.Yes:
            return
        def task(sigs):
            import plutonium
            from shortcut import create_launcher_shortcut, repair_launcher_files
            from wrapper import kill_steam
            log = lambda m: sigs.log.emit(m.strip())
            # Steam must be closed first. Steam holds shortcuts.vdf and the
            # controller configsets in memory and flushes them on exit, which
            # clobbers any edits made while it is running. This was the cause
            # of the launcher controller template intermittently reverting.
            sigs.progress.emit(2, "Closing Steam...")
            kill_steam(on_progress=sigs.log.emit)
            sigs.progress.emit(10, "Checking launcher files...")
            repair_launcher_files(log)
            # Also the offline server's prefix; _lan_prefixes sets it up again
            sigs.progress.emit(40, "Rebuilding launcher prefix...")
            shutil.rmtree(plutonium.LAN_SERVER_PREFIX, ignore_errors=True)
            plutonium._lan_prefixes(log)
            try: os.remove(os.path.join(PROJECT_ROOT, ".lan_intent.json"))
            except FileNotFoundError: pass
            sigs.progress.emit(90, "Re-adding shortcut...")
            create_launcher_shortcut(on_progress=log)
            sigs.log.emit("✓  Offline launcher repaired. Safe to reopen Steam.")
        us = get_screen(self.stack, "UpdateScreen")
        us.task = ("REPAIR OFFLINE LAUNCHER", task)
        go_to(self.stack, "UpdateScreen")

    def _current_cod4_client(self):
        """
        The CoD4 MP client that is set up now: "cod4x" or "cod4r". From
        setup_games, else from the game folder's DeckOps metadata (after a
        config reset). InstallScreen used to fall back to "cod4r" whenever
        the Setup screen hadn't run this session, so Set Up / Reinstall from
        here replaced a working CoD4x with CoD4R.
        """
        c = cfg.get_setup_games().get("cod4mp", {}).get("client")
        if c in ("cod4r", "cod4x"):
            return c
        d = (self.installed.get("cod4mp") or {}).get("install_dir", "")
        if d and os.path.exists(os.path.join(d, "deckops_cod4x.json")) \
                and not os.path.exists(os.path.join(d, "deckops_cod4r.json")):
            return "cod4x"
        return "cod4r"

    def _setup(self, gd, force_downgrade_ids=None, cod4_client=None):
        """Route a single card's keys through the install flow.
        cod4_client -- the CoD4 MP client to install (Reinstall reads it
        before clearing the setup state); default: the one set up now."""
        root = find_steam_root()
        keys = _active_keys(gd)
        # Find which keys are present in the merged installed dict
        present_keys = [k for k in keys if k in self.installed]
        if not present_keys:
            self._status.setText("Game files not found.")
            return

        selected = [(k, gd, self.installed[k]) for k in present_keys]

        # Asked first so the DLC choice is part of the space check.
        dlc = _ask_iw4x_dlc(self, selected)

        import preflight
        rows = [(k, KEY_CLIENT.get(k, ""), g.get("install_dir", ""))
                for k, _gd, g in selected]
        try:
            checks = preflight.check(rows, iw4x_dlc=(dlc == "install"),
                                     downgrade_keys=[k for k, _, _ in rows])
        except Exception as ex:
            _log_to_file(f"[ManagementScreen] preflight failed: {ex}")
            checks = []
        if not _show_preflight(self, checks):
            return

        s = get_screen(self.stack, "InstallScreen")
        own_selected = {}
        steam_selected = []
        for k, g_gd, game in selected:
            if game.get("source") == "own":
                own_selected[k] = game
            else:
                steam_selected.append((k, g_gd, game))
        s.own_selected = own_selected
        s.steam_selected = steam_selected
        s.steam_root = root
        s._return_to_management = True
        s.install_iw4x_dlc = dlc
        s.force_downgrade_ids = set(force_downgrade_ids or ())
        s.cod4_client = cod4_client or self._current_cod4_client()
        go_to(self.stack, "InstallScreen")

    def _add_mw3_ds(self, gd):
        """Request Steam install the free MW3 Dedicated Server (42750).

        Used when MW3 isn't installed at all. Once the user installs it
        via Steam and returns to DeckOps, showEvent rescans and the card
        picks up the normal Set Up flow.
        """
        from depot_downgrade import open_steam_install
        open_steam_install(42750)
        QMessageBox.information(
            self, gd["base"],
            "DeckOps is adding the free MW3 Dedicated Server tools to "
            "your Steam account.\n\n"
            "Please let it finish installing in Steam. Once it's done, "
            "close DeckOps and reopen it, then click Set Up on the "
            "MW3 card."
        )

    def _configure(self, gd, installed_keys):
        """Show configure dialog with Mods, Update, and Reinstall options."""
        _MOD_CLIENTS = ("cod4r", "cod4x", "iw4x", "plutonium", "alterware", "t7x")
        has_mods_support = any(KEY_CLIENT.get(k, "") in _MOD_CLIENTS for k in installed_keys)
        has_mod_client = any(KEY_CLIENT.get(k, "") not in ("steam", "") for k in installed_keys) \
            or any(cfg.get_setup_games().get(k, {}).get("client") == "iw-pad" for k in installed_keys)

        # Depot downgrade option: shown for any Steam-sourced game that
        # can be downgraded (MW3, Ghosts, AW). Whether the downgrade
        # is actually needed is checked on click.
        _DG_KEY_MAP = {
            "iw5": ("iw5mp", "iw5mp_ds", "iw5sp"),
            "iw6": ("iw6mp", "iw6sp"),
            "s1":  ("s1mp", "s1sp"),
        }
        _DG_LABELS = {"iw5": "Downgrade MW3",
                       "iw6": "Downgrade Ghosts", "s1": "Downgrade AW"}
        has_depot_dg = None
        for _dg_id, _dg_keys in _DG_KEY_MAP.items():
            _matched = [k for k in installed_keys if k in _dg_keys]
            if _matched:
                setup_games = cfg.get_setup_games()
                for k in _matched:
                    entry = setup_games.get(k, {})
                    if entry.get("source", "steam") == "steam":
                        has_depot_dg = _dg_id
                        break
            if has_depot_dg:
                break

        # Zombies Declassified: available when t6zm is installed via Plutonium
        # and user has all BO2 Zombies DLC
        has_zd = False
        if ("t6zm" in installed_keys
                and KEY_CLIENT.get("t6zm") == "plutonium"
                and cfg.is_game_setup("t6zm")):
            _bo2 = self.installed.get("t6zm", {}).get("install_dir", "")
            if _bo2:
                from zombies_declassified import has_bo2_zm_dlc
                has_zd = has_bo2_zm_dlc(_bo2)

        # MW3 SP: old in-place downgrades left a 32-bit iw5sp.exe that
        # won't launch through Steam until a Verify restores it
        sp_verify = False
        _sp = self.installed.get("iw5sp", {}) if "iw5sp" in installed_keys else {}
        if _sp.get("install_dir") and _sp.get("source", "steam") != "own":
            from depot_downgrade import iw5sp_needs_verify
            sp_verify = iw5sp_needs_verify(_sp["install_dir"])

        msg = QMessageBox(self)
        msg.setWindowTitle(gd["base"])
        msg.setText("What would you like to do?")

        mods_btn = None
        if has_mods_support:
            mods_btn = msg.addButton("Mods", QMessageBox.ButtonRole.AcceptRole)
        zd_btn = None
        if has_zd:
            zd_btn = msg.addButton("Zombies Declassified", QMessageBox.ButtonRole.AcceptRole)
        upd_btn = None
        if has_mod_client:
            upd_btn = msg.addButton("Update", QMessageBox.ButtonRole.AcceptRole)
        dg_btn = None
        if has_depot_dg:
            dg_btn = msg.addButton(_DG_LABELS[has_depot_dg], QMessageBox.ButtonRole.AcceptRole)
        sp_btn = None
        if sp_verify:
            sp_btn = msg.addButton("Verify SP Files", QMessageBox.ButtonRole.AcceptRole)
        rei_btn = msg.addButton("Reinstall", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == mods_btn:
            self._mods(gd, installed_keys)
        elif clicked == zd_btn:
            self._zombies_declassified(gd, installed_keys)
        elif clicked == upd_btn:
            self._update(gd, installed_keys)
        elif clicked == dg_btn:
            self._depot_downgrade(has_depot_dg, gd, installed_keys)
        elif clicked == sp_btn:
            from wrapper import launch_steam
            launch_steam("steam://validate/42680")
            self._status.setText("Steam is verifying MW3 Single Player files...")
        elif clicked == rei_btn:
            self._reinstall(gd)

    def _update(self, gd, keys):
        """Route a single card's installed keys through the update flow."""
        root = find_steam_root()
        # Use the merged installed dict (already built in showEvent)
        selected = [(k, gd, self.installed.get(k, {})) for k in keys
                     if self.installed.get(k)]
        if not selected:
            self._status.setText("Game not found.")
            return
        s = get_screen(self.stack, "UpdateScreen")
        s.selected   = selected
        s.steam_root = root
        go_to(self.stack, "UpdateScreen")

    def _zombies_declassified(self, gd, installed_keys):
        """Install, update, or uninstall Zombies Declassified DLC5 for BO2 ZM."""

        # One at a time: the progress dialog wasn't modal, so the card's
        # Options -> ZD could start a second worker on the same storage folder
        if getattr(self, "_zd_running", False):
            self._status.setText("Zombies Declassified is already running -- wait for it to finish.")
            return

        from zombies_declassified import resolve_zd_storage
        plut_storage_t6 = resolve_zd_storage(self.installed.get("t6zm"))

        if not plut_storage_t6 or not os.path.isdir(plut_storage_t6):
            self._status.setText("Could not find Plutonium storage/t6 directory.")
            return

        from zombies_declassified import is_zd_installed as _zd_present

        is_installed = _zd_present(plut_storage_t6)

        msg = QMessageBox(self)
        msg.setWindowTitle("Zombies Declassified")
        if is_installed:
            info = cfg.get_zd_info()
            tag = info.get("release_tag", "unknown")
            msg.setText(f"Zombies Declassified {tag} is installed.\nWhat would you like to do?")
            upd_btn = msg.addButton("Check for Updates", QMessageBox.ButtonRole.AcceptRole)
            rm_btn  = msg.addButton("Uninstall", QMessageBox.ButtonRole.DestructiveRole)
        else:
            msg.setText(
                "Zombies Declassified adds 10 classic Zombies maps to BO2.\n"
                "Download size: ~9 GB\n\n"
                "Install now?"
            )
            upd_btn = msg.addButton("Install", QMessageBox.ButtonRole.AcceptRole)
            rm_btn  = None
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == upd_btn:
            action = "update" if is_installed else "install"
        elif clicked == rm_btn:
            action = "uninstall"
        else:
            return

        # Run on background thread with a simple progress dialog. Modal, and
        # sleep blocked for the ~9 GB download (neither was done before, so the
        # Deck could suspend mid-download). Escape can still hide the dialog;
        # it comes back with the result when the worker ends.
        from qt import QProgressDialog
        self._zd_running = True
        inhibit.start({"install": "DeckOps is downloading Zombies Declassified",
                       "update": "DeckOps is updating Zombies Declassified",
                       "uninstall": "DeckOps is removing Zombies Declassified"}[action])
        pd = QProgressDialog("Starting...", None, 0, 100, self)
        pd.setWindowTitle("Zombies Declassified")
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setMinimumDuration(0); pd.setAutoClose(False); pd.setAutoReset(False)
        pd.setCancelButton(None); pd.show()

        _st6 = plut_storage_t6

        class _ZDSigs(QObject):
            progress = pyqtSignal(int, str)
            done     = pyqtSignal(str)
        sigs = _ZDSigs()
        sigs.progress.connect(lambda p, m: (pd.setValue(p), pd.setLabelText(m)))

        def _on_done(result_msg):
            self._zd_running = False
            inhibit.stop()
            pd.setValue(100); pd.setLabelText(result_msg)
            pd.setCancelButtonText("OK")
            pd.canceled.connect(pd.close)
            pd.show()       # if Escape hid it while the worker ran
            if action == "install":
                from zombies_declassified import get_zd_info as _get_info
                info = _get_info(_st6)
                if info.get("manifest_hash"):
                    cfg.mark_zd_installed(info["manifest_hash"])
            elif action == "uninstall":
                cfg.unmark_zd_installed()
        sigs.done.connect(_on_done)

        def _worker():
            try:
                from zombies_declassified import install_zd, update_zd, uninstall_zd
                if action == "install":
                    errors = install_zd(_st6, lambda p, m: sigs.progress.emit(p, m))
                    if errors:
                        sigs.done.emit(f"Installed with {len(errors)} error(s). Check log.")
                    else:
                        sigs.done.emit("Zombies Declassified installed!")
                elif action == "update":
                    updated = update_zd(_st6, lambda p, m: sigs.progress.emit(p, m))
                    if updated:
                        from zombies_declassified import get_zd_info as _get_info
                        info = _get_info(_st6)
                        if info.get("manifest_hash"):
                            cfg.mark_zd_installed(info["manifest_hash"])
                        sigs.done.emit("Updated!")
                    else:
                        sigs.done.emit("Already up to date.")
                elif action == "uninstall":
                    uninstall_zd(_st6, lambda p, m: sigs.progress.emit(p, m))
                    sigs.done.emit("Zombies Declassified removed.")
            except Exception as ex:
                _log.error("Zombies Declassified %s failed", action, exc_info=True)
                sigs.done.emit(f"Error: {ex}")
        threading.Thread(target=_worker, daemon=True).start()

    def _reinstall(self, gd):
        """Unmark game keys as set up, then route through the install flow."""
        keys = _active_keys(gd)
        # Read before the setup state (which holds it) is cleared
        cod4_client = self._current_cod4_client()
        cfg.unmark_game_setup(keys)
        self._status.setText(f"Cleared setup state for {gd['base']}. Running clean install...")
        # Route through the same path as "Set Up"
        self._setup(gd, cod4_client=cod4_client)

    def _depot_downgrade(self, game_id, gd, installed_keys):
        """Downgrade a game from 64-bit to 32-bit and trigger reinstall.

        Checks whether the downgrade is actually needed first; if the
        install is already 32-bit, informs the user and does nothing.
        Otherwise unmarks the game's keys so the install flow re-runs
        the depot downgrade gate.
        """
        _DG_KEY_MAP = {
            "iw5": ("iw5mp", "iw5mp_ds", "iw5sp"),
            "iw6": ("iw6mp", "iw6sp"),
            "s1":  ("s1mp", "s1sp"),
        }
        _dg_keys = [k for k in installed_keys if k in _DG_KEY_MAP.get(game_id, ())]
        if not _dg_keys:
            return

        from depot_downgrade import is_downgrade_needed, GAME_CONFIGS
        gcfg = GAME_CONFIGS.get(game_id, {})
        game_name = gcfg.get("name", gd["base"])

        idir = ""
        for k in _dg_keys:
            game = self.installed.get(k, {})
            if game.get("install_dir"):
                idir = game["install_dir"]
                break

        if idir and not is_downgrade_needed(game_id, idir):
            msg = QMessageBox(self)
            msg.setWindowTitle(gd["base"])
            msg.setText(f"{game_name} already has the correct depot files.")
            msg.addButton(QMessageBox.StandardButton.Ok)
            force_btn = msg.addButton("Force Downgrade", QMessageBox.ButtonRole.AcceptRole)
            msg.exec()
            if msg.clickedButton() != force_btn:
                return

        # Force a fresh downgrade: drop both the receipt and the legacy flag
        from depot_downgrade import clear_receipt
        if idir: clear_receipt(game_id, idir)
        if gcfg.get("always_64bit"):
            cfg.clear_depot_patched(game_id)
        cfg.unmark_game_setup(_dg_keys)
        self._status.setText(
            f"{game_name} downgrade selected. Running depot download and reinstall..."
        )
        self._setup(gd, force_downgrade_ids={game_id})

    def _mods(self, gd, installed_keys):
        """Open the mod folder for a game. Shows a chooser for games with
        both mods/ and usermaps/ directories (CoD4x, IW4x, T7X), for
        AlterWare games with data/scripts/mp and data/scripts/sp, or for
        OLED Plutonium games whose SP/MP modes live in separate prefixes."""

        # ── Plutonium storage name mapping ────────────────────────────────
        _PLUT_STORAGE = {
            "t4sp": "t4", "t4mp": "t4",
            "t5sp": "t5", "t5mp": "t5",
            "t6mp": "t6", "t6zm": "t6",
            "iw5mp": "iw5", "iw5mp_ds": "iw5",
        }

        # Appid per key — used to detect when keys on the same card
        # have separate Wine prefixes (different appids = different
        # mods/ folders on OLED).
        _PLUT_APPID = {
            "t4sp": 10090, "t4mp": 10090,
            "t5sp": 42700, "t5mp": 42710,
            "t6mp": 202990, "t6zm": 212910,
            "iw5mp": 42690, "iw5mp_ds": 42750,
        }

        # Friendly labels for the dialog buttons
        _PLUT_MODE_LABEL = {
            "t4sp": "Campaign / Zombies", "t4mp": "Multiplayer",
            "t5sp": "Campaign / Zombies", "t5mp": "Multiplayer",
            "t6zm": "Zombies",            "t6mp": "Multiplayer",
            "iw5mp": "Multiplayer",       "iw5mp_ds": "Dedicated Server",
        }

        def _open_folder(path):
            os.makedirs(path, exist_ok=True)
            _detached_open(["xdg-open", path])
            self._status.setText(f"Opened: {path}")

        def _resolve_plut_mods_dir(game_key):
            """Resolve the Plutonium mods/ folder for a game key."""
            storage_name = _PLUT_STORAGE.get(game_key)
            if not storage_name:
                return None

            if cfg.is_lcd():
                # LCD: single Heroic shared prefix for all games
                from plutonium_lcd import get_shared_plut_dir
                plut_dir = get_shared_plut_dir()
            else:
                # OLED: read plut_dir from per-game metadata
                game = self.installed.get(game_key, {})
                install_dir = game.get("install_dir", "")
                if install_dir:
                    meta_path = os.path.join(install_dir, "deckops_plutonium.json")
                    if os.path.exists(meta_path):
                        try:
                            import json
                            with open(meta_path) as f:
                                meta = json.load(f)
                            plut_dir = meta.get("plut_dir", "")
                        except Exception:
                            plut_dir = ""
                    else:
                        plut_dir = ""
                else:
                    plut_dir = ""

            if not plut_dir:
                return None
            return os.path.join(plut_dir, "storage", storage_name, "mods")

        # Determine which client we're dealing with from the installed keys
        client = None
        plut_keys = []
        alterware_keys = []
        for k in installed_keys:
            c = KEY_CLIENT.get(k, "")
            if c in ("cod4r", "cod4x", "iw4x"):
                client = c
                break
            elif c == "t7x":
                client = "t7x"
                break
            elif c == "alterware":
                client = "alterware"
                alterware_keys.append(k)
            elif c == "plutonium" and k in _PLUT_STORAGE:
                client = "plutonium"
                plut_keys.append(k)

        if not client:
            self._status.setText("No mod support for this game.")
            return

        if client in ("cod4r", "cod4x", "iw4x"):
            mod_key = installed_keys[0]
            game = self.installed.get(mod_key, {})
            install_dir = game.get("install_dir", "")
            if not install_dir:
                self._status.setText("Game install directory not found.")
                return
            # Two folders — ask user which one
            msg = QMessageBox(self)
            msg.setWindowTitle("Open Mod Folder")
            msg.setText(f"Which folder would you like to open for {gd['base']}?")
            mods_btn = msg.addButton("Mods", QMessageBox.ButtonRole.AcceptRole)
            usermaps_btn = msg.addButton("User Maps", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == mods_btn:
                _open_folder(os.path.join(install_dir, "mods"))
            elif clicked == usermaps_btn:
                _open_folder(os.path.join(install_dir, "usermaps"))

        elif client == "t7x":
            from t7x import _get_sibling_dir
            mod_key = [k for k in installed_keys if KEY_CLIENT.get(k) == "t7x"]
            mod_key = mod_key[0] if mod_key else installed_keys[0]
            game = self.installed.get(mod_key, {})
            stock_dir = game.get("install_dir", "")
            if not stock_dir:
                self._status.setText("Game install directory not found.")
                return
            # T7X writes mods/usermaps into the DeckOps-T7X sibling dir,
            # not the stock BO3 install directory that detect_games returns.
            install_dir = _get_sibling_dir(stock_dir)
            if not os.path.isdir(install_dir):
                self._status.setText("T7X directory not found. Has T7X been installed?")
                return
            # Two folders — ask user which one
            msg = QMessageBox(self)
            msg.setWindowTitle("Open Mod Folder")
            msg.setText(f"Which folder would you like to open for {gd['base']} (T7x)?")
            mods_btn = msg.addButton("Mods", QMessageBox.ButtonRole.AcceptRole)
            usermaps_btn = msg.addButton("User Maps", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == mods_btn:
                _open_folder(os.path.join(install_dir, "mods"))
            elif clicked == usermaps_btn:
                _open_folder(os.path.join(install_dir, "usermaps"))

        elif client == "alterware":
            if not alterware_keys:
                self._status.setText("No AlterWare mod keys found.")
                return
            mod_key = alterware_keys[0]
            game = self.installed.get(mod_key, {})
            install_dir = game.get("install_dir", "")
            if not install_dir:
                self._status.setText("Game install directory not found.")
                return

            # Determine which mode buttons to show based on installed keys
            has_mp = any(k.endswith("mp") for k in alterware_keys)
            has_sp = any(k.endswith("sp") for k in alterware_keys)

            if has_mp and has_sp:
                msg = QMessageBox(self)
                msg.setWindowTitle("Open Scripts Folder")
                msg.setText(f"Which scripts folder for {gd['base']}?")
                mp_btn = msg.addButton("MP Scripts", QMessageBox.ButtonRole.AcceptRole)
                sp_btn = msg.addButton("SP Scripts", QMessageBox.ButtonRole.AcceptRole)
                msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                msg.exec()
                clicked = msg.clickedButton()
                if clicked == mp_btn:
                    _open_folder(os.path.join(install_dir, "data", "scripts", "mp"))
                elif clicked == sp_btn:
                    _open_folder(os.path.join(install_dir, "data", "scripts", "sp"))
            elif has_mp:
                _open_folder(os.path.join(install_dir, "data", "scripts", "mp"))
            elif has_sp:
                _open_folder(os.path.join(install_dir, "data", "scripts", "sp"))

        elif client == "plutonium":
            if not plut_keys:
                self._status.setText("No Plutonium mod keys found.")
                return

            # Check if multiple keys have different appids (OLED only).
            # If so, they have separate prefixes with separate mods/ folders.
            unique_appids = set(_PLUT_APPID.get(k) for k in plut_keys)
            need_chooser = not cfg.is_lcd() and len(unique_appids) > 1

            if need_chooser:
                msg = QMessageBox(self)
                msg.setWindowTitle("Open Mod Folder")
                msg.setText(f"Which mode's mods folder for {gd['base']}?")
                key_buttons = []
                for k in plut_keys:
                    label = _PLUT_MODE_LABEL.get(k, k)
                    btn = msg.addButton(label, QMessageBox.ButtonRole.AcceptRole)
                    key_buttons.append((btn, k))
                msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                msg.exec()
                clicked = msg.clickedButton()
                for btn, k in key_buttons:
                    if clicked == btn:
                        mods_path = _resolve_plut_mods_dir(k)
                        if mods_path:
                            _open_folder(mods_path)
                        else:
                            self._status.setText(f"Could not resolve mods folder for {k}.")
                        break
            else:
                # Single prefix or LCD — open directly
                mods_path = _resolve_plut_mods_dir(plut_keys[0])
                if mods_path:
                    _open_folder(mods_path)
                else:
                    self._status.setText("Could not resolve Plutonium mods folder.")


# ── SetupCompleteScreen ──────────────────────────────────────────────────────
class SetupCompleteScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack = stack; self.screen_name = "SetupCompleteScreen"
        self._from_guide = False
        lay = QVBoxLayout(self); lay.setContentsMargins(60,20,60,20); lay.setSpacing(0)

        t = QLabel("SETUP COMPLETE"); t.setFont(font(28, True)); t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet("color:#FFF;background:transparent;"); lay.addWidget(t)
        lay.addWidget(_lbl(
            "Your games are configured with controller profiles, GE-Proton, "
            "and display settings. Open Steam (or return to Game Mode) to start playing.",
            12, C_DIM))

        # ── Scrollable content area ───────────────────────────────────────────
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        sl = QVBoxLayout(inner); sl.setContentsMargins(0,8,0,8); sl.setSpacing(6)

        # ── Cloud saves ───────────────────────────────────────────────────────
        sl.addWidget(_hdiv())
        sl.addWidget(_lbl("⚠  Steam Cloud Saves", 12, C_TREY, bold=True, align=Qt.AlignmentFlag.AlignLeft))
        sl.addWidget(_lbl(
            "If Steam asks about cloud saves, choose Keep Local. "
            "If a game asks for Safe Mode, choose No.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft))

        # ── MW1 / WaW shortcuts ───────────────────────────────────────────────
        self._mw1waw_div = _hdiv()
        self._mw1waw_hdr = _lbl("🎮  MW1 & WaW Multiplayer", 12, C_IW, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._mw1waw_body = _lbl(
            "MW1 and WaW have separate DeckOps multiplayer shortcuts in your "
            "Steam library. Use the main game entry for singleplayer and the "
            "DeckOps shortcut for multiplayer.\n\n"
            "MW1 Singleplayer: on first launch, select the \"Player\" profile. "
            "This is the profile DeckOps created with your display settings.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        sl.addWidget(self._mw1waw_div)
        sl.addWidget(self._mw1waw_hdr)
        sl.addWidget(self._mw1waw_body)

        # ── First launch ─────────────────────────────────────────────────────
        sl.addWidget(_hdiv())
        sl.addWidget(_lbl("⏳  First Launch", 12, "#5B9BD5", bold=True, align=Qt.AlignmentFlag.AlignLeft))
        sl.addWidget(_lbl(
            "All games take longer to launch the first time. This is normal "
            "and they will be faster on subsequent launches.\n\n"
            "Plutonium games must be launched once before they can be used "
            "with the offline LAN launcher.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft))

        # ── MW2 MP own-game first launch ──────────────────────────────────────
        self._mw2own_div = _hdiv()
        self._mw2own_hdr = _lbl("🎮  MW2 Multiplayer (Non-Steam)", 12, C_IW, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._mw2own_body = _lbl(
            "MW2 Multiplayer (IW4x) needs to be launched twice on initial "
            "installation. The first launch will fail, this is normal. "
            "Launch it again and it will work.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        sl.addWidget(self._mw2own_div)
        sl.addWidget(self._mw2own_hdr)
        sl.addWidget(self._mw2own_body)

        # ── BO3 first launch ─────────────────────────────────────────────────
        self._bo3_div = _hdiv()
        self._bo3_hdr = _lbl("🎮  Black Ops III: First Launch", 12, C_TREY, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._bo3_body = _lbl(
            "CleanOps finishes installing on its first run. Do the "
            "first launch in Desktop Mode:\n\n"
            "1. Launch Black Ops III (CleanOps). It will patch the game and "
            "may slow the Deck temporarily.\n"
            "2. If it does not launch, press the blue Stop button in Steam, "
            "then relaunch to verify it works.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        sl.addWidget(self._bo3_div)
        sl.addWidget(self._bo3_hdr)
        sl.addWidget(self._bo3_body)

        # ── LCD notes (LCD users only) ────────────────────────────────────────
        self._lcd_div = _hdiv()
        self._lcd_hdr = _lbl("⚠  LCD Steam Deck Notes", 12, C_TREY, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._lcd_body = _lbl(
            "Plutonium games on LCD may take a moment to launch while a "
            "shader cache cleanup runs. If a game does not start right "
            "away, be patient or try again.\n\n"
            "If Steam tries to compile Vulkan shaders before launching, "
            "skip it. These are not used by LCD Plutonium games.\n\n"
            "Quit games from the in-game menu rather than the Steam "
            "overlay for a faster exit.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        sl.addWidget(self._lcd_div)
        sl.addWidget(self._lcd_hdr)
        sl.addWidget(self._lcd_body)

        # ── Restore player saves (visible only when backups exist) ────────────
        self._restore_div = _hdiv()
        self._restore_hdr = _lbl("💾  Restore Player Saves", 12, C_IW, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._restore_body = _lbl(
            "Backups from a previous install were found. "
            "Restore your configs, stats, and custom classes?",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        self._restore_status = _lbl("", 11, C_IW, align=Qt.AlignmentFlag.AlignLeft)
        self._restore_status.setVisible(False)

        self._restore_btn = _btn("Restore Saves", C_IW, size=12, h=40)
        self._restore_btn.setFixedWidth(200)
        self._restore_btn.clicked.connect(self._do_restore)
        self._restore_skip = _btn("Skip", C_DARK_BTN, size=12, h=40)
        self._restore_skip.setFixedWidth(120)
        self._restore_skip.clicked.connect(self._skip_restore)

        rbw = QHBoxLayout(); rbw.setSpacing(12)
        rbw.addWidget(self._restore_btn)
        rbw.addWidget(self._restore_skip)
        rbw.addStretch()

        sl.addWidget(self._restore_div)
        sl.addWidget(self._restore_hdr)
        sl.addWidget(self._restore_body)
        sl.addLayout(rbw)
        sl.addWidget(self._restore_status)

        self._restore_widgets = [
            self._restore_div, self._restore_hdr, self._restore_body,
            self._restore_btn, self._restore_skip,
        ]

        sl.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

        # ── Continue button (pinned outside scroll) ───────────────────────────
        cont = _btn("Continue  >>", C_IW, size=13, h=42)
        cont.setFixedWidth(240)
        cont.clicked.connect(self._go_management)
        cw = QHBoxLayout(); cw.setContentsMargins(0,4,0,12); cw.addStretch(); cw.addWidget(cont); cw.addStretch()
        lay.addLayout(cw)

    def showEvent(self, e):
        super().showEvent(e)
        if e.spontaneous():     # window restored, not a screen change
            return
        is_lcd = cfg.is_lcd()
        self._lcd_div.setVisible(is_lcd)
        self._lcd_hdr.setVisible(is_lcd)
        self._lcd_body.setVisible(is_lcd)
        show_mw1waw = cfg.is_game_setup("cod4mp") or cfg.is_game_setup("t4mp")
        for w in (self._mw1waw_div, self._mw1waw_hdr, self._mw1waw_body):
            w.setVisible(show_mw1waw)
        show_bo3 = cfg.is_game_setup("t7")
        for w in (self._bo3_div, self._bo3_hdr, self._bo3_body):
            w.setVisible(show_bo3)
        show_mw2own = cfg.is_game_setup_for_source("iw4mp", "own")
        self._mw2own_div.setVisible(show_mw2own)
        self._mw2own_hdr.setVisible(show_mw2own)
        self._mw2own_body.setVisible(show_mw2own)
        try:
            from save_backup import has_backups
            show_restore = has_backups()
        except Exception:
            show_restore = False
        for w in self._restore_widgets:
            w.setVisible(show_restore)
        self._restore_status.setVisible(False)

    def _do_restore(self):
        """Run save restore in a background thread."""
        self._restore_btn.setEnabled(False)
        self._restore_skip.setEnabled(False)
        self._restore_status.setText("Restoring saves...")
        self._restore_status.setVisible(True)

        def _run():
            try:
                from save_backup import restore_saves
                steam_root = find_steam_root()
                restored = restore_saves(
                    steam_root=steam_root,
                    on_progress=lambda msg: None,
                )
                count = len(restored) if restored else 0
                if count > 0:
                    groups = ", ".join(restored.keys())
                    self._s.log.emit(f"✓  Restored saves for {count} group(s): {groups}")
                else:
                    self._s.log.emit("No matching save data found to restore.")
            except Exception as ex:
                self._s.log.emit(f"⚠  Restore failed: {ex}")

        self._s = _Sigs()
        self._s.log.connect(self._restore_finished)
        threading.Thread(target=_run, daemon=True).start()

    def _restore_finished(self, message):
        self._restore_status.setText(message)
        self._restore_status.setVisible(True)
        self._restore_btn.setVisible(False)
        self._restore_skip.setVisible(False)
        self._restore_body.setVisible(False)

    def _skip_restore(self):
        """Hide the restore section without restoring."""
        for w in self._restore_widgets:
            w.setVisible(False)
        self._restore_status.setVisible(False)

    def _go_management(self):
        self._from_guide = False
        go_to(self.stack, "ManagementScreen")
