"""
ui_configure.py — ConfigureScreen, DeckOps's Settings screen

Includes the in-app 'Update DeckOps' button and its download-and-apply worker.
Moved out of ui_manage.py, which had grown past 2,400 lines.
"""

import os, subprocess, sys, shutil, stat, threading, urllib.request, urllib.error, urllib.parse, compileall

from qt import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
    QPlainTextEdit,
    QMessageBox, QLineEdit, QSlider, QDialog,
)
from qt import Qt, QTimer

from detect_games import find_steam_root
import config as cfg

from ui_constants import (
    C_IW, C_TREY, C_DIM, C_DARK_BTN, C_RED_BTN, C_BLUE_BTN,
    font, _btn, _lbl, _copy_log_to_clipboard,
    _Sigs, _detached_open, _header_bar, PROJECT_ROOT,
    _music_volume, _music_enabled,
    _set_audio_volume, _set_audio_enabled, _start_audio, _kill_audio,
    go_to,
)

from identity import GITHUB_USER, GITHUB_REPO
from log import get_logger
from updater import HEADERS, check_update_status, remote_changes

_log = get_logger(__name__)


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

        # ── Left: Background Music ────────────────────────────────────────
        _head(left, "Background Music", first=True)
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
        left.addWidget(_lbl("Closes Steam, then re-adds any missing DeckOps shortcuts.",
                            11, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
        sc_fix_btn = _btn("Repair Shortcuts", C_DARK_BTN, size=12, h=40)
        sc_fix_btn.setFixedWidth(220); sc_fix_btn.clicked.connect(self._repair_shortcuts)
        left.addWidget(sc_fix_btn)

        # ── Left: Player Name ─────────────────────────────────────────────
        _head(left, "Player Name")
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
        self._name_note = _lbl("Sets your offline name for CoD4x, IW4x, AlterWare, T7X, and Plutonium. Does not affect CleanOps.", 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        left.addWidget(self._name_note)

        # ── Left: Shader Cache (LCD only) ─────────────────────────────────
        self._shader_row = QWidget()
        sr_lay = QVBoxLayout(self._shader_row); sr_lay.setContentsMargins(0,0,0,0); sr_lay.setSpacing(10)
        sr_lay.addSpacing(12)
        sr_lay.addWidget(_lbl("Shader Cache", 13, "#CCC", bold=True, align=Qt.AlignmentFlag.AlignLeft))
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
        right.addWidget(_lbl("These remove DeckOps changes. Each asks for confirmation first.",
                             11, C_DIM, align=Qt.AlignmentFlag.AlignLeft))
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
        self.status.setText(f"Gyro mode set to {mode.upper()}. Hit Re-apply Templates to update controller profiles.")

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
        self._update_sig.log.emit(check_update_status())

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
        """Download every file that differs from GitHub main, stage, check, apply, then signal relaunch."""
        from net import download
        from steam_common import git_blob_sha
        install_dir = PROJECT_ROOT
        update_dir = os.path.join(install_dir, ".update")
        try:
            local_sha, remote_sha, stale, gone = remote_changes(removed=True)
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
                         label=os.path.basename(path), timeout=30, headers=HEADERS)
                if git_blob_sha(dest) != sha:
                    raise RuntimeError(f"{path} does not match GitHub")
                got += size
            self._apply_sig.progress.emit(99, "Installing update...")

            # A syntax error pushed to main would otherwise brick every install on relaunch
            if not compileall.compile_dir(update_dir, quiet=1):
                raise RuntimeError("update has Python errors, nothing applied")

            # Copied over the old file, not renamed: offline copies hard-link some of these
            for path, (_, mode, _) in stale.items():
                target = os.path.join(install_dir, path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(os.path.join(update_dir, path), target)
                if mode == "100755":
                    os.chmod(target, 0o755)

            # Delete files removed or renamed away upstream, so stale modules can never shadow current ones
            for path in gone:
                target = os.path.join(install_dir, path)
                if os.path.isfile(target):
                    os.remove(target)
                    _log.info("Removed stale file: %s", path)

            shutil.rmtree(update_dir, ignore_errors=True)

            # chmod +x shell scripts
            for script in ("launcher.sh", "deckops_uninstall.sh", "install.sh"):
                spath = os.path.join(install_dir, script)
                if os.path.isfile(spath):
                    os.chmod(spath, os.stat(spath).st_mode | stat.S_IEXEC)

            # Last, so an update that failed part way is retried in full
            with open(os.path.join(install_dir, "VERSION"), "w") as f:
                f.write(remote_sha)

            # Clean up stale flag file if present
            flag = os.path.expanduser("~/.deckops_update_requested")
            if os.path.isfile(flag):
                os.remove(flag)

            _log.info("Update applied successfully — SHA %s (%d file(s), %d removed)", remote_sha, total, len(gone))
            self._apply_sig.log.emit("RELAUNCH|Update complete! Relaunching...")

        except Exception as ex:
            _log.error("Update failed: %s", ex)
            shutil.rmtree(update_dir, ignore_errors=True)
            self._apply_sig.log.emit(f"FAIL|Update failed: {ex}")

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
