"""
ui_manage.py — Post-install management screens for DeckOps

Screens: ManagementCard, ManagementScreen, SetupCompleteScreen,
         ConfigureScreen, UpdateScreen
Extracted from ui_qt.py — all hardcoded stack indices replaced with named lookups.
"""

import os, subprocess, sys, shutil, threading, json, urllib.request, urllib.error, urllib.parse, compileall

from qt import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea,
    QLabel, QPushButton, QProgressBar, QPlainTextEdit,
    QFrame, QSizePolicy, QMessageBox, QLineEdit, QSlider, QDialog,
)
from qt import Qt, QTimer
from qt import QPixmap
from qt import QGraphicsOpacityEffect

from detect_games import find_steam_root, find_all_games
import config as cfg
import inhibit

from ui_constants import (
    C_BG, C_CARD, C_SURFACE, C_LINE, C_TEXT, C_DIM, C_IW, C_TREY,
    C_DARK_BTN, C_RED_BTN, C_FOCUS,
    font, _btn, _button_style, _lbl, _hdiv, _log_to_file, _log_html, _copy_log_to_clipboard,
    _Sigs, _detached_open, _header_bar, _header_path, _ask_iw4x_dlc,
    _show_preflight,
    ALL_GAMES, KEY_CLIENT, KEY_MODE_LABEL,
    _active_keys, _active_client, _active_appid,
    SP_IMAGE_URLS, IMG_RATIO,
    HEROES_DIR, PROJECT_ROOT,
    _clean_volume, _clean_enabled, _reload_audio_settings,
    _set_audio_volume, _set_audio_enabled, _start_audio, _kill_audio,
    go_to, get_screen,
    FONT_BODY, FONT_ACTION, BTN_MIN_H, LABELS, screen_title,
    _no_focus_scroll, _chain_focus, _focus_first, _ensure_focus_in, _plain_marks,
)

from guide_text import how_to_start_text
from identity import GITHUB_USER, GITHUB_REPO
from log import get_logger

_log = get_logger(__name__)


def _saved_music_volume():
    return _clean_volume(cfg.get_music_volume())


def _saved_music_enabled():
    return _clean_enabled(cfg.get_music_enabled())


def _update_candidates(installed, keys=None):
    """Only update modes configured for the currently detected game source."""
    configured = cfg.get_setup_games()
    allowed = set(keys) if keys is not None else None
    selected = []
    for gd in ALL_GAMES:
        for key in _active_keys(gd):
            if allowed is not None and key not in allowed:
                continue
            game = installed.get(key)
            entry = configured.get(key, {})
            if not isinstance(entry, dict):
                continue  # a hand-edited or corrupt entry: not a configured mode
            if not game or not entry or entry.get("source", "steam") != game.get("source", "steam"):
                continue
            client = entry.get("client") or KEY_CLIENT.get(key, "")
            if client and client not in ("steam", "cod4x"):
                selected.append((key, gd, game))
    return selected


def _partial_write_msg(count, failures):
    """Status line for a config pass where some files could not be written."""
    return f"⚠  Updated {count} file(s), {len(failures)} could not be written (see log)"


def _update_selection(installed, keys=None):
    """Resolve shared Plutonium dependants before showing the confirmation."""
    selected = _update_candidates(installed, keys)
    original = {key for key, _, _ in selected}
    configured = cfg.get_setup_games()
    if any((configured.get(key, {}).get("client") or KEY_CLIENT.get(key)) == "plutonium"
           for key in original):
        for item in _update_candidates(installed):
            if (item[0] not in original and
                    (configured.get(item[0], {}).get("client") or KEY_CLIENT.get(item[0])) == "plutonium"):
                selected.append(item)
    return selected, {key for key, _, _ in selected} - original


def _confirm_game_update(parent, selected, extras=()):
    """Make the effective scope and Steam interruption explicit."""
    groups = {}
    for key, gd, game in selected:
        groups.setdefault(gd['base'], []).append(
            f"{KEY_MODE_LABEL.get(key, key)} ({game.get('source', 'steam')})")
    lines = [f"• {title} — {', '.join(modes)}" for title, modes in groups.items()]
    msg = QMessageBox(parent)
    msg.setWindowTitle("Update Games")
    msg.setText(f"Update {len(selected)} configured mode(s)?\n\n" + "\n".join(lines) +
                ("\n\nAdditional Plutonium modes are included to keep shared files in sync."
                 if extras else "") +
                "\n\nYour saves will be backed up first. Steam will close during the update.")
    msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    msg.setDefaultButton(QMessageBox.StandardButton.No)
    return msg.exec() == QMessageBox.StandardButton.Yes


# Compact library cards: cover art beside the status, one row of actions
# below. Every card fits on one 1280x800 screen (two rows of four); the width
# flexes between these bounds so a small window still gets three columns.
_LIBRARY_CARD_MIN_W = 236
_LIBRARY_CARD_MAX_W = 300
_LIBRARY_CARD_W = _LIBRARY_CARD_MAX_W      # nominal width before the first layout
_LIBRARY_MAX_COLS = 4
# Steam library covers are portrait (2:3); contained, never cropped.
_LIBRARY_COVER_H = 144
_LIBRARY_COVER_W = round(_LIBRARY_COVER_H / IMG_RATIO)


class _StatusLabel(QLabel):
    """Status line: worker messages keep their ✓ ✗ ⚠ marks (fine in the log
    file), but the bundled fonts lack those glyphs, so the label shows the
    plain-word version (_plain_marks)."""

    def __init__(self, size=FONT_BODY, color=C_DIM, align=Qt.AlignmentFlag.AlignLeft, auto_hide=False):
        super().__init__("")
        self._auto_hide = auto_hide
        self.setFont(font(size))
        self.setAlignment(align)
        self.setWordWrap(True)
        self.setStyleSheet(f"color:{color};background:transparent;")
        if auto_hide:
            self.setVisible(False)

    def setText(self, text):
        super().setText(_plain_marks(text or ""))
        if self._auto_hide:
            # An empty status line takes no room (Settings sections stay compact)
            self.setVisible(bool(text))


