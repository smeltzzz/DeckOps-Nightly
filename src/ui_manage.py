"""
ui_manage.py — Post-install management screens for DeckOps

Screens: ManagementCard, ManagementScreen, SetupCompleteScreen,
         ConfigureScreen, UpdateScreen
Extracted from ui_qt.py — all hardcoded stack indices replaced with named lookups.
"""

import os, subprocess, sys, shutil, threading, json, urllib.request, urllib.error, urllib.parse, compileall

from qt import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea,
    QLabel, QPushButton, QCheckBox, QProgressBar, QPlainTextEdit,
    QFrame, QSizePolicy, QMessageBox, QLineEdit, QSlider, QDialog,
)
from qt import Qt, QTimer, QObject, pyqtSignal
from qt import QPixmap
from qt import QGraphicsOpacityEffect

from detect_games import find_steam_root, find_all_games
import config as cfg
import inhibit

from ui_constants import (
    C_CARD, C_IW, C_TREY, C_DIM, C_DARK_BTN, C_RED_BTN, C_BLUE_BTN,
    font, _btn, _lbl, _hdiv, _log_to_file, _log_html, _copy_log_to_clipboard,
    _Sigs, _detached_open, _header_bar, _header_path, _ask_iw4x_dlc,
    _show_preflight,
    ALL_GAMES, KEY_CLIENT,
    _active_keys, _active_client, _active_appid,
    SP_IMAGE_URLS, IMG_RATIO, CARD_COLS, CARD_MAX_W,
    HEROES_DIR, PROJECT_ROOT,
    _music_volume, _music_enabled,
    _set_audio_volume, _set_audio_enabled, _start_audio, _kill_audio,
    go_to, get_screen,
)

from identity import GITHUB_USER, GITHUB_REPO
from log import get_logger

_log = get_logger(__name__)


# VERSION is per install; the repo still tracks an old copy from 2026-05
_PROTECTED = ("deckops.json", "VERSION", "assets/music/background.mp3")
_HDR = {"User-Agent": "DeckOps"}


def _protected(path: str) -> bool:
    return path.startswith("logs/") or path in _PROTECTED


def _tree(api: str, sha: str) -> dict:
    """{path: (blob sha, mode, size)} for every file in the commit."""
    req = urllib.request.Request(f"{api}/git/trees/{sha}?recursive=1", headers=_HDR)
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    if d.get("truncated"):
        raise RuntimeError("GitHub's file list came back incomplete")
    return {e["path"]: (e["sha"], e["mode"], e.get("size", 0)) for e in d.get("tree", []) if e.get("type") == "blob"}


def _remote_changes(removed: bool = False):
    """
    (local_sha, remote_sha, stale, gone) for this install vs GitHub main.
    stale is {path: (blob sha, mode, size)} for every file on disk that differs
    from main or is missing, so a download that was cut off or served stale
    gets fixed on the next update even when VERSION already matches.
    gone lists files of the installed version that main no longer has
    (looked up only with removed=True). Raises if GitHub can't be reached.
    """
    from steam_common import git_blob_sha
    local_sha = "0"
    try:
        with open(os.path.join(PROJECT_ROOT, "VERSION")) as f:
            local_sha = f.read().strip() or "0"
    except OSError:
        pass
    api = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
    with urllib.request.urlopen(urllib.request.Request(f"{api}/commits/main", headers=_HDR), timeout=10) as r:
        remote_sha = json.loads(r.read()).get("sha", "")
    if not remote_sha:
        raise RuntimeError("GitHub returned no commit")
    want = _tree(api, remote_sha)
    stale = {}
    for path, (sha, mode, size) in want.items():
        if _protected(path):
            continue
        f = os.path.join(PROJECT_ROOT, path)
        try:
            ok = os.path.isfile(f) and git_blob_sha(f) == sha and (mode != "100755" or os.access(f, os.X_OK))
        except OSError:
            ok = False
        if not ok:
            stale[path] = (sha, mode, size)
    gone = []
    if removed and local_sha not in ("0", remote_sha):
        try:
            gone = [p for p in _tree(api, local_sha) if p not in want and not _protected(p)]
        except Exception:
            # e.g. the installed commit is gone after a force push; leftovers stay
            _log.warning("could not list files of installed version %s", local_sha, exc_info=True)
    return local_sha, remote_sha, stale, gone


def _reset_hint(names) -> bool:
    # Mirrors launcher.sh RESET_HINT: config.py or more than 3 src/ files changed
    return "src/config.py" in names or sum(n.startswith("src/") for n in names) > 3


def _rate_limited(ex) -> bool:
    """GitHub refusing because of its limit for requests without an account
    (60 an hour per internet connection), not a broken connection."""
    return isinstance(ex, urllib.error.HTTPError) and ex.code in (403, 429)


_RATE_LIMIT_MSG = ("GitHub is limiting requests from your internet connection right now. "
                   "Try again in about an hour.")


def _quick_update_check(timeout: int = 8):
    """One GitHub request: True if main is ahead of this install's VERSION,
    False if up to date, None if it can't tell (offline, rate limit, ...).
    Used for the notice at startup; the full check runs on the button."""
    try:
        with open(os.path.join(PROJECT_ROOT, "VERSION")) as f:
            local_sha = f.read().strip()
    except OSError:
        local_sha = ""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/commits/main", headers=_HDR)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            remote_sha = json.loads(r.read()).get("sha", "")
    except Exception as ex:
        _log.info("startup update check skipped: %s", ex)
        return None
    if not isinstance(remote_sha, str) or len(remote_sha) != 40:
        return None
    return remote_sha != local_sha


def _check_update_status():
    """Compare this install to GitHub main. Returns 'OK|..', 'UPDATE|..' or 'FAIL|..'."""
    try:
        local_sha, remote_sha, stale, _ = _remote_changes()
        if local_sha == remote_sha:
            if not stale:
                return "OK|You're up to date!"
            return f"UPDATE|{len(stale)} DeckOps file(s) need repair."
        hint = "\nReset Config recommended after updating." if _reset_hint(stale) else ""
        return f"UPDATE|Update available — {len(stale)} file(s) to download.{hint}"
    except Exception as ex:
        _log.warning("Update check failed: %s", ex)
        if _rate_limited(ex):
            return f"FAIL|Update check failed. {_RATE_LIMIT_MSG}"
        return "FAIL|Update check failed. Check your internet connection and try again."


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
            os.makedirs(os.path.dirname(dest), exist_ok=True)
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
        # The desktop icon skips the launcher's update prompt: say here, once
        # per session, when an update is out. One GitHub request; silent if
        # offline or rate limited.
        if not getattr(self, "_startup_checked", False):
            self._startup_checked = True
            self._startup_sig = _Sigs()
            self._startup_sig.done.connect(self._on_startup_update_check)
            threading.Thread(target=lambda: self._startup_sig.done.emit(_quick_update_check() is True),
                             daemon=True).start()

    def _on_startup_update_check(self, available):
        # Never overwrite a check or update the player started meanwhile
        if available and self._deckops_update_btn.isEnabled() and not self._deckops_update_status.text():
            self._deckops_update_status.setText(
                "A DeckOps update is available. Click Update DeckOps to see it and install it.")
            self._deckops_update_status.setStyleSheet(f"color:{C_IW};")

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
        self._du_sig.log.emit(_check_update_status())

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
            upd_btn = msg.addButton("Check and Repair", QMessageBox.ButtonRole.AcceptRole)
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
            if action == "install" and result_msg == "Zombies Declassified installed!":
                from zombies_declassified import get_zd_info as _get_info
                info = _get_info(_st6)
                if info.get("manifest_hash"):
                    cfg.mark_zd_installed(info["manifest_hash"])
            elif action == "uninstall" and result_msg == "Zombies Declassified removed.":
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
                        sigs.done.emit("Zombies Declassified checked and repaired!")
                    else:
                        sigs.done.emit("Files checked; no changes needed.")
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


