"""
ui_constants.py — Shared constants, helpers, and game definitions for DeckOps UI

Extracted from ui_qt.py so that ui_setup.py, ui_install.py, ui_manage.py,
and ui_qt.py can all import from a single source without circular deps.
"""

import html
import os
import shutil
import subprocess

from qt import (
    QApplication, QLabel, QPushButton, QFrame, QHBoxLayout, QMessageBox, QWidget,
    QAbstractButton, QScrollArea, QSizePolicy, QStackedWidget,
)
from qt import Qt, pyqtSignal, QObject, QUrl, QT6, QTimer, QEvent
from qt import QFont, QFontDatabase, QKeyEvent

import config as cfg
from identity import BUILD_BADGE, BRANCH
from log import get_logger
from ui_theme import (
    C_BG, C_CARD, C_SURFACE, C_LINE, C_TEXT, C_DIM, C_IW, C_TREY,
    C_DARK_BTN, C_RED_BTN, C_FOCUS,
)

_log = get_logger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────────────

from steam_common import PROJECT_ROOT
FONTS_DIR    = os.path.join(PROJECT_ROOT, "assets", "fonts")
HEADERS_DIR  = os.path.join(PROJECT_ROOT, "assets", "images", "headers")
HEROES_DIR   = os.path.join(PROJECT_ROOT, "assets", "images", "heroes")
MUSIC_PATH   = os.path.join(PROJECT_ROOT, "assets", "music", "background.mp3")
LOG_DIR      = os.path.join(PROJECT_ROOT, "logs")
LOG_PATH     = os.path.join(LOG_DIR, "install.log")

