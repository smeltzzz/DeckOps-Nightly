"""Fifth sweep: install / library / settings UI regressions (offscreen Qt).

Every installer, Steam call and config write is replaced, so nothing touches a
real Steam, game folder or ~/DeckOps-Nightly.
"""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import detect_games  # noqa: E402
import setup_check  # noqa: E402
from guide_text import how_to_start_text  # noqa: E402

try:
    from qt import QApplication, QStackedWidget, Qt, QWidget
    import ui_constants
    import ui_install
    import ui_manage
except ImportError:
    QApplication = None
    QWidget = None

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class _SyncThread:
    """threading.Thread stand-in that runs the target on start()."""

    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


if QWidget is not None:
    class _Destination(QWidget):
        def __init__(self, name):
            super().__init__()
            self.screen_name = name
else:
    _Destination = None


# ── install pipeline ─────────────────────────────────────────────────────────

class _Cfg:
    """The slice of config the install pipeline reads or writes."""

    def __init__(self):
        self.unmarked = []

    def is_lcd(self): return False
    def is_first_run(self): return False
    def complete_first_run(self, root): pass
    def is_game_setup_for_source(self, key, source): return True
    def unmark_game_setup(self, keys): self.unmarked.append(keys)
    def get_offline_mode(self): return False
    def set_offline_mode(self, on): pass
    def set_ge_proton_version(self, version): pass
    def mark_game_setup(self, key, client, source="steam"): pass
    def get_setup_games(self): return {}
    def get_gyro_mode(self): return "on"
    def get_deck_model(self): return "oled"


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class InstallShortcutResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.cfg = _Cfg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for target in (ui_install, ui_constants):
            p = patch.object(target, "cfg", self.cfg)
            p.start(); self.addCleanup(p.stop)
        for name in ("_log_to_file", "inhibit"):
            p = patch.object(ui_install, name, MagicMock())
            p.start(); self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.screen = ui_install.InstallScreen(self.stack)
        self.stack.addWidget(self.screen)
        self.addCleanup(self.stack.close)
        self.logs = []
        self.screen._s.log.connect(self.logs.append)
        self.mocks = {}

    def _entry(self, key):
        gd = next(g for g in ui_constants.ALL_GAMES if key in g["keys"])
        game_dir = Path(self.root, "games", key)
        game_dir.mkdir(parents=True, exist_ok=True)
        return key, gd, {"install_dir": str(game_dir), "source": "steam", "name": key}

    def _select(self, steam=(), own=()):
        self.screen.steam_selected = [self._entry(k) for k in steam]
        self.screen.own_selected = {k: self._entry(k)[2] for k in own}
        self.screen.steam_root = self.root
        self.screen.offline_mode = None

    def _run(self, overrides=None):
        targets = {
            "wrapper.get_proton_path": {"return_value": "proton"},
            "wrapper.find_compatdata": {"return_value": self.root},
            "wrapper.kill_steam": {},
            "wrapper.set_compat_tool": {"return_value": True},
            "wrapper.clear_launch_options": {},
            "wrapper.clear_compat_tool": {},
            "wrapper.set_steam_input_enabled": {},
            "wrapper.set_default_launch_option": {},
            "wrapper.clear_default_launch_option": {},
            "cod4r.install_cod4r": {},
            "iw3sp.install_iw3sp": {},
            "iw3sp.apply_cod4_launch_option": {"return_value": True},
            "iw3sp.cod4_launch_option": {"return_value": ""},
            "iw4x.install_iw4x": {},
            "t6sp_mod.install_t6sp_mod": {},
            "sp_mod.install_sp_mod": {},
            "ge_proton.install_ge_proton": {"return_value": "GE-Proton10-1"},
            "ge_proton.resolve_ge_version": {"return_value": "GE-Proton10-1"},
            "ge_proton.ensure_all_prefix_deps": {"return_value": 99},
            "ge_proton.cleanup_old_ge_versions": {},
            "plutonium.launch_bootstrapper": {},
            "plutonium.is_plutonium_ready": {"return_value": False},
            "plutonium.install_plutonium": {},
            "plutonium.cleanup_plut_from_non_plut_prefixes": {"return_value": 0},
            "game_config.apply_game_configs": {"return_value": (0, 0, 0)},
            "controller_profiles.install_controller_templates": {},
            "controller_profiles.assign_controller_profiles": {},
            "shortcut.create_shortcuts": {"return_value": set()},
            "shortcut.create_launcher_shortcut": {},
            "shortcut.apply_steam_artwork": {},
            "shortcut.enrich_own_games": {"side_effect": lambda own_games, **kw: own_games},
            "shortcut.write_own_shortcuts": {"return_value": set()},
            # A real config read otherwise; CoD4 MP as CoD4x keeps its shortcut
            "shortcut.mp_on_main_tile": {"return_value": False},
        }
        targets.update(overrides or {})
        with contextlib.ExitStack() as stack:
            for target, spec in targets.items():
                m = spec if isinstance(spec, MagicMock) else MagicMock(**spec)
                stack.enter_context(patch(target, m))
                self.mocks[target] = m
            self.screen._run()
        _APP.processEvents()

    def _row(self, key):
        return next(r for r in self.screen._results if r["key"] == key)

    def test_steam_shortcut_that_was_not_written_fails_only_that_game(self):
        self._select(steam=("cod4mp", "iw4mp"))
        self._run()
        self.assertEqual(self._row("cod4mp")["status"], "failed")
        self.assertEqual(self._row("cod4mp")["reason"], "Steam shortcut not created")
        # IW4x has no non-Steam shortcut: nothing to fail
        self.assertEqual(self._row("iw4mp")["status"], "ok")

    def test_written_steam_shortcut_reports_ready(self):
        self._select(steam=("cod4mp",))
        self._run({"shortcut.create_shortcuts": {"return_value": {"cod4mp"}}})
        self.assertEqual(self._row("cod4mp")["status"], "ok")

    def test_shortcut_exception_does_not_fail_games_without_a_shortcut(self):
        self._select(steam=("cod4mp", "iw4mp"))
        self._run({"shortcut.create_shortcuts": {"side_effect": RuntimeError("vdf broken")}})
        self.assertEqual(self._row("cod4mp")["status"], "failed")
        self.assertEqual(self._row("iw4mp")["status"], "ok")

    def test_main_tile_mp_is_not_failed_for_having_no_shortcut(self):
        self._select(steam=("cod4mp",))
        self._run({"shortcut.mp_on_main_tile": {"return_value": True}})
        self.assertEqual(self._row("cod4mp")["status"], "ok")

    def test_own_shortcut_that_was_not_written_fails_the_game(self):
        self._select(own=("cod4sp",))
        self._run()
        self.assertEqual(self._row("cod4sp")["status"], "failed")
        self.assertEqual(self._row("cod4sp")["reason"], "Steam shortcut not created")

    def test_written_own_shortcut_reports_ready(self):
        self._select(own=("cod4sp",))
        self._run({"shortcut.write_own_shortcuts": {"return_value": {"cod4sp"}}})
        self.assertEqual(self._row("cod4sp")["status"], "ok")

    def test_cod4_launch_option_written_nowhere_is_reported(self):
        self._select(steam=("cod4mp",))
        self._run({"iw3sp.apply_cod4_launch_option": {"return_value": False},
                   "iw3sp.cod4_launch_option": {"return_value": "%command%"},
                   "shortcut.create_shortcuts": {"return_value": {"cod4mp"}}})
        self.assertTrue(any("CoD4 launch option not set" in m for m in self.logs))

    def test_iw4x_warnings_stay_in_the_log(self):
        def install(game, sr, proton, compat, prog, **kw):
            prog(80, "  ⚠ launch option not set — launch the game once from Steam")
            prog(90, "Writing files...")
        self._select(steam=("iw4mp",))
        self._run({"iw4x.install_iw4x": {"side_effect": install}})
        self.assertTrue(any("⚠ launch option not set" in m for m in self.logs))
        self.assertFalse(any("Writing files" in m for m in self.logs))


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ShortcutExpectationTests(unittest.TestCase):
    def test_lcd_plutonium_own_games_are_left_to_heroic(self):
        own = {"t5mp": {"install_dir": "/g", "_own_actual_exe": None},
               "cod4sp": {"install_dir": "/g", "_own_actual_exe": "/g/iw3sp.exe"},
               "iw4mp": {}}
        self.assertEqual(ui_install._own_shortcut_keys(own, ["t5mp", "cod4sp", "iw4mp"]), {"cod4sp"})

    def test_waw_mp_off_lcd_and_main_tile_games_need_no_shortcut(self):
        games = {"cod4mp": {"install_dir": "/c"}, "t4mp": {"install_dir": "/w"},
                 "iw4mp": {"install_dir": "/m"}}
        with patch("shortcut.mp_on_main_tile", return_value=False), \
             patch.object(ui_install.cfg, "is_lcd", return_value=False):
            self.assertEqual(ui_install._steam_shortcut_keys(games, list(games)), {"cod4mp"})
        with patch("shortcut.mp_on_main_tile", return_value=True):
            self.assertEqual(ui_install._steam_shortcut_keys(games, list(games)), set())