# ── ConfigureScreen ───────────────────────────────────────────────────────────
class ConfigureScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.screen_name = "ConfigureScreen"
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        hdr, hl = _header_bar()
        t = _lbl("SETTINGS", 16, "#CCC", bold=True, wrap=False)
        hl.insertSpacing(2, 20); hl.insertWidget(3, t)
        back = _btn("<< Back", C_DARK_BTN, size=12, h=40); back.setFixedWidth(140)
        back.clicked.connect(lambda: go_to(self.stack, "ManagementScreen"))
        hl.addWidget(back)
        lay.addWidget(hdr)

        # Scroll only kicks in on windows smaller than the Deck screen
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        cols = QHBoxLayout(inner); cols.setContentsMargins(40,16,40,16); cols.setSpacing(40)
        left = QVBoxLayout(); left.setSpacing(10)
        right = QVBoxLayout(); right.setSpacing(10)
        cols.addLayout(left, 1); cols.addLayout(right, 1)

        def _head(col, text, color="#CCC", first=False):
            if not first: col.addSpacing(12)
            col.addWidget(_lbl(text, 13, color, bold=True, align=Qt.AlignmentFlag.AlignLeft))

        def _desc(col, text):
            """The small grey line under a section heading."""
            lbl = _lbl(text, 11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
            col.addWidget(lbl)
            return lbl

        # ── Left: Background Music ────────────────────────────────────────
        _head(left, "Background Music", first=True)
        _desc(left, "Plays music inside DeckOps. The slider sets the volume.")
        mr = QHBoxLayout(); mr.setSpacing(12)
        self._music_on = _music_enabled
        self._music_toggle = _btn(
            "Music: ON" if self._music_on else "Music: OFF",
            C_IW if self._music_on else C_DARK_BTN,
            size=12, h=40,
        )
        self._music_toggle.setFixedWidth(160)
        self._music_toggle.clicked.connect(self._toggle_music)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal); self._vol_slider.setRange(0,100)
        self._vol_slider.setValue(int(_music_volume*100))
        self._vol_label = _lbl(f"{int(_music_volume*100)}%", 12, C_DIM, wrap=False)
        self._vol_label.setFixedWidth(44)
        self._vol_slider.valueChanged.connect(self._set_volume)
        mr.addWidget(self._music_toggle); mr.addWidget(self._vol_slider,stretch=1); mr.addWidget(self._vol_label)
        left.addLayout(mr)

        # ── Left: Controller Profiles ─────────────────────────────────────
        _head(left, "Controller Profiles")
        _desc(left, "Pick how gyro aiming works, then press Re-apply Templates to write the "
                    "controller layout for all your games. Closes Steam.")
        gr = QHBoxLayout(); gr.setSpacing(8)
        gr.addWidget(_lbl("Gyro:", 12, "#AAA", wrap=False))
        self._gyro_btns = {}
        # SD LCD/OLED and Steam Machine get 4 modes; others get 2.
        # Hold/toggle hidden dynamically in showEvent.
        for mode in ("on", "hold", "toggle", "off"):
            labels = {"on": "ADS", "hold": "Hold", "toggle": "Toggle", "off": "Off"}
            b = _btn(labels[mode], C_DARK_BTN, size=11, h=40)
            b.setFixedWidth(96)
            b.clicked.connect(lambda checked, m=mode: self._set_gyro(m))
            gr.addWidget(b)
            self._gyro_btns[mode] = b
        gr.addStretch()
        left.addLayout(gr)
        ctrl_btn = _btn("Re-apply Templates", C_DARK_BTN, size=12, h=40)
        ctrl_btn.setFixedWidth(220)
        ctrl_btn.clicked.connect(self._apply_controller_profiles)
        left.addWidget(ctrl_btn)

        # ── Left: Shortcuts ───────────────────────────────────────────────
        _head(left, "Shortcuts")
        _desc(left, "Re-adds any missing DeckOps shortcuts and repairs the launch menus and "
                    "launch options of your games. Closes Steam.")
        sc_fix_btn = _btn("Repair Shortcuts", C_DARK_BTN, size=12, h=40)
        sc_fix_btn.setFixedWidth(220); sc_fix_btn.clicked.connect(self._repair_shortcuts)
        left.addWidget(sc_fix_btn)

        # ── Left: Save Backup ─────────────────────────────────────────────
        _head(left, "Save Backup")
        _desc(left, "Copies your saves, stats and Plutonium mods to a folder that survives an "
                    "uninstall. Steam can stay open.")
        backup_btn = _btn("Back Up My Saves Now", C_DARK_BTN, size=12, h=40)
        backup_btn.setFixedWidth(220); backup_btn.clicked.connect(self._backup_saves_now)
        left.addWidget(backup_btn)

        # ── Left: Check My Setup ──────────────────────────────────────────
        _head(left, "Check My Setup")
        _desc(left, "Checks each game's folder, mod files, GE-Proton and Steam launch option. It "
                    "changes nothing; Fix runs that game's Update.")
        check_btn = _btn("Check My Setup", C_DARK_BTN, size=12, h=40)
        check_btn.setFixedWidth(220); check_btn.clicked.connect(self._check_setup)
        left.addWidget(check_btn)

        # ── Left: Player Name ─────────────────────────────────────────────
        _head(left, "Player Name")
        self._name_note = _desc(left, "Your name in CoD4x, IW4x, AlterWare, T7X and Plutonium offline. "
                                      "It is not used by CleanOps.")
        nr = QHBoxLayout(); nr.setSpacing(12)
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Enter player name...")
        self._name_input.setFixedHeight(40)
        self._name_input.setMaxLength(32)
        self._name_input.setStyleSheet(
            "QLineEdit{background:#2A2A3A;color:#FFF;border:1px solid #444;"
            "border-radius:6px;padding:0 10px;font-size:13px;}"
            "QLineEdit:focus{border:1px solid #888;}"
        )
        save_btn = _btn("Save", C_IW, size=12, h=40)
        save_btn.setFixedWidth(90)
        save_btn.clicked.connect(self._save_name)
        nr.addWidget(self._name_input, stretch=1); nr.addWidget(save_btn)
        left.addLayout(nr)

        # ── Left: FPS and FOV ─────────────────────────────────────────────
        _head(left, "FPS and FOV")
        _desc(left, "Sets 90 FOV and a 90 FPS cap in the configs of MW1, MW2, MW3, WaW, BO1 and BO2 "
                    "(all modes). Only those lines change. Steam can stay open.")
        fps_btn = _btn("Apply 90 FPS / 90 FOV", C_DARK_BTN, size=12, h=40)
        fps_btn.setFixedWidth(220); fps_btn.clicked.connect(self._apply_fps_fov)
        left.addWidget(fps_btn)
        _desc(left, "MW2 and MW3 campaign: DeckOps holds FOV at 90 while the game runs, and DXVK caps "
                    "the frame rate at 90 in the Steam launch option, because these builds have no "
                    "in-game cap. The button below closes Steam. MW2 also keeps an F10 key for FOV 90: "
                    "map a back paddle to F10 in Steam Input.")
        self._cap_btn = _btn("", C_DARK_BTN, size=12, h=40)
        self._cap_btn.setFixedWidth(220); self._cap_btn.clicked.connect(self._toggle_dxvk_cap)
        self._refresh_cap_btn()
        left.addWidget(self._cap_btn)

        # ── Left: Shader Cache (LCD only) ─────────────────────────────────
        self._shader_row = QWidget()
        sr_lay = QVBoxLayout(self._shader_row); sr_lay.setContentsMargins(0,0,0,0); sr_lay.setSpacing(10)
        sr_lay.addSpacing(12)
        sr_lay.addWidget(_lbl("Shader Cache", 13, "#CCC", bold=True, align=Qt.AlignmentFlag.AlignLeft))
        _desc(sr_lay, "Clears the shader cache of every game you have set up. LCD only.")
        sc_btn = _btn("Clear Shader Cache", C_DARK_BTN, size=12, h=40)
        sc_btn.setFixedWidth(220)
        sc_btn.clicked.connect(self._clear_shader_cache)
        sr_lay.addWidget(sc_btn)
        left.addWidget(self._shader_row)
        # Kept for showEvent's LCD toggle; the two-column layout has no dividers
        self._shader_hdiv = QWidget(); self._shader_hdiv.setFixedHeight(0)
        left.addWidget(self._shader_hdiv)
        left.addStretch()

        # ── Right: Links ──────────────────────────────────────────────────
        _head(right, "Links", first=True)
        _desc(right, "Support on Discord, and the Stable and Nightly project pages.")
        lr = QHBoxLayout(); lr.setSpacing(12)
        discord_btn = _btn("Discord", "#5865F2", size=12, h=40)
        discord_btn.setFixedWidth(120)
        discord_btn.clicked.connect(lambda: self._open_url("https://discord.gg/bkSQeq5Azk"))
        stable_btn = _btn("Stable", C_DARK_BTN, size=12, h=40)
        stable_btn.setFixedWidth(120)
        stable_btn.clicked.connect(lambda: self._open_url(f"https://github.com/{GITHUB_USER}/DeckOps"))
        nightly_btn = _btn("Nightly", C_DARK_BTN, size=12, h=40)
        nightly_btn.setFixedWidth(120)
        nightly_btn.clicked.connect(lambda: self._open_url(f"https://github.com/{GITHUB_USER}/DeckOps-Nightly"))
        lr.addWidget(discord_btn); lr.addWidget(stable_btn); lr.addWidget(nightly_btn); lr.addStretch()
        right.addLayout(lr)

        # ── Right: Updates & Logs ─────────────────────────────────────────
        _head(right, "Updates & Logs")
        _desc(right, "Checks for a new DeckOps version. The log shows what DeckOps has done; "
                     "copy it when you report a problem.")
        self._update_btn = _btn("Check for Updates", C_BLUE_BTN, size=12, h=40)
        self._update_btn.setFixedWidth(220)
        self._update_btn.clicked.connect(self._check_for_updates)
        right.addWidget(self._update_btn)
        self._update_status = _lbl("", 11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self._update_status)
        udr = QHBoxLayout(); udr.setSpacing(12)
        log_btn = _btn("Copy Log", C_DARK_BTN, size=12, h=40)
        log_btn.setFixedWidth(130)
        view_log_btn = _btn("View Log", C_DARK_BTN, size=12, h=40)
        view_log_btn.setFixedWidth(130)
        view_log_btn.clicked.connect(self._view_log)
        udr.addWidget(log_btn); udr.addWidget(view_log_btn); udr.addStretch()
        right.addLayout(udr)
        self._log_status = _lbl("", 11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self._log_status)
        log_btn.clicked.connect(lambda: _copy_log_to_clipboard(self._log_status))

        # ── Right: Danger Zone ────────────────────────────────────────────
        _head(right, "Danger Zone", "#E05050")
        _desc(right, "Full Uninstall removes DeckOps from your games and Steam; save backups stay. "
                     "Reset DeckOps Config clears your setup and runs setup again. "
                     "Each asks for confirmation first.")
        dzr = QHBoxLayout(); dzr.setSpacing(12)
        uninstall_btn = _btn("Full Uninstall", C_RED_BTN, size=12, h=40)
        reset_cfg_btn = _btn("Reset DeckOps Config", C_RED_BTN, size=12, h=40)
        uninstall_btn.clicked.connect(self._confirm_uninstall)
        reset_cfg_btn.clicked.connect(self._confirm_reset)
        dzr.addWidget(uninstall_btn); dzr.addWidget(reset_cfg_btn); dzr.addStretch()
        right.addLayout(dzr)

        # ── Right: About + status ─────────────────────────────────────────
        right.addSpacing(12)
        self._about_label = _lbl("", 11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self._about_label)
        self.status = _lbl("", 12, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self.status)
        right.addStretch()

        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

    def showEvent(self, event):
        super().showEvent(event)
        if event.spontaneous():     # window restored: don't overwrite what's being typed
            return
        # Sync music toggle with current state (may have changed via mute button)
        self._music_on = cfg.get_music_enabled()
        self._music_toggle.setText("Music: ON" if self._music_on else "Music: OFF")
        self._music_toggle.setStyleSheet(
            self._music_toggle.styleSheet()
            .replace(C_IW if not self._music_on else C_DARK_BTN,
                     C_IW if self._music_on else C_DARK_BTN))
        # Refresh dynamic state
        model = cfg.get_deck_model() or "unknown"
        source_label = "Steam & Non-Steam"
        player = cfg.get_player_name() or "Player"
        self._name_input.setText(player if player != "Player" else "")

        # Show hold/toggle only for Steam Deck LCD/OLED and Steam Machine
        has_full_modes = model in ("lcd", "oled", "steam_machine")
        self._gyro_btns["hold"].setVisible(has_full_modes)
        self._gyro_btns["toggle"].setVisible(has_full_modes)
        # Relabel ADS button for non-SD/SM devices
        self._gyro_btns["on"].setText("ADS" if has_full_modes else "Gyro On")

        # Gyro highlight
        gyro = cfg.get_gyro_mode() or "on"
        # If non-SD/SM device has hold/toggle saved, treat as "on" for highlight
        if not has_full_modes and gyro in ("hold", "toggle"):
            gyro = "on"
        for mode, btn in self._gyro_btns.items():
            if mode == gyro:
                btn.setStyleSheet(btn.styleSheet().replace(C_DARK_BTN, C_IW))
            else:
                btn.setStyleSheet(btn.styleSheet().replace(C_IW, C_DARK_BTN))

        # LCD-only shader cache section
        is_lcd = (model == "lcd")
        self._shader_row.setVisible(is_lcd)
        self._shader_hdiv.setVisible(is_lcd)

        # Build info
        build = "dev"
        build_path = os.path.join(PROJECT_ROOT, "BUILD")
        if os.path.exists(build_path):
            try:
                with open(build_path, "r") as f:
                    build = f.read().strip() or "dev"
            except Exception:
                _log.debug("BUILD file read failed", exc_info=True)

        # Device display name for about label
        if model == "steam_machine":
            device_display = "Steam Machine"
        elif model == "other":
            other_dev = cfg.get_other_device() or "unknown"
            _DEVICE_NAMES = {
                "1920x1200": "Other (1920×1200)",
                "1920x1200_144hz": "Other (1920×1200 144Hz)",
                "1920x1080": "Other (1920×1080)",
                "1280x720": "Other (1280×720)",
            }
            device_display = _DEVICE_NAMES.get(other_dev, f"Other ({other_dev})")
        elif model == "oled":
            device_display = "Steam Deck OLED"
        elif model == "lcd":
            device_display = "Steam Deck LCD"
        else:
            device_display = f"Steam Deck {model.upper()}"

        self._about_label.setText(
            f"Device: {device_display}  |  Source: {source_label}  |  "
            f"Player: {player}\n"
            f"Build: {build}"
        )

    def _toggle_music(self):
        self._music_on = not self._music_on
        _set_audio_enabled(self._music_on)
        if self._music_on:
            _start_audio()
            self._music_toggle.setText("Music: ON")
            self._music_toggle.setStyleSheet(
                self._music_toggle.styleSheet().replace(C_DARK_BTN, C_IW))
        else:
            _kill_audio()
            self._music_toggle.setText("Music: OFF")
            self._music_toggle.setStyleSheet(
                self._music_toggle.styleSheet().replace(C_IW, C_DARK_BTN))

    def _set_volume(self, val):
        self._vol_label.setText(f"{val}%")
        _set_audio_volume(val / 100.0)

    def _set_gyro(self, mode):
        cfg.set_gyro_mode(mode)
        # Update button highlights
        for m, btn in self._gyro_btns.items():
            if m == mode:
                btn.setStyleSheet(btn.styleSheet().replace(C_DARK_BTN, C_IW))
            else:
                btn.setStyleSheet(btn.styleSheet().replace(C_IW, C_DARK_BTN))
        self.status.setText(f"Gyro mode set to {mode.upper()}. Press Re-apply Templates to use it.")

    def _settings_task(self, s):
        """
        Start of a Settings action (Re-apply Templates, Repair Shortcuts, Save
        name, Clear Shader Cache): False, with a message, while another one
        runs. They used to start a thread straight from the click, so a double
        tap -- easy on the touchscreen -- ran two at once, both closing Steam
        and rewriting shortcuts.vdf / configsets / game configs together.
        The worker's done signal (always emitted) frees it again.
        """
        if getattr(self, "_settings_running", False):
            self.status.setText("Please wait for the current task to finish.")
            return False
        self._settings_running = True
        s.done.connect(lambda _ok: setattr(self, "_settings_running", False))
        return True

    def _apply_controller_profiles(self):
        s = _Sigs()
        if not self._settings_task(s):
            return
        self.status.setText("Re-applying controller profiles...")
        s.log.connect(lambda msg: self.status.setText(msg))
        s.done.connect(lambda ok: self.status.setText(
            "✓  Controller profiles applied." if ok else "✗  Failed — check that Steam is closed."
        ))
        def _run():
            try:
                from controller_profiles import install_controller_templates, assign_controller_profiles, assign_external_controller_profiles
                from wrapper import kill_steam, set_steam_input_enabled
                s.log.emit("Closing Steam...")
                kill_steam(on_progress=lambda msg: s.log.emit(f"  {msg}"))
                install_controller_templates(
                    on_progress=lambda msg: s.log.emit(msg)
                )
                gyro_mode = cfg.get_gyro_mode() or "on"
                assign_controller_profiles(
                    gyro_mode,
                    on_progress=lambda msg: s.log.emit(msg)
                )
                if cfg.uses_external_controller():
                    controller_type = cfg.get_external_controller() or "playstation"
                    assign_external_controller_profiles(
                        controller_type,
                        gyro_mode,
                        on_progress=lambda msg: s.log.emit(msg)
                    )
                sr = cfg.load().get("steam_root", "")
                if sr:
                    set_steam_input_enabled(sr)
                s.done.emit(True)
                s.log.emit("✓  Controller profiles applied. It is now safe to reopen Steam.")
            except Exception as ex:
                s.log.emit(f"✗  Failed: {ex}")
                s.done.emit(False)
        threading.Thread(target=_run, daemon=True).start()

    def _repair_shortcuts(self):
        s = _Sigs()
        if not self._settings_task(s):
            return
        self.status.setText("Checking shortcuts...")
        s.log.connect(lambda msg: self.status.setText(msg))
        def _run():
            try:
                from shortcut import repair_shortcuts
                from wrapper import kill_steam
                s.log.emit("Closing Steam...")
                kill_steam(on_progress=lambda msg: s.log.emit(f"  {msg}"))
                sr = cfg.load().get("steam_root", "") or None
                readded = repair_shortcuts(sr, on_progress=s.log.emit)
                if readded:
                    s.log.emit(f"✓  Re-added: {', '.join(readded)}. It is now safe to reopen Steam.")
                else:
                    s.log.emit("✓  All shortcuts present. It is now safe to reopen Steam.")
            except Exception as ex:
                s.log.emit(f"✗  Repair failed: {ex}")
            finally:
                s.done.emit(True)
        threading.Thread(target=_run, daemon=True).start()

    def _save_name(self):
        typed = self._name_input.text().strip()
        name = cfg.clean_player_name(typed)
        if not name:
            self.status.setText("Please enter a name (letters, numbers and most symbols; "
                                "not \" \\ / $ ` ; < > : | ? *).")
            return
        s = _Sigs()
        if not self._settings_task(s):
            return
        self._name_input.setText(name)
        cfg.set_player_name(name)
        self.status.setText(f"Updating player name to \"{name}\"...")
        s.log.connect(lambda msg: self.status.setText(msg))
        def _run():
            try:
                from game_config import rename_player
                from detect_games import find_all_games as _find_all
                steam_root = cfg.load().get("steam_root", "") or find_steam_root()
                installed = _find_all(steam_root) if steam_root else {}
                count = rename_player(
                    name, steam_root, installed_games=installed,
                    on_progress=lambda msg: s.log.emit(msg),
                )
                s.log.emit(f"✓  Player name set to \"{name}\" ({count} config(s) updated).")
            except Exception as ex:
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        threading.Thread(target=_run, daemon=True).start()

    def _refresh_cap_btn(self):
        self._cap_btn.setText("DXVK Frame Cap: " + ("On" if cfg.get_dxvk_frame_cap() else "Off"))

    def _toggle_dxvk_cap(self):
        """Flip the setting, then run Repair Shortcuts, which closes Steam and rewrites
        the MW2 / MW3 SP launch options to match."""
        c = cfg.load()
        c["dxvk_frame_cap"] = not cfg.get_dxvk_frame_cap()
        cfg.save(c)
        self._refresh_cap_btn()
        self._repair_shortcuts()

    def _apply_fps_fov(self):
        s = _Sigs()
        if not self._settings_task(s):
            return
        self.status.setText("Setting 90 FPS and 90 FOV...")
        s.log.connect(lambda msg: self.status.setText(msg))
        def _run():
            try:
                from game_config import set_fps_fov
                from detect_games import find_all_games as _find_all
                steam_root = cfg.load().get("steam_root", "") or find_steam_root()
                installed = _find_all(steam_root) if steam_root else {}
                count = set_fps_fov(steam_root, installed,
                                    on_progress=lambda msg: _log.info("fps/fov: %s", msg.strip()))
                s.log.emit(f"✓  90 FPS / 90 FOV set in {count} config file(s). "
                           "Close the games first if they are running.")
            except Exception as ex:
                _log.error("fps/fov failed", exc_info=True)
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        threading.Thread(target=_run, daemon=True).start()

    def _backup_saves_now(self):
        s = _Sigs()
        if not self._settings_task(s):
            return
        self.status.setText("Backing up your saves...")
        s.log.connect(lambda msg: self.status.setText(msg))
        def _run():
            try:
                from save_backup import backup_saves, describe_backup
                failures = []
                backed_up = backup_saves(find_steam_root(), failures=failures,
                                         on_progress=lambda m: _log.info("save backup: %s", m.strip()))
                s.log.emit(describe_backup(backed_up, failures))
            except Exception as ex:
                _log.error("save backup failed", exc_info=True)
                s.log.emit(f"✗  Save backup failed: {ex}")
            finally:
                s.done.emit(True)
        threading.Thread(target=_run, daemon=True).start()

    def _clear_shader_cache(self):
        s = _Sigs()
        if not self._settings_task(s):
            return
        self.status.setText("Clearing shader cache...")
        s.log.connect(lambda msg: self.status.setText(msg))
        def _run():
            try:
                from cache_cleanup import cleanup_shader_cache, STEAM_APPIDS
                setup = cfg.get_setup_games()
                cleared = 0
                for key in STEAM_APPIDS:
                    if key in setup:
                        source = setup[key].get("source", "steam")
                        cleanup_shader_cache(key, source)
                        cleared += 1
                s.log.emit(f"✓  Shader cache cleared for {cleared} game(s).")
            except Exception as ex:
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        threading.Thread(target=_run, daemon=True).start()

    def _check_setup(self):
        """Run setup_check in the background, then show the report."""
        s = _Sigs()
        if not self._settings_task(s):
            return
        self.status.setText("Checking your setup...")
        self._setup_results = None

        def _run():
            try:
                from setup_check import check_setup
                mgmt = get_screen(self.stack, "ManagementScreen")
                installed = (getattr(mgmt, "installed", None) or find_all_games())
                self._setup_results = (check_setup(find_steam_root(), installed, cfg.get_setup_games()),
                                       installed)
                s.log.emit("Setup check finished.")
            except Exception as ex:
                _log.error("setup check failed", exc_info=True)
                s.log.emit(f"✗  Setup check failed: {ex}")
            finally:
                s.done.emit(True)

        def _show(msg):
            self.status.setText(msg)
            if self._setup_results:
                self._show_setup_report(*self._setup_results)
        s.log.connect(_show)
        threading.Thread(target=_run, daemon=True).start()

    def _show_setup_report(self, results, installed):
        self._setup_installed = installed
        dlg = QDialog(self)
        dlg.setWindowTitle("Check My Setup")
        dlg.resize(720, 480)
        vl = QVBoxLayout(dlg); vl.setContentsMargins(12,12,12,12); vl.setSpacing(8)
        bad = [r for r in results if r["problems"]]
        vl.addWidget(_lbl("No games are set up yet." if not results else
                          "Everything looks fine." if not bad else
                          f"{len(bad)} of {len(results)} game(s) need attention.",
                          13, "#CCC", bold=True, align=Qt.AlignmentFlag.AlignLeft))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); rows = QVBoxLayout(inner); rows.setSpacing(8)
        for r in results:
            row = QHBoxLayout()
            text = f"✓  {r['name']}" if not r["problems"] else \
                f"⚠  {r['name']}\n" + "\n".join("     • " + p for p in r["problems"])
            row.addWidget(_lbl(text, 11, C_DIM if not r["problems"] else "#E0B050",
                               align=Qt.AlignmentFlag.AlignLeft), stretch=1)
            if r["fixable"]:
                fix = _btn("Fix", C_IW, size=11, h=36); fix.setFixedWidth(90)
                fix.clicked.connect(lambda _c=False, k=r["key"]: (dlg.accept(), self._fix_game(k)))
                row.addWidget(fix)
            rows.addLayout(row)
        rows.addStretch()
        scroll.setWidget(inner)
        vl.addWidget(scroll, stretch=1)
        close = _btn("Close", C_DARK_BTN, size=11, h=40); close.setFixedWidth(130)
        close.clicked.connect(dlg.reject)
        br = QHBoxLayout(); br.addStretch(); br.addWidget(close); br.addStretch()
        vl.addLayout(br)
        self._setup_dialog = dlg
        dlg.exec()

    def _fix_game(self, key):
        """Fix = the game's normal Update (reinstalls the client, GE-Proton,
        launch options; closes Steam itself)."""
        gd = next((g for g in ALL_GAMES if key in g.get("keys", [])), None)
        mgmt = get_screen(self.stack, "ManagementScreen")
        if not mgmt.installed:          # My Games hasn't scanned yet: use the check's scan
            mgmt.installed = dict(getattr(self, "_setup_installed", None) or {})
        if not gd or not mgmt.installed.get(key):
            self.status.setText("That game could not be found; open My Games and try again.")
            return
        mgmt._update(gd, [key])

    def _view_log(self):
        from ui_constants import LOG_PATH
        lines = []
        try:
            with open(LOG_PATH, "r", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            pass
        tail = "".join(lines[-500:]) if lines else "(No log file found)"

        dlg = QDialog(self)
        dlg.setWindowTitle("Install Log")
        dlg.resize(700, 480)
        vl = QVBoxLayout(dlg); vl.setContentsMargins(12,12,12,12); vl.setSpacing(8)
        txt = QPlainTextEdit(); txt.setReadOnly(True); txt.setFont(font(10))
        txt.setStyleSheet("QPlainTextEdit{color:#AAB;background:#1A1A24;border:1px solid #333;border-radius:4px;padding:6px;}")
        txt.setPlainText(tail)
        vl.addWidget(txt, stretch=1)
        br = QHBoxLayout()
        copy_btn = _btn("Copy to Clipboard", C_DARK_BTN, size=11, h=40)
        copy_btn.clicked.connect(lambda: (
            QApplication.clipboard().setText(tail),
            self.status.setText("Log copied to clipboard."),
        ))
        br.addStretch(); br.addWidget(copy_btn); br.addStretch()
        vl.addLayout(br)
        txt.verticalScrollBar().setValue(txt.verticalScrollBar().maximum())
        dlg.exec()

    def _open_url(self, url):
        _detached_open(["xdg-open", url])

    def _confirm_uninstall(self):
        reply = QMessageBox.warning(
            self, "Full Uninstall",
            "This will remove all DeckOps files, shortcuts, controller profiles, "
            "and mod client installations. Your Steam games will NOT be deleted.\n\n"
            "DeckOps will close and the uninstaller will run in a terminal window.\n\n"
            "Are you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._run_uninstaller()

    def _run_uninstaller(self):
        uninstall_script = os.path.join(PROJECT_ROOT, "deckops_uninstall.sh")
        if not os.path.exists(uninstall_script):
            self.status.setText("✗  Uninstall script not found.")
            return
        self.status.setText("Launching uninstaller...")
        try:
            # Launch in Konsole so the user can see progress
            subprocess.Popen(
                ["konsole", "-e", "bash", uninstall_script],
                start_new_session=True,
            )
            # Close DeckOps — the uninstaller will remove our files
            _kill_audio()
            QApplication.instance().quit()
        except Exception:
            # Konsole not available (shouldn't happen on SteamOS)
            try:
                subprocess.Popen(
                    ["xterm", "-e", "bash", uninstall_script],
                    start_new_session=True,
                )
                _kill_audio()
                QApplication.instance().quit()
            except Exception as ex:
                self.status.setText(f"✗  Could not launch uninstaller: {ex}")

    # ── Update check ────────────────────────────────────────────────────────
    _GITHUB_USER = GITHUB_USER
    _GITHUB_REPO = GITHUB_REPO

    def _init_update_signals(self):
        """Connect the update result signal (call once during __init__)."""
        self._update_sig = _Sigs()
        self._update_sig.log.connect(self._on_update_signal)

    def _check_for_updates(self):
        """Hit GitHub API to compare local SHA against remote HEAD."""
        self._init_update_signals()
        self._update_btn.setEnabled(False)
        self._update_status.setText("Checking...")
        self._update_status.setStyleSheet(f"color:{C_DIM};")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        self._update_sig.log.emit(_check_update_status())

    def _on_update_signal(self, msg):
        """Slot that runs on the main thread via pyqtSignal."""
        kind, text = msg.split("|", 1)
        self._update_result(text, kind == "UPDATE")

    def _update_result(self, msg, update_available):
        self._update_btn.setEnabled(True)
        self._update_status.setText(msg)
        if update_available:
            self._update_status.setStyleSheet(f"color:{C_IW};")
            self._update_btn.setText("Update && Restart")
            try:
                self._update_btn.clicked.disconnect()
            except TypeError:
                pass
            self._update_btn.clicked.connect(self._apply_update)
        else:
            self._update_status.setStyleSheet(f"color:{C_DIM};")

    def _apply_update(self):
        reply = QMessageBox.question(
            self, "Update DeckOps",
            "DeckOps will download the update and relaunch.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._update_btn.setEnabled(False)
        self._update_btn.setText("Updating...")
        self._update_status.setText("Downloading update...")
        self._update_status.setStyleSheet(f"color:{C_DIM};")

        # Same popup as the Zombies Declassified download
        from qt import QProgressDialog
        pd = self._update_pd = QProgressDialog("Checking files...", None, 0, 100, self)
        pd.setWindowTitle("Update DeckOps")
        pd.setMinimumDuration(0); pd.setAutoClose(False); pd.setAutoReset(False)
        pd.setCancelButton(None); pd.show()

        self._apply_sig = _Sigs()
        self._apply_sig.progress.connect(lambda p, m: (pd.setValue(p), pd.setLabelText(m)))
        self._apply_sig.log.connect(self._on_apply_signal)
        threading.Thread(target=self._apply_worker, daemon=True).start()

    def _apply_worker(self):
        from update_manifest import update_lock
        try:
            with update_lock(PROJECT_ROOT):
                self._apply_worker_locked()
        except BlockingIOError:
            self._apply_sig.log.emit("FAIL|Another DeckOps update is running. Wait for it to finish and try again.")
        except OSError as ex:
            _log.error("Cannot lock DeckOps update", exc_info=True)
            self._apply_sig.log.emit(f"FAIL|Could not start DeckOps update: {ex}")

    def _apply_worker_locked(self):
        """Download differing files, then apply them as a recoverable update."""
        from net import download
        from steam_common import git_blob_sha
        from update_manifest import apply_update, recover_update
        install_dir = PROJECT_ROOT
        update_dir = os.path.join(install_dir, ".update")
        try:
            if recover_update(install_dir):
                _log.warning("Rolled back an interrupted DeckOps update before retrying")
            local_sha, remote_sha, stale, gone = _remote_changes(removed=True)
            if not stale and not gone and local_sha == remote_sha:
                self._apply_sig.log.emit("FAIL|Already up to date.")
                return
            # Files come from the commit itself: main on raw.githubusercontent is cached
            # for minutes after a push and served the previous version (Deck, 2026-10-22)
            raw = f"https://raw.githubusercontent.com/{self._GITHUB_USER}/{self._GITHUB_REPO}/{remote_sha}"
            if os.path.isdir(update_dir):
                shutil.rmtree(update_dir)
            os.makedirs(update_dir, exist_ok=True)

            total = len(stale)
            nbytes = sum(size for _, _, size in stale.values()) or 1
            got = 0
            for idx, (path, (sha, _, size)) in enumerate(sorted(stale.items())):
                head = f"Downloading ({idx+1}/{total})"
                self._apply_sig.log.emit(f"PROG|{head}: {os.path.basename(path)}")
                dest = os.path.join(update_dir, path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # Bar fills by bytes across all files; the label shows this file's size and speed
                prog = lambda p, m, g=got, sz=size, h=head: self._apply_sig.progress.emit(
                    min(99, int((g + sz * p / 100) * 100 / nbytes)), f"{h}\n{m}")
                # net.download retries and rejects truncated streams; the hash check catches the rest
                download(f"{raw}/{urllib.parse.quote(path)}", dest, on_progress=prog,
                         label=os.path.basename(path), timeout=30, headers=_HDR)
                if git_blob_sha(dest) != sha:
                    raise RuntimeError(f"{path} does not match GitHub")
                got += size
            self._apply_sig.progress.emit(99, "Installing update...")

            # A syntax error pushed to main would otherwise brick every install on relaunch
            if not compileall.compile_dir(update_dir, quiet=1):
                raise RuntimeError("update has Python errors, nothing applied")

            # Stage is complete: back up every touched file, then install as
            # one recoverable operation. VERSION changes only after all files
            # are in place. A failed apply restores the old installation.
            changes = [("M", path, sha) for path, (sha, _, _) in stale.items()]
            changes.extend(("D", path, "-") for path in gone)
            apply_update(install_dir, update_dir, remote_sha, changes)
            shutil.rmtree(update_dir, ignore_errors=True)

            # Clean up stale flag file if present
            flag = os.path.expanduser("~/.deckops_update_requested")
            if os.path.isfile(flag):
                os.remove(flag)

            _log.info("Update applied successfully — SHA %s (%d file(s), %d removed)", remote_sha, total, len(gone))
            self._apply_sig.log.emit("RELAUNCH|Update complete! Relaunching...")

        except Exception as ex:
            _log.error("Update failed: %s", ex)
            if os.path.isfile(os.path.join(update_dir, "transaction.json")):
                # Do not keep offering an app with a possibly mixed install.
                # The next launch will attempt the rollback again.
                self._apply_sig.log.emit(f"ROLLBACK|Could not restore the previous DeckOps version: {ex}")
            else:
                shutil.rmtree(update_dir, ignore_errors=True)
                why = _RATE_LIMIT_MSG if _rate_limited(ex) else str(ex)
                self._apply_sig.log.emit(f"FAIL|Update did not finish. {why}")

    def _on_apply_signal(self, msg):
        kind, text = msg.split("|", 1)
        if kind == "PROG":
            self._update_status.setText(text)
        elif kind == "RELAUNCH":
            self._update_pd.setValue(100); self._update_pd.setLabelText(text)
            self._update_status.setText(text)
            self._update_status.setStyleSheet(f"color:{C_IW};")
            _kill_audio()
            QTimer.singleShot(1000, self._relaunch)
        elif kind == "ROLLBACK":
            self._update_pd.close()
            self._update_btn.setEnabled(False)
            QMessageBox.critical(self, "DeckOps update needs recovery",
                f"{text}\n\nDeckOps will close to avoid using a partial update. "
                "Open it again to retry recovery. Your data has been kept; "
                "contact DeckOps support if it still cannot open.")
            _kill_audio()
            QApplication.instance().quit()
        else:
            # FAIL
            self._update_pd.close()
            self._update_btn.setEnabled(True)
            self._update_btn.setText("Check for Updates")
            try:
                self._update_btn.clicked.disconnect()
            except TypeError:
                pass
            self._update_btn.clicked.connect(self._check_for_updates)
            self._update_status.setText(text)
            self._update_status.setStyleSheet(f"color:{C_TREY};")

    def _relaunch(self):
        """Relaunch DeckOps by exec-ing the same Python + main.py."""
        # Clear bytecode cache so updated .py files are re-imported
        pycache = os.path.join(PROJECT_ROOT, "src", "__pycache__")
        if os.path.isdir(pycache):
            shutil.rmtree(pycache, ignore_errors=True)
        python = sys.executable
        entry = os.path.join(PROJECT_ROOT, "src", "main.py")
        _log.info("Relaunching: %s %s", python, entry)
        os.execv(python, [python, entry])

    def _confirm_reset(self):
        reply = QMessageBox.warning(
            self, "Reset Config",
            "This will wipe the DeckOps configuration file (deckops.json). "
            "DeckOps will restart and run the setup flow again.\n\n"
            "Your game files and Steam games are not affected.\n\n"
            "Are you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._reset_deckops()

    def _reset_deckops(self):
        cfg.reset()
        self.status.setText("Config wiped. Restarting setup...")
        QTimer.singleShot(1500, lambda: go_to(self.stack, "BootstrapScreen"))



# ── UpdateScreen ──────────────────────────────────────────────────────────────
class UpdateScreen(QWidget):
    """Handles Update from ManagementScreen."""

    def __init__(self, stack):
        super().__init__(); self.stack = stack; self.screen_name = "UpdateScreen"
        self.update_zd = False
        self.selected   = []
        self.steam_root = ""
        # (title, fn(sigs)) run in place of the game updates, e.g. adding Offline Mode
        self.task = None
        self._steam_closed = threading.Event()
        self._failed_games = []
        self._failed_zd = False
        self._zd_missing = False

        lay = QVBoxLayout(self); lay.setContentsMargins(80,60,80,60); lay.setSpacing(20)
        self.title = QLabel("UPDATE"); self.title.setFont(font(36, True))
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("color:#FFF;background:transparent;")
        lay.addWidget(self.title)

        self.cur = _lbl("Preparing...", 16, "#CCC"); lay.addWidget(self.cur)
        self.bar = QProgressBar(); self.bar.setMaximum(100); self.bar.setTextVisible(False)
        self.bar.setFixedHeight(22)
        bw = QHBoxLayout(); bw.setContentsMargins(60,0,60,0); bw.addWidget(self.bar)
        lay.addLayout(bw)

        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        self.log.setFont(font(11))
        self.log.setStyleSheet("QPlainTextEdit{color:#666677;background:transparent;border:none;padding:10px;}")
        lay.addWidget(self.log, stretch=1)

        self._log_status = _lbl("", 10, C_DIM, wrap=False)
        log_btn = _btn("Copy Log", C_DARK_BTN, size=11, h=40); log_btn.setFixedWidth(130)
        log_btn.clicked.connect(lambda: _copy_log_to_clipboard(self._log_status))
        lr = QHBoxLayout(); lr.addStretch(); lr.addWidget(log_btn); lr.addWidget(self._log_status); lr.addStretch()
        lay.addLayout(lr)

        # Shown while waiting for the Plutonium window; DeckOps closes Steam itself afterwards
        self.steam_btn = _btn("I've closed Plutonium  ✓", C_TREY, size=13, h=52)
        self.steam_btn.setFixedWidth(360); self.steam_btn.setVisible(False)
        self.steam_btn.clicked.connect(lambda: self._steam_closed.set())
        sw = QHBoxLayout(); sw.addStretch(); sw.addWidget(self.steam_btn); sw.addStretch()
        lay.addLayout(sw)

        self.back_btn = _btn("<< Back to My Games", C_DARK_BTN, h=48)
        self.back_btn.setFixedWidth(220); self.back_btn.setVisible(False)
        self.back_btn.clicked.connect(self._go_back)
        self.retry_btn = _btn("Retry Failed Games", C_IW, h=48)
        self.retry_btn.setFixedWidth(220); self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self._retry_failed)
        bk = QHBoxLayout(); bk.addStretch(); bk.addWidget(self.back_btn)
        bk.addWidget(self.retry_btn); bk.addStretch()
        lay.addLayout(bk)

        self._s = _Sigs()
        self._s.progress.connect(lambda p,m: (self.bar.setValue(p), self.cur.setText(m)))
        self._s.log.connect(self._append_log)
        self._s.done.connect(self._on_done)
        self._s.plut_wait.connect(lambda: self.steam_btn.setVisible(True))
        self._s.plut_go.connect(lambda: self.steam_btn.setVisible(False))
        self._s.ask.connect(self._ask_continue)
        self._ask_event = threading.Event()
        self._ask_yes = False
        self._cancelled = False

    def _ask_continue(self, text):
        """UI thread: answer a worker's Yes/No question (see _confirm)."""
        try:
            self._ask_yes = QMessageBox.question(self, "Save Backup", text) == QMessageBox.StandardButton.Yes
        finally:
            self._ask_event.set()

    def _confirm(self, text):
        """Worker thread: ask the player and wait for the answer."""
        self._ask_yes = False
        self._ask_event.clear()
        self._s.ask.emit(text)
        self._ask_event.wait()
        return self._ask_yes

    def _backup_first(self):
        """Back up saves before anything is changed. If the backup is
        incomplete the player decides whether to go on; No ends the update
        with nothing touched. Returns True to continue."""
        self._s.progress.emit(1, "Backing up your saves...")
        self._s.log.emit("Backing up your saves before updating...")
        failures = []
        try:
            from save_backup import backup_saves, describe_backup
            backed_up = backup_saves(self.steam_root, failures=failures,
                                     on_progress=lambda m: self._s.log.emit(f"  {m.strip()}"),
                                     installed_games={k: g for k, _, g in self.selected if g})
            self._s.log.emit(describe_backup(backed_up, failures))
        except Exception as ex:
            _log.error("save backup before update failed", exc_info=True)
            failures.append("all")
            self._s.log.emit(f"⚠  Save backup failed: {ex}")
        if not failures:
            return True
        if self._confirm("Your saves could not be fully backed up (see the log).\n\n"
                         "Updating does not normally touch saves, but without a backup a problem "
                         "could not be undone.\n\nContinue updating anyway?"):
            self._s.log.emit("  Continuing without a complete save backup.")
            return True
        self._cancelled = True
        self._s.log.emit("Update cancelled. Nothing was changed.")
        self._s.progress.emit(100, "Update cancelled.")
        self._s.done.emit(False)
        return False

    def _append_log(self, text):
        self.log.appendHtml(_log_html(text))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        _log_to_file(text)

    def showEvent(self, e):
        super().showEvent(e)
        # Spontaneous = window restored / re-mapped, not a screen change: that
        # used to clear the log and start a second update thread
        if not e.spontaneous():
            self._start_update()

    def _start_update(self):
        self.bar.setValue(0); self.log.clear()
        self.cur.setText("Preparing...")
        self.title.setText(self.task[0] if self.task else "UPDATE")
        self.steam_btn.setVisible(False); self.back_btn.setVisible(False)
        self.retry_btn.setVisible(False)
        self._failed_games = []
        self._failed_zd = False
        self._zd_missing = False
        self._cancelled = False
        self._steam_closed.clear()
        inhibit.start("DeckOps is updating")
        _log_to_file("── Update started ──")
        QTimer.singleShot(400, lambda: threading.Thread(target=self._run, daemon=True).start())

    def _on_done(self, ok):
        inhibit.stop()
        self.back_btn.setVisible(True)
        if ok:
            self.cur.setText("Update complete. It is now safe to open Steam.")
            return
        if self._cancelled:
            self.cur.setText("Update cancelled. Nothing was changed.")
            return
        if self._failed_games:
            names = ", ".join(dict.fromkeys(gd["base"] for _, gd, _ in self._failed_games))
            if self._zd_missing:
                self.cur.setText(f"Update incomplete: {names}. Zombies Declassified is missing; reinstall it from BO2 Options.")
            else:
                self.cur.setText(f"Update incomplete: {names}. Retry the failed games or check the log.")
            self.retry_btn.setVisible(bool(self._retryable_games()))
        else:
            self.cur.setText("Update failed. Check the log above, then try again from My Games.")

    def _retryable_games(self):
        return [item for item in self._failed_games
                if not (self._zd_missing and item[0] == "t6zm")]

    def _retry_failed(self):
        self.selected = self._retryable_games()
        retry_zd = self._failed_zd and not self._zd_missing
        self._start_update()
        self.update_zd = retry_zd

    def _go_back(self):
        go_to(self.stack, "ManagementScreen")

    def _run(self):
        # Several steps (get_proton_path, find_all_games, get_setup_games,
        # is_plutonium_ready, ...) aren't in a try of their own. An exception
        # used to end the thread without done: no Back button, the user was
        # stuck on this screen and sleep stayed blocked until DeckOps closed.
        try:
            self._run_steps()
        except Exception as ex:
            import traceback
            _log.error("update failed:\n%s", traceback.format_exc())
            if self.selected and not self._failed_games:
                self._failed_games = list(self.selected)
            self._restore_failed_launch_options()
            self._s.log.emit(f"✗  Update failed: {ex}")
            self._s.progress.emit(100, "Update failed.")
            self._s.done.emit(False)

    def _restore_failed_launch_options(self):
        """Put back the launch options cleared for games that then failed.
        Appids a successful game also uses (CoD4 MP/SP) were just rewritten
        and are left alone; restore_launch_options also skips any entry that
        is no longer empty. Runs once per snapshot."""
        snap, self._lo_snapshot = getattr(self, "_lo_snapshot", None), None
        if not snap or not self._failed_games:
            return
        try:
            from wrapper import restore_launch_options
            from detect_games import GAMES
            failed = {k for k, _, _ in self._failed_games}
            ok_appids = {str(GAMES[k]["appid"]) for k, _, _ in self.selected
                         if k in GAMES and k not in failed}
            for k in failed:
                appid = str(GAMES.get(k, {}).get("appid", ""))
                if appid and appid in snap and appid not in ok_appids:
                    if restore_launch_options(self.steam_root, snap, appid):
                        self._s.log.emit(f"  Kept the previous launch options for {GAMES[k].get('name', k)}")
        except Exception as ex:
            self._s.log.emit(f"  Could not restore launch options: {ex}")

    def _run_steps(self):
        self._lo_snapshot = None
        task, self.task = self.task, None
        if task:
            try:
                task[1](self._s)
            except Exception as ex:
                self._s.log.emit(f"✗  {task[0].title()} failed: {ex}")
                self._s.progress.emit(100, "Update failed.")
                self._s.done.emit(False)
                return
            self._s.progress.emit(100, "All done!")
            self._s.done.emit(True)
            return
        if not self._backup_first():
            return
        from wrapper import get_proton_path, find_compatdata, kill_steam, set_compat_tool
        from ge_proton import install_ge_proton, resolve_ge_version, MANAGED_APPIDS
        from iw4x import install_iw4x
        from cod4x import install_cod4x
        from cod4r import install_cod4r
        from iw3sp import install_iw3sp
        from t6sp_mod import install_t6sp_mod
        from cleanops import install_cleanops
        from t7x import install_t7x
        from net import DownloadError

        is_lcd = cfg.is_lcd()

        if is_lcd:
            from plutonium_lcd import (install_plutonium_lcd,
                                       launch_bootstrapper_lcd,
                                       is_plutonium_ready_lcd)
        else:
            from plutonium import (install_plutonium,
                                        launch_bootstrapper,
                                        is_plutonium_ready)

        has_plut  = any(KEY_CLIENT.get(k) == "plutonium" for k, _, _ in self.selected)
        proton    = get_proton_path(self.steam_root)

        # Auto-expand: if any Plutonium key is selected, pull in all other
        # installed Plutonium games so the shared cache rebuild covers every
        # prefix (OLED) or every sideload entry (LCD).
        if has_plut:
            _PLUT_KEYS = {"t4sp","t4mp","t5sp","t5mp","t6mp","t6zm","iw5mp","iw5mp_ds"}
            already = {k for k, _, _ in self.selected}
            _all_installed = find_all_games(self.steam_root)
            # Find the right gd entry for each sibling key
            _gd_by_key = {}
            for gd_entry in ALL_GAMES:
                for k in _active_keys(gd_entry):
                    _gd_by_key[k] = gd_entry
            extras = []
            for pk in _PLUT_KEYS - already:
                if pk in _all_installed and cfg.is_game_setup(pk):
                    gd_entry = _gd_by_key.get(pk)
                    if gd_entry:
                        extras.append((pk, gd_entry, _all_installed[pk]))
            if extras:
                names = ", ".join(e[2].get("name", e[0]) for e in extras)
                self._s.log.emit(f"Also updating {len(extras)} sibling Plutonium game(s): {names}")
                self.selected.extend(extras)

        total = len(self.selected)

        # Read setup_games to determine source for each key
        setup_games = cfg.get_setup_games()

        # ── Plutonium update: re-run bootstrapper ─────────────────────
        if has_plut:
            if is_lcd:
                self._s.progress.emit(5, "Setting up Plutonium through HGL...")
                self._s.log.emit(
                    "Setting up Plutonium through HGL...\n"
                    "  1. HGL will download and launch Plutonium (this may take a few minutes)\n"
                    "  2. Log in if prompted\n"
                    "  3. Close the Plutonium window\n"
                    "  4. Click the button below to continue"
                )
            else:
                self._s.progress.emit(5, "Launching Plutonium to check for updates...")
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
                        on_progress=lambda p, m: self._s.progress.emit(p, m),
                    )
                else:
                    launch_bootstrapper(
                        proton,
                        on_progress=lambda p, m: self._s.progress.emit(p, m),
                        steam_root=self.steam_root,
                    )
            except DownloadError as dl_ex:
                self._s.log.emit(f"✗  {dl_ex.label} download failed: {dl_ex}")
                self._failed_games = list(self.selected)
                self._s.progress.emit(100, "Update failed.")
                self._s.done.emit(False)
                return
            except Exception as ex:
                self._s.log.emit(f"✗  Plutonium launch failed: {ex}")
                self._failed_games = list(self.selected)
                self._s.progress.emit(100, "Update failed.")
                self._s.done.emit(False)
                return

            self._s.plut_wait.emit()
            self._steam_closed.wait()
            self._steam_closed.clear()
            self._s.plut_go.emit()

            _ready = is_plutonium_ready_lcd() if is_lcd else is_plutonium_ready()
            if not _ready:
                self._s.log.emit(
                    "✗  Plutonium does not appear to be fully set up.\n"
                    "   Make sure you let it finish updating."
                )
                self._failed_games = list(self.selected)
                self._s.progress.emit(100, "Update incomplete.")
                self._s.done.emit(False)
                return

            self._s.log.emit("✓  Plutonium updated.")

            # Remove the old shared copy prefixes used to link to. OLED games
            # are reinstalled below and link to the master in the dedicated
            # prefix instead; LCD reads everything from the Heroic prefix.
            from plutonium import SHARED_PLUT_DIR
            if os.path.isdir(SHARED_PLUT_DIR):
                shutil.rmtree(SHARED_PLUT_DIR, ignore_errors=True)
                self._s.log.emit("  Removed the old shared Plutonium copy")

        # ── GE-Proton download (Steam may still be running) ─────────
        ge_version = None
        try:
            self._s.log.emit("Checking GE-Proton...")
            ge_version = install_ge_proton(
                on_progress=lambda pct, msg: self._s.progress.emit(2 + int(pct * 0.03), msg)
            )
            cfg.set_ge_proton_version(ge_version)
            self._s.log.emit(f"✓  {ge_version} ready")
        except Exception as ex:
            self._s.log.emit(f"  GE-Proton update skipped: {ex}")
        # A failed download must not leave the games without a compat tool:
        # keep using the GE-Proton that is already installed.
        if not ge_version:
            ge_version = resolve_ge_version()
            if ge_version:
                self._s.log.emit(f"  Using the installed {ge_version}")
            else:
                self._s.log.emit("✗  No GE-Proton is installed; Steam's compatibility settings were left unchanged.")

        # Always close Steam before the config writes below (launch options,
        # compat tools, Steam Input, shortcuts): Steam rewrites config.vdf and
        # localconfig.vdf when it exits, so anything written while it runs is
        # lost. This used to be skipped whenever a Plutonium game was in the
        # update -- the button only waits for the Plutonium window, not Steam.
        # Same order as InstallScreen: Plutonium step first, then close Steam.
        self._s.progress.emit(5, "Closing Steam...")
        self._s.log.emit("Closing Steam...")
        try:
            kill_steam(on_progress=lambda msg: self._s.log.emit(f"  {msg}"))
            self._s.log.emit("  ✓ Steam closed.")
        except Exception as ex:
            self._s.log.emit(f"✗  Could not close Steam: {ex}")
            raise RuntimeError("Close Steam manually, then retry this update; game configuration was not continued.") from ex

        # ── Clean slate: clear launch options and compat tools ─────────
        # Same clean-slate approach as InstallScreen.
        # Prevents stale entries from previous installs (LCD→OLED, older
        # DeckOps versions) interfering with the update.
        # Launch options only for the Steam games in this update, so a
        # per-game update doesn't wipe other games' options.
        try:
            from wrapper import clear_launch_options, clear_compat_tool, snapshot_launch_options
            from detect_games import GAMES
            run_appids = {GAMES[k]["appid"] for k, _, g in self.selected
                          if k in GAMES and (g or {}).get("source") != "own"}
            # A game that fails below never gets its options written again;
            # keep what it had so it can be put back (_restore_failed_launch_options)
            try:
                self._lo_snapshot = snapshot_launch_options(
                    self.steam_root, [a for a in MANAGED_APPIDS if a in run_appids])
            except Exception as ex:
                self._s.log.emit(f"  Could not save current launch options: {ex}")
            for appid in MANAGED_APPIDS:
                if appid in run_appids:
                    clear_launch_options(self.steam_root, appid)
            # Only clear mappings that are put back below.
            if ge_version:
                clear_compat_tool(MANAGED_APPIDS)
            self._s.log.emit("✓  Cleared launch options" + (" and compat tools" if ge_version else ""))
            if "7940" in run_appids:
                from iw3sp import apply_cod4_launch_option
                apply_cod4_launch_option(self.steam_root)
        except Exception as ex:
            self._s.log.emit(f"  Launch option / compat tool cleanup skipped: {ex}")

        # Build installed_games dict for Plutonium sibling key resolution
        installed_for_plut = {k: g for k, _, g in self.selected if g}

        for idx, (key, gd, game) in enumerate(self.selected):
            if not game:
                continue
            base_name = gd["base"]
            bp = int(idx / total * 90)
            self._s.progress.emit(bp, f"Updating {base_name}...")
            def op(pct, msg, _b=bp): self._s.progress.emit(_b + int(pct / 100 * (90 // total)), msg)
            c = KEY_CLIENT.get(key, gd.get("client", ""))

            # Determine source from setup_games config entry
            entry = setup_games.get(key, {})
            source = entry.get("source", "steam")
            # Prefer the client from the original install if available
            if key in ("cod4mp", "iw4sp", "iw5sp") and entry.get("client"):
                c = entry["client"]
            try:
                from plutonium import GAME_META as _PLUT_META
                from detect_games import GAMES as _GAMES_MAP
                if c == "plutonium" and key in _PLUT_META:
                    _appid = _PLUT_META[key][0]
                elif c == "alterware" and key in _GAMES_MAP:
                    _appid = _GAMES_MAP[key]["appid"]
                else:
                    _appid = gd["appid"]

                # Resolve compatdata - own games may have CRC-based prefix
                if source == "own":
                    compat = game.get("compatdata_path", "")
                    # Scanned games are not enriched here; use the same
                    # prefix the install flow gave this own-copy game
                    if not compat and c == "plutonium" and not cfg.is_lcd():
                        from shortcut import own_plut_prefix
                        compat = own_plut_prefix(key, game)
                    if not compat:
                        compat = find_compatdata(self.steam_root, _appid,
                                                  game_install_dir=game.get("install_dir"))
                else:
                    compat = find_compatdata(self.steam_root, _appid,
                                              game_install_dir=game.get("install_dir"))
                if not compat and game.get("install_dir"):
                    steamapps = os.path.dirname(os.path.dirname(game["install_dir"]))
                    compat = os.path.join(steamapps, "compatdata", str(_appid))

                # CoD4 installers put registry keys, launcher.dll and the client
                # DLL into the prefix of the appid they're given. An own copy runs
                # in its shortcut's prefix, not Steam CoD4's (7940) -- the same
                # appid InstallScreen passes (enrich_own_games' shortcut_appid).
                cod4_appid = gd["appid"]
                if source == "own" and c in ("cod4r", "cod4x", "iw3sp"):
                    from shortcut import own_shortcut_appid
                    from steam_common import nvme_compatdata
                    cod4_appid = game.get("shortcut_appid") or own_shortcut_appid(key, game)
                    compat = game.get("compatdata_path") or nvme_compatdata(cod4_appid)

                if c == "cod4r":
                    install_cod4r(game, self.steam_root, proton, compat, op,
                                  appid=cod4_appid, source=source)
                elif c == "cod4x":
                    install_cod4x(game, self.steam_root, proton, compat, op,
                                  appid=cod4_appid)
                elif c == "iw3sp":
                    install_iw3sp(game, self.steam_root, proton, compat, op,
                                  source=source)
                elif c == "iw4x":
                    install_iw4x(game, self.steam_root, proton, compat, op,
                                 source=source, install_dlc=False)
                elif c == "plutonium":
                    if is_lcd:
                        install_plutonium_lcd(game, key,
                                             installed_games=installed_for_plut,
                                             on_progress=op,
                                             steam_root=self.steam_root,
                                             proton_path=proton,
                                             compatdata_path=compat,
                                             source=source)
                    else:
                        install_plutonium(game, key, self.steam_root, proton, compat,
                                         on_progress=op,
                                         installed_games=installed_for_plut,
                                         source=source)
                elif c == "cleanops":
                    install_cleanops(game, self.steam_root, proton, compat, op,
                                    source=source)
                elif c == "t7x":
                    t7x_dir = install_t7x(game, on_progress=op)
                    game["install_dir"] = t7x_dir
                elif c == "alterware":
                    from alterware import install_alterware
                    install_alterware(game, key, self.steam_root, proton, compat, op,
                                     source=source)
                elif c == "t6sp_mod":
                    install_t6sp_mod(game, self.steam_root, proton, compat, op,
                                    source=source)
                    if source == "own":
                        from shortcut import migrate_own_t6sp
                        migrate_own_t6sp(game, self._s.log.emit)
                elif c == "iw-pad":
                    from sp_mod import install_sp_mod
                    install_sp_mod(key, game["install_dir"], op,
                                   source=source, steam_root=self.steam_root)
                self._s.log.emit(f"✓  {base_name} ({key}) done")
                # Installs that mark themselves (Plutonium) just saved fresh paths
                entry = cfg.get_setup_games().get(key, entry)
                cfg.mark_game_setup(key, c, source=source,
                                    wrapper_path=entry.get("wrapper_path"),
                                    lan_wrapper_path=entry.get("lan_wrapper_path"))
            except Exception as ex:
                self._failed_games.append((key, gd, game))
                self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")

        self._restore_failed_launch_options()

        # ── Re-apply GE-Proton compat + Steam Input ──────────────────
        if ge_version:
            try:
                set_compat_tool(MANAGED_APPIDS, ge_version)
                self._s.log.emit(f"✓  {ge_version} set for Steam game appids")
            except Exception as ex:
                self._s.log.emit(f"  CompatToolMapping skipped: {ex}")

        try:
            from wrapper import set_steam_input_enabled
            set_steam_input_enabled(self.steam_root)
            self._s.log.emit("✓  Steam Input enabled for all games")
        except Exception as ex:
            self._s.log.emit(f"  Steam Input setup skipped: {ex}")

        # Steam WaW has Plutonium entries in its launch menu; a Deck default
        # from older installs would auto-pick Campaign and hide it.
        _setup = cfg.get_setup_games()
        if any(k in ("t4sp", "t4mp") and _setup.get(k, {}).get("source", "steam") == "steam"
                              for k, _, _ in self.selected):
            try:
                from wrapper import clear_default_launch_option
                clear_default_launch_option(self.steam_root, ["10090"])
            except Exception as ex:
                self._s.log.emit(f"  WaW launch picker reset skipped: {ex}")

        # Zombies Declassified: only when the user opted in on the Update Games prompt
        _do_zd = self.update_zd; self.update_zd = False
        zd_game = next((item for item in self.selected if item[0] == "t6zm"), None)
        if _do_zd and cfg.is_zd_installed() and zd_game:
            try:
                self._s.log.emit("Checking Zombies Declassified for updates...")
                from zombies_declassified import resolve_zd_storage
                _zd_storage = resolve_zd_storage({k: g for k, _, g in self.selected}.get("t6zm"))

                from zombies_declassified import is_zd_installed as _zd_at
                if not _zd_storage or not _zd_at(_zd_storage):
                    self._s.log.emit(f"✗  Zombies Declassified not found in {_zd_storage}; reinstall it from BO2 Options.")
                    self._zd_missing = True
                    if zd_game not in self._failed_games:
                        self._failed_games.append(zd_game)
                else:
                    from zombies_declassified import update_zd
                    updated = update_zd(_zd_storage,
                                        lambda p, m: self._s.log.emit(f"  ZD: {m}"))
                    if updated:
                        from zombies_declassified import get_zd_info as _zdi
                        info = _zdi(_zd_storage)
                        if info.get("manifest_hash"):
                            cfg.mark_zd_installed(info["manifest_hash"])
                        self._s.log.emit("✓  Zombies Declassified updated")
                    else:
                        self._s.log.emit("✓  Zombies Declassified is up to date")
            except Exception as ex:
                self._s.log.emit(f"✗  Zombies Declassified update failed: {ex}")
                self._failed_zd = True
                if zd_game not in self._failed_games:
                    self._failed_games.append(zd_game)

        # Clean up ZD files leaked into non-t6zm prefixes by older versions
        if has_plut and not is_lcd:
            try:
                from plutonium import cleanup_zd_from_other_prefixes
                n = cleanup_zd_from_other_prefixes(
                    {k: g for k, _, g in self.selected if g},
                    self.steam_root,
                    on_progress=lambda msg: self._s.log.emit(msg),
                )
                if n:
                    self._s.log.emit(f"✓  Cleaned ZD files from {n} prefix(es)")
            except Exception as ex:
                self._s.log.emit(f"  ZD cleanup skipped: {ex}")

        # Clean up Plutonium dirs leaked into non-Plutonium prefixes by older versions
        if has_plut and not is_lcd:
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

        failed = len(self._failed_games)
        self._s.progress.emit(100, "Update complete." if not failed else f"{failed} game(s) failed to update.")
        self._s.done.emit(not failed)
