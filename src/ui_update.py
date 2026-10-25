"""
ui_update.py — UpdateScreen, which runs updates for the installed games

Reached from ManagementScreen. Moved out of ui_manage.py.
"""

import os, shutil, threading

from qt import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPlainTextEdit,
)
from qt import Qt, QTimer

from detect_games import find_all_games
import config as cfg
import inhibit

from ui_constants import (
    C_TREY, C_DIM, C_DARK_BTN, font, _btn, _lbl, _log_to_file, _log_html, _copy_log_to_clipboard,
    _Sigs, ALL_GAMES, KEY_CLIENT,
    _active_keys, go_to,
)

from log import get_logger

_log = get_logger(__name__)


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
        bk = QHBoxLayout(); bk.addStretch(); bk.addWidget(self.back_btn); bk.addStretch()
        lay.addLayout(bk)

        self._s = _Sigs()
        self._s.progress.connect(lambda p,m: (self.bar.setValue(p), self.cur.setText(m)))
        self._s.log.connect(self._append_log)
        self._s.done.connect(self._on_done)
        self._s.plut_wait.connect(lambda: self.steam_btn.setVisible(True))
        self._s.plut_go.connect(lambda: self.steam_btn.setVisible(False))

    def _append_log(self, text):
        self.log.appendHtml(_log_html(text))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        _log_to_file(text)

    def showEvent(self, e):
        super().showEvent(e)
        # Spontaneous = window restored / re-mapped, not a screen change: that
        # used to clear the log and start a second update thread
        if e.spontaneous():
            return
        self.bar.setValue(0); self.log.clear()
        self.title.setText(self.task[0] if self.task else "UPDATE")
        self.steam_btn.setVisible(False); self.back_btn.setVisible(False)
        self._steam_closed.clear()
        inhibit.start("DeckOps is updating")
        _log_to_file("── Update started ──")
        QTimer.singleShot(400, lambda: threading.Thread(target=self._run, daemon=True).start())

    def _on_done(self, _):
        inhibit.stop()
        self.cur.setText("Done! It is now safe to open Steam.")
        self.back_btn.setVisible(True)

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
            self._s.log.emit(f"✗  Update failed: {ex}")
            self._s.progress.emit(100, "Update failed.")
            self._s.done.emit(False)

    def _run_steps(self):
        task, self.task = self.task, None
        if task:
            try:
                task[1](self._s)
            except Exception as ex:
                self._s.log.emit(f"✗  {task[0].title()} failed: {ex}")
            self._s.progress.emit(100, "All done!")
            self._s.done.emit(True)
            return
        from wrapper import get_proton_path, find_compatdata, kill_steam, set_compat_tool
        from ge_proton import install_ge_proton, MANAGED_APPIDS
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
                self._s.progress.emit(100, "Update failed.")
                self._s.done.emit(True)
                return
            except Exception as ex:
                self._s.log.emit(f"✗  Plutonium launch failed: {ex}")
                self._s.progress.emit(100, "Update failed.")
                self._s.done.emit(True)
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
                self._s.progress.emit(100, "Update incomplete.")
                self._s.done.emit(True)
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
            self._s.log.emit(f"  Could not close Steam: {ex}")

        # ── Clean slate: clear launch options and compat tools ─────────
        # Same clean-slate approach as InstallScreen.
        # Prevents stale entries from previous installs (LCD→OLED, older
        # DeckOps versions) interfering with the update.
        # Launch options only for the Steam games in this update, so a
        # per-game update doesn't wipe other games' options.
        try:
            from wrapper import clear_launch_options, clear_compat_tool
            from detect_games import GAMES
            run_appids = {GAMES[k]["appid"] for k, _, g in self.selected
                          if k in GAMES and (g or {}).get("source") != "own"}
            for appid in MANAGED_APPIDS:
                if appid in run_appids:
                    clear_launch_options(self.steam_root, appid)
            clear_compat_tool(MANAGED_APPIDS)
            self._s.log.emit("✓  Cleared launch options and compat tools")
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
                self._s.log.emit(f"✗  {base_name} ({key}) failed: {ex}")

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
        if _do_zd and cfg.is_zd_installed() and any(k == "t6zm" for k, _, _ in self.selected):
            try:
                self._s.log.emit("Checking Zombies Declassified for updates...")
                from zombies_declassified import resolve_zd_storage
                _zd_storage = resolve_zd_storage({k: g for k, _, g in self.selected}.get("t6zm"))

                from zombies_declassified import is_zd_installed as _zd_at
                if not _zd_storage or not _zd_at(_zd_storage):
                    self._s.log.emit(f"⚠  Zombies Declassified not found in {_zd_storage}, skipped (nothing downloaded).")
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
                self._s.log.emit(f"⚠  Zombies Declassified update skipped: {ex}")

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

        self._s.progress.emit(100, "All done!")
        self._s.done.emit(True)
