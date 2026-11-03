"""Regression tests for the fourth sweep: game-key tables, save backups, client
installers, preflight, the install / manage screens and the shell scripts."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cache_cleanup
import cod4x
import detect_games
import iw4x
import plutonium
import preflight
import save_backup
import shortcut
import ui_constants

try:
    from qt import QApplication, QStackedWidget
    import ui_install
    import ui_manage
except ImportError:
    QApplication = None

_APP = None


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class GameKeyTableTests(unittest.TestCase):
    """The same game keys live in several modules (AGENTS.md): they must agree."""

    def test_ui_tables_cover_exactly_the_detected_games(self):
        games = set(detect_games.GAMES)
        listed = {k for g in ui_constants.ALL_GAMES for k in g["keys"]}
        self.assertEqual(listed, games)
        for name in ("KEY_CLIENT", "KEY_EXES", "KEY_MODE_LABEL"):
            self.assertEqual(set(getattr(ui_constants, name)), games, name)
        self.assertEqual(set(detect_games.KEY_TO_SENTINEL), games)

    def test_shortcuts_and_plutonium_tables_only_name_known_games(self):
        games = set(detect_games.GAMES)
        self.assertLessEqual(set(shortcut.SHORTCUTS), games)
        self.assertLessEqual(set(shortcut.OWN_SHORTCUTS), games)
        self.assertLessEqual(set(plutonium.GAME_META), games)
        plut = {k for k, c in ui_constants.KEY_CLIENT.items() if c == "plutonium"}
        self.assertEqual(set(plutonium.GAME_META), plut)
        self.assertEqual(set(plutonium.STEAM_MENU_EXES), set(plutonium.GAME_META))
        self.assertLessEqual(set(plutonium.OLED_LAN_WRAPPER_NAMES), set(plutonium.GAME_META))
        self.assertLessEqual(set(plutonium.LAN_KEYS), set(plutonium.GAME_META))
        self.assertLessEqual(set(cache_cleanup.STEAM_APPIDS), plut)
        for group in save_backup.SAVE_GROUPS.values():
            self.assertLessEqual(set(group["keys"]), games)

    def test_glob_over_a_game_folder_always_escapes_it(self):
        """A library on an SD card can be called "SD [128GB]": [ is a glob character."""
        bad = []
        for path in (ROOT / "src").glob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"glob\.glob\(\s*os\.path\.join\(\s*(install_dir|game_dir)\b", line):
                    bad.append(f"{path.name}:{number}")
        self.assertEqual(bad, [])


class SaveBackupTagTests(unittest.TestCase):
    GROUP = {"keys": ["iw5mp", "iw5mp_ds"], "appids": ["42690", "42750"], "plut_store": "iw5"}

    def _library(self, root, appid):
        storage = save_backup._plut_storage(os.path.join(root, "compatdata", appid), "iw5")
        os.makedirs(os.path.join(storage, "players"))
        _write(os.path.join(storage, "players", "stats"), appid.encode())
        return os.path.join(root, "compatdata")

    def test_same_appid_in_two_libraries_gets_two_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            internal = self._library(os.path.join(tmp, "internal"), "42690")
            sd = self._library(os.path.join(tmp, "sd"), "42690")
            with patch.object(save_backup, "_find_all_compatdata_dirs", return_value=[internal, sd]), \
                 patch.object(save_backup, "HEROIC_PREFIX", os.path.join(tmp, "no-heroic")):
                found = save_backup._find_plutonium_storages("/steam", self.GROUP, {})
            self.assertEqual(sorted(found), ["42690", "42690@1"])
            self.assertIn("internal", found["42690"])
            self.assertIn("sd", found["42690@1"])

    def test_restore_sends_each_tag_back_to_its_own_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            internal = self._library(os.path.join(tmp, "internal"), "42690")
            sd = self._library(os.path.join(tmp, "sd"), "42690")
            with patch.object(save_backup, "_find_all_compatdata_dirs", return_value=[internal, sd]):
                first = save_backup._plut_restore_dst("/steam", self.GROUP, "42690")
                second = save_backup._plut_restore_dst("/steam", self.GROUP, "42690@1")
                missing = save_backup._plut_restore_dst("/steam", self.GROUP, "42750")
            self.assertIn("internal", first)
            self.assertIn("sd", second)
            self.assertTrue(missing.startswith(os.path.join("/steam", "steamapps", "compatdata", "42750")))

    def test_backup_folder_names_are_recognised_as_tags(self):
        for name in ("42690", "42690@1", "heroic", "own_t4mp", "own_old_t4mp"):
            self.assertTrue(save_backup._is_plut_tag(name), name)
        for name in ("storage", "players", "42690@x", "zone"):
            self.assertFalse(save_backup._is_plut_tag(name), name)

    def test_tagged_backup_is_restored_not_treated_as_an_old_flat_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = os.path.join(tmp, "backup")
            _write(os.path.join(backup, "iw5", "42690@1", "players", "stats"), b"saved")
            _write(os.path.join(backup, "backup_info.json"), b'{"groups": ["iw5"]}')
            dest = os.path.join(tmp, "dest")
            os.makedirs(dest)
            with patch.object(save_backup, "BACKUP_ROOT", backup), \
                 patch.object(save_backup, "_load_config", return_value={"setup_games": {}}), \
                 patch.object(save_backup, "_plut_restore_dst", return_value=dest) as dst:
                restored = save_backup.restore_saves("/steam", on_progress=lambda m: None)
            self.assertEqual(restored, {"iw5": dest})
            self.assertEqual(dst.call_args.args[2], "42690@1")
            self.assertEqual(Path(dest, "players", "stats").read_bytes(), b"saved")


class PreRestoreSafetyTests(unittest.TestCase):
    def test_keep_pre_restore_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            players = os.path.join(tmp, "players")
            _write(os.path.join(players, "stats"), b"newest")
            self.assertTrue(save_backup._keep_pre_restore(players))
            self.assertEqual(Path(players + ".pre-restore", "stats").read_bytes(), b"newest")
            self.assertTrue(save_backup._keep_pre_restore(os.path.join(tmp, "nothing-here")))
            with patch.object(save_backup.shutil, "copytree", side_effect=OSError(28, "No space left")):
                self.assertFalse(save_backup._keep_pre_restore(players))

    def test_no_safety_copy_means_no_restore_over_the_newest_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup, game = os.path.join(tmp, "backup"), os.path.join(tmp, "game")
            _write(os.path.join(backup, "cod4", "stats"), b"old backup")
            _write(os.path.join(backup, "backup_info.json"), b'{"groups": ["cod4"]}')
            _write(os.path.join(game, "players", "stats"), b"newest stats")
            msgs = []
            with patch.object(save_backup, "BACKUP_ROOT", backup), \
                 patch.object(save_backup, "_load_config", return_value={"setup_games": {"cod4mp": {}}}), \
                 patch.object(save_backup, "_find_install_dir", return_value=game), \
                 patch.object(save_backup, "_keep_pre_restore", return_value=False):
                restored = save_backup.restore_saves("/steam", on_progress=msgs.append)
            self.assertEqual(restored, {})
            self.assertEqual(Path(game, "players", "stats").read_bytes(), b"newest stats")
            self.assertTrue(any("could not keep your current saves" in m for m in msgs))


class UndoCod4rTests(unittest.TestCase):
    def _folder(self, tmp, with_origs):
        for name in cod4x.STOCK_FILES:
            _write(os.path.join(tmp, name), b"cod4r build")
        _write(os.path.join(tmp, cod4x.COD4R_METADATA_FILE), b"{}")
        for name in with_origs:
            _write(os.path.join(tmp, name + cod4x.ORIG_SUFFIX), b"stock " + name.encode())

    def test_no_backups_at_all_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._folder(tmp, [])
            self.assertFalse(cod4x.undo_cod4r(tmp))
            self.assertTrue(os.path.exists(os.path.join(tmp, cod4x.COD4R_METADATA_FILE)))

    def test_retry_after_a_half_finished_undo_completes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = list(cod4x.STOCK_FILES)
            # the first run restored names[0] and was cut off before names[1]
            self._folder(tmp, names[1:])
            _write(os.path.join(tmp, names[0]), b"stock " + names[0].encode())
            self.assertTrue(cod4x.undo_cod4r(tmp))
            self.assertEqual(Path(tmp, names[1]).read_bytes(), b"stock " + names[1].encode())
            self.assertEqual(Path(tmp, names[0]).read_bytes(), b"stock " + names[0].encode())
            self.assertFalse(os.path.exists(os.path.join(tmp, cod4x.COD4R_METADATA_FILE)))

    def test_full_undo_still_restores_every_stock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._folder(tmp, list(cod4x.STOCK_FILES))
            self.assertTrue(cod4x.undo_cod4r(tmp))
            for name in cod4x.STOCK_FILES:
                self.assertEqual(Path(tmp, name).read_bytes(), b"stock " + name.encode())


class Iw4xStagingTests(unittest.TestCase):
    def test_staging_left_by_a_killed_run_is_swept_even_in_a_bracketed_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = os.path.join(tmp, "SD [128GB]", "Call of Duty MW2")
            for name in (".deckops_iw4x_abc", ".deckops_iw4x_manifest_xyz"):
                _write(os.path.join(game, name, "release.zip"), b"zip")
            _write(os.path.join(game, "iw4mp.exe"))
            iw4x._sweep_staging(game)
            self.assertEqual(sorted(os.listdir(game)), ["iw4mp.exe"])


class CleanupShaderCacheTests(unittest.TestCase):
    def test_reports_a_folder_it_could_not_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "10090"))
            with patch.object(cache_cleanup, "_shadercache_dirs", return_value=[tmp]):
                with patch.object(cache_cleanup.shutil, "rmtree", side_effect=OSError("busy")):
                    self.assertFalse(cache_cleanup.cleanup_shader_cache("t4mp", "steam"))
                self.assertTrue(cache_cleanup.cleanup_shader_cache("t4mp", "steam"))
            self.assertFalse(os.path.exists(os.path.join(tmp, "10090")))


class PreflightTests(unittest.TestCase):
    def _check(self, selected, keys):
        with patch.object(preflight, "_on_ac", return_value=True), \
             patch.object(preflight, "_online", return_value=True), \
             patch.object(preflight, "free_gb", return_value=500.0), \
             patch("steam_common.default_steam_root", return_value="/steam"), \
             patch("depot_downgrade.estimate_space_gb", return_value=11.0) as estimate:
            return preflight.check(selected, downgrade_keys=keys), estimate

    def test_mw3_campaign_alone_needs_no_downgrade_warning(self):
        checks, estimate = self._check([("iw5sp", "steam", "/g/mw3")], ["iw5sp"])
        estimate.assert_not_called()
        self.assertEqual(checks, [])

    def test_mw3_multiplayer_still_does(self):
        checks, estimate = self._check([("iw5mp", "plutonium", "/g/mw3")], ["iw5mp"])
        estimate.assert_called_once()
        self.assertTrue(any(c.title == "This will take a long time" for c in checks))


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class InstallScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cfg = MagicMock()
        self.cfg.is_lcd.return_value = False
        self.cfg.is_first_run.return_value = False
        for target in (ui_install, ui_constants):
            p = patch.object(target, "cfg", self.cfg)
            p.start()
            self.addCleanup(p.stop)
        for name in ("_log_to_file", "inhibit"):
            p = patch.object(ui_install, name, MagicMock())
            p.start()
            self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.addCleanup(self.stack.close)
        self.screen = ui_install.InstallScreen(self.stack)
        self.stack.addWidget(self.screen)

    def _results(self, *keys):
        self.screen._results = [{"key": k, "name": k, "status": "ok", "reason": ""} for k in keys]

    def test_a_game_failed_by_a_later_step_is_no_longer_recorded_as_set_up(self):
        self.screen.own_selected = {"cod4mp": {}}
        self._results("cod4mp", "t4mp")
        self.cfg.is_game_setup_for_source.return_value = True
        self.screen._fail_ok(["cod4mp", "t4mp"], "Steam shortcut not created")
        sources = {c.args[0]: c.args for c in self.cfg.is_game_setup_for_source.call_args_list}
        self.assertEqual(sources["cod4mp"], ("cod4mp", "own"))
        self.assertEqual(sources["t4mp"], ("t4mp", "steam"))
        self.assertEqual({c.args[0] for c in self.cfg.unmark_game_setup.call_args_list}, {"cod4mp", "t4mp"})
        self.assertEqual([r["status"] for r in self.screen._results], ["failed", "failed"])

    def test_a_game_that_is_not_ok_keeps_its_own_reason_and_its_record(self):
        self.screen.own_selected = {}
        self.screen._results = [{"key": "t4mp", "name": "t4mp", "status": "skipped", "reason": "by user"}]
        self.screen._fail_ok(["t4mp"], "later")
        self.cfg.unmark_game_setup.assert_not_called()
        self.assertEqual(self.screen._results[0]["reason"], "by user")

    def test_notes_about_other_steps_reach_the_summary(self):
        self._results("t4mp")
        logs = []
        self.screen._s.log.connect(logs.append)
        self.screen._note("The Steam Deck controller layout could not be written for any game")
        self.screen._render_summary()
        _APP.processEvents()
        self.assertIn("controller layout could not be written", self.screen.summary.text())
        self.assertTrue(any("controller layout" in m for m in logs))

    def test_summary_without_notes_is_unchanged(self):
        self._results("t4mp")
        self.screen._notes = []
        self.screen._render_summary()
        self.assertNotIn("⚠", self.screen.summary.text())

    def test_a_failed_rescan_does_not_hand_old_games_to_set_up(self):
        welcome = ui_install.WelcomeScreen(self.stack)
        welcome.steam_installed = {"t4mp": {}}
        welcome.own_installed = {"cod4mp": {}}
        welcome.installed = {"t4mp": {}, "cod4mp": {}}
        with patch.object(ui_install.threading, "Thread"):
            welcome._start_scan()
        self.assertEqual((welcome.steam_installed, welcome.own_installed, welcome.installed), ({}, {}, {}))


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ManageScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        patches = [
            patch.object(ui_manage.ManagementScreen, "_refresh", lambda _self: None),
            patch.object(ui_manage.ManagementCard, "_fetch", lambda _self, _appid: None),
            patch.object(ui_manage.cfg, "get_offline_mode", return_value=False),
            patch.object(ui_manage, "find_steam_root", return_value="/steam"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.addCleanup(self.stack.close)
        self.library = ui_manage.ManagementScreen(self.stack)
        self.library._startup_checked = True
        self.stack.addWidget(self.library)

    def test_add_games_waits_for_a_running_settings_task(self):
        with patch.object(ui_manage.inhibit, "busy", return_value="DeckOps is changing Steam settings"), \
             patch.object(ui_manage, "get_screen") as get_screen, \
             patch.object(ui_manage, "go_to") as go_to:
            self.library._add_games()
        get_screen.assert_not_called()
        go_to.assert_not_called()
        self.assertIn("Wait for this to finish", self.library._status.text())

    def test_library_update_button_waits_too(self):
        with patch.object(ui_manage.inhibit, "busy", return_value="DeckOps is installing"), \
             patch.object(ui_manage, "go_to") as go_to:
            self.library._apply_deckops_update()
        go_to.assert_not_called()

    def test_failed_update_leaves_the_library_button_plain_again(self):
        btn = self.library._deckops_update_btn
        btn.setText("Update && Restart")
        self.library._deckops_update_status.setText("Update available")
        settings = SimpleNamespace(_apply_update=MagicMock())
        with patch.object(ui_manage.inhibit, "busy", return_value=None), \
             patch.object(ui_manage, "get_screen", return_value=settings), \
             patch.object(ui_manage, "go_to"), \
             patch.object(ui_manage.QTimer, "singleShot", lambda _ms, fn: fn()):
            self.library._apply_deckops_update()
        self.assertEqual(btn.text(), "DeckOps Update")
        self.assertEqual(self.library._deckops_update_status.text(), "")
        settings._apply_update.assert_called_once()

    def test_settings_update_refuses_while_a_task_runs(self):
        settings = ui_manage.ConfigureScreen(self.stack)
        with patch.object(ui_manage.inhibit, "busy", return_value="DeckOps is changing Steam settings"), \
             patch.object(ui_manage.QMessageBox, "question",
                          side_effect=AssertionError("must not ask")):
            settings._apply_update()
        self.assertIn("Wait for this to finish", settings._update_status.text())

    def test_downgrade_of_mw3_does_not_pull_in_the_dedicated_server(self):
        gd = next(g for g in ui_constants.ALL_GAMES if "iw5mp" in g["keys"])
        calls = []
        with patch.object(self.library, "_setup", side_effect=lambda *a, **k: calls.append(k)), \
             patch("depot_downgrade.is_downgrade_needed", return_value=True):
            self.library.installed = {"iw5mp": {"install_dir": "/g"}, "iw5mp_ds": {"install_dir": "/g"}}
            self.library._depot_downgrade("iw5", gd, ["iw5mp", "iw5mp_ds", "iw5sp"])
        self.assertEqual(calls[0]["keys_to_setup"], ["iw5mp", "iw5sp"])

    def test_restore_that_failed_keeps_the_offer(self):
        screen = ui_manage.SetupCompleteScreen(self.stack)
        with patch.object(ui_manage.SetupCompleteScreen, "_mark_offer_handled") as handled:
            screen._restore_finished("RETRY|⚠  Restore failed: disk full")
            handled.assert_not_called()
            self.assertTrue(screen._restore_btn.isEnabled())
            self.assertIn("disk full", screen._restore_status.text())
            screen._restore_finished("DONE|✓  Restored saves for 1 group(s): cod4.")
            handled.assert_called_once()
            self.assertFalse(screen._restore_btn.isVisible())

    def test_restore_message_without_a_prefix_still_counts_as_done(self):
        screen = ui_manage.SetupCompleteScreen(self.stack)
        with patch.object(ui_manage.SetupCompleteScreen, "_mark_offer_handled") as handled:
            screen._restore_finished("No matching save data found to restore.")
            handled.assert_called_once()

    def test_uninstaller_does_not_start_twice_when_quitting_fails(self):
        settings = ui_manage.ConfigureScreen(self.stack)
        launched = []
        with patch.object(ui_manage.inhibit, "busy", return_value=None), \
             patch.object(ui_manage.subprocess, "Popen", side_effect=lambda cmd, **k: launched.append(cmd[0])), \
             patch.object(ui_manage, "_kill_audio", side_effect=RuntimeError("audio")), \
             patch.object(ui_manage.os.path, "exists", return_value=True):
            with self.assertRaises(RuntimeError):
                settings._run_uninstaller()
        self.assertEqual(launched, ["konsole"])

    def test_uninstaller_falls_back_to_xterm_when_konsole_is_missing(self):
        settings = ui_manage.ConfigureScreen(self.stack)
        launched = []

        def popen(cmd, **kwargs):
            if cmd[0] == "konsole":
                raise FileNotFoundError("konsole")
            launched.append(cmd[0])

        with patch.object(ui_manage.inhibit, "busy", return_value=None), \
             patch.object(ui_manage.subprocess, "Popen", side_effect=popen), \
             patch.object(ui_manage, "_kill_audio"), \
             patch.object(ui_manage.QApplication, "instance", return_value=MagicMock()), \
             patch.object(ui_manage.os.path, "exists", return_value=True):
            settings._run_uninstaller()
        self.assertEqual(launched, ["xterm"])


class DiagReportTests(unittest.TestCase):
    def test_report_is_written_even_when_a_step_fails(self):
        import diag_host_menus as diag
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "diag.txt")
            with patch.object(diag, "OUT", out), patch.object(diag, "LINES", []), \
                 patch.object(diag, "_report", side_effect=KeyError("launch_menus")):
                diag.main()
            self.assertIn("the report stopped early", Path(out).read_text())

    def test_report_without_a_steam_folder_says_so(self):
        import diag_host_menus as diag
        with patch.object(diag, "LINES", []) as lines, \
             patch.object(diag.c, "load", return_value={}), \
             patch.object(diag, "find_steam_root", return_value=None):
            diag._report()
        self.assertTrue(any("No Steam folder" in line for line in lines))

    def test_apply_is_refused_while_steam_runs(self):
        import diag_host_menus as diag
        lines = []
        with patch.object(diag, "LINES", lines), patch.object(diag.sys, "argv", ["diag", "--apply"]), \
             patch.object(diag, "_steam_running", return_value=True), \
             patch.object(diag.c, "load", return_value={"steam_root": "/nonexistent", "launch_menus": None}), \
             patch.object(diag, "find_installed_games", return_value={}), \
             patch.object(diag, "parse_library_folders", return_value=[]), \
             patch("plutonium.ensure_host_menus") as ensure:
            diag._report()
        ensure.assert_not_called()
        self.assertTrue(any("NOT run" in line for line in lines))


class UninstallScriptTests(unittest.TestCase):
    TEXT = (ROOT / "deckops_uninstall.sh").read_text(encoding="utf-8")

    def test_save_backup_question_does_not_read_a_missing_zenity_as_no(self):
        before, after = self.TEXT.split("Back up your save data before uninstalling?", 1)
        self.assertIn('command -v zenity >/dev/null 2>&1', before.rsplit("BACKUP_ANSWER=0", 1)[1])
        self.assertIn("/dev/tty", after.split("fi", 1)[0])
        self.assertIn('if [ "$BACKUP_ANSWER" -ne 0 ]; then', self.TEXT)

    def test_a_failing_folder_removal_does_not_end_the_own_game_cleanup(self):
        match = re.search(r"def rmtree_safe\(path, label\):.*?\n\ndef restore_cod4_audio", self.TEXT, re.S)
        self.assertIsNotNone(match)
        namespace = {}
        exec("import os, shutil\n" + match.group(0).rsplit("\n\ndef restore_cod4_audio", 1)[0], namespace)
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good")
            os.makedirs(os.path.join(good, "sub"))
            namespace["rmtree_safe"](good, "good/")
            self.assertFalse(os.path.exists(good))
            with patch("shutil.rmtree", side_effect=PermissionError("locked")):
                namespace["rmtree_safe"](tmp, "locked/")        # reported, not raised
            self.assertTrue(os.path.isdir(tmp))

    def test_cleanup_calls_use_the_safe_helper(self):
        block = self.TEXT.split("# DLC directories: ask before removing", 1)[1].split("# Remove files in subdirectories", 1)[0]
        self.assertNotIn("shutil.rmtree(dp)", block)
        self.assertNotIn("shutil.rmtree(ff_path)", block)
        self.assertNotIn("shutil.rmtree(zd_path)", block)
        self.assertEqual(block.count("rmtree_safe("), 3)

    def test_controller_templates_are_looked_for_in_the_steam_root_too(self):
        self.assertIn('TEMPLATE_DIR="$STEAM_ROOT/controller_base/templates"', self.TEXT)
        self.assertIn("ALT_TEMPLATE_DIR=", self.TEXT)

    @unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash on Linux")
    def test_scripts_parse(self):
        for name in ("install.sh", "launcher.sh", "prelaunch.sh", "deckops_uninstall.sh",
                     "deckops_identity.sh", "assets/LAN/lan_server.sh", "assets/LAN/launcher_offline.sh"):
            result = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")


class InstallScriptTests(unittest.TestCase):
    TEXT = (ROOT / "install.sh").read_text(encoding="utf-8")

    def test_stale_sidecar_sweep_knows_flatpak_steam(self):
        sweep = self.TEXT.split("STEAM_ROOT_FOR_SWEEP=\"\"", 1)[1].split("LAN_SWEEP_DIRS=()", 1)[0]
        self.assertIn("com.valvesoftware.Steam/data/Steam", sweep)
        self.assertIn(".steam/debian-installation", sweep)

    def test_no_library_file_lookup_without_a_steam_root(self):
        self.assertIn('if [ -n "$STEAM_ROOT_FOR_SWEEP" ] && [ -f "$LF_VDF_SWEEP" ]; then', self.TEXT)


if __name__ == "__main__":
    unittest.main()