for _d in (HEADERS_DIR, LOG_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        # A read-only or full install dir must not make importing the UI (before the
        # window exists) a crash with nothing on screen; the writers report their own errors
        _log.warning("could not create %s", _d, exc_info=True)


# ── Logging ───────────────────────────────────────────────────────────────────

def _log_to_file(text: str):
    """Append a line to the install log via the logging framework.

    Kept as a thin bridge so existing callers in ui_install.py and
    ui_manage.py continue to work without changes.
    """
    _log.info(text)


def _copy_log_to_clipboard(status_widget=None):
    """Read the last 200 lines of install.log and copy to clipboard."""
    from depot_downgrade import copy_to_clipboard
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        # An early failure in a long install scrolls out of the tail
        errs = [l for l in lines[:-200] if "  ERROR  " in l or "✗" in l][-50:]
        tail = "".join(lines[-200:])
        if errs:
            tail = "--- earlier errors ---\n" + "".join(errs) + "--- last 200 lines ---\n" + tail
        # copy_to_clipboard() returns normally when neither wl-copy nor xclip
        # exists, so the line below used to claim success with nothing copied.
        # Qt's own clipboard needs neither tool.
        app = QApplication.instance()
        if app is not None:
            app.clipboard().setText(tail)
        try:
            copy_to_clipboard(tail)
        except Exception:
            if app is None:
                raise
            _log.debug("clipboard helper failed; Qt clipboard was set", exc_info=True)
        if status_widget:
            status_widget.setText("Log copied to clipboard (last 200 lines)")
    except FileNotFoundError:
        if status_widget:
            status_widget.setText("No log file found")
    except Exception as ex:
        if status_widget:
            status_widget.setText(f"Failed to copy log: {ex}")


# ── Colors ────────────────────────────────────────────────────────────────────
# Branch accent — green for stable, orange for nightly. Game-family color is
# never used as the sole indication of success or failure.
C_ACCENT    = C_IW if BRANCH == "stable" else C_TREY
C_ACCENT_BG = "#203321" if BRANCH == "stable" else "#3A2619"
C_FAIL_TEXT = "#FF817A"     # failure text on dark surfaces (log lines, marks)


# ── Deck scale ────────────────────────────────────────────────────────────────
# Minimum sizes for a 1280x800 7" screen read at arm's length and driven by a
# controller. New and reworked screen code should use these, not literals.
FONT_BODY       = 11    # body copy, status text, status bar
FONT_ACTION     = 12    # minimum for anything the player activates (buttons, checkboxes)
BTN_MIN_H       = 44    # minimum height of an activatable button
CHECK_INDICATOR = 26    # QCheckBox indicator square, px
FOCUS_RING_W    = 3     # focus ring width, px


# ── Labels ────────────────────────────────────────────────────────────────────
# One name per screen. The status bar shows SCREEN_TITLES[screen_name].upper();
# a screen header that names the screen must use the same text (header labels
# are upper-cased the same way), so the two never disagree.
SCREEN_TITLES = {
    "BootstrapScreen":     "Opening",
    "SetupFlowScreen":     "Device Setup",
    "SetupScreen":         "Select Games",
    "InstallScreen":       "Installation",
    "ManagementScreen":    "Game Library",
    "ConfigureScreen":     "Settings",
    "SetupCompleteScreen": "Field Guide",
    "UpdateScreen":        "Game Updates",
}


def screen_title(name: str) -> str:
    """Display title for a screen_name (falls back to the name itself)."""
    return SCREEN_TITLES.get(name, name)


# Standard button and navigation wording. "Game Library" is the only name for
# the post-install home screen ("My Games" is retired).
LABELS = {
    "back":                 "Back",
    "continue":             "Continue",
    "back_to_library":      "Back to Game Library",
    "continue_to_library":  "Continue to Game Library",
    "back_to_selection":    "Back to Game Selection",
    "library":              SCREEN_TITLES["ManagementScreen"],
    "settings":             SCREEN_TITLES["ConfigureScreen"],
    "field_guide":          SCREEN_TITLES["SetupCompleteScreen"],
    "game_updates":         SCREEN_TITLES["UpdateScreen"],
    "offline_badge":        "offline",
}


# ── Fonts ─────────────────────────────────────────────────────────────────────

_FONT_FAMILY      = "Sans Serif"
_FONT_FAMILY_DISP = "Sans Serif"
_FONT_LOADED      = False

def _load_font():
    global _FONT_FAMILY, _FONT_FAMILY_DISP, _FONT_LOADED
    if _FONT_LOADED:
        return
    russo = os.path.join(FONTS_DIR, "RussoOne-Regular.ttf")
    if not os.path.exists(russo):
        raise FileNotFoundError(
            f"Required font not found: {russo}\n"
            f"Ensure assets/fonts/RussoOne-Regular.ttf is present in the repo."
        )
    fid = QFontDatabase.addApplicationFont(russo)
    fams = QFontDatabase.applicationFontFamilies(fid)
    if fams:
        _FONT_FAMILY_DISP = fams[0]
    # Bundle an OFL body face rather than relying on the host's Qt font
    # database. Some Wine/offscreen installations otherwise resolve every
    # generic sans-serif request to our only bundled face, Russo One.
    for filename in ("AtkinsonHyperlegible-Regular.ttf", "AtkinsonHyperlegible-Bold.ttf"):
        body = os.path.join(FONTS_DIR, filename)
        if os.path.exists(body):
            body_id = QFontDatabase.addApplicationFont(body)
            body_families = QFontDatabase.applicationFontFamilies(body_id)
            if body_families:
                _FONT_FAMILY = body_families[0]
    _FONT_LOADED = True

def font(size=13, bold=False, weight=None, display=False):
    family = _FONT_FAMILY_DISP if display else _FONT_FAMILY
    f = QFont(family, size)
    if weight is not None:
        f.setWeight(weight)
    else:
        f.setBold(bold)
    return f


# ── Game card definitions ─────────────────────────────────────────────────────
#
# Each entry is one card in the UI: two rows of CARD_COLS (5). What each card shows
# depends on the Deck model. OLED and Other users get the full key list. LCD
# users get a reduced set via the lcd_* overrides:
#
#   lcd_keys   — which game keys this card includes on LCD (empty = card hidden)
#   lcd_client — client badge label on LCD (e.g. "steam" instead of "plutonium")
#   lcd_appid  — appid used for the header image and Steam store link on LCD
#
# If an lcd_* field is missing, the card uses the default (OLED) value.
# The _active_keys(), _active_client(), _active_appid() helpers below
# resolve the correct value based on the user's saved deck_model config.

ALL_GAMES = [
    # ── Row 1: Infinity Ward / Sledgehammer ──────────────────────────────
    {"base":"Call of Duty 4: Modern Warfare","keys":["cod4mp","cod4sp"],"appid":7940,"dev":"iw","client":"cod4r + iw3sp",
     "badge":"COD4R + SP MOD",
     "launch_note":"DeckOps creates Proton prefixes automatically."},
    {"base":"Call of Duty: Modern Warfare 2","keys":["iw4mp","iw4sp"],"appid":10190,"dev":"iw","client":"iw4x",
     "launch_note":"DeckOps creates Proton prefixes automatically."},
    {"base":"Call of Duty: Modern Warfare 3","keys":["iw5mp","iw5sp","iw5mp_ds"],"appid":42690,"dev":"iw","client":"plutonium",
     "lcd_keys":["iw5mp","iw5sp","iw5mp_ds"],"lcd_client":"plutonium","lcd_appid":42680,
     "launch_note":"DeckOps creates Proton prefixes automatically."},
    # ── Row 2: Treyarch + LAN launcher ───────────────────────────────────
    {"base":"Call of Duty: World at War","keys":["t4mp","t4sp"],"appid":10090,"dev":"trey","client":"plutonium",
     "lcd_keys":["t4mp","t4sp"],"lcd_client":"plutonium",
     "launch_note":"DeckOps creates Proton prefixes automatically."},
    {"base":"Call of Duty: Black Ops","keys":["t5mp","t5sp"],"appid":42700,"dev":"trey","client":"plutonium",
     "lcd_keys":["t5mp","t5sp"],"lcd_client":"plutonium",
     "launch_note":"DeckOps creates Proton prefixes automatically."},
    {"base":"Call of Duty: Black Ops II","keys":["t6mp","t6sp","t6zm"],"appid":202990,"dev":"trey","client":"plutonium + t6sp-mod",
     "badge":"PLUTONIUM + SP MOD",
     "lcd_keys":["t6mp","t6sp","t6zm"],"lcd_client":"plutonium + t6sp-mod","lcd_appid":202970,
     "launch_note":"DeckOps creates Proton prefixes automatically."},
    {"base":"DeckOps: Plutonium Offline","keys":[],"appid":None,"dev":"trey","client":"lan",
     # "client" stays "lan" (ui_manage keys the offline card on it); the visible
     # library badge reads "offline", the name the rest of the UI uses.
     "badge":LABELS["offline_badge"].upper(),
     "launch_note":"Offline Mode: the offline launcher, an Offline entry in each Plutonium game's launch menu, and bot matches."},
]

def _active_keys(gd):
    """Return the keys to show for this card based on the user's Deck model."""
    if not cfg.is_lcd():
        return gd["keys"]
    return gd.get("lcd_keys", gd["keys"])

def _active_client(gd):
    """Return the client label for this card based on the user's Deck model."""
    if not cfg.is_lcd():
        return gd["client"]
    return gd.get("lcd_client", gd["client"])

def _active_appid(gd):
    """Return the display appid for this card based on the user's Deck model."""
    if not cfg.is_lcd():
        return gd["appid"]
    return gd.get("lcd_appid", gd["appid"])

KEY_CLIENT = {
    "cod4mp": "cod4r",
    "cod4sp": "iw3sp",
    "iw4mp":  "iw4x",
    "iw4sp":  "steam",
    "iw5mp":    "plutonium",
    "iw5mp_ds": "plutonium",
    "iw5sp":  "steam",
    "t4sp":   "plutonium",
    "t4mp":   "plutonium",
    "t5sp":   "plutonium",
    "t5mp":   "plutonium",
    "t6zm":   "plutonium",
    "t6mp":   "plutonium",
    "t6sp":   "t6sp_mod",
}

KEY_EXES = {
    "cod4mp":"iw3mp.exe","cod4sp":"iw3sp.exe",
    "iw4mp":"iw4mp.exe","iw4sp":"iw4sp.exe",
    "iw5mp":"iw5mp.exe","iw5mp_ds":"iw5mp_server.exe","iw5sp":"iw5sp.exe",
    "t4sp":"CoDWaW.exe","t4mp":"CoDWaWmp.exe",
    "t5sp":"BlackOps.exe","t5mp":"BlackOpsMP.exe",
    "t6zm":"t6zm.exe","t6mp":"t6mp.exe","t6sp":"t6sp.exe",
}

# Label shown beneath each per-key checkbox in SetupScreen.
KEY_MODE_LABEL = {
    "cod4mp": "MP",    "cod4sp": "SP",
    "iw4mp":  "MP",    "iw4sp":  "SP",
    "iw5mp":  "MP",    "iw5mp_ds": "DS",    "iw5sp":  "SP",
    "t4sp":   "S/Z",   "t4mp":   "MP",
    "t5sp":   "S/Z",   "t5mp":   "MP",
    "t6sp":   "SP",    "t6zm":   "ZM",    "t6mp":   "MP",
}


SP_IMAGE_URLS = {
    7940:   "https://shared.steamstatic.com/store_item_assets/steam/apps/7940/library_600x900_2x.jpg",
    10180:  "https://shared.steamstatic.com/store_item_assets/steam/apps/10180/library_600x900_2x.jpg",
    10190:  "https://shared.steamstatic.com/store_item_assets/steam/apps/10180/library_600x900_2x.jpg",
    42680:  "https://shared.steamstatic.com/store_item_assets/steam/apps/42680/library_600x900_2x.jpg",
    42690:  "https://shared.steamstatic.com/store_item_assets/steam/apps/42680/library_600x900_2x.jpg",
    10090:  "https://shared.steamstatic.com/store_item_assets/steam/apps/10090/library_600x900_2x.jpg",
    42700:  "https://shared.steamstatic.com/store_item_assets/steam/apps/42700/library_600x900_2x.jpg",
    202970: "https://shared.steamstatic.com/store_item_assets/steam/apps/202970/library_600x900_2x.jpg",
    202990: "https://shared.steamstatic.com/store_item_assets/steam/apps/202970/library_600x900_2x.jpg",
}

IMG_RATIO = 1.5
CARD_COLS  = 5
CARD_MAX_W = 187

MUSIC_URL = "https://archive.org/download/adrenaline-klickaud/Adrenaline_KLICKAUD.mp3"


# ── Audio ─────────────────────────────────────────────────────────────────────

def _clean_volume(value, default: float = 0.4) -> float:
    """A usable 0.0-1.0 volume from whatever deckops.json holds. A hand-edited
    or damaged value (null, text, NaN, 7) otherwise crashed the Settings screen
    on every start (int(None * 100)) and silently muted the player."""
    if isinstance(value, bool):
        return default
    try:
        vol = float(value)
    except (TypeError, ValueError):
        return default
    if vol != vol:          # NaN
        return default
    return max(0.0, min(1.0, vol))


def _clean_enabled(value, default: bool = True) -> bool:
    return value if isinstance(value, bool) else default


def saved_music_volume() -> float:
    return _clean_volume(cfg.get_music_volume())


def saved_music_enabled() -> bool:
    return _clean_enabled(cfg.get_music_enabled())


_music_volume  = saved_music_volume()
_music_enabled = saved_music_enabled()

_player = None

# Qt's media player; PyQt6 wheels bundle FFmpeg for mp3. PyQt5 would need
# system GStreamer plugins, so the PyQt5 fallback plays no music.
def _kill_audio():
    if _player:
        _player.stop()

def _start_audio():
    global _player
    if not _music_enabled or not QT6 or not os.path.exists(MUSIC_PATH):
        return
    try:
        from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
        if _player is None:
            _player = QMediaPlayer(); _player.setAudioOutput(QAudioOutput(_player))
            _player.setSource(QUrl.fromLocalFile(MUSIC_PATH)); _player.setLoops(QMediaPlayer.Loops.Infinite)
        _player.audioOutput().setVolume(_music_volume)
        if _player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            _player.play()
    except Exception:
        _log.debug("audio start failed", exc_info=True)

def _set_audio_volume(vol: float, persist: bool = True):
    """Apply the music volume; persist=False for a slider drag, which saves once when
    it settles instead of rewriting deckops.json on every step."""
    global _music_volume
    _music_volume = vol
    if persist:
        cfg.set_music_volume(vol)
    if _player:
        _player.audioOutput().setVolume(vol)

def _set_audio_enabled(enabled: bool):
    global _music_enabled
    _music_enabled = enabled
    cfg.set_music_enabled(enabled)

def _reload_audio_settings():
    """Re-read the saved music settings (after Reset Config wiped them). The
    module copies kept the old choice: with music turned off, a reset left the
    window saying "Music: On" while _start_audio, which reads the copy, stayed
    silent."""
    global _music_volume, _music_enabled
    _music_volume = saved_music_volume()
    _music_enabled = saved_music_enabled()
    if _player:
        try:
            _player.audioOutput().setVolume(_music_volume)
        except Exception:
            _log.debug("audio volume reload failed", exc_info=True)
    if not _music_enabled:
        _kill_audio()


# ── UI helpers ────────────────────────────────────────────────────────────────

def _header_path(appid: int) -> str:
    return os.path.join(HEADERS_DIR, f"{appid}_grid.jpg")

def _button_style(color):
    """Shared button variant; light faction colors always use dark ink."""
    foreground = C_BG if color in (C_IW, C_TREY) else C_TEXT
    # The ring is drawn in the button's own sheet: a per-widget sheet always wins
    # over the app sheet, so the app's QPushButton:focus rule never reaches these.
    # A transparent border of the same width keeps the size steady on focus; the
    # white ring reads on every variant (dark, red, green and orange fills).
    return (
        f"QPushButton{{background:{color};color:{foreground};"
        f"border:{FOCUS_RING_W}px solid transparent;"
        "border-radius:8px;padding:0 18px;}"
        f"QPushButton:hover{{border-color:{C_DIM};}}"
        f"QPushButton:focus{{border:{FOCUS_RING_W}px solid {C_FOCUS};}}"
        f"QPushButton:pressed{{background:{C_SURFACE};color:{C_TEXT};}}"
        f"QPushButton:disabled{{background:{C_CARD};color:{C_DIM};border-color:{C_LINE};}}"
    )


def _btn(text, color, size=13, h=BTN_MIN_H):
    """Standard action button. Defaults are Deck scale (13pt bold, 44px); pass
    smaller values only where space truly forces it. autoDefault makes Return /
    Enter (the controller's A button) click the focused button outside dialogs too."""
    b = QPushButton(text); b.setFont(font(size, True)); b.setFixedHeight(h)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setAccessibleName(text.replace("&&", "&"))
    b.setStyleSheet(_button_style(color))
    b.setAutoDefault(True)
    return b

def _lbl(text, size=14, color="#FFF", bold=False, align=Qt.AlignmentFlag.AlignCenter, wrap=True):
    w = QLabel(text); w.setFont(font(size,bold)); w.setAlignment(align)
    w.setWordWrap(wrap); w.setStyleSheet(f"color:{color};background:transparent;")
    return w

def _header_bar():
    """Shared compact screen header; screen-specific actions follow the build badge."""
    hdr = QWidget(); hdr.setFixedHeight(60)
    hdr.setObjectName("deckopsHeader")
    hdr.setStyleSheet(f"QWidget#deckopsHeader{{background:{C_CARD};border-bottom:1px solid {C_LINE};}}")
    hl = QHBoxLayout(hdr); hl.setContentsMargins(20,0,20,0); hl.setSpacing(10)
    t = QLabel("DECKOPS"); t.setFont(font(21, display=True))
    t.setStyleSheet(f"color:{C_TEXT};background:transparent;"); hl.addWidget(t)
    b = QLabel(BUILD_BADGE.upper()); b.setFont(font(9, bold=True))
    b.setObjectName("deckopsBuildBadge")
    b.setStyleSheet(
        f"color:{C_ACCENT};background:{C_ACCENT_BG};border:1px solid {C_ACCENT};"
        "border-radius:4px;padding:3px 8px;")
    # A plain QLabel in an HBox stretched to the full 60px header height, so the
    # badge became a tall orange slab. Keep it at its text height, centered.
    b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    b.setAlignment(Qt.AlignmentFlag.AlignCenter)
    hl.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter); hl.addStretch()
    return hdr, hl

