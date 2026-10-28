#!/usr/bin/env python3
"""
launcher_plut_win.py - Offline LAN launcher for Plutonium on Steam Deck
(Windows .exe version -- runs inside Proton)

A lightweight PyQt5 app that shows installed Plutonium games and lets
the user pick a mode (MP, S/Z, ZM) to launch in offline LAN mode.
Designed to run as a non-Steam shortcut with Proton in Game Mode.

This is a Windows executable built with PyInstaller. It runs inside
GE-Proton as a non-Steam shortcut.

Launching writes an intent file (.lan_intent.json) and exits; the wrapper
script (launcher_offline.sh) reads it and exec's the game's -lan wrapper,
which runs the game in its own Proton prefix. Same flow on every device.

Every launch attempt is logged to .lan_launch.log on the Linux filesystem.

Flow:
  1. Read deckops.json (via Wine Z: drive) for installed Plutonium games
     and each game's stored lan_wrapper_path.
  2. Show compact game rows with hero background art and mode buttons.
  3. User taps a mode -- the exe writes the intent file and lets the
     wrapper script take over.

Gamepad input: Steam Deck Game Mode maps D-pad to arrow keys and A/B to
Return/Escape. Qt sees these as keyPressEvents. evdev is not available
inside Wine, but the keyboard mapping works identically.
"""

import os
import sys
import json
import tempfile

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QScrollArea,
)
from PyQt5.QtGui import (
    QFont, QFontDatabase, QPixmap, QPainter, QColor, QLinearGradient, QPen,
)
from PyQt5.QtCore import Qt, QTimer, QRect, pyqtSlot

from ui_theme import (
    C_BG, C_CARD, C_IW, C_TREY, C_DIM, C_DARK_BTN, C_TEXT, C_FOCUS,
    C_RED_BTN,
)

# ── paths ────────────────────────────────────────────────────────────────────
# Inside Wine/Proton, Z: maps to the Linux root filesystem.
# All paths are derived from the exe location so they work on any Linux
# username, not just "deck".  The exe lives at:
#   ~/<INSTALL_DIR_NAME>/assets/LAN/DeckOps_Offline.exe
# so walking four parents up gives us the Linux home directory via Z:.

# ── Branch identity (mirrors identity.py -- keep in sync) ─────────────────────
_BRANCH = "nightly"  # "nightly" or "stable"
_INSTALL_DIR_NAME = "DeckOps-Nightly" if _BRANCH == "nightly" else "DeckOps"
_GITHUB_USER = "smeltzzz"
_GITHUB_REPO = "DeckOps-Nightly" if _BRANCH == "nightly" else "DeckOps"
_GITHUB_RAW = f"https://raw.githubusercontent.com/{_GITHUB_USER}/{_GITHUB_REPO}/refs/heads/main"


def _detect_linux_home() -> str:
    """Derive the Linux home directory (as a Wine Z: path) from the exe location.

    PyInstaller sets sys.executable to the .exe path, which sits at:
        Z:\\home\\<user>\\<INSTALL_DIR_NAME>\\assets\\LAN\\DeckOps_Offline.exe

    Walking four parents up (LAN -> assets -> <INSTALL_DIR_NAME> -> home dir)
    gives us Z:\\home\\<user>.

    Falls back to Z:\\home\\deck if detection fails.
    """
    try:
        exe = os.path.abspath(sys.executable)
        # exe  = .../<INSTALL_DIR_NAME>/assets/LAN/DeckOps_Offline.exe
        # We want four levels up: exe -> LAN -> assets -> <INSTALL_DIR_NAME> -> home
        home = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(exe)
        )))
        # Sanity check: the install dir should exist one level down
        deckops_dir = os.path.join(home, _INSTALL_DIR_NAME)
        if os.path.isdir(deckops_dir):
            return home
    except Exception:
        pass
    return r"Z:\home\deck"


_LINUX_HOME = _detect_linux_home()

# DeckOps config lives on the Linux filesystem
_LINUX_PROJECT_ROOT = os.path.join(_LINUX_HOME, _INSTALL_DIR_NAME)
_DECKOPS_JSON = os.path.join(_LINUX_PROJECT_ROOT, "deckops.json")