# ── extra scan folders (Choose Folder) ───────────────────────────────────────

class ExtraScanPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = str(Path(self.tmp.name) / "deckops.json")
        for p in (patch.object(config, "CONFIG_PATH", path),
                  patch.object(config, "_cache", None),
                  patch.object(config, "_cache_key", None),
                  patch.object(config, "_unreadable", None)):
            p.start(); self.addCleanup(p.stop)

    def test_picked_folders_are_saved_once(self):
        self.assertEqual(config.get_extra_scan_paths(), [])
        folder = os.path.join(self.tmp.name, "Games")
        config.add_extra_scan_path(folder)
        config.add_extra_scan_path(folder)
        config.add_extra_scan_path("")
        self.assertEqual(config.get_extra_scan_paths(), [os.path.normpath(folder)])

    def test_damaged_saved_value_reads_as_no_folders(self):
        config._set("extra_scan_paths", "not a list")
        self.assertEqual(config.get_extra_scan_paths(), [])

    def test_library_scan_includes_saved_folders(self):
        folder = os.path.join(self.tmp.name, "Games")
        config.add_extra_scan_path(folder)
        with patch.object(detect_games, "parse_library_folders", return_value=[]), \
             patch.object(detect_games, "find_installed_games", return_value={}), \
             patch.object(detect_games, "find_own_installed", return_value={}) as own:
            detect_games.find_all_games("/steam")
            self.assertEqual(own.call_args.kwargs["extra_paths"], [os.path.normpath(folder)])
            detect_games.find_all_games("/steam", extra_paths=[])
            self.assertIsNone(own.call_args.kwargs["extra_paths"])

    @unittest.skipIf(QApplication is None, "PyQt is not installed")
    def test_setup_saves_only_folders_that_hold_a_found_game(self):
        with_game = os.path.join(self.tmp.name, "Games")
        empty = os.path.join(self.tmp.name, "Empty")
        screen = MagicMock(_extra_paths=[with_game, empty])
        own = {"cod4sp": {"install_dir": os.path.join(with_game, "CoD4")}}
        with patch.object(ui_install.cfg, "add_extra_scan_path") as add:
            ui_install.SetupScreen._remember_folders(screen, own)
        add.assert_called_once_with(with_game)