def _badge(text, color, w, h=30, size=10, radius=6):
    b = QPushButton(text); b.setFont(font(size, True)); b.setFixedSize(w, h); b.setEnabled(False)
    ink = C_BG if color in (C_IW, C_TREY) else C_TEXT
    b.setStyleSheet(
        f"QPushButton{{background:{color};color:{ink};border:none;border-radius:{radius}px;}}"
        f"QPushButton:disabled{{background:{color};color:{ink};}}")
    return b

def _back_row(lay, slot):
    b = _btn(LABELS["back"], C_DARK_BTN, size=FONT_ACTION, h=BTN_MIN_H); b.setFixedWidth(120)
    b.clicked.connect(slot)
    row = QHBoxLayout(); row.addWidget(b); row.addStretch(); lay.addLayout(row)
    return b


# ── Status marks ──────────────────────────────────────────────────────────────
# ✓ ✗ ⚠ ⏳ ← are not in the bundled fonts (Atkinson Hyperlegible, Russo One), so
# on a Deck they fell back to whatever the system had, or drew as boxes. Worker
# code may keep writing them (the log file is fine with them); anything shown in
# the UI goes through _log_html / _plain_marks, or uses _mark / MARK_* directly.
# The replacements are plain ASCII words, which every bundled face covers, and
# color is never the only signal.