# Intent file -- written by the exe when the user picks a game.
# launcher_offline.sh reads this after the exe exits and exec's the
# per-game LAN wrapper script. Used by all deck models.
_INTENT_FILE = os.path.join(_LINUX_PROJECT_ROOT, ".lan_intent.json")
# The launcher's own settings (music mute). deckops.json is only read here.
_SETTINGS_FILE = os.path.join(_LINUX_PROJECT_ROOT, ".lan_launcher.json")

# Launch diagnostics log (Linux filesystem, via Z:). Written before and
# after every launch attempt so a failed launch leaves evidence rather
# than failing silently.
_LAN_LOG = os.path.join(_LINUX_PROJECT_ROOT, ".lan_launch.log")


def _lan_log(msg: str):
    """Append a line to the launch diagnostics log. Never raises."""
    try:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_LAN_LOG, "a") as f:
            f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass

# Assets
FONTS_DIR  = os.path.join(_LINUX_PROJECT_ROOT, "assets", "fonts")
HEROES_DIR = os.path.join(_LINUX_PROJECT_ROOT, "assets", "images", "heroes")
MUSIC_PATH = os.path.join(_LINUX_PROJECT_ROOT, "assets", "music",
                           "background.mp3")


# Palette comes from ui_theme, shared with the Linux main app.
# The light faction colors need dark text to meet readable contrast.

# Four cards fit 1280x800; a scroll area keeps every card reachable on
# smaller screens instead of clipping the last row.
HEADER_H = 62
ROW_H    = 148
ROW_GAP  = 10
PAD_X    = 14
PAD_Y    = 10

# Ship both faces beside the exe: Russo for headings, Atkinson for all body
# text, including bold controls. Do not rely on Wine's Sans Serif alias.
_DISPLAY_FAMILY = "Arial"
_BODY_FAMILY = "Arial"
_BODY_BOLD_FAMILY = "Arial"
_FONT_LOADED = False

def _registered_family(filename):
    path = os.path.join(FONTS_DIR, filename)
    if not os.path.isfile(path):
        return None
    fid = QFontDatabase.addApplicationFont(path)
    if fid < 0:
        return None
    families = QFontDatabase.applicationFontFamilies(fid)
    return families[0] if families else None


def _load_font():
    global _DISPLAY_FAMILY, _BODY_FAMILY, _BODY_BOLD_FAMILY, _FONT_LOADED
    if _FONT_LOADED:
        return
    _DISPLAY_FAMILY = _registered_family("RussoOne-Regular.ttf") or _DISPLAY_FAMILY
    _BODY_FAMILY = _registered_family("AtkinsonHyperlegible-Regular.ttf") or _BODY_FAMILY
    _BODY_BOLD_FAMILY = _registered_family("AtkinsonHyperlegible-Bold.ttf") or _BODY_FAMILY
    _FONT_LOADED = True

def _font(size=13, bold=False, display=False):
    family = _DISPLAY_FAMILY if display else (_BODY_BOLD_FAMILY if bold else _BODY_FAMILY)
    f = QFont(family, size)
    f.setBold(bold)
    return f

# ── Plutonium game data ──────────────────────────────────────────────────────

PLUT_GAMES = [
    {
        "base": "Call of Duty: World at War",
        "dev": "trey",
        "modes": [("t4mp", "MP"), ("t4sp", "S/Z")],
        "hero_url": f"{_GITHUB_RAW}/assets/images/heroes/waw-banner.png",
        "hero_file": "waw-banner.png",
    },
    {
        "base": "Call of Duty: Black Ops",
        "dev": "trey",
        "modes": [("t5mp", "MP"), ("t5sp", "S/Z")],
        "hero_url": f"{_GITHUB_RAW}/assets/images/heroes/bo1-banner.png",
        "hero_file": "bo1-banner.png",
    },
    {
        "base": "Call of Duty: Black Ops II",
        "dev": "trey",
        "modes": [("t6mp", "MP"), ("t6zm", "ZM")],
        "hero_url": f"{_GITHUB_RAW}/assets/images/heroes/bo2-banner.png",
        "hero_file": "bo2-banner.png",
    },
    {
        "base": "Call of Duty: Modern Warfare 3",
        "dev": "iw",
        "modes": [("iw5mp", "MP"), ("iw5mp_ds", "DS")],
        "hero_url": f"{_GITHUB_RAW}/assets/images/heroes/mw3-banner.png",
        "hero_file": "mw3-banner.png",
    },
]