# ── guide text / setup check ─────────────────────────────────────────────────

class DamagedSetupEntryTests(unittest.TestCase):
    def test_guide_text_skips_entries_that_are_not_dicts(self):
        text = how_to_start_text({"cod4mp": "junk", "iw4mp": {"source": "steam"},
                                  "t5sp": None}, False)
        self.assertIn("MW2", text)
        self.assertNotIn("BO1", text)


class SetupCheckFixTests(unittest.TestCase):
    def _check(self, client):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(setup_check, "_appid", return_value="10180"), \
             patch.object(setup_check, "_compat_tool", return_value=""), \
             patch.object(setup_check, "uses_steam_prefix", return_value=False), \
             patch.object(setup_check, "_launch_options", return_value=[]), \
             patch.object(setup_check, "CLIENT_MARKERS", {}):
            return setup_check.check_game("iw4sp", {"client": client, "source": "steam"},
                                          {"install_dir": tmp}, "/steam", {})

    def test_vanilla_game_problems_have_no_fix_button(self):
        r = self._check("steam")
        self.assertIn("Steam is not set to run it with GE-Proton.", r["problems"])
        self.assertFalse(r["fixable"])

    def test_mod_client_problems_stay_fixable(self):
        self.assertTrue(self._check("iw-pad")["fixable"])