MARK_OK   = "OK"
MARK_FAIL = "FAILED"
MARK_WARN = "WARNING"
MARK_WAIT = "WAIT"

_MARK_STYLE = {
    "ok":   (MARK_OK,   C_IW),
    "fail": (MARK_FAIL, C_FAIL_TEXT),
    "warn": (MARK_WARN, C_TREY),
    "wait": (MARK_WAIT, C_DIM),
}
# Unsupported glyph -> mark kind (None: decorative, dropped)
_GLYPH_KIND = {
    "\u2713": "ok", "\u2714": "ok",          # ✓ ✔
    "\u2717": "fail", "\u2718": "fail",      # ✗ ✘
    "\u26a0": "warn",                        # ⚠
    "\u23f3": "wait",                        # ⏳
    "\u2190": None,                          # ←
}
_VS16 = "\ufe0f"     # emoji presentation selector that may trail ⚠


def _mark(kind: str) -> str:
    """Rich-text status mark for QLabel/QMessageBox/appendHtml: a bold, colored word.
    kind is "ok", "fail", "warn" or "wait"."""
    word, color = _MARK_STYLE[kind]
    return f'<b style="color:{color}">{word}</b>'


def _split_mark(text: str):
    """(leading whitespace, mark kind or None, rest) for a line that may start
    with one of the unsupported status glyphs."""
    stripped = text.lstrip()
    lead = text[:len(text) - len(stripped)]
    first = stripped[:1]
    if first in _GLYPH_KIND and _GLYPH_KIND[first]:
        rest = stripped[1:]
        if rest.startswith(_VS16):
            rest = rest[1:]
        return lead, _GLYPH_KIND[first], rest
    return lead, None, text


def _strip_glyphs(text: str) -> str:
    for g in _GLYPH_KIND:
        text = text.replace(g, "")
    return text.replace(_VS16, "")


def _plain_marks(text: str) -> str:
    """Plain-text version of a status string for QLabel.setText / button text:
    a leading ✓ ✗ ⚠ ⏳ becomes OK / FAILED / WARNING / WAIT, and any other
    unsupported glyph (a trailing ✓, "← Back") is dropped."""
    lead, kind, rest = _split_mark(text)
    if kind:
        return lead + _MARK_STYLE[kind][0] + _strip_glyphs(rest).rstrip()
    out = _strip_glyphs(text)
    return out.strip() if out != text else out