# ── XInput gamepad polling ────────────────────────────────────────────────────
# Uses XInput-Python to read the Steam Deck's gamepad inside Wine/Proton.
# Steam Deck presents as an XInput controller in Game Mode.
# Polls on a QTimer -- no threading needed.

_XINPUT_AVAILABLE = False
try:
    import XInput
    _XINPUT_AVAILABLE = True
except ImportError:
    pass

_GAMEPAD_POLL_MS = 50  # 20 Hz polling
_GAMEPAD_REPEAT_DELAY_MS = 400  # initial delay before repeat
_GAMEPAD_REPEAT_RATE_MS = 150   # repeat interval after initial delay


class GamepadPoller:
    """Polls XInput state and emits navigation actions."""

    # XInput button constants
    DPAD_UP    = 0x0001
    DPAD_DOWN  = 0x0002
    DPAD_LEFT  = 0x0004
    DPAD_RIGHT = 0x0008
    BTN_A      = 0x1000
    BTN_B      = 0x2000
    BTN_Y      = 0x8000

    NAV_BUTTONS = (DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, BTN_A, BTN_B)

    def __init__(self, on_up, on_down, on_left, on_right, on_activate, on_back, on_mute=None):
        self._callbacks = {
            self.DPAD_UP:    on_up,
            self.DPAD_DOWN:  on_down,
            self.DPAD_LEFT:  on_left,
            self.DPAD_RIGHT: on_right,
            self.BTN_A:      on_activate,
            self.BTN_B:      on_back,
            self.BTN_Y:      on_mute,
        }
        self._prev_buttons = 0
        self._held_button = None
        self._held_time_ms = 0

        self._timer = QTimer()
        self._timer.setInterval(_GAMEPAD_POLL_MS)
        self._timer.timeout.connect(self._poll)

    def start(self):
        if not _XINPUT_AVAILABLE:
            return
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        try:
            # Find first connected controller
            connected = XInput.get_connected()
            ctrl_idx = None
            for i in range(4):
                if connected[i]:
                    ctrl_idx = i
                    break
            if ctrl_idx is None:
                return

            state = XInput.get_state(ctrl_idx)
            buttons = state.Gamepad.wButtons

            for btn in self.NAV_BUTTONS:
                is_pressed = bool(buttons & btn)
                was_pressed = bool(self._prev_buttons & btn)

                if is_pressed and not was_pressed:
                    # Fresh press -- fire immediately
                    self._fire(btn)
                    self._held_button = btn
                    self._held_time_ms = 0
                elif is_pressed and was_pressed and self._held_button == btn:
                    # Held -- handle repeat
                    self._held_time_ms += _GAMEPAD_POLL_MS
                    if self._held_time_ms >= _GAMEPAD_REPEAT_DELAY_MS:
                        if (self._held_time_ms - _GAMEPAD_REPEAT_DELAY_MS) % _GAMEPAD_REPEAT_RATE_MS < _GAMEPAD_POLL_MS:
                            self._fire(btn)
                elif not is_pressed and was_pressed and self._held_button == btn:
                    # Released
                    self._held_button = None
                    self._held_time_ms = 0

            # Y toggles, so it never repeats while held
            if buttons & self.BTN_Y and not self._prev_buttons & self.BTN_Y:
                self._fire(self.BTN_Y)

            self._prev_buttons = buttons

        except Exception:
            pass

    def _fire(self, btn):
        cb = self._callbacks.get(btn)
        if cb:
            cb()


# ── audio (background music) ─────────────────────────────────────────────

