"""
ui_setup.py — First-run setup flow for DeckOps

Flow: [Autodetect Confirm] → OS → Device → Name → [Play Mode / Resolution] → Done

When both OS and device are detected from DMI and os-release, a confirm
screen lets the user accept with one click or fall through to the manual
flow. All OS paths now go through the model screen so a Deck on Bazzite
or CachyOS can still pick LCD/OLED.
"""

from qt import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QScrollArea, QLabel, QProgressBar
from qt import Qt

import config as cfg
from detect_hw import detect_os, detect_device

from ui_constants import (
    C_CARD, C_LINE, C_TEXT, C_IW, C_TREY, C_DIM, C_DARK_BTN,
    font, _btn, _lbl, _title_block, _back_row,
    go_to,
)


# ── Device definitions ────────────────────────────────────────────────────────
#
# Each device maps to a model and native resolution. The bundled Steam Input
# layout targets Steam Deck controls only; device detection is still needed for
# display, Proton and Plutonium setup.

DEVICES = {
    "sd_lcd":       {"label": "Steam Deck LCD",       "deck_model": "lcd",   "other_device": None,              "other_device_type": None,         "has_gyro": True},
    "sd_oled":      {"label": "Steam Deck OLED",      "deck_model": "oled",  "other_device": None,              "other_device_type": None,         "has_gyro": True},
    "legion_go":    {"label": "Lenovo Legion Go",     "deck_model": "other", "other_device": "1920x1200",       "other_device_type": "legion_go",  "has_gyro": True},
    "legion_go_s":  {"label": "Lenovo Legion Go S",   "deck_model": "other", "other_device": "1920x1200",       "other_device_type": "legion_go_s","has_gyro": True},
    "legion_go_2":  {"label": "Lenovo Legion Go 2",   "deck_model": "other", "other_device": "1920x1200_144hz", "other_device_type": "legion_go_2","has_gyro": True},
    "rog_ally":     {"label": "ROG Ally",              "deck_model": "other", "other_device": "1920x1080",       "other_device_type": "2btn",       "has_gyro": True},
    "rog_ally_x":   {"label": "ROG Ally X",            "deck_model": "other", "other_device": "1920x1080",       "other_device_type": "2btn",       "has_gyro": True},
    "xbox_ally_x":  {"label": "ROG Xbox Ally X",       "deck_model": "other", "other_device": "1920x1080",       "other_device_type": "2btn",       "has_gyro": True},
    "msi_claw_8":   {"label": "MSI Claw 8",           "deck_model": "other", "other_device": "1920x1200",       "other_device_type": "2btn",       "has_gyro": True},
    "rp6":          {"label": "Retroid Pocket 6",     "deck_model": "other", "other_device": "1920x1080",       "other_device_type": "generic",    "has_gyro": False},
    "general_pc":   {"label": "Desktop / laptop PC",   "deck_model": "other", "other_device": None,              "other_device_type": "generic",    "has_gyro": True},
    "other_handheld":{"label": "Other handheld",       "deck_model": "other", "other_device": None,              "other_device_type": "generic",    "has_gyro": True},
    "steam_machine":{"label": "Steam Machine",         "deck_model": "steam_machine", "other_device": None,  "other_device_type": "steam_machine", "has_gyro": True},
}


# Texts used on more than one setup screen
_INTRO = ("DeckOps sets up community clients and display settings for your Call of Duty "
          "games. On Steam Deck it also applies one bundled Steam Input layout.")