# ── Heroic shortcut result ───────────────────────────────────────────────────

class HeroicShortcutResultTests(unittest.TestCase):
    def setUp(self):
        import plutonium_lcd
        self.lcd = plutonium_lcd
        self.key = next(iter(plutonium_lcd.HEROIC_PLUT_GAMES))
        self.title = plutonium_lcd.HEROIC_PLUT_GAMES[self.key]["title"]
        for p in (patch.object(plutonium_lcd, "add_shortcut", return_value=1234),
                  patch("config.get_gyro_mode", return_value="on")):
            p.start(); self.addCleanup(p.stop)

    def test_shortcut_missing_from_steam_is_false(self):
        for present in (None, set()):
            with self.subTest(present=present), \
                 patch("shortcut.present_shortcut_names", return_value=present):
                self.assertIs(self.lcd._create_heroic_steam_shortcut(self.key, source="own"), False)

    def test_written_shortcut_is_truthy(self):
        with patch("shortcut.present_shortcut_names", return_value={self.title}):
            self.assertEqual(self.lcd._create_heroic_steam_shortcut(self.key, source="own"), 1234)

    def test_steam_source_needs_no_shortcut(self):
        self.assertIsNone(self.lcd._create_heroic_steam_shortcut(self.key, source="steam"))


# ── library / settings ───────────────────────────────────────────────────────