def _log_html(text: str) -> str:
    """Colorize a log line by its leading marker. Everything else stays dim.
    The marker itself is replaced by a supported-font word (see _mark)."""
    lead, kind, rest = _split_mark(text)
    color = _MARK_STYLE[kind][1] if kind else C_DIM

    def esc(s):
        return html.escape(_strip_glyphs(s)).replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")

    body = esc(lead) + (_mark(kind) if kind else "") + esc(rest)
    return f'<span style="color:{color};white-space:pre-wrap">{body}</span>'


def _hdiv():
    d = QFrame(); d.setFrameShape(QFrame.Shape.HLine); d.setFixedHeight(1)
    d.setStyleSheet(f"background:{C_LINE};border:none;"); return d

def _detached_open(args):
    """
    Launch an external command fully detached from DeckOps.

    A short-lived sh in a new session starts the command in the background
    and exits at once, so the command is adopted by PID 1 (init/systemd) and
    has no parent relationship to DeckOps. It will not appear as a child
    or subprocess of DeckOps in any task manager.

    This used to os.fork() the whole (multithreaded Qt) process: when the
    exec failed (e.g. no xdg-open), the forked copy fell into the except
    below and carried on as a second DeckOps; a failed second fork also
    left the parent stuck in waitpid. Nothing of DeckOps runs in the child
    now; a failed launch is only logged.
    """
    if not args or not shutil.which(args[0]):
        _log.warning("detached_open: %s not found, cannot open %s",
                     args[0] if args else "(nothing)", args[1:])
        return
    try:
        sh = subprocess.Popen(
            ["/bin/sh", "-c", 'exec "$@" </dev/null >/dev/null 2>&1 &', "sh", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        sh.wait(timeout=5)       # returns at once; reaps the sh
    except (OSError, subprocess.SubprocessError):
        _log.warning("detached_open failed for %s", args, exc_info=True)


# ── Install dialogs ───────────────────────────────────────────────────────────

def _ask_iw4x_dlc(parent, selected) -> str:
    """
    Ask about free IW4x DLC maps during install.

    Returns:
      "install" - download DLC (user said yes)
      "keep"    - DLC already present, skip silently
      ""        - no IW4x selected, or user declined
    """
    iw4x_games = [(k, gd, g) for k, gd, g in selected if KEY_CLIENT.get(k) == "iw4x"]
    if not iw4x_games:
        return ""
    from iw4x import is_iw4x_dlc_installed
    dlc_present = all(is_iw4x_dlc_installed(g["install_dir"])
                      for _, _, g in iw4x_games if g.get("install_dir"))

    if dlc_present:
        return "keep"

    reply = QMessageBox.question(
        parent,
        "Free DLC Maps",
        "Install free DLC maps for Modern Warfare 2?\n\n"
        "Includes CoD4, MW3, Black Ops, and CoD Online maps.\n"
        "Recommended for joining most servers.\n\n"
        "Download size: ~3 GB",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    return "install" if reply == QMessageBox.StandardButton.Yes else ""

# ── Title block ───────────────────────────────────────────────────────────────

def _show_preflight(parent, checks) -> bool:
    """
    Show the pre-install checks. Returns True to go ahead.

    Nothing here is a hard stop: the space figures are estimates, so a
    wrong one must not wall off a valid install. The recommendation is
    plain and the choice stays with the user.
    """
    import preflight as _pf
    if not checks:
        return True
    level = _pf.worst(checks)
    if level == _pf.OK:
        return True

    kinds = {_pf.OK: "ok", _pf.WARN: "warn", _pf.BLOCK: "fail"}
    colors = {_pf.OK: C_IW, _pf.WARN: C_TREY, _pf.BLOCK: C_TREY}
    rows = []
    for c in checks:
        rows.append(
            f'<div style="color:{colors[c.level]}"><b>{_mark(kinds[c.level])}&nbsp;&nbsp;'
            f'{html.escape(c.title)}</b></div>'
            f'<div style="color:#999999;margin-bottom:6px">'
            f'&nbsp;&nbsp;&nbsp;&nbsp;{html.escape(c.detail)}</div>')

    msg = QMessageBox(parent)
    msg.setWindowTitle("Before installing")
    msg.setTextFormat(Qt.TextFormat.RichText)
    msg.setText(
        ("<b>Some checks did not pass.</b><br><br>" if level == _pf.BLOCK
         else "<b>Worth knowing before you start.</b><br><br>")
        + "".join(rows))
    go = msg.addButton("Install Anyway", QMessageBox.ButtonRole.AcceptRole)
    back = msg.addButton("Go Back", QMessageBox.ButtonRole.RejectRole)
    msg.setDefaultButton(back if level == _pf.BLOCK else go)
    msg.exec()
    return msg.clickedButton() is go


def _title_block(lay, main_size=56):
    t = QLabel("DECKOPS")
    t.setFont(font(main_size, display=True))
    t.setAlignment(Qt.AlignmentFlag.AlignCenter)
    t.setStyleSheet(f"color:{C_TEXT}; background:transparent;")
    lay.addWidget(t)
    sub = QLabel()
    sub.setTextFormat(Qt.TextFormat.RichText)
    sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
    sub.setStyleSheet(f"color:{C_ACCENT}; background:transparent;")
    sub.setText(
        f'<span style="font-family:\'{_FONT_FAMILY_DISP}\'; font-size:28pt; color:{C_ACCENT};">'
        f'COMBAT'
        f'<span style="font-size:16pt;">on</span>'
        f'DECK'
        f'</span>'
    )
    lay.addWidget(sub)
    # Build badge — always shown (orange for nightly, green for stable)
    badge = QLabel(BUILD_BADGE)
    badge.setFont(font(10, bold=True))
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setStyleSheet(
        f"color:{C_ACCENT};background:{C_ACCENT_BG};border:1px solid {C_ACCENT};"
        "border-radius:4px;padding:2px 10px;"
    )
    bw = QHBoxLayout(); bw.addStretch(); bw.addWidget(badge); bw.addStretch()
    lay.addLayout(bw)


# ── Shared signals ────────────────────────────────────────────────────────────

class _Sigs(QObject):
    progress    = pyqtSignal(int, str)
    log         = pyqtSignal(str)
    done        = pyqtSignal(bool)
    plut_wait   = pyqtSignal()
    plut_go     = pyqtSignal()
    # Yes/No question from a worker thread (UpdateScreen: incomplete save backup)
    ask         = pyqtSignal(str)
    pulse_start = pyqtSignal(str)
    pulse_stop  = pyqtSignal()
    # Download retry dialog: (label, error_msg)
    retry_dl    = pyqtSignal(str, str)
    # Manual download fallback: (url, dest_folder, filename, label)
    manual_dl   = pyqtSignal(str, str, str, str)
    # IW5 (MW3) 32-bit depot downgrade
    iw5_dg_wait   = pyqtSignal(str, str)   # manual gate: (command, step_label)
    iw5_dg_go     = pyqtSignal()           # manual gate: dismiss
    iw5_qr_show   = pyqtSignal(str)        # QR path: display/refresh QR text
    iw5_qr_hide   = pyqtSignal()           # QR path: dismiss QR display
    iw5_dg_choose = pyqtSignal()           # show method chooser dialog
    iw5_trim_ask  = pyqtSignal()           # offer to trim duplicate 64-bit files


# ── App stylesheet ────────────────────────────────────────────────────────────

def _app_style():
    check = os.path.join(PROJECT_ROOT, "assets", "images", "check.svg").replace("\\", "/")
    ring, ind = FOCUS_RING_W, CHECK_INDICATOR
    return f"""
QWidget {{ background-color:{C_BG}; color:{C_TEXT}; }}
QLabel {{ background:transparent; }}
QDialog, QMessageBox {{ background:{C_CARD}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background:{C_BG}; border:none; }}
QScrollBar:vertical {{ background:{C_CARD}; width:10px; border-radius:5px; }}
QScrollBar::handle:vertical {{ background:{C_LINE}; border-radius:5px; min-height:32px; }}
QScrollBar::handle:vertical:hover {{ background:{C_DIM}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QProgressBar {{ background:{C_SURFACE}; color:{C_TEXT}; border:1px solid {C_LINE}; border-radius:6px; text-align:center; }}
QProgressBar::chunk {{ background:{C_ACCENT}; border-radius:5px; }}
QCheckBox, QRadioButton {{ spacing:10px; background:transparent; font-size:{FONT_ACTION}pt; border:{ring}px solid transparent; border-radius:6px; padding:2px 6px; }}
QCheckBox::indicator {{ width:{ind}px; height:{ind}px; border:2px solid {C_LINE}; border-radius:6px; background:{C_SURFACE}; }}
QCheckBox::indicator:checked {{ background:{C_IW}; border-color:{C_IW}; image:url("{check}"); }}
QCheckBox::indicator:disabled {{ background:{C_CARD}; border-color:{C_LINE}; }}
QRadioButton::indicator {{ width:{ind}px; height:{ind}px; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{ background:{C_CARD}; color:{C_TEXT}; border:1px solid {C_LINE}; border-radius:7px; padding:6px 10px; selection-background-color:{C_IW}; selection-color:{C_BG}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border:2px solid {C_FOCUS}; }}
QPushButton:focus, QCheckBox:focus, QRadioButton:focus {{ outline:none; border:{ring}px solid {C_FOCUS}; }}
QMessageBox QPushButton, QDialogButtonBox QPushButton {{ min-height:{BTN_MIN_H}px; min-width:110px; padding:0 14px; font-size:{FONT_ACTION}pt; font-weight:bold; background:{C_DARK_BTN}; color:{C_TEXT}; border:{ring}px solid {C_LINE}; border-radius:8px; }}
QMessageBox QPushButton:hover, QDialogButtonBox QPushButton:hover {{ border-color:{C_DIM}; }}
QMessageBox QPushButton:focus, QDialogButtonBox QPushButton:focus {{ border:{ring}px solid {C_FOCUS}; }}
QMessageBox QPushButton:pressed, QDialogButtonBox QPushButton:pressed {{ background:{C_SURFACE}; }}
QMessageBox QLabel {{ font-size:{FONT_BODY}pt; }}
QSlider {{ background:transparent; min-height:32px; border:2px solid transparent; border-radius:6px; }}
QSlider:focus {{ border:2px solid {C_FOCUS}; }}
QSlider::groove:horizontal {{ height:8px; background:{C_SURFACE}; border:1px solid {C_LINE}; border-radius:4px; }}
QSlider::sub-page:horizontal {{ background:{C_ACCENT}; border-radius:4px; }}
QSlider::handle:horizontal {{ background:{C_TEXT}; width:22px; margin:-8px 0; border:2px solid {C_LINE}; border-radius:11px; }}
QSlider::handle:horizontal:focus {{ background:{C_ACCENT}; border:{ring}px solid {C_FOCUS}; }}
QSlider::handle:horizontal:disabled {{ background:{C_LINE}; }}
QToolTip {{ background:{C_SURFACE}; color:{C_TEXT}; border:1px solid {C_LINE}; padding:6px; }}
"""


# ── Named screen navigation ──────────────────────────────────────────────────
#
# Replace all hardcoded setCurrentIndex(N) / widget(N) calls with these.
# Each screen sets self.screen_name in __init__; these helpers look it up.

def go_to(stack, name):
    """Navigate to a screen by name. Returns the screen widget."""
    for i in range(stack.count()):
        w = stack.widget(i)
        if getattr(w, "screen_name", w.__class__.__name__) == name:
            stack.setCurrentIndex(i)
            return w
    return None

def get_screen(stack, name):
    """Get a screen widget by name without navigating to it."""
    for i in range(stack.count()):
        w = stack.widget(i)
        if getattr(w, "screen_name", w.__class__.__name__) == name:
            return w
    return None


# ── Controller navigation ────────────────────────────────────────────────────
#
# In Game Mode Steam turns the D-pad into arrow keys and A into Return/Enter;
# B is usually Escape. Qt alone handles none of that well: QCheckBox ignores
# Return, a non-dialog QPushButton ignores Return unless autoDefault, and a
# QScrollArea swallows arrows to scroll instead of moving to its buttons.
#
# Screen protocol (all optional, looked up on the current QStackedWidget page):
#   back_action()     Escape/B calls it when nothing modal is open.
#   escape_allowed()  return False to ignore Escape for now (install running).
#   focus_target()    widget to focus when the screen is shown and nothing on
#                     it has focus yet; default is the first focusable widget.
#
# The filter only acts inside widgets opted in with _nav_scope (DeckOpsWindow
# is, and so is every dialog parented to it).
#
# Helpers for screen code:
#   _no_focus_scroll(sa)   scroll area never takes focus; follows focused child
#   _scroll_area(widget)   new QScrollArea(widgetResizable) set up the same way
#   _chain_focus([w, ...]) explicit tab/D-pad order
#   _focus_first(w | [w])  focus first visible, enabled candidate (deferred)

_ACTIVATE_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select)
_PREV_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Left)
_NEXT_KEYS = (Qt.Key.Key_Down, Qt.Key.Key_Right)
_NAV_KEYS = _PREV_KEYS + _NEXT_KEYS


def _policy_bits(widget) -> int:
    p = widget.focusPolicy()
    return int(getattr(p, "value", p))


def _tab_focusable(widget) -> bool:
    """Reachable by Tab / D-pad right now: TabFocus policy, visible, enabled."""
    try:
        tab = int(getattr(Qt.FocusPolicy.TabFocus, "value", Qt.FocusPolicy.TabFocus))
        return bool(_policy_bits(widget) & tab) and widget.isVisible() and widget.isEnabled()
    except RuntimeError:        # deleted C++ object
        return False


def _has_focusable_child(widget) -> bool:
    for child in widget.findChildren(QWidget):
        if _tab_focusable(child) and not isinstance(child, QScrollArea):
            return True
    return False


def _is_nav_candidate(widget) -> bool:
    # A QScrollArea holding focusable widgets is a container, never a stop: it
    # used to take first focus on several screens and then ate the D-pad.
    if not _tab_focusable(widget):
        return False
    if isinstance(widget, QScrollArea) and _has_focusable_child(widget):
        return False
    return True


def _first_focusable(target):
    """First visible, enabled, Tab-focusable widget: the first such entry of a
    list, or for a widget itself-or-descendant in focus-chain order. None if none."""
    if target is None:
        return None
    if isinstance(target, (list, tuple)):
        for w in target:
            if w is not None and _is_nav_candidate(w):
                return w
        return None
    if _is_nav_candidate(target):
        return target
    w = target.nextInFocusChain()
    seen = 0
    while w is not None and w is not target and seen < 2000:
        if target.isAncestorOf(w) and _is_nav_candidate(w):
            return w
        w = w.nextInFocusChain(); seen += 1
    return None


def _focus_first(target, reason=Qt.FocusReason.TabFocusReason):
    """Focus the first visible, enabled candidate of a widget or list. Deferred
    with QTimer.singleShot(0) so it works from showEvent (children are not
    visible yet there)."""
    def go():
        try:
            w = _first_focusable(target)
        except RuntimeError:
            return
        if w is not None:
            w.setFocus(reason)
    QTimer.singleShot(0, go)


def _chain_focus(widgets):
    """Set the Tab / D-pad order to follow the given list. None entries and
    widgets that can never take focus (NoFocus) are skipped; hidden or disabled
    ones stay in the chain because Qt skips them at navigation time, so a button
    shown or enabled later still lands in the right place."""
    nofocus = int(getattr(Qt.FocusPolicy.NoFocus, "value", Qt.FocusPolicy.NoFocus))
    chain = [w for w in widgets if w is not None and _policy_bits(w) != nofocus]
    for a, b in zip(chain, chain[1:]):
        QWidget.setTabOrder(a, b)
    return chain


_SCROLL_FOLLOW_APP = None


def _scroll_to_focus(_old, new):
    if new is None:
        return
    try:
        p = new.parentWidget()
        while p is not None:
            if isinstance(p, QScrollArea) and p.widget() is not None and p.widget().isAncestorOf(new):
                p.ensureWidgetVisible(new, 24, 24)
            p = p.parentWidget()
    except RuntimeError:
        pass


def _install_scroll_follow():
    """Once per QApplication: scroll every QScrollArea that holds the newly
    focused widget so the widget is on screen."""
    global _SCROLL_FOLLOW_APP
    app = QApplication.instance()
    if app is None or _SCROLL_FOLLOW_APP is app:
        return
    app.focusChanged.connect(_scroll_to_focus)
    _SCROLL_FOLLOW_APP = app


def _no_focus_scroll(sa):
    """Keep a QScrollArea out of the focus chain (so the D-pad goes to the
    widgets inside it, not to the area's own scrolling) and have it scroll the
    focused child into view. Returns sa."""
    sa.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    _install_scroll_follow()
    return sa


def _scroll_area(widget=None, resizable=True):
    """QScrollArea factory: frameless, widgetResizable, no focus of its own."""
    sa = QScrollArea()
    sa.setWidgetResizable(resizable)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    if widget is not None:
        sa.setWidget(widget)
    return _no_focus_scroll(sa)


def _current_page(widget):
    """The visible screen for a widget: the current page of its window's stack
    (DeckOpsWindow.stack), else the window itself."""
    win = widget.window()
    stack = getattr(win, "stack", None)
    if isinstance(stack, QStackedWidget) and stack.currentWidget() is not None:
        return stack.currentWidget()
    return win


def _screen_back_action(screen):
    """The screen's Escape handler, or None (none defined, or opted out)."""
    allowed = getattr(screen, "escape_allowed", None)
    if callable(allowed):
        try:
            if not allowed():
                return None
        except Exception:
            _log.debug("escape_allowed failed", exc_info=True)
            return None
    action = getattr(screen, "back_action", None) or getattr(screen, "_on_escape", None)
    return action if callable(action) else None


def _screen_focus_target(screen):
    target = getattr(screen, "focus_target", None)
    if callable(target):
        try:
            w = target()
        except Exception:
            _log.debug("focus_target failed", exc_info=True)
            w = None
        if w is not None and _tab_focusable(w):
            return w
    return _first_focusable(screen)


def _ensure_focus_in(screen):
    """Deferred: when nothing inside screen has focus (and no dialog is up),
    focus its focus_target() or first focusable widget. A screen's own
    _focus_first from showEvent runs first and wins."""
    def go():
        try:
            if screen is None or not screen.isVisible() or QApplication.activeModalWidget():
                return
            fw = QApplication.focusWidget()
            if fw is not None and (fw is screen or screen.isAncestorOf(fw)):
                return
            w = _screen_focus_target(screen)
            if w is not None:
                w.setFocus(Qt.FocusReason.TabFocusReason)
        except RuntimeError:
            pass
    QTimer.singleShot(0, go)


def _consumes_arrows(widget) -> bool:
    """Widgets that keep their own arrow keys (text cursor, slider, spin box,
    list/combo, scrolling a text-only area). Buttons and checkboxes do not."""
    if isinstance(widget, QAbstractButton):
        return False
    if isinstance(widget, QScrollArea):
        # A plain scroll area with nothing focusable inside scrolls; one holding
        # buttons is a container the D-pad should move through.
        return not _has_focusable_child(widget)
    return True


_NAV_PROP = "deckopsNav"


def _nav_scope(widget, enabled=True):
    """Turn controller navigation on (or off) for widget and everything under it,
    including dialogs parented to it. The nearest ancestor with the property set
    decides; DeckOpsWindow turns it on. Use enabled=False on a custom widget that
    handles its own arrow keys."""
    widget.setProperty(_NAV_PROP, bool(enabled))
    return widget


def _nav_enabled(widget) -> bool:
    w = widget
    while w is not None:
        v = w.property(_NAV_PROP)
        if v is not None:
            return bool(v)
        w = w.parentWidget()        # crosses into a dialog's parent window
    return False


class _ControllerNav(QObject):
    """Application event filter mapping controller keys onto focus navigation.
    Only acts inside widgets that opted in with _nav_scope (any other window in
    the process, e.g. a test's, keeps plain Qt keys)."""

    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Type.KeyPress or not isinstance(obj, QWidget):
                return False
            if not _nav_enabled(obj):
                return False
            return self._key(obj, event)
        except RuntimeError:        # receiver deleted mid-event
            return False

    def _key(self, obj, event):
        key = event.key()
        if key not in _ACTIVATE_KEYS and key not in _NAV_KEYS and key != Qt.Key.Key_Escape:
            return False
        mods = event.modifiers()
        keypad = int(getattr(Qt.KeyboardModifier.KeypadModifier, "value",
                             Qt.KeyboardModifier.KeypadModifier))
        if (int(getattr(mods, "value", mods)) & ~keypad) != 0:
            return False        # Shift+Tab-like combos and shortcuts stay Qt's
        fw = QApplication.focusWidget()
        # An app filter sees the event again for each parent it propagates to;
        # act once, on the widget that has focus (or the window, if none does).
        if fw is not None:
            if obj is not fw:
                return False
        elif not obj.isWindow():
            return False
        if QApplication.activePopupWidget() is not None:
            return False

        if key == Qt.Key.Key_Escape:
            return self._escape(obj)

        if fw is None:
            # Nothing focused (screen just shown): the first press only lands focus.
            w = _screen_focus_target(_current_page(obj))
            if w is None:
                return False
            w.setFocus(Qt.FocusReason.TabFocusReason)
            return True

        if key in _ACTIVATE_KEYS:
            if isinstance(fw, QAbstractButton):
                if event.isAutoRepeat():
                    return True             # holding A must not fire repeatedly
                if fw.isEnabled():
                    fw.click()              # QCheckBox/QRadioButton toggle, QPushButton clicks
                return True
            return False

        # Arrow keys
        if _consumes_arrows(fw):
            return False
        self._move(fw, key in _NEXT_KEYS)
        return True

    @staticmethod
    def _move(fw, forward):
        tab = QKeyEvent(QEvent.Type.KeyPress,
                        Qt.Key.Key_Tab if forward else Qt.Key.Key_Backtab,
                        Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(fw, tab)
        # Skip containers that still sit in the chain (a QScrollArea of buttons
        # that was not given _no_focus_scroll).
        for _ in range(8):
            now = QApplication.focusWidget()
            if now is None or now is fw or _is_nav_candidate(now):
                break
            QApplication.sendEvent(now, QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Tab if forward else Qt.Key.Key_Backtab,
                Qt.KeyboardModifier.NoModifier))

    @staticmethod
    def _escape(obj):
        if QApplication.activeModalWidget() is not None:
            return False                    # the dialog's own Escape = reject
        page = _current_page(obj)
        action = _screen_back_action(page)
        if action is None:
            return False
        action()
        return True


_NAV_FILTER = None


def install_controller_nav(app=None, scope=None):
    """Install the controller-navigation event filter (once per QApplication)
    and the focus-follows-scroll hook; scope, if given, is opted in with
    _nav_scope. Returns the filter."""
    global _NAV_FILTER
    if scope is not None:
        _nav_scope(scope)
    app = app or QApplication.instance()
    if app is None:
        return None
    if _NAV_FILTER is not None:
        try:
            if _NAV_FILTER.parent() is app:
                return _NAV_FILTER
        except RuntimeError:
            pass
    _NAV_FILTER = _ControllerNav(app)
    app.installEventFilter(_NAV_FILTER)
    _install_scroll_follow()
    return _NAV_FILTER