LAUNCHER_VOLUME_MULT = 0.5
FADEOUT_MS           = 1000


def _pygame_available():
    try:
        import pygame  # noqa: F401
        return True
    except ImportError:
        return False


def _audio_volume() -> float:
    try:
        data = _load_deckops_config()
        return data.get("music_volume", 0.4) * LAUNCHER_VOLUME_MULT
    except Exception:
        return 0.2


def _music_muted() -> bool:
    """The launcher's saved choice; until there is one, start muted if DeckOps' music is off."""
    try:
        with open(_SETTINGS_FILE) as f:
            return bool(json.load(f)["music_muted"])
    except Exception:
        pass
    try:
        return not _load_deckops_config().get("music_enabled", True)
    except Exception:
        return False


def _set_music_muted(muted: bool):
    try:
        with open(_SETTINGS_FILE, "w") as f:
            json.dump({"music_muted": muted}, f)
    except Exception:
        pass


def _start_audio():
    if _music_muted() or not _pygame_available():
        return
    if not os.path.exists(MUSIC_PATH):
        return
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        if not pygame.mixer.music.get_busy():
            pygame.mixer.music.load(MUSIC_PATH)
            pygame.mixer.music.set_volume(_audio_volume())
            pygame.mixer.music.play(-1)
    except Exception:
        pass


def _fadeout_audio(ms: int = FADEOUT_MS):
    if not _pygame_available():
        return
    try:
        import pygame
        if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
            pygame.mixer.music.fadeout(ms)
    except Exception:
        pass


# ── config helpers ────────────────────────────────────────────────────────

def _load_deckops_config() -> dict:
    """Load deckops.json from the Linux filesystem via Z: drive."""
    if not os.path.exists(_DECKOPS_JSON):
        return {}
    try:
        with open(_DECKOPS_JSON, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}



# ── launch helper ────────────────────────────────────────────────────────

_LAUNCH_FIRED = False

# Set by LauncherWindow: starting screen and recoverable error notice.
_ON_LAUNCH = None
_ON_ERROR = None
_LAUNCH_MESSAGE_MS = 3000