@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ManageSweep5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.state = {"steam_root": "/steam"}
        self.setup = {}
        patches = [
            patch.object(ui_manage.ManagementScreen, "_refresh", lambda _self: None),
            patch.object(ui_manage.ManagementCard, "_fetch", lambda _self, _appid: None),
            patch.object(ui_manage.cfg, "get_offline_mode", return_value=False),
            patch.object(ui_manage, "find_steam_root", return_value="/steam"),
            patch.object(ui_manage.cfg, "load", side_effect=lambda: dict(self.state)),
            patch.object(ui_manage.cfg, "get_setup_games", side_effect=lambda: self.setup),
            patch.object(ui_manage.cfg, "is_lcd", return_value=False),
            patch.object(ui_manage.inhibit, "busy", return_value=None),
            patch.object(ui_manage.inhibit, "start"),
            patch.object(ui_manage.inhibit, "stop"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.busy, self.inh_start, self.inh_stop = mocks[-3:]
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.library = ui_manage.ManagementScreen(self.stack)
        self.library._startup_checked = True
        self.stack.addWidget(self.library)
        for name in ("SetupScreen", "InstallScreen", "UpdateScreen"):
            self.stack.addWidget(_Destination(name))
        self.settings = ui_manage.ConfigureScreen(self.stack)
        self.stack.addWidget(self.settings)
        self.addCleanup(self.stack.close)

    # ── E1: self-update ────────────────────────────────────────────────────
    def _start_update(self):
        with patch.object(ui_manage.QMessageBox, "question",
                          return_value=ui_manage.QMessageBox.StandardButton.Yes) as ask, \
             patch.object(ui_manage.threading, "Thread") as thread:
            self.settings._apply_update()
        return ask, thread

    def test_self_update_holds_the_busy_lock_and_blocks_the_window(self):
        ask, thread = self._start_update()
        self.addCleanup(lambda: self.settings._update_pd.close())
        self.inh_start.assert_called_once_with("DeckOps is updating itself")
        thread.assert_called_once()
        self.assertEqual(self.settings._update_pd.windowModality(),
                         Qt.WindowModality.ApplicationModal)
        pd = self.settings._update_pd
        # A second click while it runs starts nothing and keeps the dialog
        ask2, thread2 = self._start_update()
        ask2.assert_not_called()
        thread2.assert_not_called()
        self.assertIs(self.settings._update_pd, pd)
        # The Library's background deadzone pass waits for it too
        self.assertTrue(self.library._settings_task_running())

    def test_failed_self_update_releases_the_lock(self):
        self._start_update()
        self.settings._on_apply_signal("FAIL|Update did not finish. offline")
        self.inh_stop.assert_called_once()
        self.assertFalse(self.settings._updating)

    def test_failed_relaunch_releases_the_lock(self):
        self._start_update()
        with patch.object(ui_manage.os, "execv", side_effect=OSError("gone")), \
             patch.object(ui_manage.shutil, "rmtree"):
            self.settings._relaunch()
        self.inh_stop.assert_called_once()

    # ── E5: background deadzone pass ───────────────────────────────────────
    def _scan_done(self):
        self.setup = {"iw4mp": {"client": "iw4x", "source": "steam"}}
        self.library._scanned = {}
        with patch.object(self.library, "_rebuild"), \
             patch.object(ui_manage.threading, "Thread") as thread:
            self.library._on_scanned(True)
        return thread

    def test_deadzone_pass_skips_while_something_else_runs(self):
        self.busy.return_value = "DeckOps is installing"
        self._scan_done().assert_not_called()
        self.busy.return_value = None
        self.settings._settings_running = True
        self._scan_done().assert_not_called()
        self.settings._settings_running = False
        self._scan_done().assert_called_once()

    # ── D3: partial config writes ──────────────────────────────────────────
    def _run_task(self, task, target):
        def write(steam_root, installed, *a, failures=None, **kw):
            failures.append(("/g/config.cfg", "read-only"))
            return 2
        with patch.object(ui_manage.threading, "Thread", _SyncThread), \
             patch.object(ui_manage, "find_all_games", return_value={}), \
             patch("detect_games.find_all_games", return_value={}), \
             patch(target, side_effect=write):
            task()
        return self.settings.status.text()

    def test_deadzone_with_unwritable_files_is_not_reported_as_done(self):
        # The status label shows the font-safe mark word, not the ⚠ glyph (sweep 6)
        text = self._run_task(self.settings._apply_deadzone, "game_config.set_deadzone")
        self.assertTrue(text.startswith("WARNING  Updated 2 file(s), 1 could not be written"), text)

    def test_fps_fov_with_unwritable_files_is_not_reported_as_done(self):
        text = self._run_task(self.settings._apply_fps_fov, "game_config.set_fps_fov")
        self.assertTrue(text.startswith("WARNING  Updated 2 file(s), 1 could not be written"), text)

    # ── D2: CoD4 launch option during Update ───────────────────────────────
    def test_update_fails_cod4_when_its_launch_option_is_written_nowhere(self):
        update = ui_manage.UpdateScreen(None)
        update._s = MagicMock()
        update.steam_root = "/steam"
        update._failed_games = []
        item = ("cod4mp", {"base": "MW1"}, {"source": "steam"})
        with patch("wrapper.clear_launch_options"), \
             patch("iw3sp.apply_cod4_launch_option", return_value=False), \
             patch("iw3sp.cod4_launch_option", return_value="%command%"):
            update._apply_cod4_launch_option([item])
        self.assertEqual(update._failed_games, [item])
        self.assertTrue(any("CoD4 launch option not set" in c.args[0]
                            for c in update._s.log.emit.call_args_list))

    def test_update_keeps_cod4_ok_when_no_launch_option_is_needed(self):
        update = ui_manage.UpdateScreen(None)
        update._s = MagicMock()
        update.steam_root = "/steam"
        update._failed_games = []
        item = ("cod4mp", {"base": "MW1"}, {"source": "steam"})
        with patch("wrapper.clear_launch_options"), \
             patch("iw3sp.apply_cod4_launch_option", return_value=False), \
             patch("iw3sp.cod4_launch_option", return_value=""):
            update._apply_cod4_launch_option([item])
        self.assertEqual(update._failed_games, [])

    # ── E6: Restore Saves ──────────────────────────────────────────────────
    def test_restore_holds_the_lock_and_continue_waits(self):
        screen = ui_manage.SetupCompleteScreen(self.stack)
        with patch.object(ui_manage.threading, "Thread"):
            screen._do_restore()
            screen._do_restore()            # a second tap starts nothing
        self.inh_start.assert_called_once()
        self.assertFalse(screen._continue_btn.isEnabled())
        screen._restore_finished("RETRY|⚠  Restore failed: disk full")
        self.inh_stop.assert_called_once()
        self.assertTrue(screen._continue_btn.isEnabled())
        with patch.object(ui_manage.threading, "Thread"), \
             patch.object(ui_manage.SetupCompleteScreen, "_mark_offer_handled"):
            screen._do_restore()
            screen._restore_finished("DONE|✓  Restored saves for 1 group(s): cod4.")
        self.assertEqual(self.inh_stop.call_count, 2)
        self.assertTrue(screen._continue_btn.isEnabled())

    # ── E8 / E9: Game Options ──────────────────────────────────────────────
    def _options(self, keys, installed):
        gd = next(g for g in ui_manage.ALL_GAMES if keys[0] in g["keys"])
        self.library.installed = installed
        titles = []
        real_btn = ui_manage._btn

        def spy(title, *a, **kw):
            titles.append(title)
            return real_btn(title, *a, **kw)
        with patch.object(ui_manage, "_btn", side_effect=spy), \
             patch.object(ui_manage.QDialog, "exec", lambda _self: 0), \
             patch("depot_downgrade.iw5sp_needs_verify", return_value=False):
            self.library._configure(gd, keys)
        return titles

    def test_mw3_campaign_alone_is_not_offered_a_downgrade(self):
        titles = self._options(["iw5sp"], {"iw5sp": {"install_dir": "/g", "source": "steam"}})
        self.assertNotIn("Downgrade MW3...", titles)
        self.assertIn("Reinstall Game...", titles)

    def test_mw3_multiplayer_is_offered_a_downgrade_even_with_a_damaged_entry(self):
        self.setup = {"iw5mp": "junk", "iw5sp": ["junk"]}
        titles = self._options(["iw5mp", "iw5sp"],
                               {"iw5mp": {"install_dir": "/g", "source": "steam"},
                                "iw5sp": {"install_dir": "/g", "source": "steam"}})
        self.assertIn("Downgrade MW3...", titles)

    def test_update_candidates_skip_damaged_entries(self):
        self.setup = {"iw4mp": "junk", "t5mp": {"client": "plutonium", "source": "steam"}}
        installed = {"iw4mp": {"source": "steam"}, "t5mp": {"source": "steam"}}
        self.assertEqual([k for k, _, _ in ui_manage._update_candidates(installed)], ["t5mp"])

    # ── E4: BO1 / BO2 mod folders ──────────────────────────────────────────
    def _open_mods(self, meta_key, pick):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        install = os.path.join(tmp.name, "bo2")
        os.makedirs(install)
        with open(os.path.join(install, "deckops_plutonium.json"), "w") as f:
            f.write('{"game_key": "%s", "plut_dir": "%s"}'
                    % (meta_key, os.path.join(tmp.name, "meta_plut").replace("\\", "/")))
        compat = os.path.join(tmp.name, "compat")
        self.library.installed = {"t6mp": {"install_dir": install, "source": "steam"},
                                  "t6zm": {"install_dir": install, "source": "steam"}}
        gd = next(g for g in ui_manage.ALL_GAMES if "t6mp" in g["keys"])

        class _Box:
            ButtonRole = ui_manage.QMessageBox.ButtonRole

            def __init__(self, *_a):
                pass

            def setWindowTitle(self, _t): pass
            def setText(self, _t): pass
            def addButton(self, label, _role): return label
            def exec(self): return 0
            def clickedButton(self): return pick

        opened = []
        with patch.object(ui_manage, "QMessageBox", _Box), \
             patch.object(ui_manage, "_detached_open", side_effect=lambda cmd: opened.append(cmd[1])), \
             patch("plutonium._plut_compat", return_value=compat):
            self.library._mods(gd, ["t6mp", "t6zm"])
        return opened, tmp.name, compat

    def test_mod_folder_of_the_other_mode_comes_from_its_own_prefix(self):
        opened, _root, compat = self._open_mods("t6zm", "Multiplayer")
        expected = os.path.join(compat, "pfx", "drive_c", "users", "steamuser",
                                "AppData", "Local", "Plutonium", "storage", "t6", "mods")
        self.assertEqual(opened, [expected])

    def test_mod_folder_metadata_is_used_for_its_own_mode(self):
        opened, root, _compat = self._open_mods("t6zm", "Zombies")
        self.assertEqual(len(opened), 1)
        self.assertEqual(os.path.normpath(opened[0]),
                         os.path.normpath(os.path.join(root, "meta_plut", "storage", "t6", "mods")))


if __name__ == "__main__":
    unittest.main()