_NOTES = [
    "⚠   DeckOps creates the Proton prefixes for your games. You don't need to launch each "
    "game through Steam first.",
    "⚠   To play Plutonium games online (WaW, BO1, BO2, MW3), create a free account at "
    "plutonium.pw before you continue.",
    "⚠   Use a stable internet connection. If the install fails, don't re-run it again and "
    "again; ask for help on the Discord instead.",
]
class SetupFlowScreen(QWidget):
    """
    Unified first-run setup. One QWidget with show/hide sections
    for progressive disclosure. screen_name = "SetupFlowScreen".
    """

    def __init__(self, stack):
        super().__init__()
        self.stack = stack
        self.screen_name = "SetupFlowScreen"
        self._selected_os = None      # "steamos", "bazzite", "cachyos", "other_linux"
        self._selected_device = None  # key from DEVICES
        self._is_general_pc = False
        self._is_steam_machine = False

        self._autodetected = False
        self._choosing_native_resolution = False
        self._has_entered = False
        self._scrolls = {}

        main_lay = QVBoxLayout(self)
        self._main_lay = main_lay
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # Keep orientation and Back visible even when a long step is scrolled.
        header = QWidget(self)
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(24, 14, 24, 10)
        header_lay.setSpacing(8)
        self._step_label = QLabel()
        self._step_label.setFont(font(12, bold=True))
        self._step_label.setStyleSheet(f"color:{C_TREY};")
        header_lay.addWidget(self._step_label)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setAccessibleName("Setup progress")
        header_lay.addWidget(self._progress)
        main_lay.addWidget(header)

        self._steps = {
            "_confirm_section": ("REVIEW DETECTED HARDWARE", 5),
            "_os_section": ("OPERATING SYSTEM", 10),
            "_model_section": ("DEVICE", 25),
            "_device_section": ("SELECT DEVICE", 30),
            "_name_section": ("PLAYER NAME", 50),
            "_play_section": ("PLAY MODE", 78),
            "_resolution_section": ("DISPLAY RESOLUTION", 85),
        }

        # Only offer one-click confirmation when both values are genuinely known.
        self._detected_os = detect_os()
        self._detected_device = detect_device()

        self._confirm_section = QWidget(); self._confirm_section.setVisible(False)
        cl = QVBoxLayout(self._confirm_section)
        cl.setContentsMargins(28, 18, 28, 18); cl.setSpacing(12)
        _title_block(cl, main_size=32)
        os_label = {"steamos": "SteamOS", "bazzite": "Bazzite", "cachyos": "CachyOS", "armada": "Armada"}.get(
            self._detected_os, self._detected_os or "Unknown")
        dev_label = DEVICES.get(self._detected_device, {}).get("label", self._detected_device or "Unknown")
        self._confirm_lbl = _lbl(f"Detected {os_label} on {dev_label}. Is this correct?", 15, C_TEXT)
        cl.addWidget(self._confirm_lbl)
        cl.addWidget(_lbl("Online Plutonium play needs an account; setup needs a stable connection.",
                          12, C_TREY))
        cl.addSpacing(12)
        crow = QHBoxLayout(); crow.setSpacing(12)
        yes_btn = _btn("Use detected", C_IW, size=12, h=48)
        yes_btn.setAccessibleName("Use detected operating system and device")
        change_btn = _btn("Choose manually", C_DARK_BTN, size=12, h=48)
        change_btn.setAccessibleName("Choose operating system and device manually")
        yes_btn.clicked.connect(self._accept_autodetect)
        change_btn.clicked.connect(self._reject_autodetect)
        crow.addWidget(yes_btn); crow.addWidget(change_btn)
        cl.addLayout(crow)
        cl.addSpacing(12)
        cl.addWidget(_lbl(_INTRO, 13, C_DIM))
        for warn in _NOTES:
            cl.addWidget(_lbl(warn, 12, C_TREY, align=Qt.AlignmentFlag.AlignLeft))
        self._register_step("_confirm_section")

        # ── 1. OS section ─────────────────────────────────────────────────
        self._os_section = QWidget()
        lay = QVBoxLayout(self._os_section)
        lay.setContentsMargins(28, 18, 28, 18); lay.setSpacing(10)
        _title_block(lay, main_size=32)
        lay.addWidget(_lbl("What operating system are you running?", 15, C_TEXT))
        lay.addWidget(_lbl("Online Plutonium play needs an account; setup needs a stable connection.",
                           12, C_TREY))
        lay.addSpacing(12)

        os_row = QHBoxLayout(); os_row.setSpacing(10)
        for index, (os_key, label) in enumerate([
            ("steamos", "SteamOS"),
            ("bazzite", "Bazzite"),
            ("cachyos", "CachyOS"),
            ("armada", "Armada"),
            ("other_linux", "Other Linux"),
        ]):
            if index == 3:
                lay.addLayout(os_row)
                os_row = QHBoxLayout(); os_row.setSpacing(10)
            b = _btn(label, C_IW, h=48)
            b.clicked.connect(lambda checked, k=os_key: self._pick_os(k))
            os_row.addWidget(b)
        lay.addLayout(os_row)
        lay.addSpacing(12)
        lay.addWidget(_lbl(_INTRO, 13, C_DIM))
        for warn in _NOTES:
            lay.addWidget(_lbl(warn, 12, C_TREY, align=Qt.AlignmentFlag.AlignLeft))
        self._register_step("_os_section")

        # ── 2. Device model section ───────────────────────────────────────
        self._model_section = QWidget(); self._model_section.setVisible(False)
        ml = QVBoxLayout(self._model_section)
        ml.setContentsMargins(28, 18, 28, 18); ml.setSpacing(16)
        self._back_os_btn = _back_row(ml, self._back_to_os)
        ml.addSpacing(12)
        _title_block(ml, main_size=32)
        ml.addWidget(_lbl("Which device do you have?", 15, C_TEXT))
        ml.addSpacing(12)
        mrow = QHBoxLayout(); mrow.setSpacing(20)
        lcd_btn  = _btn("Steam Deck LCD",  C_IW, h=56)
        oled_btn = _btn("Steam Deck OLED", C_IW, h=56)
        sm_btn   = _btn("Steam Machine",   C_IW, h=56)
        other_btn = _btn("Other Device",   C_TREY, h=56)
        lcd_btn.clicked.connect(lambda: self._pick_device("sd_lcd"))
        oled_btn.clicked.connect(lambda: self._pick_device("sd_oled"))
        sm_btn.clicked.connect(lambda: self._pick_device("steam_machine"))
        other_btn.clicked.connect(self._show_device_picker)
        mrow.addWidget(lcd_btn); mrow.addWidget(oled_btn)
        ml.addLayout(mrow)
        mrow2 = QHBoxLayout(); mrow2.setSpacing(12)
        mrow2.addWidget(sm_btn); mrow2.addWidget(other_btn)
        ml.addLayout(mrow2)
        ml.addSpacing(12)
        self._register_step("_model_section")

        # ── 3. Specific device picker (Other) ────────────────────────────
        self._device_section = QWidget(); self._device_section.setVisible(False)
        dvl = QVBoxLayout(self._device_section)
        dvl.setContentsMargins(28, 18, 28, 18); dvl.setSpacing(16)
        self._back_model_btn = _back_row(dvl, self._back_to_model)
        dvl.addSpacing(12)
        _title_block(dvl, main_size=32)
        dvl.addWidget(_lbl("Select your device", 15, C_TEXT))
        dvl.addSpacing(4)
        dvl.addWidget(_lbl(
            "Choose your exact model when listed. Otherwise select Other handheld "
            "or Desktop / laptop PC; don't choose another model as a substitute.",
            13, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        dvl.addSpacing(12)

        dev_cols = QHBoxLayout(); dev_cols.setSpacing(20)

        # Left column: Lenovo
        col_lenovo = QVBoxLayout(); col_lenovo.setSpacing(10)
        col_lenovo.addWidget(_lbl("Lenovo", 12, C_TREY, bold=True))
        for dev_key in ("legion_go", "legion_go_s", "legion_go_2"):
            b = _btn(DEVICES[dev_key]["label"], C_DARK_BTN, h=48)
            b.clicked.connect(lambda checked, k=dev_key: self._pick_device(k))
            col_lenovo.addWidget(b)
        dev_cols.addLayout(col_lenovo)

        # Middle column: ASUS
        col_asus = QVBoxLayout(); col_asus.setSpacing(10)
        col_asus.addWidget(_lbl("ASUS", 12, C_TREY, bold=True))
        for dev_key in ("rog_ally", "rog_ally_x", "xbox_ally_x"):
            b = _btn(DEVICES[dev_key]["label"], C_DARK_BTN, h=48)
            b.clicked.connect(lambda checked, k=dev_key: self._pick_device(k))
            col_asus.addWidget(b)
        dev_cols.addLayout(col_asus)

        # Right column: MSI + PC
        col_right = QVBoxLayout(); col_right.setSpacing(10)
        col_right.addWidget(_lbl("MSI", 12, C_TREY, bold=True))
        msi_btn = _btn(DEVICES["msi_claw_8"]["label"], C_DARK_BTN, h=48)
        msi_btn.clicked.connect(lambda: self._pick_device("msi_claw_8"))
        col_right.addWidget(msi_btn)
        col_right.addSpacing(10)
        col_right.addWidget(_lbl("Retroid", 12, C_TREY, bold=True))
        rp6_btn = _btn(DEVICES["rp6"]["label"], C_DARK_BTN, h=48)
        rp6_btn.clicked.connect(lambda: self._pick_device("rp6"))
        col_right.addWidget(rp6_btn)
        col_right.addSpacing(10)
        col_right.addWidget(_lbl("Other", 12, C_TREY, bold=True))
        handheld_btn = _btn("Other handheld", C_DARK_BTN, h=48)
        handheld_btn.clicked.connect(lambda: self._pick_device("other_handheld"))
        col_right.addWidget(handheld_btn)
        pc_btn = _btn("Desktop / laptop PC", C_DARK_BTN, h=48)
        pc_btn.clicked.connect(lambda: self._pick_device("general_pc"))
        col_right.addWidget(pc_btn)
        # Two columns fit an 800px window; the remaining devices follow below
        # instead of forcing a clipped third column with hidden buttons.
        dvl.addLayout(dev_cols)
        dvl.addLayout(col_right)
        dvl.addSpacing(12)
        self._register_step("_device_section")

        # ── 4. Player name section ────────────────────────────────────────
        self._name_section = QWidget(); self._name_section.setVisible(False)
        nl = QVBoxLayout(self._name_section)
        nl.setContentsMargins(28, 18, 28, 18); nl.setSpacing(16)
        self._back_name_btn = _back_row(nl, self._back_to_device_from_name)
        nl.addSpacing(12)
        _title_block(nl, main_size=32)
        nl.addWidget(_lbl("What's your player name?", 15, C_TEXT))
        nl.addSpacing(4)
        nl.addWidget(_lbl(
            "Your name in IW4x and Plutonium offline. "
            "Your Steam name is filled in; change it to anything you like.",
            13, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        nl.addSpacing(12)
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Player")
        self._name_input.setAccessibleName("Player name")
        self._name_input.setMaxLength(32)
        self._name_input.setFixedHeight(48)
        self._name_input.setFont(font(14))
        self._name_input.setStyleSheet(
            f"QLineEdit{{background:{C_CARD};color:{C_TEXT};border:2px solid {C_LINE};"
            f"border-radius:8px;padding:0 16px;}}"
            f"QLineEdit:focus{{border-color:{C_IW};}}"
        )
        nl.addWidget(self._name_input)
        nl.addSpacing(16)
        name_cont = _btn("Continue >>", C_IW, h=52)
        name_cont.setFixedWidth(260)
        name_cont.clicked.connect(self._save_player_name)
        nc_row = QHBoxLayout(); nc_row.addStretch(); nc_row.addWidget(name_cont); nc_row.addStretch()
        nl.addLayout(nc_row)
        nl.addSpacing(12)
        self._register_step("_name_section")

        # ── 5. Resolution section (General PC, or docked) ─────────────────
        self._resolution_section = QWidget(); self._resolution_section.setVisible(False)
        rl = QVBoxLayout(self._resolution_section)
        rl.setContentsMargins(28, 18, 28, 18); rl.setSpacing(16)
        self._back_res_btn = _back_row(rl, self._back_to_prev_from_res)
        rl.addSpacing(12)
        _title_block(rl, main_size=32)
        self._res_title_lbl = _lbl("What resolution is your display?", 15, C_TEXT)
        rl.addWidget(self._res_title_lbl)
        rl.addSpacing(4)
        rl.addWidget(_lbl(
            "Choose your screen resolution, or choose Set in game to configure it "
            "yourself later.",
            13, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        rl.addSpacing(12)

        res_cols = QHBoxLayout(); res_cols.setSpacing(20)
        col_1610 = QVBoxLayout(); col_1610.setSpacing(10)
        col_1610.addWidget(_lbl("16:10", 13, C_IW, bold=True))
        for res_key, label in [("1280x800", "1280 x 800"), ("1920x1200", "1920 x 1200")]:
            b = _btn(label, C_DARK_BTN, h=52)
            b.clicked.connect(lambda checked, k=res_key: self._pick_resolution(k))
            col_1610.addWidget(b)
        res_cols.addLayout(col_1610)

        col_169 = QVBoxLayout(); col_169.setSpacing(10)
        col_169.addWidget(_lbl("16:9", 13, C_IW, bold=True))
        for res_key, label in [("1280x720", "1280 x 720"), ("1920x1080", "1920 x 1080")]:
            b = _btn(label, C_DARK_BTN, h=52)
            b.clicked.connect(lambda checked, k=res_key: self._pick_resolution(k))
            col_169.addWidget(b)
        res_cols.addLayout(col_169)

        rl.addLayout(res_cols)
        rl.addSpacing(8)
        own_res = _btn("Set in game", C_DARK_BTN, h=44)
        own_res.setAccessibleName("Set my own resolution in the game")
        own_res.setFixedWidth(200)
        own_res.clicked.connect(lambda: self._pick_resolution("own"))
        own_row = QHBoxLayout(); own_row.addStretch(); own_row.addWidget(own_res); own_row.addStretch()
        rl.addLayout(own_row)
        rl.addSpacing(12)
        self._register_step("_resolution_section")

        # ── 8. Play mode section (handhelds only, not General PC) ─────────
        self._play_section = QWidget(); self._play_section.setVisible(False)
        pl = QVBoxLayout(self._play_section)
        pl.setContentsMargins(28, 18, 28, 18); pl.setSpacing(16)
        self._back_play_btn = _back_row(pl, self._back_to_prev_from_play)
        pl.addSpacing(12)
        _title_block(pl, main_size=32)
        pl.addWidget(_lbl("How do you play?", 15, C_TEXT))
        pl.addSpacing(4)
        pl.addWidget(_lbl(
            "Handheld Only:  you only play on the device's own screen.\n"
            "Also Docked:  you also play on a TV or monitor with an external controller.",
            13, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        pl.addSpacing(12)
        prow = QHBoxLayout(); prow.setSpacing(20)
        hh_btn = _btn("Handheld Only", C_DARK_BTN, h=56)
        dk_btn = _btn("Also Docked",   C_DARK_BTN, h=56)
        hh_btn.clicked.connect(lambda: self._pick_play_mode("handheld"))
        dk_btn.clicked.connect(lambda: self._pick_play_mode("docked"))
        prow.addWidget(hh_btn); prow.addWidget(dk_btn)
        pl.addLayout(prow)
        pl.addSpacing(12)
        self._register_step("_play_section")

        # Keep the legacy back-button references for callers, but use the
        # persistent footer instead of repeating Back inside every scroll area.
        for back_button in (
            self._back_os_btn, self._back_model_btn, self._back_name_btn,
            self._back_res_btn, self._back_play_btn,
        ):
            back_button.hide()

        # Pinned navigation never disappears below long descriptions or choices.
        footer = QWidget(self)
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(24, 8, 24, 10)
        self._footer_back = _btn("Back to previous step", C_DARK_BTN, size=12, h=38)
        self._footer_back.clicked.connect(self._go_back)
        footer_lay.addWidget(self._footer_back)
        footer_lay.addStretch()
        self._main_lay.addWidget(footer)
        self.reset_flow()

    # ── Section visibility helpers ────────────────────────────────────────

    def _register_step(self, name):
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(getattr(self, name))
        self._scrolls[name] = area
        self._main_lay.addWidget(area, 1)

    def _hide_all(self):
        for name, area in self._scrolls.items():
            area.hide()
            getattr(self, name).hide()

    def _show(self, section_name):
        self._hide_all()
        self._current_section = section_name
        area = self._scrolls[section_name]
        getattr(self, section_name).show()
        area.show()
        area.verticalScrollBar().setValue(0)
        label, progress = self._steps[section_name]
        if section_name == "_resolution_section" and self._choosing_native_resolution:
            label, progress = "HANDHELD RESOLUTION", 74
        self._step_label.setText(f"INITIAL SETUP  /  {label}")
        self._progress.setValue(progress)
        self._progress.setAccessibleDescription(f"{label}, {progress}% through setup")
        self._footer_back.setVisible(section_name not in ("_confirm_section", "_os_section"))

    def _go_back(self):
        back = {
            "_model_section": self._back_to_os,
            "_device_section": self._back_to_model,
            "_name_section": self._back_to_device_from_name,
            "_play_section": self._back_to_prev_from_play,
            "_resolution_section": self._back_to_prev_from_res,
        }
        if self._current_section in back:
            back[self._current_section]()

    def reset_flow(self):
        """Clear wizard-local choices after a settings reset (not on normal Back)."""
        self._selected_os = None
        self._selected_device = None
        self._is_general_pc = False
        self._is_steam_machine = False
        self._autodetected = False
        self._choosing_native_resolution = False
        self._name_input.clear()
        self._detected_os = detect_os()
        self._detected_device = detect_device()
        os_label = {"steamos": "SteamOS", "bazzite": "Bazzite", "cachyos": "CachyOS",
                    "armada": "Armada"}.get(self._detected_os, self._detected_os or "Unknown")
        dev_label = DEVICES.get(self._detected_device, {}).get("label", "Unknown")
        self._confirm_lbl.setText(f"Detected {os_label} on {dev_label}. Is this correct?")
        self._show("_confirm_section" if self._detected_os and self._detected_device
                   else "_os_section")

    def showEvent(self, event):
        super().showEvent(event)
        if event.spontaneous():
            return
        # Reset Config returns via Bootstrap using the same screen instance.
        # Back from the game scan must *not* clear in-progress choices.
        if (self._has_entered and cfg.is_first_run() and self._selected_os
                and cfg.load().get("os_type") is None):
            self.reset_flow()
        self._has_entered = True

    # ── Navigation logic ─────────────────────────────────────────────────

    def _accept_autodetect(self):
        self._autodetected = True
        self._pick_os(self._detected_os)
        self._pick_device(self._detected_device)

    def _reject_autodetect(self):
        self._autodetected = False
        self._show("_os_section")

    def _pick_os(self, os_key):
        self._selected_os = os_key
        cfg.set_os_type(os_key)
        self._show("_model_section")

    def _back_to_os(self):
        self._show("_os_section")

    def _pick_device(self, dev_key):
        same_device = self._selected_device == dev_key
        if self._selected_device is not None and not same_device:
            cfg.set_play_mode(None)
            cfg.set_docked_resolution(None)
            cfg.set_external_controller(None)
        self._choosing_native_resolution = False
        self._selected_device = dev_key
        dev = DEVICES[dev_key]
        self._is_general_pc = (dev_key == "general_pc")
        self._is_steam_machine = (dev_key == "steam_machine")

        # Save device config
        cfg.set_deck_model(dev["deck_model"])
        # Clear stale device settings on a model change, but keep a custom
        # resolution if the user navigated Back and chose the same model again.
        if not same_device or dev["other_device"] is not None:
            cfg.set_other_device(dev["other_device"])

        # Steam Deck's single bundled layout is assigned during install.
        # There is no controller or gyro setup question.
        self._show_name_section()

    def _show_device_picker(self):
        self._show("_device_section")

    def _back_to_model(self):
        self._show("_model_section")

    def _back_to_device_from_name(self):
        if self._autodetected:
            self._show("_confirm_section")
            return
        dev = DEVICES.get(self._selected_device, {})
        self._show("_model_section" if dev.get("deck_model") in ("lcd", "oled", "steam_machine")
                   else "_device_section")

    def _show_name_section(self):
        """Show the player name input, pre-filled with Steam display name."""
        if not self._name_input.text():
            saved = cfg.get_player_name()
            if saved:
                self._name_input.setText(saved)
            else:
                steam_name = cfg.get_steam_display_name()
                if steam_name:
                    self._name_input.setText(steam_name)
        self._show("_name_section")

    def _save_player_name(self):
        # Characters that break configs or scripts are dropped (see
        # config.clean_player_name); the box shows what was actually saved
        name = cfg.clean_player_name(self._name_input.text()) or "Player"
        self._name_input.setText(name)
        cfg.set_player_name(name)
        cfg.set_game_source("both")
        if self._is_steam_machine:
            self._res_title_lbl.setText("What resolution is your display?")
            self._show("_resolution_section")
        elif self._is_general_pc or self._selected_device == "other_handheld":
            if self._is_general_pc:
                cfg.set_play_mode("docked")
            self._choosing_native_resolution = self._selected_device == "other_handheld"
            self._res_title_lbl.setText("What resolution is your handheld screen?" if self._choosing_native_resolution
                                         else "What resolution is your display?")
            self._show("_resolution_section")
        else:
            self._show("_play_section")

    def _back_to_prev_from_play(self):
        """Return to the last step on the chosen device's path."""
        if self._selected_device == "other_handheld":
            self._choosing_native_resolution = True
            self._res_title_lbl.setText("What resolution is your handheld screen?")
            self._show("_resolution_section")
        else:
            self._show("_name_section")

    def _pick_play_mode(self, mode):
        cfg.set_play_mode(mode)
        if mode == "docked":
            self._choosing_native_resolution = False
            self._res_title_lbl.setText("What resolution is your external display?")
            self._show("_resolution_section")
        else:
            self._finish()

    # ── Resolution ────────────────────────────────────────────────────────

    def _back_to_prev_from_res(self):
        if self._choosing_native_resolution:
            self._show("_name_section")
        elif self._is_steam_machine:
            self._show("_name_section")
        elif self._is_general_pc:
            self._show("_name_section")
        else:
            # Docked: back to play mode
            self._show("_play_section")

    def _pick_resolution(self, resolution):
        if self._choosing_native_resolution:
            cfg.set_other_device(resolution)
            self._show("_play_section")
            return
        if self._is_steam_machine:
            # Steam Machine: store resolution in other_device for config dir resolution
            cfg.set_other_device(resolution)
            self._finish()
            return
        cfg.set_docked_resolution(resolution)
        self._finish()

    # ── Finish ────────────────────────────────────────────────────────────

    def _finish(self):
        # Setup hands straight over to the game scan; there is no optional-mods step.
        go_to(self.stack, "WelcomeScreen" if cfg.is_first_run() else "ManagementScreen")