def _write_intent(lan_wrapper_path: str):
    """Commit a complete intent to the same directory before exiting the UI.

    The shell wrapper consumes only .lan_intent.json; a partial write must
    never be mistaken for a successful launch. Keep the payload unchanged.
    """
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=_LINUX_PROJECT_ROOT,
                                         prefix=".lan_intent-", suffix=".tmp", delete=False) as f:
            pending = f.name
            json.dump({"lan_wrapper_path": lan_wrapper_path}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(pending, _INTENT_FILE)
    finally:
        if pending and os.path.exists(pending):
            os.unlink(pending)


def _launch_error(detail: str):
    _lan_log(f"Launch intent FAILED: {detail}")
    if _ON_ERROR:
        _ON_ERROR(detail)


def _launch_lan_lcd(lan_wrapper_path: str, title: str = ""):
    """Write the intent and exit only after it is safely committed.

    launcher_offline.sh reads the unchanged lan_wrapper_path payload after
    the exe exits. The function name is retained for external callers.
    """
    global _LAUNCH_FIRED
    if _LAUNCH_FIRED:
        return False
    try:
        _write_intent(lan_wrapper_path)
    except Exception as ex:
        _launch_error(f"Could not save launch request: {ex}. Check the DeckOps folder and retry.")
        return False

    _LAUNCH_FIRED = True
    _lan_log(f"Intent written: {lan_wrapper_path}")
    _fadeout_audio()
    if _ON_LAUNCH:
        _ON_LAUNCH(title)
    QTimer.singleShot(_LAUNCH_MESSAGE_MS if _ON_LAUNCH else 500,
                      lambda: QApplication.instance().quit())
    return True


def _dispatch_launch(info: dict, title: str = ""):
    """Use the per-game wrapper path selected by the installed game config."""
    lan_path = info.get("lan_wrapper_path", "")
    if lan_path:
        return _launch_lan_lcd(lan_path, title)
    _launch_error("This mode has no LAN wrapper. Repair its setup in DeckOps and retry.")
    return False


# ── helpers ──────────────────────────────────────────────────────────────────

def _hero_path(hero_file: str) -> str:
    return os.path.join(HEROES_DIR, hero_file)


def _get_setup_plut_keys() -> dict:
    """
    Return Plutonium game keys that have been set up, with the data each
    launch path needs.

    All models now use the intent-file bridge: the exe writes
    .lan_intent.json with the lan_wrapper_path and exits.
    launcher_offline.sh reads the intent and exec's the per-game LAN
    wrapper script, which runs the bootstrapper in the correct per-game
    Proton prefix.

    Returns dict -- {game_key: {...}} where a game is included only if
    it has a valid lan_wrapper_path on disk.
    """
    try:
        deckops     = _load_deckops_config()
        setup_games = deckops.get("setup_games", {})

        result = {}
        for k, v in setup_games.items():
            if v.get("client") != "plutonium":
                continue
            lan_path = v.get("lan_wrapper_path")
            if lan_path and os.path.exists(lan_path):
                result[k] = {"lan_wrapper_path": lan_path}
        return result
    except Exception:
        return {}


# ── widgets ──────────────────────────────────────────────────────────────────

def _card_shade(alpha: int) -> QColor:
    """Warm, translucent ink over hero art without replacing the artwork."""
    shade = QColor(C_BG)
    shade.setAlpha(alpha)
    return shade


class GameRow(QWidget):
    """A local-art card with a readable fallback and full-name mode actions."""

    def __init__(self, game_def: dict, setup_info: dict, parent=None):
        super().__init__(parent)
        self._game_def = game_def
        self._bg_pixmap = None
        self._color = C_IW if game_def["dev"] == "iw" else C_TREY

        self._available = [
            (key, label) for key, label in game_def["modes"]
            if key in setup_info
        ]
        self._has_modes = len(self._available) > 0

        self._row_focused = False
        self._mode_buttons = []
        self._focused_btn_idx = None

        self.setFixedHeight(ROW_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Assets ship beside the exe, not inside it. QPixmap is constructed
        # only on the UI thread; absent/corrupt art gets a painted fallback.
        self._bg_pixmap = QPixmap(_hero_path(game_def["hero_file"]))
        if self._bg_pixmap.isNull():
            self._bg_pixmap = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 15, 20, 14)
        lay.setSpacing(4)

        heading = QHBoxLayout()
        title = QLabel(game_def["base"])
        title.setFont(_font(17, display=True))
        title.setWordWrap(True)
        title.setStyleSheet(f"color:{C_TEXT};background:transparent;")
        heading.addWidget(title, 1)

        badge = QLabel("OFFLINE  /  LAN")
        badge.setFont(_font(9, bold=True))
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(104, 24)
        badge.setStyleSheet(
            f"background:{self._color};color:{C_BG};border-radius:4px;"
        )
        heading.addWidget(badge, 0, Qt.AlignTop)
        lay.addLayout(heading)
        lay.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        if self._has_modes:
            actions.addStretch(1)
            for key, label in self._available:
                info = setup_info[key]
                mode_name = {
                    "MP": "Multiplayer", "S/Z": "Solo / Zombies",
                    "ZM": "Zombies", "DS": "Dedicated server",
                }[label]
                btn = QPushButton(mode_name)
                btn.setAccessibleName(f"{game_def['base']} — {mode_name}, offline LAN")
                btn.setToolTip(f"Launch {game_def['base']} {mode_name} in offline LAN mode")
                btn.setFont(_font(11, bold=True))
                btn.setMinimumWidth(118)
                btn.setMaximumWidth(186)
                btn.setFixedHeight(46)
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                # The window owns keyboard/XInput navigation; the selected
                # button has a visible ring even before the first key press.
                btn.setFocusPolicy(Qt.NoFocus)
                self._apply_btn_style(btn, focused=False)
                btn.clicked.connect(
                    lambda checked=False, i=info, t=f"{game_def['base']} ({mode_name})": _dispatch_launch(i, t)
                )
                self._mode_buttons.append((btn, key))
                actions.addWidget(btn)
        else:
            no_lbl = QLabel("NOT INSTALLED  ·  Set up this game in DeckOps")
            no_lbl.setFont(_font(10, bold=True))
            no_lbl.setStyleSheet(f"color:{C_DIM};background:transparent;")
            actions.addWidget(no_lbl)
        lay.addLayout(actions)

    def _apply_btn_style(self, btn: QPushButton, focused: bool):
        border = f"3px solid {C_FOCUS}" if focused else "3px solid transparent"
        btn.setStyleSheet(
            f"QPushButton{{background:{self._color};color:{C_BG};"
            f"border:{border};border-radius:8px;}}"
            f"QPushButton:hover{{border:3px solid {C_FOCUS};}}"
            f"QPushButton:pressed{{background:{C_DARK_BTN};color:{C_TEXT};}}"
        )

    def launchable_count(self) -> int:
        return len(self._mode_buttons)

    def trigger_focused_button(self):
        if self._focused_btn_idx is None:
            return
        if 0 <= self._focused_btn_idx < len(self._mode_buttons):
            btn, _ = self._mode_buttons[self._focused_btn_idx]
            btn.click()

    def set_row_focused(self, focused: bool):
        if self._row_focused == focused:
            return
        self._row_focused = focused
        if not focused:
            self.set_focused_button(None)
        self.update()

    def set_focused_button(self, idx):
        if self._focused_btn_idx == idx:
            return
        if self._focused_btn_idx is not None and \
                0 <= self._focused_btn_idx < len(self._mode_buttons):
            old_btn, _ = self._mode_buttons[self._focused_btn_idx]
            self._apply_btn_style(old_btn, focused=False)
        self._focused_btn_idx = idx
        if idx is not None and 0 <= idx < len(self._mode_buttons):
            new_btn, _ = self._mode_buttons[idx]
            self._apply_btn_style(new_btn, focused=True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.rect()
        painter.fillRect(rect, QColor(C_CARD))

        if self._bg_pixmap:
            pw, ph = self._bg_pixmap.width(), self._bg_pixmap.height()
            # Cover, not stretch: center-crop on both axes at any width.
            scale = max(rect.width() / pw, rect.height() / ph)
            sw, sh = min(pw, int(rect.width() / scale)), min(ph, int(rect.height() / scale))
            painter.drawPixmap(rect, self._bg_pixmap,
                               QRect((pw - sw) // 2, (ph - sh) // 2, sw, sh))
        else:
            # Deliberate offline fallback, with no download or worker thread.
            fallback = QLinearGradient(0, 0, rect.width(), rect.height())
            fallback.setColorAt(0, QColor(self._color).darker(350))
            fallback.setColorAt(1, QColor(C_CARD))
            painter.fillRect(rect, fallback)

        shade = QLinearGradient(0, 0, 0, rect.height())
        shade.setColorAt(0, _card_shade(195))
        shade.setColorAt(1, _card_shade(235))
        painter.fillRect(rect, shade)
        if not self._has_modes:
            painter.fillRect(rect, _card_shade(65))

        painter.fillRect(QRect(0, 0, 4, rect.height()), QColor(self._color))
        if self._row_focused:
            painter.setPen(QPen(QColor(C_FOCUS), 3))
            painter.drawRect(rect.adjusted(2, 2, -3, -3))
        painter.end()
        super().paintEvent(event)


class LauncherWindow(QWidget):
    """Fullscreen launcher with scrollable, controller-accessible cards."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DeckOps Offline")
        self.setStyleSheet(f"background:{C_BG};")

        setup_info = _get_setup_plut_keys()

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # ── header bar ──
        hdr = QWidget()
        hdr.setFixedHeight(HEADER_H)
        hdr.setStyleSheet(f"background:{C_CARD};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(20, 0, 20, 0)

        title = QLabel("DECKOPS")
        title.setFont(_font(20, display=True))
        title.setStyleSheet(f"color:{C_TREY};background:transparent;")
        hl.addWidget(title)

        subtitle = QLabel("/  OFFLINE LAN")
        subtitle.setFont(_font(11, bold=True))
        subtitle.setStyleSheet(f"color:{C_DIM};background:transparent;")
        subtitle.setContentsMargins(8, 4, 0, 0)
        hl.addWidget(subtitle)
        hl.addStretch()

        self._music_btn = QPushButton()
        self._music_btn.setAccessibleName("Toggle background music")
        self._music_btn.setToolTip("Toggle music (Y / M)")
        self._music_btn.setFont(_font(11, bold=True))
        self._music_btn.setFixedHeight(36)
        self._music_btn.setFocusPolicy(Qt.NoFocus)
        self._music_btn.setStyleSheet(
            f"QPushButton{{background:{C_DARK_BTN};color:{C_TEXT};border:none;"
            f"border-radius:8px;padding:0 12px;}}"
            f"QPushButton:hover{{background:{C_DIM};color:{C_BG};}}"
        )
        self._music_btn.clicked.connect(self._toggle_music)
        self._update_music_btn()
        hl.addWidget(self._music_btn)

        quit_btn = QPushButton("✕")
        quit_btn.setAccessibleName("Close offline launcher")
        quit_btn.setToolTip("Exit (B / Esc)")
        quit_btn.setFont(_font(16, bold=True))
        quit_btn.setFixedSize(44, 44)
        quit_btn.setFocusPolicy(Qt.NoFocus)
        quit_btn.setStyleSheet(
            f"QPushButton{{background:transparent;color:{C_DIM};border:none;"
            f"border-radius:8px;}}"
            f"QPushButton:hover{{background:{C_DARK_BTN};color:{C_TEXT};}}"
        )
        quit_btn.clicked.connect(self._quit_with_fade)
        hl.addWidget(quit_btn)
        root_lay.addWidget(hdr)

        # ── game rows ──
        rows_container = QWidget()
        rows_container.setStyleSheet(f"background:{C_BG};")
        rows_lay = QVBoxLayout(rows_container)
        rows_lay.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
        rows_lay.setSpacing(ROW_GAP)

        installed = []
        uninstalled = []
        for game_def in PLUT_GAMES:
            has_any = any(k in setup_info for k, _ in game_def["modes"])
            row = GameRow(game_def, setup_info)
            if has_any:
                installed.append(row)
            else:
                uninstalled.append(row)

        for row in installed:
            rows_lay.addWidget(row)
        for row in uninstalled:
            rows_lay.addWidget(row)

        rows_lay.addStretch()
        self._rows_scroll = QScrollArea()
        self._rows_scroll.setWidgetResizable(True)
        self._rows_scroll.setFrameShape(QScrollArea.NoFrame)
        self._rows_scroll.setFocusPolicy(Qt.NoFocus)
        self._rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._rows_scroll.setStyleSheet(f"QScrollArea{{background:{C_BG};border:none;}}")
        self._rows_scroll.setWidget(rows_container)
        root_lay.addWidget(self._rows_scroll, 1)

        self._starting = QLabel()
        self._starting.setAlignment(Qt.AlignCenter)
        self._starting.setTextFormat(Qt.PlainText)
        self._starting.setWordWrap(True)
        self._starting.setFont(_font(18, display=True))
        self._starting.setStyleSheet(f"color:{C_TEXT};background:{C_BG};padding:24px;")
        self._starting.hide()
        root_lay.addWidget(self._starting, 1)

        self._status = QLabel("D-PAD / ARROWS  Navigate     A / ENTER  Launch     B / ESC  Exit     Y / M  Music")
        self._status.setFont(_font(10, bold=True))
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self._status.setContentsMargins(20, 5, 20, 5)
        self._status.setMinimumHeight(42)
        self._status.setStyleSheet(f"color:{C_DIM};background:{C_CARD};")
        root_lay.addWidget(self._status)
        global _ON_LAUNCH, _ON_ERROR
        _ON_LAUNCH = self._show_starting
        _ON_ERROR = self._show_error

        if not setup_info:
            empty = QLabel(
                "No Plutonium games installed.\n"
                "Run the DeckOps installer first."
            )
            empty.setFont(_font(14))
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"color:{C_DIM};background:transparent;")
            rows_lay.insertWidget(0, empty)

        # ── keyboard focus tracking ──
        self._focus_rows = [r for r in installed if r.launchable_count() > 0]
        self._row_idx = 0
        if self._focus_rows:
            self._focus_rows[0].set_row_focused(True)
            self._focus_rows[0].set_focused_button(0)

        self.setFocusPolicy(Qt.StrongFocus)

        # ── XInput gamepad ──
        self._gamepad = GamepadPoller(
            on_up=lambda: self.on_move_row(-1),
            on_down=lambda: self.on_move_row(+1),
            on_left=lambda: self.on_move_btn(-1),
            on_right=lambda: self.on_move_btn(+1),
            on_activate=self._activate_focused,
            on_back=self._quit_with_fade,
            on_mute=self._toggle_music,
        )
        self._gamepad.start()

    def keyPressEvent(self, event):
        key = event.key()

        if key == Qt.Key_Escape:
            self._quit_with_fade()
            return
        if key == Qt.Key_M:
            self._toggle_music()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._activate_focused()
            return
        if key == Qt.Key_Up:
            self.on_move_row(-1)
            return
        if key == Qt.Key_Down:
            self.on_move_row(+1)
            return
        if key == Qt.Key_Left:
            self.on_move_btn(-1)
            return
        if key == Qt.Key_Right:
            self.on_move_btn(+1)
            return

        super().keyPressEvent(event)

    @pyqtSlot(int)
    def on_move_row(self, delta: int):
        if not self._focus_rows:
            return
        new_idx = max(0, min(len(self._focus_rows) - 1, self._row_idx + delta))
        if new_idx == self._row_idx:
            return
        self._focus_rows[self._row_idx].set_row_focused(False)
        self._row_idx = new_idx
        self._focus_rows[self._row_idx].set_row_focused(True)
        self._focus_rows[self._row_idx].set_focused_button(0)
        self._rows_scroll.ensureWidgetVisible(self._focus_rows[self._row_idx], 0, 10)

    @pyqtSlot(int)
    def on_move_btn(self, delta: int):
        if not self._focus_rows:
            return
        row = self._focus_rows[self._row_idx]
        n = row.launchable_count()
        if n == 0:
            return
        cur = row._focused_btn_idx if row._focused_btn_idx is not None else 0
        new_idx = max(0, min(n - 1, cur + delta))
        row.set_focused_button(new_idx)

    @pyqtSlot()
    def _activate_focused(self):
        if not self._focus_rows:
            return
        self._focus_rows[self._row_idx].trigger_focused_button()

    def _update_music_btn(self):
        self._music_btn.setText("Y  MUSIC OFF" if _music_muted() else "Y  MUSIC ON")

    @pyqtSlot()
    def _toggle_music(self):
        muted = not _music_muted()
        _set_music_muted(muted)
        if muted:
            _fadeout_audio(300)
        else:
            _start_audio()
        self._update_music_btn()

    def _show_starting(self, title: str):
        self._gamepad.stop()
        self._rows_scroll.hide()
        self._status.hide()
        self._starting.setText(
            f"Starting {title}…\n\nThe screen may stay black for up to a minute while the game starts.")
        self._starting.show()

    def _show_error(self, detail: str):
        # Remain in the launcher: A/Enter or a mode button retries the write.
        self._status.setText(f"COULD NOT LAUNCH  ·  {detail}  Select a mode to retry; B / Esc exits.")
        self._status.setStyleSheet(f"color:{C_TEXT};background:{C_RED_BTN};")
        self._status.setAccessibleName(self._status.text())

    @pyqtSlot()
    def _quit_with_fade(self):
        _fadeout_audio()
        QTimer.singleShot(FADEOUT_MS, QApplication.instance().quit)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    _load_font()

    win = LauncherWindow()
    win.showFullScreen()
    win.activateWindow()
    win.raise_()
    win.setFocus()
    _start_audio()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