def _card_modes(keys, installed):
    """The modes a library card expects: the free MW3 server is an
    alternative to MP, not an extra mode required beside it."""
    modes = [k for k in keys if k != "iw5mp_ds"] if "iw5mp" in installed else list(keys)
    if "iw5mp_ds" in installed and "iw5mp" not in installed:
        modes = [k for k in modes if k != "iw5mp"]
    return modes


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
                 on_readd=None, on_add_ds=None, on_offline=None, on_add_games=None, parent=None):
        super().__init__(parent)
        color = C_IW if gd["dev"] == "iw" else C_TREY
        self._color  = color
        self._appid  = _active_appid(gd)
        self._is_lan = gd.get("client") == "lan"
        self.setObjectName("MC")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(_LIBRARY_CARD_W)

        keys       = _active_keys(gd)
        client     = _active_client(gd)

        if self._is_lan:
            ik = []
            ready = bool(cfg.get_offline_mode())
            missing = []
            state = "Ready" if ready else "Not set up"
            detail = "Offline launcher and bot matches" if ready else "Add the offline launcher"
        else:
            modes = _card_modes(keys, installed)
            ik = [k for k in keys if k in installed]
            ready_keys = [k for k in modes if k in installed and
                          cfg.is_game_setup_for_source(k, installed[k].get("source", "steam"))]
            missing = [k for k in modes if k in installed and k not in ready_keys]
            ready = bool(ready_keys)
            if not ik:
                state, detail = "Not found", "Game files not detected"
            elif len(ready_keys) == len(modes):
                state, detail = "Ready", "All detected modes set up"
            else:
                state = "Partial" if ready else "Not set up"
                detail = f"{len(ready_keys)} of {len(modes)} modes ready"
                if missing:
                    detail += " · " + ", ".join(KEY_MODE_LABEL.get(k, k) for k in missing) + " to set up"

        border_color = color if state == "Ready" else (C_TREY if ik or self._is_lan else C_LINE)
        self.setStyleSheet(
            f"QFrame#MC{{background:{C_CARD};border-top:3px solid {border_color};border-radius:8px;}}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)

        # Keep the client label outside the artwork: even its corner should
        # remain visible, rather than being covered by a badge overlay.
        badge_strip = QWidget()
        badge_strip.setFixedHeight(26)
        badge_strip.setStyleSheet("background:transparent;border:none;")
        bsl = QHBoxLayout(badge_strip); bsl.setContentsMargins(0, 0, 0, 0); bsl.setSpacing(0)
        # CoD 4 and BO2 set an explicit "badge" so the tag also shows that the
        # campaign runs on a modded client; other cards show the primary client.
        badge_text = (gd.get("badge") or client.split(" + ")[0]).lower()
        self._client_badge = QPushButton(badge_text)
        self._client_badge.setToolTip(client)
        self._client_badge.setFont(font(10, True)); self._client_badge.setEnabled(False)
        self._client_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._client_badge.setStyleSheet(
            f"QPushButton{{background:{color};color:{C_BG};border:none;border-radius:3px;padding:2px 6px;}}"
            f"QPushButton:disabled{{background:{color};color:{C_BG};}}"
        )
        self._client_badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        bsl.addWidget(self._client_badge); bsl.addStretch()
        lay.addWidget(badge_strip)

        # Cover and status side by side. The cover's size is reserved before
        # the first layout pass (a zero-height cover once hid the status).
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.setSpacing(10)
        img_container = QWidget()
        img_container.setFixedSize(_LIBRARY_COVER_W, _LIBRARY_COVER_H)
        img_container.setStyleSheet(f"background:{C_SURFACE};border:none;border-radius:4px;")
        self._img = QLabel(img_container)
        self._img.setFixedSize(_LIBRARY_COVER_W, _LIBRARY_COVER_H)
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setFont(font(10, True))
        self._img.setWordWrap(True)
        self._img.setText("NO ARTWORK\nAVAILABLE")
        self._img.setStyleSheet(f"color:{C_DIM};background:transparent;border:none;")
        self._dim_art = state != "Ready"
        self._img_container = img_container
        top.addWidget(img_container, 0, Qt.AlignmentFlag.AlignTop)

        info = QWidget()
        info.setStyleSheet("background:transparent;border:none;")
        il = QVBoxLayout(info); il.setContentsMargins(0, 0, 0, 0); il.setSpacing(4)
        name = gd["base"].replace("Call of Duty 4: Modern Warfare", "Modern Warfare 1")
        name = name.replace("Call of Duty: ", "")
        if self._is_lan: name = "Offline Mode"
        title = _lbl(name, FONT_ACTION, C_TEXT, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._title_label = title
        il.addWidget(title)
        self._state_label = _lbl(state.upper(), FONT_BODY,
                                  color if state == "Ready" else C_TREY if ik or self._is_lan else C_DIM,
                                  bold=True, align=Qt.AlignmentFlag.AlignLeft, wrap=False)
        self._state_label.setAccessibleName(f"{name}: {state}")
        il.addWidget(self._state_label)
        summary = _lbl(detail, 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        self._summary_label = summary
        il.addWidget(summary)
        il.addStretch()
        top.addWidget(info, 1)
        lay.addLayout(top)

        # One row of actions: a single primary action, plus at most one short
        # secondary one. Everything else lives in Game Options.
        self._btn_bar = QWidget()
        self._btn_bar.setStyleSheet("background:transparent;border:none;")
        bb = QHBoxLayout(self._btn_bar)
        bb.setContentsMargins(0, 0, 0, 0); bb.setSpacing(6)
        self._buttons = []

        def _card_btn(text, bg, stretch=1, tip=None):
            b = _btn(text, bg, size=FONT_ACTION, h=BTN_MIN_H)
            b.setStyleSheet(b.styleSheet().replace("padding:0 18px;", "padding:0 4px;"))
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(0)
            if tip:
                b.setToolTip(tip)
            bb.addWidget(b, stretch)
            self._buttons.append(b)
            return b

        if self._is_lan and not ready:
            b = _card_btn("Add Offline", C_IW, tip="Add Offline Mode")
            if on_offline: b.clicked.connect(lambda: on_offline(True))
        elif self._is_lan:
            b = _card_btn("Repair", C_DARK_BTN, tip="Repair the offline launcher")
            if on_readd: b.clicked.connect(lambda: on_readd(gd))
            rm = _card_btn("Remove", C_RED_BTN, tip="Remove Offline Mode")
            rm.setAccessibleName("Remove Offline Mode")
            if on_offline: rm.clicked.connect(lambda: on_offline(False))
        elif missing:
            n = len(missing)
            b = _card_btn(f"Set Up {n} Mode{'s' if n != 1 else ''}" if ready else "Set Up", C_IW, 3,
                          tip="Set up only the detected modes that still need setup")
            b.clicked.connect(lambda: on_setup(gd))
            if ik and ready:
                opt = _card_btn("Options", C_DARK_BTN, 2, tip="Game Options")
                opt.setAccessibleName("Game Options")
                opt.clicked.connect(lambda: on_configure(gd, ik))
        elif not ik and "iw5mp_ds" in keys and on_add_ds:
            b = _card_btn("Get Free MW3", C_IW, tip="Get the free MW3 Dedicated Server from Steam")
            b.clicked.connect(lambda: on_add_ds(gd))
        elif not ik:
            b = _card_btn("Add Games", C_DARK_BTN)
            if on_add_games: b.clicked.connect(on_add_games)
        else:
            # Ready, or every detected mode ready with some game files missing:
            # Game Options also offers Find Missing Modes then.
            b = _card_btn("Game Options", C_DARK_BTN)
            b.clicked.connect(lambda: on_configure(gd, ik))
        lay.addWidget(self._btn_bar)
        self._primary = self._buttons[0] if self._buttons else None
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
                try:
                    threading.Thread(target=self._fetch, args=(self._appid,), daemon=True).start()
                except Exception:
                    # Cover art is optional; keep the placeholder and the game actions.
                    _log.debug("cover download could not start", exc_info=True)
        self._scale_image()
        self.fit_width(_LIBRARY_CARD_W)

    def fit_width(self, width):
        """Fix the card at width and at the height its wrapped text needs
        there. The scroll area otherwise compresses grid rows to its
        viewport's height on first show, despite sizeHint()."""
        self.setFixedWidth(width)
        self.setFixedHeight(self.natural_height(width))

    def natural_height(self, width):
        h = self.heightForWidth(width)
        return h if h > 0 else self.sizeHint().height()

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
        # Contain, never crop: both Steam's portrait covers and the local
        # Offline Mode artwork remain fully visible at their native ratio.
        # A missing or broken cached file retains the text placeholder.
        if self._raw_pixmap and not self._raw_pixmap.isNull():
            w, h = self._img.width(), self._img.height()
            scaled = self._raw_pixmap.scaled(
                w, h, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._img.setText("")
            self._img.setPixmap(scaled)
            if self._dim_art and self._img.graphicsEffect() is None:
                effect = QGraphicsOpacityEffect(self._img)
                effect.setOpacity(0.65)
                self._img.setGraphicsEffect(effect)

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
        hl.addWidget(_lbl(screen_title("ManagementScreen").upper(), 12, C_IW, bold=True, wrap=False))
        lay.addWidget(hdr)

        # Separate toolbar: the logo never competes with actions at 800x500.
        toolbar = QWidget(); toolbar.setStyleSheet(f"background:{C_CARD};")
        actions = QHBoxLayout(toolbar)
        actions.setContentsMargins(12, 4, 12, 8); actions.setSpacing(6)
        add_btn = _btn("Add Games", C_IW, size=FONT_ACTION, h=BTN_MIN_H)
        add_btn.clicked.connect(self._add_games)
        self._game_update_btn = _btn("Update Games", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        self._game_update_btn.clicked.connect(self._update_all_games)
        cfg_btn = _btn(LABELS["settings"], C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        cfg_btn.clicked.connect(lambda: go_to(self.stack, "ConfigureScreen"))
        # DeckOps updates are checked and installed from Settings only. This
        # button is just the notice: shown when the startup check finds an
        # update, it opens Settings and runs that check there.
        self._deckops_update_btn = _btn("DeckOps Update", C_TREY, size=FONT_ACTION, h=BTN_MIN_H)
        self._deckops_update_btn.setToolTip("A DeckOps update is available: open Settings to install it")
        self._deckops_update_btn.clicked.connect(self._open_deckops_update)
        self._deckops_update_btn.setVisible(False)
        self._toolbar_buttons = [add_btn, self._game_update_btn, self._deckops_update_btn, cfg_btn]
        for button in self._toolbar_buttons:
            button.setStyleSheet(button.styleSheet().replace("padding:0 18px;", "padding:0 4px;"))
            button.setMinimumWidth(0)
            actions.addWidget(button, 1)
        self._toolbar = toolbar
        lay.addWidget(toolbar)

        self._scroll = _no_focus_scroll(QScrollArea()); self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget(); self._grid_host = inner
        self._grid = QGridLayout(inner)
        self._grid.setContentsMargins(16,12,16,12); self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(inner); lay.addWidget(self._scroll, stretch=1)
        self._cards = []
        self._columns = 0
        self._card_w = 0

        self._deckops_update_status = _StatusLabel(FONT_BODY, C_IW, Qt.AlignmentFlag.AlignCenter, auto_hide=True)
        self._deckops_update_status.setContentsMargins(16,0,16,4)
        self._deckops_update_status.setVisible(False)
        lay.addWidget(self._deckops_update_status)

        footer = QWidget()
        fl = QHBoxLayout(footer); fl.setContentsMargins(16,4,16,6); fl.setSpacing(12)
        self._status = _StatusLabel(FONT_BODY, C_DIM)
        fl.addWidget(self._status, 1, Qt.AlignmentFlag.AlignVCenter)
        self._scan_again_btn = _btn("Scan Again", C_IW, size=FONT_ACTION, h=BTN_MIN_H)
        self._scan_again_btn.clicked.connect(self._scan_again)
        self._scan_again_btn.setVisible(False)
        fl.addWidget(self._scan_again_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(footer)

    # ── controller protocol (see ui_constants) ─────────────────────────────
    def focus_target(self):
        """First card's primary action; the toolbar when there are no cards."""
        for card in self._cards:
            if card._primary is not None:
                return card._primary
        return self._toolbar_buttons[0]

    def _focus_order(self):
        cards = [b for card in self._cards for b in card._buttons]
        return self._toolbar_buttons + cards + [self._scan_again_btn]

    def _add_games(self):
        """Open game selection directly, including its Choose Folder action."""
        # Like every other Library action: a Settings task may still be running (Back
        # works mid-task), and an install started now would write the same Steam files
        # and release the sleep lock that task still needs when it ends
        if self._busy_elsewhere():
            return
        s = get_screen(self.stack, "SetupScreen")
        if s is None:
            return
        s.steam_installed = {k: v for k, v in self.installed.items() if v.get("source") != "own"}
        s.own_installed = {k: v for k, v in self.installed.items() if v.get("source") == "own"}
        s.steam_root = find_steam_root() or ""
        s._from_manage = True
        install = get_screen(self.stack, "InstallScreen")
        if install is not None:
            install._return_to_management = True
        go_to(self.stack, "SetupScreen")

    def set_installed(self, installed):
        self.installed = installed
        self._rebuild()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._cards:
            self._layout_cards()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh()
        import preflight
        preflight.start_online_probe()      # cached before Set Up, so the check can't stall the UI
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
                "A DeckOps update is available. Open Settings to review and install it.")
            self._deckops_update_status.setVisible(True)
            self._deckops_update_btn.setText("Update Available")
            self._deckops_update_btn.setAccessibleName("Update Available")
            self._deckops_update_btn.setVisible(True)

    def _scan_again(self):
        self._scan_again_btn.setVisible(False)
        self._status.setText("")
        self._refresh()

    def _refresh(self):
        if getattr(self, "_scanning", False): return
        self._scanning = True
        self._scan_again_btn.setVisible(False)
        self._game_update_btn.setEnabled(False)
        if not self.installed: self._status.setText("Loading games...")
        self._scan_sigs = _Sigs()
        self._scan_sigs.done.connect(self._on_scanned)
        try:
            threading.Thread(target=self._do_scan, daemon=True).start()
        except Exception:
            _log.warning("game scan could not start", exc_info=True)
            self._scanned = None
            self._scan_sigs.done.emit(False)

    def _do_scan(self):
        try:
            self._scanned = find_all_games(preferred_sources=cfg.get_setup_games())
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
            # Bring existing installs up to the shipped deadzone defaults too.
            # Idempotent and limited to configured games and existing text configs.
            # Not while an install, update or Settings task runs: those rewrite the
            # same game configs (Settings tasks hold the busy lock while they run).
            if (cfg.get_setup_games() and not getattr(self, "_deadzone_running", False)
                    and not inhibit.busy() and not self._settings_task_running()):
                self._deadzone_running = True
                # This writes game configs. Hold the same busy flag as other
                # writers so a task started just after the scan cannot race it.
                inhibit.start("DeckOps is applying controller deadzones")
                installed = dict(self.installed)
                def _defaults():
                    try:
                        from game_config import set_deadzone
                        sr = cfg.load().get("steam_root", "") or find_steam_root()
                        if sr:
                            set_deadzone(sr, installed,
                                         on_progress=lambda msg: _log.info("deadzone: %s", msg.strip()))
                    except Exception:
                        _log.warning("could not update existing deadzones", exc_info=True)
                    finally:
                        self._deadzone_running = False
                        inhibit.stop()
                try:
                    threading.Thread(target=_defaults, daemon=True).start()
                except Exception:
                    _log.warning("deadzone pass could not start", exc_info=True)
                    self._deadzone_running = False
                    inhibit.stop()
        else:
            # This message is shown on the Library itself: offer the retry here.
            self._status.setText("Couldn't scan for games. Check that Steam is installed, then try again.")
            self._scan_again_btn.setVisible(True)

    def _card_focus(self):
        """(card index, button index) of the focused card action, or None."""
        fw = QApplication.focusWidget()
        for ci, card in enumerate(self._cards):
            try:
                if fw in card._buttons:
                    return ci, card._buttons.index(fw)
            except RuntimeError:
                return None
        return None

    def _rebuild(self):
        # A rescan (every time the Library is shown) rebuilds the cards: keep the
        # player's place, the focused card action and the scroll position.
        where = self._card_focus()
        # Shown before the first scan finished: focus could only land on the
        # toolbar. Move it to the first card once there are cards.
        first_cards = not self._cards and QApplication.focusWidget() is self._toolbar_buttons[0]
        scroll = self._scroll.verticalScrollBar().value()
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        games = [g for g in ALL_GAMES
                 if _active_keys(g) or g.get("client") == "lan"]
        self._cards = [ManagementCard(
            gd, self.installed,
            on_setup=self._setup, on_configure=self._configure,
            on_readd=self._readd, on_add_ds=self._add_mw3_ds,
            on_offline=self._offline, on_add_games=self._add_games,
        ) for gd in games]
        self._columns = 0
        self._layout_cards()
        # Toolbar, then the card actions in grid (row-major) order, then the footer
        _chain_focus(self._focus_order())
        if where is not None and self._cards:
            ci = min(where[0], len(self._cards) - 1)
            buttons = self._cards[ci]._buttons
            if buttons:
                target = buttons[min(where[1], len(buttons) - 1)]
                target.setFocus(Qt.FocusReason.OtherFocusReason)
            QTimer.singleShot(0, lambda: self._restore_scroll(scroll))
        elif first_cards and self._cards:
            self.focus_target().setFocus(Qt.FocusReason.OtherFocusReason)
        elif self.isVisible():
            _ensure_focus_in(self)

    def _restore_scroll(self, value):
        try:
            bar = self._scroll.verticalScrollBar()
            bar.setValue(min(value, bar.maximum()))
        except RuntimeError:
            pass

    def _layout_cards(self):
        # Always leave room for the vertical scroll bar: it appears only after
        # this layout makes the grid taller than the viewport, and the screen
        # gets no resize event for that.
        bar = self._scroll.verticalScrollBar()
        width = (self._scroll.width() or self.width()) - max(bar.sizeHint().width(), 10)
        margins = self._grid.contentsMargins()
        spacing = self._grid.horizontalSpacing()
        available = width - margins.left() - margins.right()
        columns = max(1, min(_LIBRARY_MAX_COLS, len(self._cards) or 1,
                             (available + spacing) // (_LIBRARY_CARD_MIN_W + spacing)))
        card_w = max(_LIBRARY_CARD_MIN_W,
                     min(_LIBRARY_CARD_MAX_W, (available - spacing * (columns - 1)) // columns))
        if columns == self._columns and card_w == self._card_w:
            return
        self._columns, self._card_w = columns, card_w
        # Same width and height for every card, so the actions line up per row.
        height = max([card.natural_height(card_w) for card in self._cards] or [0])
        for card in self._cards:
            card.setFixedSize(card_w, height)
            self._grid.removeWidget(card)
        for idx, card in enumerate(self._cards):
            self._grid.addWidget(card, idx // columns, idx % columns,
                                 Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        # widgetResizable may otherwise shrink the inner widget to the short
        # viewport while fixed-height cards spill into the next grid row.
        rows = (len(self._cards) + columns - 1) // columns
        self._grid_host.setMinimumHeight(
            margins.top() + margins.bottom() + rows * height
            + max(0, rows - 1) * self._grid.verticalSpacing())

    # ── DeckOps update notice (installs run from Settings) ─────────────────
    _GITHUB_USER = GITHUB_USER
    _GITHUB_REPO = GITHUB_REPO

    def _clear_update_notice(self):
        self._deckops_update_btn.setText("DeckOps Update")
        self._deckops_update_btn.setAccessibleName("DeckOps Update")
        self._deckops_update_btn.setVisible(False)
        self._deckops_update_status.setText("")
        self._deckops_update_status.setVisible(False)

    def _open_deckops_update(self):
        """The notice button: Settings owns the one DeckOps update entry point;
        open it and run its check, which then offers Update && Restart."""
        self._clear_update_notice()
        s = get_screen(self.stack, "ConfigureScreen")
        go_to(self.stack, "ConfigureScreen")
        if s is not None and hasattr(s, "_check_for_updates"):
            QTimer.singleShot(0, s._check_for_updates)

    def _apply_deckops_update(self):
        # The update ends in a relaunch that would cut a running Settings task off mid-write
        if self._busy_elsewhere():
            return
        # The update runs on Settings and ends in a relaunch. When it fails there ("Already
        # up to date", a download error) the notice must not keep offering it.
        self._clear_update_notice()
        # Apply runs on Settings; show it so download progress is visible
        s = get_screen(self.stack, "ConfigureScreen")
        go_to(self.stack, "ConfigureScreen")
        QTimer.singleShot(0, s._apply_update)

    def _settings_task_running(self):
        """True while a Settings task (or the DeckOps self-update) runs."""
        try:
            s = get_screen(self.stack, "ConfigureScreen")
        except Exception:
            return False
        return bool(s is not None and (getattr(s, "_settings_running", False)
                                       or getattr(s, "_updating", False)))

    def _busy_elsewhere(self):
        """True, with a message, while another operation holds the sleep lock.
        Settings has a Back button that works mid-task, so a Settings task
        (closing Steam, rewriting shortcuts.vdf / configsets) could still be
        running when an update, setup or Offline Mode change started here and
        did the same: two writers on the same files."""
        busy = inhibit.busy()
        if not busy:
            return False
        self._status.setText(f"Wait for this to finish first: {str(busy).rstrip('.')}.")
        return True

    # ── Update all games (batch) ──────────────────────────────────────────
    def _update_all_games(self):
        if self._busy_elsewhere():
            return
        selected, extras = _update_selection(self.installed)
        if not selected:
            self._status.setText("No configured mod-client modes found for the installed game sources.")
            return
        if not _confirm_game_update(self, selected, extras):
            return
        us = get_screen(self.stack, "UpdateScreen")
        us.selected = selected
        us.all_plut_games = {k: g for k, _, g in selected}
        us.steam_root = find_steam_root() or ""
        go_to(self.stack, "UpdateScreen")

    def _offline(self, on):
        """Add or remove Offline Mode (offline launcher, Steam menu Offline entries, bot matches)."""
        if self._busy_elsewhere():
            return
        text = ("Add Offline Mode? It adds the offline launcher, an Offline entry in each game's Steam "
                "launch menu, and bot matches in WaW, BO1, BO2 and MW3 multiplayer. "
                "Steam closes while it is set up."
                if on else
                "Remove Offline Mode? It removes the offline launcher, the Offline launch menu entries "
                "and the bot match files. Steam closes while it is removed.")
        if QMessageBox.question(
                self, "Offline Mode", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes if on else QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
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
        if self._busy_elsewhere():
            return
        if QMessageBox.question(self, "Offline Mode", "Repair the offline launcher? It checks the "
                                "files, rebuilds the prefix and re-adds the shortcut. Steam closes while it runs.") \
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
            files_ok = repair_launcher_files(log)
            # Also the offline server's prefix; _lan_prefixes sets it up again. The old one
            # is moved aside, not deleted first: when the rebuild fails (offline, GE-Proton
            # missing) the working prefix is put back instead of lost.
            sigs.progress.emit(40, "Rebuilding launcher prefix...")
            prefix = plutonium.LAN_SERVER_PREFIX
            aside = prefix + ".deckops_old"
            shutil.rmtree(aside, ignore_errors=True)
            moved = os.path.isdir(prefix)
            if moved:
                os.replace(prefix, aside)
            try:
                plutonium._lan_prefixes(log)
            except BaseException:
                if moved:
                    shutil.rmtree(prefix, ignore_errors=True)
                    os.replace(aside, prefix)
                raise
            shutil.rmtree(aside, ignore_errors=True)
            try: os.remove(os.path.join(PROJECT_ROOT, ".lan_intent.json"))
            except FileNotFoundError: pass
            sigs.progress.emit(90, "Re-adding shortcut...")
            if create_launcher_shortcut(on_progress=log) is False:
                raise RuntimeError("The launcher shortcut could not be added to Steam (see the log above).")
            if files_ok is False:
                sigs.log.emit("⚠  Offline launcher repaired, but its files could not be checked against GitHub "
                              "(no connection?). Run the repair again when online.")
            else:
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

    def _setup(self, gd, force_downgrade_ids=None, cod4_client=None,
               keys_to_setup=None, clear_setup_keys=None, clear_receipt=None):
        """Preflight only the requested modes; commit a reinstall only after approval."""
        if self._busy_elsewhere():
            return False
        keys = _active_keys(gd)
        present_keys = [k for k in keys if k in self.installed]
        if not present_keys:
            self._status.setText("Game files not found. Use Add Games to find them.")
            return False
        if keys_to_setup is None:
            # Never send working modes to the installer just because another
            # mode on the same card has not been configured yet.
            keys_to_setup = [k for k in present_keys if not cfg.is_game_setup_for_source(
                k, self.installed[k].get("source", "steam"))]
            if "iw5mp" in present_keys:
                keys_to_setup = [k for k in keys_to_setup if k != "iw5mp_ds"]
        selected = [(k, gd, self.installed[k]) for k in present_keys if k in keys_to_setup]
        if not selected:
            expected = list(keys)
            if "iw5mp" in present_keys:
                expected = [k for k in expected if k != "iw5mp_ds"]
            elif "iw5mp_ds" in present_keys:
                expected = [k for k in expected if k != "iw5mp"]
            self._status.setText(
                "Detected modes are set up. Use Add Games to find missing game files."
                if any(k not in present_keys for k in expected)
                else "All detected modes are already set up.")
            return False
        root = find_steam_root()

        # Asked first so the DLC choice is part of the space check.
        dlc = _ask_iw4x_dlc(self, selected)

        import preflight
        rows = [(k, KEY_CLIENT.get(k, ""), g.get("install_dir", ""))
                for k, _gd, g in selected]
        try:
            checks = preflight.check(rows, iw4x_dlc=(dlc == "install"),
                                     downgrade_keys=[k for k, _, g in selected
                                                     if g.get("source") != "own"])
        except Exception as ex:
            _log_to_file(f"[ManagementScreen] preflight failed: {ex}")
            self._status.setText("Could not check installation requirements. No setup was changed; try again.")
            return False
        if not _show_preflight(self, checks):
            self._status.setText("Setup canceled. Existing game setup was not changed.")
            return False

        # Do not alter receipts or setup markers until the player has accepted
        # all prompts, including the disk/power preflight dialog.
        if clear_receipt:
            from depot_downgrade import clear_receipt as _clear_receipt
            _clear_receipt(*clear_receipt)
        if clear_setup_keys:
            cfg.unmark_game_setup(clear_setup_keys)

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
        s.cod4_client = "cod4r"  # Legacy CoD4x may only migrate; never install it again.
        self._status.setText("")
        go_to(self.stack, "InstallScreen")
        return True

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
            "DeckOps is adding the free MW3 Dedicated Server to your Steam account.\n\n"
            "Let Steam finish the download, then close and reopen DeckOps and press "
            "Set Up on the MW3 card."
        )

    def _configure(self, gd, installed_keys):
        """Show configure dialog with Mods, Update, and Reinstall options."""
        _MOD_CLIENTS = ("cod4r", "cod4x", "iw4x", "plutonium")
        has_mods_support = any(KEY_CLIENT.get(k, "") in _MOD_CLIENTS for k in installed_keys)
        has_mod_client = bool(_update_candidates(self.installed, installed_keys))

        # Depot downgrade option: shown for any Steam-sourced game that
        # can be downgraded (MW3). Whether the downgrade
        # is actually needed is checked on click. Only MW3 MP / the DS need
        # it (Plutonium); a campaign-only MW3 has nothing to downgrade for.
        _DG_KEY_MAP = {
            "iw5": ("iw5mp", "iw5mp_ds"),
        }
        _DG_LABELS = {"iw5": "Downgrade MW3"}
        has_depot_dg = None
        for _dg_id, _dg_keys in _DG_KEY_MAP.items():
            _matched = [k for k in installed_keys if k in _dg_keys]
            if _matched:
                setup_games = cfg.get_setup_games()
                for k in _matched:
                    entry = setup_games.get(k, {})
                    if not isinstance(entry, dict):
                        entry = {}
                    if entry.get("source", "steam") == "steam":
                        has_depot_dg = _dg_id
                        break
            if has_depot_dg:
                break

        # MW3 SP: old in-place downgrades left a 32-bit iw5sp.exe that
        # won't launch through Steam until a Verify restores it
        sp_verify = False
        _sp = self.installed.get("iw5sp", {}) if "iw5sp" in installed_keys else {}
        if _sp.get("install_dir") and _sp.get("source", "steam") != "own":
            from depot_downgrade import iw5sp_needs_verify
            sp_verify = iw5sp_needs_verify(_sp["install_dir"])

        dlg = QDialog(self)
        dlg.setWindowTitle(f"{gd['base']} · Game Options")
        dlg.resize(460, 420)
        outer = QVBoxLayout(dlg); outer.setContentsMargins(18, 16, 18, 14); outer.setSpacing(8)
        outer.addWidget(_lbl("GAME OPTIONS", 15, C_TEXT, bold=True,
                             align=Qt.AlignmentFlag.AlignLeft, wrap=False))
        outer.addWidget(_lbl(gd["base"], 11, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        options = _no_focus_scroll(QScrollArea()); options.setWidgetResizable(True)
        options.setFrameShape(QScrollArea.Shape.NoFrame)
        options.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); actions = QVBoxLayout(content)
        actions.setContentsMargins(0, 4, 4, 4); actions.setSpacing(9)
        choice = []
        buttons = []

        def add_action(title, description, token, destructive=False):
            b = _btn(title, C_RED_BTN if destructive else C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
            b.clicked.connect(lambda _checked=False: (choice.append(token), dlg.accept()))
            actions.addWidget(b)
            buttons.append(b)
            actions.addWidget(_lbl(description, 10, C_DIM,
                                   align=Qt.AlignmentFlag.AlignLeft))

        # Detected modes are set up but some game files were not found (the card's
        # only action is Game Options then): the search lives here.
        missing_files = [k for k in _card_modes(_active_keys(gd), self.installed)
                         if k not in self.installed]
        if missing_files and installed_keys:
            add_action("Find Missing Modes", "Look for the missing game files through Add Games.", "find")
        if has_mods_support:
            add_action("Open Mod Folders", "Open the game's mod or custom map folder.", "mods")
        if has_mod_client:
            add_action("Update Client", "Check and repair this game's mod client.", "update")
        if sp_verify:
            add_action("Verify SP Files", "Ask Steam to restore MW3 campaign files.", "verify")
        actions.addWidget(_hdiv())
        actions.addWidget(_lbl("ADVANCED · CHANGES GAME FILES", 10, "#FF9292",
                               bold=True, align=Qt.AlignmentFlag.AlignLeft))
        if has_depot_dg:
            add_action(_DG_LABELS[has_depot_dg] + "...",
                       "Download an older Steam depot after reviewing disk checks.", "downgrade", True)
        add_action("Reinstall Game...", "Set up all detected modes again after reviewing disk checks.",
                   "reinstall", True)
        actions.addStretch()
        options.setWidget(content); outer.addWidget(options, 1)
        cancel = _btn("Cancel", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        cancel.clicked.connect(dlg.reject)
        outer.addWidget(cancel)
        _chain_focus(buttons + [cancel])
        # Open on the first action, not on the scroll area or Cancel
        (buttons[0] if buttons else cancel).setFocus(Qt.FocusReason.OtherFocusReason)
        dlg.exec()

        selected = choice[0] if choice else None
        if selected == "find":
            self._add_games()
        elif selected == "mods":
            self._mods(gd, installed_keys)
        elif selected == "update":
            self._update(gd, installed_keys)
        elif selected == "downgrade":
            self._depot_downgrade(has_depot_dg, gd, installed_keys)
        elif selected == "verify":
            from wrapper import launch_steam
            launch_steam("steam://validate/42680")
            self._status.setText("Steam is verifying MW3 Single Player files...")
        elif selected == "reinstall":
            self._reinstall(gd)

    def _update(self, gd, keys):
        """Update the configured modes on this card and their shared dependants."""
        if self._busy_elsewhere():
            return False
        selected, extras = _update_selection(self.installed, keys)
        if not selected:
            self._status.setText("No configured modes found for this game's installed source.")
            return False
        if not _confirm_game_update(self, selected, extras):
            return True         # the player said no: nothing to explain
        s = get_screen(self.stack, "UpdateScreen")
        s.selected = selected
        s.all_plut_games = {k: g for k, _, g in selected}
        s.steam_root = find_steam_root() or ""
        go_to(self.stack, "UpdateScreen")
        return True

    def _reinstall(self, gd):
        """Run a clean install of detected modes after preflight approval."""
        keys = [k for k in _active_keys(gd) if k in self.installed]
        if "iw5mp" in keys:
            # The Dedicated Server is an alternative to MW3 MP, not an extra: the card and
            # the setup screen treat it that way, and _setup drops it the same way
            keys = [k for k in keys if k != "iw5mp_ds"]
        if not keys:
            self._status.setText("Game files not found. Use Add Games to find them.")
            return
        if "cod4mp" in keys and self._current_cod4_client() == "cod4x":
            if QMessageBox.question(
                self, "Switch to CoD4R",
                "CoD4x is no longer offered. Reinstalling MW1 Multiplayer will replace "
                "the existing CoD4x setup with CoD4R and remove its DeckOps shortcut. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
        self._setup(gd, cod4_client="cod4r",
                    keys_to_setup=keys, clear_setup_keys=keys)

    def _depot_downgrade(self, game_id, gd, installed_keys):
        """Downgrade a game from 64-bit to 32-bit and trigger reinstall.

        Checks whether the downgrade is actually needed first; if the
        install is already 32-bit, informs the user and does nothing.
        Otherwise unmarks the game's keys so the install flow re-runs
        the depot downgrade gate.
        """
        _DG_KEY_MAP = {
            "iw5": ("iw5mp", "iw5mp_ds", "iw5sp"),
        }
        _dg_keys = [k for k in installed_keys if k in _DG_KEY_MAP.get(game_id, ())]
        # MW3 DS shares MW3 MP's files and is not set up as its own Plutonium mode when MP
        # is there (_setup and _reinstall drop it the same way)
        if "iw5mp" in _dg_keys:
            _dg_keys = [k for k in _dg_keys if k != "iw5mp_ds"]
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

        # The receipt and setup markers must survive a cancelled preflight.
        self._setup(gd, force_downgrade_ids={game_id}, keys_to_setup=_dg_keys,
                    clear_setup_keys=_dg_keys,
                    clear_receipt=(game_id, idir) if idir else None)

    def _mods(self, gd, installed_keys):
        """Open the mod folder for a game. Shows a chooser for games with
        both mods/ and usermaps/ directories (CoD4x, IW4x), or for
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
                # OLED: read plut_dir from per-game metadata. BO1 / BO2 modes share
                # one game folder (and so one metadata file) but not one prefix:
                # trust it only when it was written for this mode.
                game = self.installed.get(game_key, {})
                install_dir = game.get("install_dir", "")
                plut_dir = ""
                if install_dir:
                    meta_path = os.path.join(install_dir, "deckops_plutonium.json")
                    if os.path.exists(meta_path):
                        try:
                            with open(meta_path) as f:
                                meta = json.load(f)
                            if isinstance(meta, dict) and meta.get("game_key") == game_key:
                                plut_dir = meta.get("plut_dir", "") or ""
                        except Exception:
                            plut_dir = ""
                if not plut_dir:
                    # This mode's own prefix, picked like install/update do
                    try:
                        from plutonium import _plut_compat, _plut_dir_in_prefix
                        sr = cfg.load().get("steam_root", "") or find_steam_root() or ""
                        compat = _plut_compat(game_key, game, game.get("source", "steam"), sr)
                        if compat:
                            plut_dir = _plut_dir_in_prefix(compat)
                    except Exception:
                        _log.warning("could not resolve the Plutonium folder for %s", game_key,
                                     exc_info=True)
                        plut_dir = ""

            if not plut_dir:
                return None
            return os.path.join(plut_dir, "storage", storage_name, "mods")

        # Determine which client we're dealing with from the installed keys
        client = None
        plut_keys = []
        for k in installed_keys:
            c = KEY_CLIENT.get(k, "")
            if c in ("cod4r", "cod4x", "iw4x"):
                client = c
                break
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
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        hdr, hl = _header_bar()
        hl.addWidget(_lbl(screen_title("SetupCompleteScreen").upper(), 12, C_IW, bold=True, wrap=False))
        lay.addWidget(hdr)

        banner = QWidget(); heading = QVBoxLayout(banner)
        heading.setContentsMargins(24,10,24,10); heading.setSpacing(4)
        self._heading = _lbl("SETUP COMPLETE", 20, C_TEXT, bold=True,
                             align=Qt.AlignmentFlag.AlignLeft, wrap=False)
        self._heading.setFont(font(20, True, display=True))
        heading.addWidget(self._heading)
        self._intro = _lbl(
            "DeckOps set up controller layouts, GE-Proton and display settings. "
            "Open Steam or return to Game Mode to play.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        heading.addWidget(self._intro)
        lay.addWidget(banner)

        # Only the notes scroll: the return action remains visible at 800x500.
        self._guide_scroll = _no_focus_scroll(QScrollArea()); scroll = self._guide_scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        sl = QVBoxLayout(inner); sl.setContentsMargins(24,8,24,12); sl.setSpacing(8)

        def _note(label, accent, background=C_CARD):
            label.setStyleSheet(f"QLabel{{color:{C_TEXT};background:{background};border-left:3px solid {accent};"
                                "border-radius:5px;padding:8px 12px;}")

        # ── Cloud saves ───────────────────────────────────────────────────────
        sl.addWidget(_hdiv())
        sl.addWidget(_lbl("STEAM CLOUD SAVES", 12, C_TREY, bold=True, align=Qt.AlignmentFlag.AlignLeft))
        cloud_note = _lbl(
            "If Steam asks about cloud saves, choose Keep Local. "
            "If a game asks about Safe Mode or hardware changes, choose No.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        _note(cloud_note, C_TREY, C_SURFACE)
        sl.addWidget(cloud_note)
        restore_position = sl.count()  # save recovery stays before the long play guide

        # ── How to start each game ────────────────────────────────────────────
        self._mw1waw_div = _hdiv()
        self._mw1waw_hdr = _lbl("STARTING EACH GAME", 12, C_IW, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._mw1waw_body = _lbl("", 11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        _note(self._mw1waw_body, C_IW)
        sl.addWidget(self._mw1waw_div)
        sl.addWidget(self._mw1waw_hdr)
        sl.addWidget(self._mw1waw_body)

        # ── First launch ─────────────────────────────────────────────────────
        sl.addWidget(_hdiv())
        sl.addWidget(_lbl("FIRST LAUNCH", 12, C_TEXT, bold=True, align=Qt.AlignmentFlag.AlignLeft))
        first_launch = _lbl(
            "Every game takes longer to launch the first time while Proton sets things up. "
            "It's faster after that.\n\n"
            "Plutonium games must be launched once before Offline Mode can use them.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        _note(first_launch, C_IW)
        sl.addWidget(first_launch)

        # ── MW2 MP own-game first launch ──────────────────────────────────────
        self._mw2own_div = _hdiv()
        self._mw2own_hdr = _lbl("MW2 MULTIPLAYER · NON-STEAM", 12, C_IW, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._mw2own_body = _lbl(
            "MW2 Multiplayer (IW4x) has to be launched twice the first time. The first "
            "launch fails; launch it again and it works.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        _note(self._mw2own_body, C_IW)
        sl.addWidget(self._mw2own_div)
        sl.addWidget(self._mw2own_hdr)
        sl.addWidget(self._mw2own_body)

        # ── LCD notes (LCD users only) ────────────────────────────────────────
        self._lcd_div = _hdiv()
        self._lcd_hdr = _lbl("LCD STEAM DECK NOTES", 12, C_TREY, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._lcd_body = _lbl(
            "Plutonium games on LCD can take a moment to start while a shader cache cleanup "
            "runs. If a game doesn't start right away, wait or try again.\n\n"
            "If Steam offers to compile Vulkan shaders before launching, skip it; LCD "
            "Plutonium games don't use them.\n\n"
            "Quit from the game's own menu, not the Steam overlay, for a faster exit.",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        _note(self._lcd_body, C_TREY, C_SURFACE)
        sl.addWidget(self._lcd_div)
        sl.addWidget(self._lcd_hdr)
        sl.addWidget(self._lcd_body)

        # ── Restore player saves (visible only when backups exist) ────────────
        self._restore_div = _hdiv()
        self._restore_hdr = _lbl("RESTORE PLAYER SAVES", 12, C_IW, bold=True, align=Qt.AlignmentFlag.AlignLeft)
        self._restore_body = _lbl(
            "DeckOps found backups from a previous install. "
            "Restore your configs, stats and custom classes?",
            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        _note(self._restore_body, C_IW)
        self._restore_status = _StatusLabel(FONT_BODY, C_IW)
        self._restore_status.setVisible(False)

        self._restore_btn = _btn("Restore Saves", C_IW, size=FONT_ACTION, h=BTN_MIN_H)
        self._restore_btn.setFixedWidth(200)
        self._restore_btn.clicked.connect(self._do_restore)
        self._restore_skip = _btn("Skip", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        self._restore_skip.setFixedWidth(120)
        self._restore_skip.clicked.connect(self._skip_restore)

        rbw = QHBoxLayout(); rbw.setSpacing(12)
        rbw.addWidget(self._restore_btn)
        rbw.addWidget(self._restore_skip)
        rbw.addStretch()

        for widget in (self._restore_div, self._restore_hdr, self._restore_body):
            sl.insertWidget(restore_position, widget)
            restore_position += 1
        sl.insertLayout(restore_position, rbw)
        restore_position += 1
        sl.insertWidget(restore_position, self._restore_status)

        self._restore_widgets = [
            self._restore_div, self._restore_hdr, self._restore_body,
            self._restore_btn, self._restore_skip,
        ]

        sl.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

        # ── Return action (pinned outside scroll) ─────────────────────────────
        self._continue_btn = _btn(LABELS["continue_to_library"], C_IW, size=FONT_ACTION, h=BTN_MIN_H)
        self._continue_btn.setFixedWidth(300)
        self._continue_btn.clicked.connect(self._go_management)
        cw = QHBoxLayout(); cw.setContentsMargins(24,6,24,10)
        cw.addStretch(); cw.addWidget(self._continue_btn)
        lay.addLayout(cw)

    def showEvent(self, e):
        super().showEvent(e)
        if e.spontaneous():     # window restored, not a screen change
            return
        self._heading.setText("SETUP COMPLETE")
        self._intro.setText(
            "DeckOps set up controller layouts, GE-Proton and display settings. "
            "Open Steam or return to Game Mode to play.")
        self._continue_btn.setText(LABELS["continue_to_library"])
        is_lcd = cfg.is_lcd()
        self._lcd_div.setVisible(is_lcd)
        self._lcd_hdr.setVisible(is_lcd)
        self._lcd_body.setVisible(is_lcd)
        guide = how_to_start_text(cfg.get_setup_games(), is_lcd)
        self._mw1waw_body.setText(guide)
        for w in (self._mw1waw_div, self._mw1waw_hdr, self._mw1waw_body):
            w.setVisible(bool(guide))
        show_mw2own = cfg.is_game_setup_for_source("iw4mp", "own")
        self._mw2own_div.setVisible(show_mw2own)
        self._mw2own_hdr.setVisible(show_mw2own)
        self._mw2own_body.setVisible(show_mw2own)
        try:
            from save_backup import restore_offer_pending
            show_restore = restore_offer_pending()
        except Exception:
            show_restore = False
        # A previous visit may have left the buttons disabled and hidden
        if getattr(self, "_restoring", False):
            return
        self._continue_btn.setEnabled(True)
        self._restore_btn.setEnabled(True)
        self._restore_skip.setEnabled(True)
        for w in self._restore_widgets:
            w.setVisible(show_restore)
        self._restore_status.setVisible(False)

    def _do_restore(self):
        """Run save restore in a background thread."""
        if getattr(self, "_restoring", False):
            return
        busy = inhibit.busy()
        if busy:
            self._restore_status.setText(f"Wait for this to finish first: {str(busy).rstrip('.')}.")
            self._restore_status.setVisible(True)
            return
        self._restoring = True
        # Copying saves back must not be cut off by a suspend or by leaving for
        # the Library (which could start an install over the same prefixes)
        self._restore_took_lock = True
        inhibit.start("DeckOps is restoring saves")
        self._continue_btn.setEnabled(False)
        self._restore_btn.setEnabled(False)
        self._restore_skip.setEnabled(False)
        self._restore_status.setText("Restoring saves...")
        self._restore_status.setVisible(True)

        def _run():
            problems = []
            try:
                from save_backup import restore_saves
                steam_root = find_steam_root()
                restored = restore_saves(
                    steam_root=steam_root,
                    on_progress=lambda msg: problems.append(msg.strip().lstrip("⚠ ").strip())
                    if "⚠" in msg else None,
                )
                count = len(restored) if restored else 0
                detail = (" " + "; ".join(problems[:2])) if problems else ""
                if count > 0:
                    groups = ", ".join(restored.keys())
                    # A partial copy is not a completed restore: keep the offer
                    # available so the remaining backups can be retried.
                    kind = "RETRY" if problems else "DONE"
                    self._s.log.emit(f"{kind}|Restored saves for {count} group(s): {groups}."
                                     + (f" Some saves could not be restored:{detail} Retry Restore Saves."
                                        if problems else ""))
                elif problems:
                    # Copies that failed (a full disk, permissions) are not "no save data":
                    # the offer stays so the player can try again
                    self._s.log.emit(f"RETRY|⚠  Restore failed:{detail}")
                else:
                    self._s.log.emit("DONE|No matching save data found to restore.")
            except Exception as ex:
                self._s.log.emit(f"RETRY|⚠  Restore failed: {ex}")

        self._s = _Sigs()
        self._s.log.connect(self._restore_finished)
        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception as ex:
            self._restore_finished(f"RETRY|⚠  Restore failed to start: {ex}")

    def _restore_finished(self, message):
        self._restoring = False
        if getattr(self, "_restore_took_lock", False):
            self._restore_took_lock = False
            inhibit.stop()
        self._continue_btn.setEnabled(True)
        kind, sep, text = message.partition("|")
        if not sep or kind not in ("DONE", "RETRY"):
            kind, text = "DONE", message
        self._restore_status.setText(text)
        self._restore_status.setVisible(True)
        if kind == "RETRY":
            # The restore did not happen: dismissing the offer for good (the backup is
            # still there) left the player no way to restore it
            self._restore_btn.setEnabled(True)
            self._restore_skip.setEnabled(True)
            return
        self._mark_offer_handled()
        for w in self._restore_widgets:
            w.setVisible(False)

    def _skip_restore(self):
        """Hide the restore section without restoring."""
        self._mark_offer_handled()
        for w in self._restore_widgets:
            w.setVisible(False)
        self._restore_status.setVisible(False)

    @staticmethod
    def _mark_offer_handled():
        try:
            from save_backup import mark_restore_offer_handled
            mark_restore_offer_handled()
        except Exception as ex:
            _log_to_file(f"[SetupCompleteScreen] could not record restore answer: {ex}")

    def _go_management(self):
        go_to(self.stack, "ManagementScreen")

    # ── controller protocol (see ui_constants) ─────────────────────────────
    def focus_target(self):
        return self._continue_btn

    def back_action(self):
        self._go_management()

    def escape_allowed(self):
        # Leaving mid-restore could start an install over the same prefixes
        return not getattr(self, "_restoring", False)


# ── ConfigureScreen ───────────────────────────────────────────────────────────
class ConfigureScreen(QWidget):
    # Sections, and where each one's status line goes
    _SECTIONS = ("maintenance", "tweaks", "support", "danger")

    def __init__(self, stack):
        super().__init__(); self.stack=stack; self.screen_name = "ConfigureScreen"
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        hdr, hl = _header_bar()
        t = _lbl(screen_title("ConfigureScreen").upper(), 16, C_TEXT, bold=True, wrap=False)
        hl.insertSpacing(2, 20); hl.insertWidget(3, t)
        back = _btn(LABELS["back"], C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H); back.setFixedWidth(140)
        back.clicked.connect(self.back_action)
        self._back_btn = back
        hl.addWidget(back)
        lay.addWidget(hdr)

        # Three columns fit the 1280x800 Deck screen without scrolling; a
        # smaller window reflows to two or one column and scrolls.
        scroll = _no_focus_scroll(QScrollArea()); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        self._settings_scroll = scroll
        self._settings_cols = QGridLayout(inner)
        self._settings_cols.setContentsMargins(20,14,20,14)
        self._settings_cols.setHorizontalSpacing(20)
        self._settings_cols.setVerticalSpacing(16)
        self._status_labels = {}
        self._task_buttons = []

        def _panel(name, title, color=C_TEXT, frame_style=None):
            panel = QFrame(); panel.setObjectName(name)
            panel.setStyleSheet(frame_style or
                                f"QFrame#{name}{{background:{C_CARD};border:1px solid {C_LINE};border-radius:8px;}}")
            col = QVBoxLayout(panel); col.setContentsMargins(14,10,14,12); col.setSpacing(6)
            col.addWidget(_lbl(title, 13, color, bold=True, align=Qt.AlignmentFlag.AlignLeft))
            return panel, col

        def _status(col, section):
            # Each section reports next to the buttons that were pressed
            lbl = _StatusLabel(FONT_BODY, C_DIM, auto_hide=True)
            lbl.setObjectName(f"status_{section}")
            col.addWidget(lbl)
            self._status_labels[section] = lbl
            return lbl

        def _desc(col, text):
            """The small grey line under an action."""
            lbl = _lbl(text, 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
            col.addWidget(lbl)
            return lbl

        def _action(col, text, slot, desc=None, task=True, color=C_DARK_BTN):
            b = _btn(text, color, size=FONT_ACTION, h=BTN_MIN_H)
            b.setMinimumWidth(0)
            b.clicked.connect(slot)
            col.addSpacing(4)
            col.addWidget(b)
            if desc:
                _desc(col, desc)
            if task:
                self._task_buttons.append(b)
            return b

        # ── Maintenance ───────────────────────────────────────────────────
        self._maint_panel, mcol = _panel("Maintenance", "Maintenance")
        self._repair_btn = _action(mcol, "Repair Shortcuts", self._repair_shortcuts,
                                   "Re-adds missing DeckOps shortcuts and repairs launch menus and "
                                   "launch options. Closes Steam.")
        self._ctrl_btn = _action(mcol, "Re-apply Controller Layout", self._apply_controller_profiles,
                                 "Steam Deck: the bundled 'Call of Duty with controller support' layout "
                                 "for DeckOps games. Closes Steam.")
        self._check_btn = _action(mcol, "Check My Setup", self._check_setup,
                                  "Checks each game's files, GE-Proton and launch option. Changes "
                                  "nothing; Fix runs that game's Update.")
        self._backup_btn = _action(mcol, "Back Up My Saves Now", self._backup_saves_now,
                                   "Copies saves, stats and Plutonium mods to a folder that survives "
                                   "an uninstall. Steam can stay open.")
        # LCD only
        self._shader_row = QWidget()
        sr_lay = QVBoxLayout(self._shader_row); sr_lay.setContentsMargins(0,0,0,0); sr_lay.setSpacing(6)
        self._shader_btn = _action(sr_lay, "Clear Shader Cache", self._clear_shader_cache,
                                   "Clears the shader cache of every game you have set up. LCD only.")
        mcol.addWidget(self._shader_row)
        # Kept for showEvent's LCD toggle; the sections have no dividers
        self._shader_hdiv = QWidget(); self._shader_hdiv.setFixedHeight(0)
        mcol.addWidget(self._shader_hdiv)
        mcol.addSpacing(4)
        _status(mcol, "maintenance")
        mcol.addStretch()

        # ── Game tweaks ───────────────────────────────────────────────────
        self._tweaks_panel, gcol = _panel("GameTweaks", "Game tweaks")
        self._name_note = _lbl("Player name in IW4x and Plutonium offline "
                               "(STEAM + X opens the keyboard)", FONT_BODY, C_DIM,
                               align=Qt.AlignmentFlag.AlignLeft)
        gcol.addWidget(self._name_note)
        nr = QHBoxLayout(); nr.setSpacing(8)
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Enter player name...")
        self._name_input.setFixedHeight(BTN_MIN_H)
        self._name_input.setMaxLength(32)
        self._name_input.setFont(font(FONT_ACTION))
        self._name_input.setStyleSheet(
            f"QLineEdit{{background:{C_SURFACE};color:{C_TEXT};border:1px solid {C_LINE};"
            "border-radius:6px;padding:0 10px;}"
            f"QLineEdit:focus{{border:2px solid {C_FOCUS};}}"
            f"QLineEdit:disabled{{background:{C_CARD};color:{C_DIM};border-color:{C_LINE};}}"
        )
        save_btn = _btn("Save", C_IW, size=FONT_ACTION, h=BTN_MIN_H)
        save_btn.setFixedWidth(90)
        save_btn.clicked.connect(self._save_name)
        self._save_name_btn = save_btn
        self._task_buttons.append(save_btn)
        nr.addWidget(self._name_input, stretch=1); nr.addWidget(save_btn)
        gcol.addLayout(nr)
        self._deadzone_btn = _action(gcol, "Re-apply Deadzone", self._apply_deadzone,
                                     "Stick deadzone 0.05 min / 0.00 max, also set when the library opens. "
                                     "Other settings are kept.")
        self._fps_btn = _action(gcol, "Apply 90 FPS / 90 FOV", self._apply_fps_fov,
                                "Game configs of MW1, MW2/MW3 multiplayer, WaW, BO1 and BO2. "
                                "Steam can stay open.")
        self._cap_btn = _action(gcol, "", self._toggle_dxvk_cap,
                                "The MW2 and MW3 campaigns ignore their configs: this limits them to "
                                "90 FPS with DXVK (FOV 90 is set at every start). Closes Steam.")
        self._refresh_cap_btn()
        gcol.addSpacing(4)
        _status(gcol, "tweaks")
        gcol.addSpacing(6)
        gcol.addWidget(_lbl("Background music", FONT_BODY, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        mr = QHBoxLayout(); mr.setSpacing(12)
        self._music_on = _saved_music_enabled()
        music_volume = _saved_music_volume()
        self._music_toggle = _btn(
            "Music: ON" if self._music_on else "Music: OFF",
            C_IW if self._music_on else C_DARK_BTN,
            size=FONT_ACTION, h=BTN_MIN_H,
        )
        self._music_toggle.setFixedWidth(140)
        self._music_toggle.clicked.connect(self._toggle_music)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal); self._vol_slider.setRange(0,100)
        self._vol_slider.setValue(int(round(music_volume*100)))
        self._vol_slider.setAccessibleName("Music volume")
        self._vol_label = _lbl(f"{self._vol_slider.value()}%", FONT_BODY, C_DIM, wrap=False)
        self._vol_label.setFixedWidth(44)
        self._vol_slider.valueChanged.connect(self._set_volume)
        mr.addWidget(self._music_toggle); mr.addWidget(self._vol_slider,stretch=1); mr.addWidget(self._vol_label)
        gcol.addLayout(mr)
        gcol.addStretch()

        # ── Support ───────────────────────────────────────────────────────
        self._support_panel, scol = _panel("Support", "Support")
        # The one place DeckOps updates are checked and installed; the Library
        # only shows a notice that leads here.
        self._update_btn = _btn("Check for Updates", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        self._update_btn.setMinimumWidth(0)
        self._update_btn.clicked.connect(self._check_for_updates)
        scol.addWidget(self._update_btn)
        self._update_status = _StatusLabel(FONT_BODY, C_DIM, auto_hide=True)
        scol.addWidget(self._update_status)
        udr = QHBoxLayout(); udr.setSpacing(8)
        log_btn = _btn("Copy Log", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        view_log_btn = _btn("View Log", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        for b in (log_btn, view_log_btn):
            b.setMinimumWidth(0)
        view_log_btn.clicked.connect(self._view_log)
        udr.addWidget(log_btn, 1); udr.addWidget(view_log_btn, 1)
        scol.addSpacing(4)
        scol.addLayout(udr)
        self._log_status = _StatusLabel(FONT_BODY, C_DIM, auto_hide=True)
        scol.addWidget(self._log_status)
        log_btn.clicked.connect(lambda: _copy_log_to_clipboard(self._log_status))
        lr = QHBoxLayout(); lr.setSpacing(8)
        discord_btn = _btn("Discord", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        discord_btn.clicked.connect(lambda: self._open_url("https://discord.gg/bkSQeq5Azk"))
        stable_btn = _btn("Stable", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        stable_btn.clicked.connect(lambda: self._open_url(f"https://github.com/{GITHUB_USER}/DeckOps"))
        nightly_btn = _btn("Nightly", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        nightly_btn.clicked.connect(lambda: self._open_url(f"https://github.com/{GITHUB_USER}/DeckOps-Nightly"))
        for b in (discord_btn, stable_btn, nightly_btn):
            b.setMinimumWidth(0)
            lr.addWidget(b, 1)
        scol.addSpacing(4)
        scol.addLayout(lr)
        _desc(scol, "Get help on Discord; the Stable and Nightly project pages.")
        self._status_labels["support"] = self._update_status
        self._about_label = _lbl("", 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        scol.addSpacing(4)
        scol.addWidget(self._about_label)

        # ── Danger Zone ───────────────────────────────────────────────────
        danger, dz = _panel("DangerZone", "Danger Zone", "#FF9292",
                            "QFrame#DangerZone{background:#291B20;border:1px solid #A24848;"
                            "border-radius:8px;}")
        _desc(dz, "Both ask first. Your save backups are kept.")
        dr = QHBoxLayout(); dr.setSpacing(8)
        uninstall_btn = _btn("Full Uninstall...", C_RED_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        reset_cfg_btn = _btn("Reset Config...", C_RED_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        reset_cfg_btn.setToolTip("Reset DeckOps Config: clears setup and starts the wizard again")
        reset_cfg_btn.setAccessibleName("Reset DeckOps Config")
        uninstall_btn.setToolTip("Removes DeckOps files, shortcuts and mod clients")
        for b in (uninstall_btn, reset_cfg_btn):
            b.setMinimumWidth(0)
            dr.addWidget(b, 1)
            self._task_buttons.append(b)
        uninstall_btn.clicked.connect(self._confirm_uninstall)
        reset_cfg_btn.clicked.connect(self._confirm_reset)
        dz.addLayout(dr)
        _status(dz, "danger")
        self._danger_panel = danger

        self._support_col = QWidget()
        sc = QVBoxLayout(self._support_col); sc.setContentsMargins(0,0,0,0); sc.setSpacing(16)
        sc.addWidget(self._support_panel); sc.addWidget(danger); sc.addStretch()

        self._active_status = self._status_labels["maintenance"]
        self._settings_columns = 0
        self._settings_narrow = None
        self._layout_settings()

        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

        _chain_focus([back] + self._focus_order_body())

    def _focus_order_body(self):
        return [self._repair_btn, self._ctrl_btn, self._check_btn, self._backup_btn, self._shader_btn,
                self._name_input, self._save_name_btn, self._deadzone_btn, self._fps_btn, self._cap_btn,
                self._music_toggle, self._vol_slider,
                self._update_btn] + self._support_panel.findChildren(QPushButton)[1:] + \
               self._danger_panel.findChildren(QPushButton)

    # ── per-section status ──────────────────────────────────────────────────
    @property
    def status(self):
        """The status line of the section whose action ran last."""
        return self._active_status

    def _use_status(self, section):
        self._active_status = self._status_labels[section]
        return self._active_status

    def _set_tasks_enabled(self, enabled):
        for b in self._task_buttons:
            b.setEnabled(enabled)
        self._name_input.setEnabled(enabled)

    # ── controller protocol (see ui_constants) ─────────────────────────────
    def focus_target(self):
        return next((b for b in self._task_buttons if b.isEnabled()), self._back_btn)

    def back_action(self):
        go_to(self.stack, "ManagementScreen")

    def escape_allowed(self):
        return not (getattr(self, "_settings_running", False) or getattr(self, "_updating", False))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_settings()

    def _layout_settings(self):
        w = self.width()
        columns = 3 if w >= 1100 else 2 if w >= 760 else 1
        if columns == self._settings_columns:
            return
        self._settings_columns = columns
        self._settings_narrow = columns < 3
        panels = (self._maint_panel, self._tweaks_panel, self._support_col)
        for panel in panels:
            self._settings_cols.removeWidget(panel)
        if columns == 3:
            spots = ((0, 0), (0, 1), (0, 2))
        elif columns == 2:
            spots = ((0, 0), (0, 1), (1, 0))
        else:
            spots = ((0, 0), (1, 0), (2, 0))
        for panel, (r, c) in zip(panels, spots):
            self._settings_cols.addWidget(panel, r, c, Qt.AlignmentFlag.AlignTop)
        for c in range(3):
            self._settings_cols.setColumnStretch(c, 1 if c < columns else 0)

    def showEvent(self, event):
        super().showEvent(event)
        if event.spontaneous():     # window restored: don't overwrite what's being typed
            return
        # Land on the first section action, not on Back (which Qt may pick
        # while the previous screen's focused widget is hidden)
        _focus_first(self.focus_target())
        # Sync music toggle with the persistent control, if it changed outside Settings.
        self._sync_music_toggle()
        # Refresh dynamic state
        model = cfg.get_deck_model() or "unknown"
        source_label = "Steam & Non-Steam"
        player = cfg.get_player_name() or "Player"
        self._name_input.setText(player if player != "Player" else "")

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

    def _sync_music_toggle(self):
        self._music_on = _saved_music_enabled()
        text = "Music: ON" if self._music_on else "Music: OFF"
        self._music_toggle.setText(text)
        self._music_toggle.setAccessibleName(text)
        self._music_toggle.setStyleSheet(
            _button_style(C_IW if self._music_on else C_DARK_BTN))

    def _reload_saved_settings(self):
        """Show what deckops.json holds now (after Reset Config): the music
        controls and the FPS-limit button kept the wiped values."""
        timer = getattr(self, "_vol_save", None)
        if timer is not None:
            timer.stop()        # a pending save would write the old slider value back
        _reload_audio_settings()
        volume = int(round(_saved_music_volume() * 100))
        self._vol_slider.blockSignals(True)
        try:
            self._vol_slider.setValue(volume)
        finally:
            self._vol_slider.blockSignals(False)
        self._vol_label.setText(f"{volume}%")
        self._sync_music_toggle()
        self._refresh_cap_btn()
        window = self.window()
        if hasattr(window, "_sync_mute_button"):
            window._muted = not self._music_on
            window._sync_mute_button()

    def _toggle_music(self):
        _set_audio_enabled(not self._music_on)
        if _saved_music_enabled():
            _start_audio()
        else:
            _kill_audio()
        self._sync_music_toggle()
        window = self.window()
        if hasattr(window, "_sync_mute_button"):
            window._muted = not self._music_on
            window._sync_mute_button()

    def _set_volume(self, val):
        self._vol_label.setText(f"{val}%")
        _set_audio_volume(val / 100.0, persist=False)
        # One save once the slider rests, not a config write per step of a drag
        timer = getattr(self, "_vol_save", None)
        if timer is None:
            timer = self._vol_save = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(400)
            timer.timeout.connect(self._persist_volume)
        timer.start()

    def _persist_volume(self):
        cfg.set_music_volume(self._vol_slider.value() / 100.0)

    def hideEvent(self, event):
        # Leaving the screen inside the 400 ms window must not lose the last value
        timer = getattr(self, "_vol_save", None)
        if timer is not None and timer.isActive():
            timer.stop()
            self._persist_volume()
        super().hideEvent(event)

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
        busy = inhibit.busy()
        if busy:
            self.status.setText(f"Wait for this to finish first: {str(busy).rstrip('.')}.")
            return False
        self._settings_running = True
        # The caller's signal object was only referenced by the worker's closure, so
        # it was freed on the worker thread right after its last emit: Qt then drops
        # the not yet delivered `done` and the slot below never ran, leaving
        # _settings_running set ("Please wait...") and the sleep lock held. Only one
        # task runs at a time, so the next one can safely replace this reference.
        self._settings_sigs = s
        # These close Steam and rewrite its files in daemon threads; without this the
        # window could be closed (or the machine suspended) half way through. Don't
        # take over a lock another operation already holds.
        took_lock = not inhibit.busy()
        if took_lock:
            inhibit.start("DeckOps is changing Steam settings")
        # Only one task at a time: the others are greyed out until this one ends
        # (success or failure). Focus returns to the button that started it.
        fw = QApplication.focusWidget()
        pressed = fw if fw in self._task_buttons else None
        self._set_tasks_enabled(False)

        def _release(_ok):
            self._settings_running = False
            if took_lock:
                inhibit.stop()
            try:
                self._set_tasks_enabled(True)
                if pressed is not None and self.isVisible() and QApplication.activeModalWidget() is None:
                    pressed.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                pass        # screen closed while the task ran
        s.done.connect(_release)
        return True

    def _start_task_thread(self, s, target):
        """Start a reserved Settings worker; a spawn failure must release the slot."""
        try:
            threading.Thread(target=target, daemon=True).start()
        except Exception as ex:
            _log.error("settings task could not start", exc_info=True)
            s.log.emit(f"✗  Could not start task: {ex}")
            s.done.emit(False)

    def _apply_controller_profiles(self):
        st = self._use_status("maintenance")
        s = _Sigs()
        if not self._settings_task(s):
            return
        st.setText("Re-applying controller profiles...")
        s.log.connect(lambda msg: st.setText(msg))
        # The worker's own explanation, kept: the generic line below used to replace
        # it the instant `done` fired, so the real reason was never readable.
        reason = []
        s.done.connect(lambda ok: st.setText(
            "✓  Steam Deck layout applied." if ok else
            f"✗  {reason[0]}" if reason else "✗  Could not apply Steam Deck layout; see log."
        ))
        def _run():
            try:
                from controller_profiles import install_controller_templates, assign_controller_profiles
                from wrapper import kill_steam, set_steam_input_enabled
                if cfg.get_deck_model() not in ("lcd", "oled"):
                    reason.append("This bundled layout supports Steam Deck controls only.")
                    s.done.emit(False)
                    return
                s.log.emit("Closing Steam...")
                kill_steam(on_progress=lambda msg: s.log.emit(f"  {msg}"))
                install_controller_templates(on_progress=s.log.emit)
                if assign_controller_profiles(on_progress=s.log.emit) == 0:
                    # No Steam account, or every layout failed to write: not "applied"
                    reason.append("No controller layout could be written (is a Steam account signed in "
                                  "on this device?); see the log.")
                    s.done.emit(False)
                    return
                # Same root the rest of DeckOps resolves: a config without steam_root
                # used to skip Steam Input silently
                from steam_common import default_steam_root
                set_steam_input_enabled(default_steam_root())
                s.done.emit(True)
                s.log.emit("✓  Controller profiles applied. It is now safe to reopen Steam.")
            except Exception as ex:
                reason.append(f"Failed: {ex}")
                s.done.emit(False)
        self._start_task_thread(s, _run)

    def _repair_shortcuts(self):
        self._use_status("maintenance")
        s = _Sigs()
        if not self._settings_task(s):
            return
        self._start_shortcut_repair(s)

    def _start_shortcut_repair(self, s, errors=None, verify_cap=False):
        """Run the shared repair worker after the Settings task slot is reserved."""
        st = self.status
        st.setText("Checking shortcuts...")
        s.log.connect(lambda msg: st.setText(msg))
        def _run():
            ok = False
            try:
                from shortcut import repair_shortcuts
                from wrapper import kill_steam
                s.log.emit("Closing Steam...")
                kill_steam(on_progress=lambda msg: s.log.emit(f"  {msg}"))
                sr = cfg.load().get("steam_root", "") or None
                readded = repair_shortcuts(sr, on_progress=s.log.emit)
                if verify_cap:
                    # repair_shortcuts deliberately swallows SP-option failures;
                    # a cap toggle cannot be considered applied if that step fails.
                    from sp_mod import repair_sp_launch_options
                    repair_sp_launch_options(sr, s.log.emit)
                if readded:
                    s.log.emit(f"✓  Re-added: {', '.join(readded)}. It is now safe to reopen Steam.")
                else:
                    s.log.emit("✓  All shortcuts present. It is now safe to reopen Steam.")
                ok = True
            except Exception as ex:
                if errors is not None:
                    errors.append(str(ex))
                s.log.emit(f"✗  Repair failed: {ex}")
            finally:
                s.done.emit(ok)
        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception as ex:
            if errors is not None:
                errors.append(str(ex))
            s.log.emit(f"✗  Repair failed to start: {ex}")
            s.done.emit(False)

    def _save_name(self):
        st = self._use_status("tweaks")
        typed = self._name_input.text().strip()
        name = cfg.clean_player_name(typed)
        if not name:
            st.setText("Please enter a name (letters, numbers and most symbols; "
                                "not \" \\ / $ ` ; < > : | ? *).")
            return
        s = _Sigs()
        if not self._settings_task(s):
            return
        try:
            cfg.set_player_name(name)
        except Exception as ex:
            _log.warning("could not save player name", exc_info=True)
            st.setText(f"✗  Could not save player name: {ex}")
            s.done.emit(False)
            return
        self._name_input.setText(name)
        st.setText(f"Updating player name to \"{name}\"...")
        s.log.connect(lambda msg: st.setText(msg))
        def _run():
            try:
                from game_config import rename_player
                from detect_games import find_all_games as _find_all
                steam_root = cfg.load().get("steam_root", "") or find_steam_root()
                # As the other Settings actions: the copy that was set up, not the Steam one
                installed = _find_all(steam_root, preferred_sources=cfg.get_setup_games()) if steam_root else {}
                count = rename_player(
                    name, steam_root, installed_games=installed,
                    on_progress=lambda msg: s.log.emit(msg),
                )
                s.log.emit(f"✓  Player name set to \"{name}\" ({count} config(s) updated).")
            except Exception as ex:
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        self._start_task_thread(s, _run)

    def _refresh_cap_btn(self):
        text = "MW2/MW3 FPS Limit: " + ("On" if cfg.get_dxvk_frame_cap() else "Off")
        self._cap_btn.setText(text)
        self._cap_btn.setAccessibleName(text)

    def _toggle_dxvk_cap(self):
        """Apply the cap with shortcut repair, restoring the old value on failure."""
        st = self._use_status("tweaks")
        s = _Sigs()
        if not self._settings_task(s):
            return  # A busy click must not change the config or the button.
        try:
            with cfg.transaction():
                config = cfg.load()
                had_cap = "dxvk_frame_cap" in config
                old_value = config.get("dxvk_frame_cap", True)
                config["dxvk_frame_cap"] = not bool(old_value)
                cfg.save(config)
            self._refresh_cap_btn()
        except Exception as ex:
            s.done.emit(False)  # Release the Settings task slot even if saving failed.
            st.setText(f"✗  Could not change FPS limit: {ex}")
            return

        errors = []
        def _finish(ok):
            if ok:
                st.setText("✓  FPS limit updated. Shortcuts repaired; safe to reopen Steam.")
                return
            reason = errors[0] if errors else "unknown error"
            try:
                with cfg.transaction():
                    config = cfg.load()
                    if had_cap:
                        config["dxvk_frame_cap"] = old_value
                    else:
                        config.pop("dxvk_frame_cap", None)
                    cfg.save(config)
                self._refresh_cap_btn()
            except Exception as ex:
                st.setText(f"✗  Shortcut repair failed ({reason}); "
                                    f"could not restore FPS limit: {ex}")
                return
            st.setText(f"✗  Shortcut repair failed ({reason}). "
                                "FPS limit restored to its previous setting.")
        s.done.connect(_finish)
        self._start_shortcut_repair(s, errors, verify_cap=True)

    def _apply_deadzone(self):
        st = self._use_status("tweaks")
        s = _Sigs()
        if not self._settings_task(s):
            return
        st.setText("Applying controller stick deadzones...")
        s.log.connect(lambda msg: st.setText(msg))
        def _run():
            try:
                from game_config import set_deadzone
                steam_root = cfg.load().get("steam_root", "") or find_steam_root()
                if not steam_root:
                    raise RuntimeError("Steam library not found")
                installed = find_all_games(steam_root, preferred_sources=cfg.get_setup_games())
                failures = []
                count = set_deadzone(steam_root, installed,
                                     on_progress=lambda msg: _log.info("deadzone: %s", msg.strip()),
                                     failures=failures)
                if failures:
                    for path, err in failures:
                        _log.warning("deadzone: could not write %s: %s", path, err)
                    s.log.emit(_partial_write_msg(count, failures))
                else:
                    s.log.emit(f"✓  Deadzones set in {count} config file(s). "
                               "Games without supported text configs are not affected.")
            except Exception as ex:
                _log.error("deadzone update failed", exc_info=True)
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        self._start_task_thread(s, _run)

    def _apply_fps_fov(self):
        st = self._use_status("tweaks")
        s = _Sigs()
        if not self._settings_task(s):
            return
        st.setText("Setting 90 FPS and 90 FOV...")
        s.log.connect(lambda msg: st.setText(msg))
        def _run():
            try:
                from game_config import set_fps_fov
                from detect_games import find_all_games as _find_all
                steam_root = cfg.load().get("steam_root", "") or find_steam_root()
                installed = _find_all(steam_root, preferred_sources=cfg.get_setup_games()) if steam_root else {}
                failures = []
                count = set_fps_fov(steam_root, installed,
                                    on_progress=lambda msg: _log.info("fps/fov: %s", msg.strip()),
                                    failures=failures)
                if failures:
                    for path, err in failures:
                        _log.warning("fps/fov: could not write %s: %s", path, err)
                    s.log.emit(_partial_write_msg(count, failures))
                else:
                    s.log.emit(f"✓  Wrote 90 FOV and 90 FPS to {count} config file(s). "
                               "Close the games first if they are running. The MW2 and MW3 campaigns are not affected by this button.")
            except Exception as ex:
                _log.error("fps/fov failed", exc_info=True)
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        self._start_task_thread(s, _run)

    def _backup_saves_now(self):
        st = self._use_status("maintenance")
        s = _Sigs()
        if not self._settings_task(s):
            return
        st.setText("Backing up your saves...")
        s.log.connect(lambda msg: st.setText(msg))
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
        self._start_task_thread(s, _run)

    def _clear_shader_cache(self):
        st = self._use_status("maintenance")
        s = _Sigs()
        if not self._settings_task(s):
            return
        st.setText("Clearing shader cache...")
        s.log.connect(lambda msg: st.setText(msg))
        def _run():
            try:
                from cache_cleanup import cleanup_shader_cache, STEAM_APPIDS
                setup = cfg.get_setup_games()
                # One cache per Steam appid: WaW SP and MP are two keys but one game
                appids, failed = set(), False
                for key, appid in STEAM_APPIDS.items():
                    if key in setup:
                        source = setup[key].get("source", "steam")
                        if cleanup_shader_cache(key, source) is False:
                            failed = True
                        appids.add(appid)
                if failed:
                    s.log.emit(f"⚠  Shader cache cleared for {len(appids)} game(s), but some "
                               "folders could not be removed. Close Steam and try again.")
                else:
                    s.log.emit(f"✓  Shader cache cleared for {len(appids)} game(s).")
            except Exception as ex:
                s.log.emit(f"✗  Failed: {ex}")
            finally:
                s.done.emit(True)
        self._start_task_thread(s, _run)

    def _check_setup(self):
        """Run setup_check in the background, then show the report."""
        st = self._use_status("maintenance")
        s = _Sigs()
        if not self._settings_task(s):
            return
        st.setText("Checking your setup...")
        self._setup_results = None
        # Looked up here: the worker must not walk the widget stack from its thread
        mgmt = get_screen(self.stack, "ManagementScreen")
        known = dict(getattr(mgmt, "installed", None) or {})

        def _run():
            try:
                from setup_check import check_setup, check_shortcuts
                installed = known or find_all_games(preferred_sources=cfg.get_setup_games())
                sr, setup = find_steam_root(), cfg.get_setup_games()
                results = check_setup(sr, installed, setup)
                try:
                    sc = check_shortcuts(sr, setup)
                except Exception as ex:
                    _log.warning("shortcut check failed", exc_info=True)
                    sc = {"key": "shortcuts", "name": "DeckOps shortcuts", "fix": "shortcuts",
                          "problems": [f"Could not be checked: {ex}"], "fixable": False}
                if sc:
                    results.append(sc)
                self._setup_results = (results, installed)
                s.log.emit("Setup check finished.")
            except Exception as ex:
                _log.error("setup check failed", exc_info=True)
                s.log.emit(f"✗  Setup check failed: {ex}")
            finally:
                s.done.emit(True)

        def _show(msg):
            st.setText(msg)
            if self._setup_results:
                self._show_setup_report(*self._setup_results)
        s.log.connect(_show)
        self._start_task_thread(s, _run)

    def _show_setup_report(self, results, installed):
        self._setup_installed = installed
        dlg = QDialog(self)
        dlg.setWindowTitle("Check My Setup")
        dlg.resize(720, 480)
        vl = QVBoxLayout(dlg); vl.setContentsMargins(12,12,12,12); vl.setSpacing(8)
        bad = [r for r in results if r["problems"]]
        vl.addWidget(_lbl("No games are set up yet." if not results else
                          "Everything looks fine." if not bad else
                          f"{len(bad)} of {len(results)} item(s) need attention.",
                          13, C_TEXT, bold=True, align=Qt.AlignmentFlag.AlignLeft))
        scroll = _no_focus_scroll(QScrollArea()); scroll.setWidgetResizable(True)
        inner = QWidget(); rows = QVBoxLayout(inner); rows.setSpacing(8)
        fixes = []
        for r in results:
            row = QHBoxLayout()
            text = _plain_marks(f"✓  {r['name']}") if not r["problems"] else \
                _plain_marks(f"⚠  {r['name']}") + "\n" + "\n".join("     • " + p for p in r["problems"])
            row.addWidget(_lbl(text, FONT_BODY, C_DIM if not r["problems"] else C_TREY,
                               align=Qt.AlignmentFlag.AlignLeft), stretch=1)
            if r["fixable"]:
                fix = _btn("Fix", C_IW, size=FONT_ACTION, h=BTN_MIN_H); fix.setFixedWidth(90)
                fix.setAccessibleName(f"Fix {r['name']}")
                if r.get("fix") == "shortcuts":      # Repair Shortcuts, not a game's Update
                    fix.clicked.connect(lambda _c=False: (dlg.accept(), self._repair_shortcuts()))
                else:
                    fix.clicked.connect(lambda _c=False, k=r["key"]: (dlg.accept(), self._fix_game(k)))
                row.addWidget(fix, 0, Qt.AlignmentFlag.AlignTop)
                fixes.append(fix)
            rows.addLayout(row)
        rows.addStretch()
        scroll.setWidget(inner)
        vl.addWidget(scroll, stretch=1)
        close = _btn("Close", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H); close.setFixedWidth(130)
        close.clicked.connect(dlg.reject)
        br = QHBoxLayout(); br.addStretch(); br.addWidget(close); br.addStretch()
        vl.addLayout(br)
        _chain_focus(fixes + [close])
        # Open on the first action (a Fix, else Close), not on the scroll area
        (fixes[0] if fixes else close).setFocus(Qt.FocusReason.OtherFocusReason)
        self._setup_dialog = dlg
        dlg.exec()

    def _fix_game(self, key):
        """Fix = the game's normal Update (reinstalls the client, GE-Proton,
        launch options; closes Steam itself)."""
        st = self._use_status("maintenance")
        gd = next((g for g in ALL_GAMES if key in g.get("keys", [])), None)
        mgmt = get_screen(self.stack, "ManagementScreen")
        if not mgmt.installed:          # the Library hasn't scanned yet: use the check's scan
            mgmt.installed = dict(getattr(self, "_setup_installed", None) or {})
        if not gd or not mgmt.installed.get(key):
            st.setText("That game could not be found; open the Game Library and try again.")
            return
        # _update refuses (busy, or nothing it can update) on the Library's status
        # line, which is not on screen here: Fix used to do nothing and say nothing.
        if mgmt._update(gd, [key]) is False:
            st.setText(mgmt._status.text() or "Update has nothing to repair for this game.")

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
        txt.setStyleSheet(
            f"QPlainTextEdit{{color:{C_TEXT};background:{C_SURFACE};border:1px solid {C_LINE};"
            "border-radius:4px;padding:6px;}"
            f"QPlainTextEdit:focus{{border:2px solid {C_FOCUS};}}"
            f"QPlainTextEdit:disabled{{background:{C_CARD};color:{C_DIM};border-color:{C_LINE};}}")
        txt.setPlainText(tail)
        vl.addWidget(txt, stretch=1)
        br = QHBoxLayout(); br.setSpacing(12)
        # The confirmation shows here, in the dialog, not behind it on Settings
        copied = _StatusLabel(FONT_BODY, C_DIM)
        copied.setObjectName("viewLogStatus")
        copy_btn = _btn("Copy to Clipboard", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        copy_btn.clicked.connect(lambda: (
            QApplication.clipboard().setText(tail),
            copied.setText("Log copied to clipboard."),
        ))
        close = _btn("Close", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H); close.setFixedWidth(130)
        close.clicked.connect(dlg.reject)
        br.addWidget(copied, 1, Qt.AlignmentFlag.AlignVCenter)
        br.addWidget(copy_btn); br.addWidget(close)
        vl.addLayout(br)
        txt.verticalScrollBar().setValue(txt.verticalScrollBar().maximum())
        _chain_focus([copy_btn, close, txt])
        copy_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        self._log_dialog = dlg
        dlg.exec()

    def _open_url(self, url):
        _detached_open(["xdg-open", url])

    def _confirm_uninstall(self):
        self._use_status("danger")
        reply = QMessageBox.warning(
            self, "Full Uninstall",
            "This removes DeckOps's files, shortcuts, controller layouts and mod client "
            "installs. Your Steam games are not deleted, and your save backups are kept.\n\n"
            "DeckOps will close and the uninstaller will run in a terminal window.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._run_uninstaller()

    def _run_uninstaller(self):
        st = self._use_status("danger")
        uninstall_script = os.path.join(PROJECT_ROOT, "deckops_uninstall.sh")
        if not os.path.exists(uninstall_script):
            st.setText("✗  Uninstall script not found.")
            return
        # quit() skips the window's "stop it part way?" question, so check here: an
        # uninstall during an install or a Steam change would cut that off mid-write
        busy = inhibit.busy()
        if busy:
            st.setText(f"✗  Wait for this to finish first: {str(busy).rstrip('.')}.")
            return
        st.setText("Launching uninstaller...")
        # Konsole first so the user can see progress; xterm if it isn't there. Only the
        # launch is in the try: an error from quitting (after Konsole started) used to
        # start a second uninstaller in xterm.
        error = None
        for terminal in ("konsole", "xterm"):
            try:
                subprocess.Popen([terminal, "-e", "bash", uninstall_script], start_new_session=True)
                error = None
                break
            except Exception as ex:
                error = ex
        if error is not None:
            st.setText(f"✗  Could not launch uninstaller: {error}")
            return
        # Close DeckOps — the uninstaller will remove our files
        _kill_audio()
        QApplication.instance().quit()

    # ── Update check ────────────────────────────────────────────────────────
    _GITHUB_USER = GITHUB_USER
    _GITHUB_REPO = GITHUB_REPO

    def _init_update_signals(self):
        """Connect the update result signal (call once during __init__)."""
        self._update_sig = _Sigs()
        self._update_sig.log.connect(self._on_update_signal)

    def _check_for_updates(self):
        """Hit GitHub API to compare local SHA against remote HEAD."""
        if getattr(self, "_checking_update", False):
            return
        self._checking_update = True
        self._init_update_signals()
        self._update_btn.setEnabled(False)
        self._update_status.setText("Checking...")
        self._update_status.setStyleSheet(f"color:{C_DIM};")
        try:
            threading.Thread(target=self._update_worker, daemon=True).start()
        except Exception as ex:
            _log.warning("update check could not start", exc_info=True)
            self._on_update_signal(f"FAIL|Update check failed: {ex}")

    def _update_worker(self):
        self._update_sig.log.emit(_check_update_status())

    def _on_update_signal(self, msg):
        """Slot that runs on the main thread via pyqtSignal."""
        self._checking_update = False
        kind, text = msg.split("|", 1)
        self._update_result(text, kind == "UPDATE")

    def _update_result(self, msg, update_available):
        self._update_btn.setEnabled(True)
        self._update_status.setText(msg)
        try:
            self._update_btn.clicked.disconnect()
        except TypeError:
            pass
        if update_available:
            self._update_status.setStyleSheet(f"color:{C_IW};")
            self._update_btn.setText("Update && Restart")
            self._update_btn.setAccessibleName("Update & Restart")
            self._update_btn.clicked.connect(self._apply_update)
        else:
            # A failed/offline re-check must not leave the button applying an
            # update that an earlier check advertised but this one could not verify.
            self._update_status.setStyleSheet(f"color:{C_DIM};")
            self._update_btn.setText("Check for Updates")
            self._update_btn.setAccessibleName("Check for Updates")
            self._update_btn.clicked.connect(self._check_for_updates)

    def _apply_update(self):
        # A second click (or the Library's prompt) while one runs would replace the
        # progress dialog and start a second worker
        if getattr(self, "_updating", False):
            return
        # The update ends in os.execv: a task still running here (closing Steam, rewriting
        # shortcuts.vdf) would be killed mid-write. The uninstaller and Reset guard the same way.
        busy = inhibit.busy()
        if busy:
            self._update_status.setText(f"Wait for this to finish first: {str(busy).rstrip('.')}.")
            self._update_status.setStyleSheet(f"color:{C_TREY};")
            return
        reply = QMessageBox.question(
            self, "Update DeckOps",
            "DeckOps will download the update and relaunch.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Something may have started while the question was open
        busy = inhibit.busy()
        if busy or getattr(self, "_updating", False):
            if busy:
                self._update_status.setText(f"Wait for this to finish first: {str(busy).rstrip('.')}.")
                self._update_status.setStyleSheet(f"color:{C_TREY};")
            return

        # The relaunch (os.execv) ends every running thread: hold the busy lock so
        # Set Up / Update / Settings tasks refuse to start meanwhile, and the
        # window asks before closing
        self._updating = True
        inhibit.start("DeckOps is updating itself")

        self._update_btn.setEnabled(False)
        self._update_btn.setText("Updating...")
        self._update_btn.setAccessibleName("Updating")
        self._update_status.setText("Downloading update...")
        self._update_status.setStyleSheet(f"color:{C_DIM};")

        from qt import QProgressDialog
        pd = self._update_pd = QProgressDialog("Checking files...", None, 0, 100, self)
        pd.setWindowTitle("Update DeckOps")
        pd.setWindowModality(Qt.WindowModality.ApplicationModal)
        pd.setMinimumDuration(0); pd.setAutoClose(False); pd.setAutoReset(False)
        pd.setCancelButton(None); pd.show()

        self._apply_sig = _Sigs()
        self._apply_sig.progress.connect(lambda p, m: (pd.setValue(p), pd.setLabelText(m)))
        self._apply_sig.log.connect(self._on_apply_signal)
        try:
            threading.Thread(target=self._apply_worker, daemon=True).start()
        except Exception as ex:
            self._on_apply_signal(f"FAIL|Could not start DeckOps update: {ex}")

    def _end_update(self):
        """The update stopped without relaunching: release the busy lock."""
        if getattr(self, "_updating", False):
            self._updating = False
            inhibit.stop()

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
        except Exception as ex:
            # Anything else (an ImportError in the worker's imports, say) used to kill
            # the thread silently and leave the modal progress dialog up forever
            _log.error("DeckOps update worker failed", exc_info=True)
            self._apply_sig.log.emit(f"FAIL|DeckOps update failed: {ex}")

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
            self._end_update()
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
            self._end_update()
            self._update_btn.setEnabled(True)
            self._update_btn.setText("Check for Updates")
            self._update_btn.setAccessibleName("Check for Updates")
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
        # execv replaces this process without running any cleanup: release the
        # sleep inhibitor (and the busy flag) now, or the relaunched DeckOps
        # starts with a leaked inhibitor nothing will ever stop.
        self._end_update()
        try:
            os.execv(python, [python, entry])
        except OSError as ex:
            # An uncaught error here left the modal "Update complete" progress
            # dialog on screen for good, with the window behind it unusable.
            _log.error("Relaunch failed", exc_info=True)
            pd = getattr(self, "_update_pd", None)
            if pd is not None:
                pd.close()
            self._end_update()
            self._update_btn.setText("Restart needed")     # stays disabled: nothing left to apply
            self._update_btn.setAccessibleName("Restart needed")
            self._update_status.setText(
                f"Update installed, but DeckOps could not restart itself ({ex}). "
                "Close DeckOps and open it again.")
            self._update_status.setStyleSheet(f"color:{C_TREY};")

    def _confirm_reset(self):
        st = self._use_status("danger")
        # Like the uninstall: a running task (or install) still writes deckops.json,
        # which would bring back half a config right after the wipe.
        busy = inhibit.busy()
        if busy:
            st.setText(f"✗  Wait for this to finish first: {str(busy).rstrip('.')}.")
            return
        reply = QMessageBox.warning(
            self, "Reset Config",
            "This clears the DeckOps settings file (deckops.json). "
            "DeckOps will restart and run setup again.\n\n"
            "Your game files and Steam games are not affected.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._reset_deckops()

    def _reset_deckops(self):
        st = self._use_status("danger")
        try:
            cfg.reset()
        except Exception as ex:
            _log.error("config reset failed", exc_info=True)
            st.setText(f"✗  Could not reset config: {ex}")
            return
        # The screens keep what the old setup chose and found; setup starts clean
        for name in ("SetupScreen",):
            screen = get_screen(self.stack, name)
            if screen is not None and hasattr(screen, "reset_state"):
                screen.reset_state()
        library = get_screen(self.stack, "ManagementScreen")
        if library is not None:
            library.installed = {}
        self._reload_saved_settings()
        st.setText("Config wiped. Restarting setup...")
        QTimer.singleShot(1500, lambda: go_to(self.stack, "BootstrapScreen"))



# ── UpdateScreen ──────────────────────────────────────────────────────────────
class UpdateScreen(QWidget):
    """Handles Update from ManagementScreen."""

    def __init__(self, stack):
        super().__init__(); self.stack = stack; self.screen_name = "UpdateScreen"
        self.selected   = []
        self.steam_root = ""
        self.all_plut_games = {}
        self._stop_requested = threading.Event()
        self._did_work = False
        # (title, fn(sigs)) run in place of the game updates, e.g. adding Offline Mode
        self.task = None
        self._steam_closed = threading.Event()
        self._failed_games = []
        self._pending_games = []

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        hdr, hl = _header_bar()
        hl.addWidget(_lbl(screen_title("UpdateScreen").upper(), 12, C_IW, bold=True, wrap=False))
        lay.addWidget(hdr)

        content = QWidget(); body = QVBoxLayout(content)
        body.setContentsMargins(20,12,20,12); body.setSpacing(8)
        self.title = _lbl("GAME UPDATES", 18, C_TEXT, bold=True,
                          align=Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self.title)
        self.cur = _StatusLabel(FONT_ACTION, C_TEXT)
        self.cur.setText("Preparing...")
        self.cur.setAccessibleName("Current update status")
        body.addWidget(self.cur)
        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        self.bar.setTextVisible(True); self.bar.setFormat("%p% progress")
        self.bar.setAccessibleName("Game update progress")
        self.bar.setFixedHeight(22)
        body.addWidget(self.bar)

        # Label, copy result and button share one vertically centred row
        lr = QHBoxLayout(); lr.setSpacing(8)
        center = Qt.AlignmentFlag.AlignVCenter
        self._log_heading = _lbl("OPERATION LOG", 10, C_DIM, bold=True,
                                 align=Qt.AlignmentFlag.AlignLeft | center, wrap=False)
        lr.addWidget(self._log_heading, 1, center)
        self._log_status = _StatusLabel(10, C_DIM, Qt.AlignmentFlag.AlignRight | center)
        lr.addWidget(self._log_status, 2, center)
        log_btn = _btn("Copy Log", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        log_btn.clicked.connect(lambda: _copy_log_to_clipboard(self._log_status))
        self._copy_log_btn = log_btn
        lr.addWidget(log_btn, 0, center)
        body.addLayout(lr)

        self.log = QPlainTextEdit(); self.log.setReadOnly(True)
        self.log.setFont(font(10))
        self.log.setMinimumHeight(100)
        self.log.setAccessibleName("Game update and repair log")
        self.log.setStyleSheet(
            f"QPlainTextEdit{{color:{C_TEXT};background:{C_CARD};border:1px solid {C_LINE};"
            "border-radius:7px;padding:8px;}"
            f"QPlainTextEdit:focus{{border:2px solid {C_FOCUS};}}"
            f"QPlainTextEdit:disabled{{background:{C_SURFACE};color:{C_DIM};border-color:{C_LINE};}}")
        body.addWidget(self.log, stretch=1)

        # Keep a footer slot reserved while work runs. Waiting, retry and Back
        # occupy the same space rather than pushing the log off a short screen.
        footer = QWidget(); footer.setFixedHeight(52)
        self._footer = footer
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(0,4,0,0); actions.setSpacing(8)
        self.steam_btn = _btn("I've closed Plutonium", C_TREY, size=FONT_ACTION, h=BTN_MIN_H)
        self.steam_btn.setVisible(False)
        self.steam_btn.setMaximumWidth(280)
        self.steam_btn.clicked.connect(lambda: self._steam_closed.set())
        self.back_btn = _btn(LABELS["back_to_library"], C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        self.back_btn.setMaximumWidth(260)
        self.back_btn.setVisible(False)
        self.back_btn.clicked.connect(self._go_back)
        self.retry_btn = _btn("Retry Failed Updates", C_IW, size=FONT_ACTION, h=BTN_MIN_H)
        self.retry_btn.setMaximumWidth(260)
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self._retry_failed)
        self.stop_btn = _btn("Stop After Current Step", C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self._request_stop)
        actions.addStretch()
        actions.addWidget(self.steam_btn)
        actions.addWidget(self.stop_btn)
        actions.addWidget(self.back_btn)
        actions.addWidget(self.retry_btn)
        actions.addStretch()
        body.addWidget(footer)
        lay.addWidget(content, stretch=1)
        # The footer actions first, then the log controls
        _chain_focus([self.steam_btn, self.stop_btn, self.retry_btn, self.back_btn, log_btn, self.log])
        self._running = False

        self._s = _Sigs()
        self._s.progress.connect(self._on_progress)
        self._s.log.connect(self._append_log)
        self._s.done.connect(self._on_done)
        self._s.plut_wait.connect(self._wait_for_plutonium)
        self._s.plut_go.connect(self._plutonium_closed)
        self._s.ask.connect(self._ask_continue)
        self._ask_event = threading.Event()
        self._ask_yes = False
        self._cancelled = False

    # ── controller protocol (see ui_constants) ─────────────────────────────
    def focus_target(self):
        """The action that matters now: the Plutonium confirmation, Retry,
        Back, or Stop while the update runs."""
        for b in (self.steam_btn, self.retry_btn, self.back_btn, self.stop_btn):
            if not b.isHidden() and b.isEnabled():
                return b
        return None

    def escape_allowed(self):
        return not self._running

    def back_action(self):
        if not self._running:
            self._go_back()

    def _land_focus(self):
        if self.isVisible():
            _focus_first([self.steam_btn, self.retry_btn, self.back_btn, self.stop_btn])

    def _on_progress(self, percent, message):
        # Steps (and some installers) can report 100 independently. Only the
        # successful final result may make the whole update 100% complete.
        self.bar.setValue(max(self.bar.value(), min(99, max(0, percent))))
        self.cur.setText(f"Stopping after current step — {message}" if self._stop_requested.is_set()
                         else message)

    def _request_stop(self):
        self._stop_requested.set()
        self._steam_closed.set()  # also release the Plutonium confirmation wait
        self.stop_btn.setEnabled(False)
        self.cur.setText("Stopping after the current step; completed changes will be kept...")

    def _wait_for_plutonium(self):
        if self._stop_requested.is_set():
            return
        self.steam_btn.setVisible(True)
        self.cur.setText("Close the Plutonium window, then press the button below to continue.")
        self.cur.setStyleSheet(f"color:{C_TREY};background:transparent;")
        self._land_focus()

    def _plutonium_closed(self):
        self.steam_btn.setVisible(False)
        self._land_focus()
        if self._stop_requested.is_set():
            return
        self.cur.setText("Checking Plutonium and finishing game updates...")
        self.cur.setStyleSheet(f"color:{C_TEXT};background:transparent;")

    def _ask_continue(self, text):
        """UI thread: answer a worker's Yes/No question (see _confirm)."""
        try:
            self._ask_yes = QMessageBox.question(
                self, "Save Backup", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
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
        self._s.log.emit("Update cancelled before changing game files or settings.")
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
        if getattr(self, "_running", False):
            return
        self.bar.setValue(0); self.bar.setFormat("%p% progress"); self.log.clear()
        self.cur.setText("Preparing...")
        self.cur.setStyleSheet(f"color:{C_TEXT};background:transparent;")
        self._log_status.setText("")
        self.title.setText(self.task[0] if self.task else "GAME UPDATES")
        self.steam_btn.setVisible(False); self.back_btn.setVisible(False)
        self.retry_btn.setVisible(False)
        self.stop_btn.setVisible(self.task is None)
        self.stop_btn.setEnabled(True)
        self._stop_requested.clear()
        self._did_work = False
        self._failed_games = []
        self._pending_games = []
        self._cancelled = False
        self._steam_closed.clear()
        # Like the Settings tasks: don't take over (and later release) a lock another
        # running operation holds, or this screen finishing would let the machine
        # sleep in the middle of that one.
        self._took_inhibit = not inhibit.busy()
        if self._took_inhibit:
            inhibit.start("DeckOps is updating")
        _log_to_file("── Update started ──")
        self._running = True
        self._land_focus()
        QTimer.singleShot(400, self._start_run_thread)

    def _start_run_thread(self):
        try:
            threading.Thread(target=self._run, daemon=True).start()
        except Exception as ex:
            _log.error("update worker could not start", exc_info=True)
            self._failed_games = list(self.selected)
            self._s.log.emit(f"✗  Could not start update: {ex}")
            self._s.done.emit(False)

    def _on_done(self, ok):
        self._running = False
        try:
            self._show_result(ok)
        finally:
            self._land_focus()

    def _show_result(self, ok):
        if getattr(self, "_took_inhibit", True):
            inhibit.stop()
        self._took_inhibit = False
        self.steam_btn.setVisible(False)
        self.stop_btn.setVisible(False)
        self.back_btn.setVisible(True)
        if ok:
            self.bar.setValue(100)
            self.bar.setFormat("%p% complete")
            self.cur.setText("Complete. It is now safe to open Steam or return to the Game Library.")
            self.cur.setStyleSheet(f"color:{C_IW};background:transparent;")
            return
        self.bar.setFormat("%p% reached")
        self.cur.setStyleSheet(f"color:{C_TREY};background:transparent;")
        self.retry_btn.setVisible(bool(self._failed_games))
        if self._cancelled:
            if not self._did_work:
                self.cur.setText("Update cancelled before changing game files or settings.")
            elif self._failed_games:
                self.cur.setText("Stopped after the current step. Completed changes were kept; retry remaining work.")
            else:
                self.cur.setText("Stopped after the current step. Completed changes were kept.")
        elif self._failed_games:
            names = ", ".join(dict.fromkeys(gd["base"] for _, gd, _ in self._failed_games))
            self.cur.setText(f"Update incomplete: {names}. Retry failed modes or check the log.")
        else:
            self.cur.setText("Update failed. Check the log, then return to the Game Library to try again.")

    def _retry_failed(self):
        retry_games = list(self._failed_games)
        if not retry_games:
            return
        self.selected = retry_games
        self._start_update()

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
            for item in self._pending_games or (self.selected if not self._failed_games else []):
                if item not in self._failed_games:
                    self._failed_games.append(item)
            self._pending_games = []
            self._restore_failed_launch_options()
            self._s.log.emit(f"✗  Update failed: {ex}")
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

    def _apply_cod4_launch_option(self, succeeded):
        """CoD4 MP and SP share appid 7940 and so one launch option. The
        installers only write their own part (IW3SP the exe swap, CoD4R
        nothing), so rebuild the combined option -- CoD4R pre-launch update
        plus IW3SP swap -- once they are done, as InstallScreen does.
        Otherwise Check My Setup keeps reporting both modes after Fix."""
        cod4 = [item for item in succeeded
                if item[0] in ("cod4mp", "cod4sp")
                and (item[2] or {}).get("source", "steam") != "own"]
        if not cod4:
            return
        failed = False
        try:
            from wrapper import clear_launch_options
            from iw3sp import apply_cod4_launch_option, cod4_launch_option, COD4_APPID
            # set_launch_options appends: drop the partial option first
            clear_launch_options(self.steam_root, COD4_APPID)
            if apply_cod4_launch_option(self.steam_root):
                self._s.log.emit("✓  CoD4 launch option set")
            elif cod4_launch_option(cfg.get_setup_games()):
                # Needed, but written to no Steam account
                self._s.log.emit("✗  CoD4 launch option not set — launch the game once "
                                 "from Steam, then run Set Up again")
                failed = True
        except Exception as ex:
            _log.warning("CoD4 launch option not set", exc_info=True)
            self._s.log.emit(f"✗  Could not set the CoD4 launch option: {ex}")
            failed = True
        if failed:
            for item in cod4:
                if item not in self._failed_games:
                    self._failed_games.append(item)

    def _ensure_missing_prefixes(self, ge_version, proton, setup_games):
        """Recreate the prefix of each selected Steam game whose prefix is
        missing (Check My Setup's "prefix is missing"). Intact prefixes are
        never touched; a failure is logged and the client update goes on."""
        try:
            from setup_check import uses_steam_prefix, prefix_path, prefix_problems, PREFIX_MISSING
            targets = []
            for key, gd, game in self.selected:
                if not game:
                    continue
                entry = setup_games.get(key, {})
                c = entry.get("client") or KEY_CLIENT.get(key, gd.get("client", ""))
                if not uses_steam_prefix(key, c, entry.get("source", "steam")):
                    continue
                compat = prefix_path(self.steam_root, key, c, game)
                if compat and prefix_problems(compat) == [PREFIX_MISSING]:
                    targets.append((key, compat))
            if not targets:
                return
            from ge_proton import ensure_all_prefix_deps
            self._s.log.emit(f"Recreating {len(targets)} missing Proton prefix(es)...")
            done = ensure_all_prefix_deps(ge_version, targets,
                                          on_progress=self._s.log.emit,
                                          proton_path=proton, steam_root=self.steam_root)
            self._s.log.emit(f"✓  Prefixes recreated: {done}/{len(targets)}")
        except Exception as ex:
            _log.warning("missing prefix setup failed", exc_info=True)
            self._s.log.emit(f"  Missing prefixes not recreated: {ex}")

    def _run_steps(self):
        self._lo_snapshot = None
        task, self.task = self.task, None
        if task:
            try:
                task[1](self._s)
            except Exception as ex:
                self._s.log.emit(f"✗  {task[0].title()} failed: {ex}")
                self._s.done.emit(False)
                return
            self._s.progress.emit(100, "All done!")
            self._s.done.emit(True)
            return
        if not self._backup_first():
            return
        if self._stop_requested.is_set():
            self._cancelled = True
            self._s.done.emit(False)
            return
        from wrapper import get_proton_path, find_compatdata, kill_steam, set_compat_tool
        from ge_proton import install_ge_proton, resolve_ge_version
        from iw4x import install_iw4x
        from cod4r import install_cod4r
        from iw3sp import install_iw3sp
        from t6sp_mod import install_t6sp_mod
        from net import DownloadError

        is_lcd = cfg.is_lcd()

        if is_lcd:
            from plutonium_lcd import (launch_bootstrapper_lcd,
                                       is_plutonium_ready_lcd)
            # install_plutonium sends LCD to the LCD installer itself, after setting up
            # Offline Mode first; calling the LCD installer directly skipped that, so an
            # update never refreshed the offline launcher or the bot-match files.
            from plutonium import install_plutonium
        else:
            from plutonium import (install_plutonium,
                                        launch_bootstrapper,
                                        is_plutonium_ready)

        setup_games = cfg.get_setup_games()
        has_plut = any((setup_games.get(k, {}).get("client") or KEY_CLIENT.get(k)) == "plutonium"
                       for k, _, _ in self.selected)
        proton = get_proton_path(self.steam_root)
        total = len(self.selected)
        if not total:
            self._s.log.emit("No configured game modes selected.")
            self._s.done.emit(False)
            return

        if self._stop_requested.is_set():
            self._cancelled = True
            self._s.done.emit(False)
            return

        # ── Plutonium update: re-run bootstrapper ─────────────────────
        if has_plut:
            self._did_work = True
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
                        on_progress=lambda p, m: self._s.progress.emit(5 + min(10, p // 10), m),
                    )
                else:
                    launch_bootstrapper(
                        proton,
                        on_progress=lambda p, m: self._s.progress.emit(5 + min(10, p // 10), m),
                        steam_root=self.steam_root,
                    )
            except DownloadError as dl_ex:
                self._s.log.emit(f"✗  {dl_ex.label} download failed: {dl_ex}")
                self._failed_games = list(self.selected)
                self._s.done.emit(False)
                return
            except Exception as ex:
                self._s.log.emit(f"✗  Plutonium launch failed: {ex}")
                self._failed_games = list(self.selected)
                self._s.done.emit(False)
                return

            self._s.plut_wait.emit()
            self._steam_closed.wait()
            self._steam_closed.clear()
            self._s.plut_go.emit()
            if self._stop_requested.is_set():
                self._cancelled = True
                self._failed_games = list(self.selected)
                self._s.done.emit(False)
                return

            _ready = is_plutonium_ready_lcd() if is_lcd else is_plutonium_ready()
            if not _ready:
                self._s.log.emit(
                    "✗  Plutonium does not appear to be fully set up.\n"
                    "   Make sure you let it finish updating."
                )
                self._failed_games = list(self.selected)
                self._s.done.emit(False)
                return

            self._s.log.emit("✓  Plutonium updated.")
            # Keep the legacy shared copy: an omitted or failed prefix can
            # still link to it. Removing it here breaks otherwise playable games.

        if self._stop_requested.is_set():
            self._cancelled = True
            self._failed_games = list(self.selected)
            self._s.done.emit(False)
            return

        # ── GE-Proton download (Steam may still be running) ─────────
        ge_version = None
        ge_fresh = False
        try:
            self._s.progress.emit(16, "Checking GE-Proton...")
            self._s.log.emit("Checking GE-Proton...")
            self._did_work = True
            ge_version = install_ge_proton(
                on_progress=lambda pct, msg: self._s.progress.emit(16 + int(pct * 0.08), msg),
                steam_root=self.steam_root,
            )
            cfg.set_ge_proton_version(ge_version)
            ge_fresh = True
            self._s.log.emit(f"✓  {ge_version} ready")
        except Exception as ex:
            self._s.log.emit(f"  GE-Proton update skipped: {ex}")
        # A failed download must not leave the games without a compat tool:
        # keep using the GE-Proton that is already installed.
        if not ge_version:
            ge_version = resolve_ge_version(steam_root=self.steam_root)
            if ge_version:
                self._s.log.emit(f"  Using the installed {ge_version}")
            else:
                self._s.log.emit("✗  No GE-Proton is installed; Steam's compatibility settings were left unchanged.")

        # A Steam game whose prefix is gone gets the same GE-Proton preload as
        # a first install, before its client writes into the prefix.
        if ge_version:
            self._ensure_missing_prefixes(ge_version, proton, setup_games)

        if self._stop_requested.is_set():
            self._cancelled = True
            self._failed_games = list(self.selected)
            self._s.done.emit(False)
            return

        # Always close Steam before the config writes below (launch options,
        # compat tools, Steam Input, shortcuts): Steam rewrites config.vdf and
        # localconfig.vdf when it exits, so anything written while it runs is
        # lost. This used to be skipped whenever a Plutonium game was in the
        # update -- the button only waits for the Plutonium window, not Steam.
        # Same order as InstallScreen: Plutonium step first, then close Steam.
        self._s.progress.emit(25, "Closing Steam...")
        self._s.log.emit("Closing Steam...")
        try:
            kill_steam(on_progress=lambda msg: self._s.log.emit(f"  {msg}"))
            self._s.log.emit("  ✓ Steam closed.")
        except Exception as ex:
            self._s.log.emit(f"✗  Could not close Steam: {ex}")
            raise RuntimeError("Close Steam manually, then retry this update; game configuration was not continued.") from ex
        if self._stop_requested.is_set():
            self._cancelled = True
            self._failed_games = list(self.selected)
            self._s.done.emit(False)
            return

        # Never erase an option without a usable snapshot. A failed game (or
        # an unfinished mode after Stop) can then recover its previous value.
        from wrapper import clear_launch_options, snapshot_launch_options
        from detect_games import GAMES
        run_appids = {GAMES[k]["appid"] for k, _, g in self.selected
                      if k in GAMES and (g or {}).get("source") != "own"}
        try:
            self._lo_snapshot = snapshot_launch_options(self.steam_root, run_appids)
        except Exception as ex:
            raise RuntimeError(f"Could not save Steam launch options; no game settings were cleared: {ex}") from ex
        try:
            for appid in run_appids:
                clear_launch_options(self.steam_root, appid)
            self._s.log.emit("✓  Prepared launch options for selected games")
            self._pending_games = list(self.selected)
        except Exception:
            self._failed_games = list(self.selected)
            self._restore_failed_launch_options()
            raise

        # Retain sibling paths for a retry without reinstalling successful siblings.
        installed_for_plut = self.all_plut_games or {k: g for k, _, g in self.selected if g}
        succeeded = []
        for idx, (key, gd, game) in enumerate(self.selected):
            if self._stop_requested.is_set():
                self._cancelled = True
                self._failed_games.extend(self.selected[idx:])
                self._pending_games = []
                self._s.log.emit(f"Stopped safely. {total - idx} mode(s) remain; retry to finish.")
                break
            if not game:
                self._failed_games.append((key, gd, game))
                self._pending_games.pop(0)
                continue
            base_name = gd["base"]
            bp = 30 + int(idx / total * 60)
            self._s.progress.emit(bp, f"Updating {base_name} ({KEY_MODE_LABEL.get(key, key)})...")
            def op(pct, msg, _b=bp):
                self._s.progress.emit(_b + int(max(0, min(100, pct)) / 100 * (60 / total)), msg)
            entry = setup_games.get(key, {})
            c = entry.get("client") or KEY_CLIENT.get(key, gd.get("client", ""))
            source = entry.get("source", "steam")
            if not entry or source != game.get("source", "steam"):
                self._failed_games.append((key, gd, game))
                self._s.log.emit(f"✗  {base_name} ({key}): setup source changed; no files were touched")
                self._pending_games.pop(0)
                continue
            self._did_work = True
            try:
                from plutonium import GAME_META as _PLUT_META
                if c == "plutonium" and key in _PLUT_META:
                    _appid = _PLUT_META[key][0]
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
                if source == "own" and c in ("cod4r", "iw3sp"):
                    from shortcut import own_shortcut_appid
                    from steam_common import nvme_compatdata
                    cod4_appid = game.get("shortcut_appid") or own_shortcut_appid(key, game)
                    compat = game.get("compatdata_path") or nvme_compatdata(cod4_appid)

                if c == "cod4r":
                    install_cod4r(game, self.steam_root, proton, compat, op,
                                  appid=cod4_appid, source=source)
                elif c == "iw3sp":
                    install_iw3sp(game, self.steam_root, proton, compat, op,
                                  source=source)
                elif c == "iw4x":
                    install_iw4x(game, self.steam_root, proton, compat, op,
                                 source=source, install_dlc=False)
                elif c == "plutonium":
                    install_plutonium(game, key, self.steam_root, proton, compat,
                                      on_progress=op,
                                      installed_games=installed_for_plut,
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
                else:
                    raise RuntimeError(f"Unsupported configured client: {c}")
                self._s.log.emit(f"✓  {base_name} ({key}) done")
                # Installs that mark themselves (Plutonium) just saved fresh paths
                entry = cfg.get_setup_games().get(key, entry)
                cfg.mark_game_setup(key, c, source=source,
                                    wrapper_path=entry.get("wrapper_path"),
                                    lan_wrapper_path=entry.get("lan_wrapper_path"))
                succeeded.append((key, gd, game))
            except Exception as ex:
                self._failed_games.append((key, gd, game))
                self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")
            self._pending_games.pop(0)

        self._restore_failed_launch_options()
        self._apply_cod4_launch_option(succeeded)

        # Touch only appids whose update succeeded. Never replace the custom
        # Proton choice of an unrelated or failed game.
        successful_steam = [(k, gd, g) for k, gd, g in succeeded
                            if g.get("source", "steam") != "own" and k in GAMES]
        success_appids = {GAMES[k]["appid"] for k, _, _ in successful_steam}
        if ge_version and success_appids:
            try:
                if set_compat_tool(success_appids, ge_version, steam_root=self.steam_root) is False:
                    raise RuntimeError("Steam rejected the compatibility mapping")
                self._s.log.emit(f"✓  {ge_version} set for updated Steam appids")
            except Exception as ex:
                self._s.log.emit(f"✗  Could not set compatibility for updated games: {ex}")
                for item in successful_steam:
                    if item not in self._failed_games:
                        self._failed_games.append(item)
        if success_appids:
            try:
                from wrapper import set_steam_input_enabled
                set_steam_input_enabled(self.steam_root, success_appids)
                self._s.log.emit("✓  Steam Input enabled for updated games")
            except Exception as ex:
                self._s.log.emit(f"✗  Steam Input setup failed: {ex}")
                for item in successful_steam:
                    if item not in self._failed_games:
                        self._failed_games.append(item)

        # Old GE-Protons nothing uses any more, then the prefixes: shared
        # DLLs from this GE-Proton, links into removed versions repointed.
        if ge_version and succeeded and not self._cancelled:
            from ge_proton import cleanup_old_ge_versions, repair_prefixes
            if ge_fresh:
                try:
                    cleanup_old_ge_versions(ge_version, self.steam_root,
                                            on_progress=self._s.log.emit)
                except Exception as ex:
                    _log.warning("GE-Proton cleanup failed", exc_info=True)
                    self._s.log.emit(f"  Old GE-Proton cleanup skipped: {ex}")
            try:
                if not repair_prefixes(ge_version, self.steam_root,
                                       on_progress=self._s.log.emit):
                    self._s.log.emit("✗  Shared prefix DLLs could not be updated")
            except Exception as ex:
                _log.warning("prefix repair failed", exc_info=True)
                self._s.log.emit(f"  Prefix check skipped: {ex}")

        if self._cancelled:
            self._s.done.emit(False)
            return

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

        if self._stop_requested.is_set():
            self._cancelled = True
            self._s.done.emit(False)
            return

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
        self._s.progress.emit(98, "Finishing update..." if not failed else "Update incomplete; see log.")
        self._s.done.emit(not failed)
