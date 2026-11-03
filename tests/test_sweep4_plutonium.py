"""Regression tests for the fourth sweep: plutonium, plutonium_lcd, plutonium_update,
wrapper (launch options) and fovwatch."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
import fovwatch
import plutonium
import plutonium_lcd
import plutonium_update
import wrapper

_LOCALCONFIG = '''"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"10190"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t""
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
'''


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class WriteScriptTests(unittest.TestCase):
    def test_script_is_written_whole_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t6plut_lan_mp.sh")
            plutonium.write_script(path, "#!/bin/bash\necho old\n")
            plutonium.write_script(path, "#!/bin/bash\necho new\n")
            with open(path) as f:
                self.assertEqual(f.read(), "#!/bin/bash\necho new\n")
            self.assertEqual(os.listdir(tmp), ["t6plut_lan_mp.sh"])       # no temp left over
            if os.name == "posix":
                self.assertTrue(os.stat(path).st_mode & 0o111)

    def test_failed_write_keeps_the_old_script_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.sh")
            plutonium.write_script(path, "old")
            with patch("os.replace", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    plutonium.write_script(path, "new")
            with open(path) as f:
                self.assertEqual(f.read(), "old")
            self.assertEqual(os.listdir(tmp), ["run.sh"])

    def test_menu_and_lcd_placeholder_files_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(plutonium, "write_script", wraps=plutonium.write_script) as spy, \
                 patch("shortcut.launcher_run_url", return_value="steam://rungameid/1"):
                plutonium.write_offline_menu_file(os.path.join(tmp, "t4plutmp_lan.exe"))
                plutonium_lcd._write_lcd_menu_files({"install_dir": tmp}, "t4mp", None)
            self.assertEqual(spy.call_count, 2)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "t4plutmp.exe")))


class OnlineWrapperQuotingTests(unittest.TestCase):
    def test_path_characters_bash_would_expand_stay_text(self):
        self.assertEqual(plutonium._dq('a$b`c"d\\e'), 'a\\$b\\`c\\"d\\\\e')
        self.assertEqual(plutonium._dq("/plain/path"), "/plain/path")

    def test_wrapper_script_escapes_the_prefix_path_and_matches_it_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = {"install_dir": tmp}
            path = plutonium._write_oled_own_wrapper(
                game, "t4mp", "/steam", "/p/proton", '/run/media/My$Card/compatdata/10090',
                "/plut", wrapper_name="t4plutmp.exe")
            with open(path) as f:
                script = f.read()
        self.assertIn('STEAM_COMPAT_DATA_PATH="/run/media/My\\$Card/compatdata/10090"', script)
        self.assertIn('grep -qF "/run/media/My\\$Card/compatdata/10090"', script)
        self.assertNotIn("My$Card", script.replace("My\\$Card", ""))


class InterruptedSwapTests(unittest.TestCase):
    def test_working_copy_left_by_a_killed_install_is_put_back_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = os.path.join(tmp, "master")
            os.makedirs(os.path.join(master, "bin"))
            dest = os.path.join(tmp, "pfx", "Plutonium")
            old = dest + ".deckops_old"
            _write(os.path.join(old, "storage", "t6", "players", "stats"), b"my stats")
            self.assertFalse(os.path.exists(dest))
            with patch.object(plutonium, "_build_plut_copy", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    plutonium._copy_plut_fresh(master, dest, "t6mp", lambda m: None)
            with open(os.path.join(dest, "storage", "t6", "players", "stats"), "rb") as f:
                self.assertEqual(f.read(), b"my stats")
            self.assertFalse(os.path.exists(old))


class SharedMw3OfflineFilesTests(unittest.TestCase):
    def _game(self, tmp):
        for name in ("iw5plut_lan.sh", "iw5plutmp_lan.exe", "iw5plutds_lan.exe"):
            _write(os.path.join(tmp, name))
        _write(os.path.join(tmp, "admin", "deckops_lan.cfg"))
        return {"install_dir": tmp}

    def test_ds_removal_keeps_the_script_mw3_mp_still_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._game(tmp)
            setup = {"iw5mp": {"client": "plutonium", "lan_wrapper_path": os.path.join(tmp, "iw5plut_lan.sh")}}
            with patch("config.get_setup_games", return_value=setup):
                plutonium.remove_offline_files(game, "iw5mp_ds")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "iw5plut_lan.sh")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "admin", "deckops_lan.cfg")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "iw5plutds_lan.exe")))   # DS's own menu file

    def test_removal_without_the_sibling_takes_the_shared_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = self._game(tmp)
            with patch("config.get_setup_games", return_value={}):
                plutonium.remove_offline_files(game, "iw5mp_ds")
            self.assertFalse(os.path.exists(os.path.join(tmp, "iw5plut_lan.sh")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "admin", "deckops_lan.cfg")))

    def test_ds_setup_does_not_take_over_mp_source_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lan = os.path.join(tmp, "lan")
            game = {"install_dir": os.path.join(tmp, "mw3"), "name": "MW3"}
            os.makedirs(game["install_dir"])
            with patch.object(plutonium, "LAN_ROOT", lan), \
                 patch.object(plutonium, "LAN_DOWNLOADS", {}), \
                 patch.object(plutonium, "_lan_prefixes"), \
                 patch.object(plutonium, "refresh_lan"), \
                 patch("config.get_offline_mode", return_value=True):
                self.assertTrue(plutonium.setup_lan(game, "iw5mp", "/online/mp", "steam", lambda m: None))
                self.assertTrue(plutonium.setup_lan(game, "iw5mp_ds", "/online/ds", "steam", lambda m: None))
            with open(os.path.join(lan, "iw5mp", "source.json")) as f:
                self.assertEqual(json.load(f), {"user_plut": "/online/mp"})

    def test_ds_alone_still_gets_a_source_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lan = os.path.join(tmp, "lan")
            game = {"install_dir": os.path.join(tmp, "mw3"), "name": "MW3"}
            os.makedirs(game["install_dir"])
            with patch.object(plutonium, "LAN_ROOT", lan), \
                 patch.object(plutonium, "LAN_DOWNLOADS", {}), \
                 patch.object(plutonium, "_lan_prefixes"), \
                 patch.object(plutonium, "refresh_lan"), \
                 patch("config.get_offline_mode", return_value=True):
                plutonium.setup_lan(game, "iw5mp_ds", "/online/ds", "steam", lambda m: None)
            with open(os.path.join(lan, "iw5mp", "source.json")) as f:
                self.assertEqual(json.load(f), {"user_plut": "/online/ds"})


class RemoveOfflineModeTests(unittest.TestCase):
    def test_failed_reinstall_still_removes_offline_scripts_and_config_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "deckops.json")
            game_dir = os.path.join(tmp, "waw")
            script = os.path.join(game_dir, "t4plut_lan_mp.sh")
            menu = os.path.join(game_dir, "t4plutmp_lan.exe")
            _write(script)
            _write(menu)
            game = {"name": "WaW", "install_dir": game_dir, "source": "steam"}
            with patch.object(config, "CONFIG_PATH", cfg_path):
                config._cache = config._cache_key = None
                config.save({**config.DEFAULTS, "offline_mode": True, "setup_games": {
                    "t4mp": {"client": "plutonium", "source": "steam", "lan_wrapper_path": script}}})
                with patch.object(plutonium, "remove_lan"), \
                     patch("shortcut.remove_launcher_shortcut", return_value=True), \
                     patch("detect_games.find_all_games", return_value={"t4mp": game}), \
                     patch("wrapper.get_proton_path", return_value="/p"), \
                     patch.object(plutonium, "_plut_compat", return_value="/prefix"), \
                     patch.object(plutonium, "install_plutonium",
                                  side_effect=RuntimeError("master copy is missing")):
                    with self.assertRaisesRegex(RuntimeError, "master copy is missing"):
                        plutonium.apply_offline_mode(False, "/steam")
                entry = config.get_setup_games()["t4mp"]
                offline = config.get_offline_mode()
                config._cache = config._cache_key = None
        self.assertNotIn("lan_wrapper_path", entry)
        self.assertFalse(offline)
        self.assertFalse(os.path.exists(script))
        self.assertFalse(os.path.exists(menu))

    def test_launcher_shortcut_that_stays_in_steam_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "CONFIG_PATH", os.path.join(tmp, "deckops.json")):
                config._cache = config._cache_key = None
                config.save({**config.DEFAULTS, "setup_games": {}})
                with patch.object(plutonium, "remove_lan"), \
                     patch("shortcut.remove_launcher_shortcut", return_value=False), \
                     patch("detect_games.find_all_games", return_value={}), \
                     patch("wrapper.get_proton_path", return_value="/p"):
                    with self.assertRaisesRegex(RuntimeError, "launcher shortcut could not be removed"):
                        plutonium.apply_offline_mode(False, "/steam")
                config._cache = config._cache_key = None


class InstallNeedsAPrefixTests(unittest.TestCase):
    def test_no_prefix_is_an_error_not_a_relative_path(self):
        game = {"name": "WaW", "install_dir": "/g", "source": "steam"}
        with patch("config.is_lcd", return_value=False), \
             patch.object(plutonium, "get_dedicated_plut_dir", return_value="/master"), \
             patch.object(plutonium, "_plut_compat", return_value=""), \
             patch.object(plutonium, "_copy_plut_to_prefix") as copy:
            with self.assertRaisesRegex(RuntimeError, "No Proton prefix"):
                plutonium.install_plutonium(game, "t4mp", "/steam", "/p", None)
            copy.assert_not_called()


class LanCopyTests(unittest.TestCase):
    def test_copy_made_across_drives_is_not_recopied_every_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "src.bin"), os.path.join(tmp, "dst.bin")
            _write(src, b"plutonium")
            with patch.object(plutonium.os, "link", side_effect=OSError("cross-device")):
                plutonium._lan_link(src, dst)
                with patch.object(plutonium.shutil, "copy2", wraps=shutil.copy2) as copy:
                    plutonium._lan_link(src, dst)
                    copy.assert_not_called()
                    _write(src, b"plutonium, newer and longer")
                    plutonium._lan_link(src, dst)
                    copy.assert_called_once()
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), b"plutonium, newer and longer")

    def test_files_plutonium_dropped_leave_the_offline_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            online, root = os.path.join(tmp, "online"), os.path.join(tmp, "lan", "client")
            _write(os.path.join(online, "bin", "keep.dll"), b"k")
            _write(os.path.join(online, "bin", "old.dll"), b"o")
            with patch.object(plutonium, "LAN_ROOT", os.path.join(tmp, "lan")):
                plutonium._lan_tree(root, online, os.path.join(tmp, "master"), "t6", {}, False)
                self.assertTrue(os.path.isfile(os.path.join(root, "bin", "old.dll")))
                os.remove(os.path.join(online, "bin", "old.dll"))
                # a file of the player's own in the copy is never in the manifest
                _write(os.path.join(root, "storage", "t6", "players", "mine"), b"m")
                plutonium._lan_tree(root, online, os.path.join(tmp, "master"), "t6", {}, False)
            self.assertFalse(os.path.exists(os.path.join(root, "bin", "old.dll")))
            self.assertTrue(os.path.isfile(os.path.join(root, "bin", "keep.dll")))
            self.assertTrue(os.path.isfile(os.path.join(root, "storage", "t6", "players", "mine")))


class LaunchOptionResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.vdf = Path(self.root, "userdata", "123", "config", "localconfig.vdf")
        self.vdf.parent.mkdir(parents=True)
        self.vdf.write_text(_LOCALCONFIG)
        p = patch.object(wrapper, "LEDGER_PATH", str(Path(self.root, "ledger.json")))
        p.start()
        self.addCleanup(p.stop)

    def test_reports_the_accounts_it_set_the_option_for(self):
        self.assertEqual(wrapper.set_launch_options(self.root, "10190", "new %command%"), 1)
        # already in place counts too
        self.assertEqual(wrapper.set_launch_options(self.root, "10190", "new %command%"), 1)

    def test_no_entry_for_the_game_is_a_zero_not_a_silent_success(self):
        with self.assertLogs("deckops.wrapper", level="WARNING"):
            self.assertEqual(wrapper.set_launch_options(self.root, "424242", "x %command%"), 0)
        self.assertNotIn("x %command%", self.vdf.read_text())

    def test_callers_do_not_claim_success_for_zero(self):
        msgs = []
        game = {"install_dir": self.root, "name": "MW2"}
        with patch("wrapper.set_launch_options", return_value=0), \
             patch("wrapper.clear_launch_options"), \
             patch.object(plutonium, "_restore_original_exe"), \
             patch.object(plutonium, "_write_oled_own_wrapper", return_value="/w"), \
             patch.object(plutonium, "add_steam_menu"):
            plutonium._setup_steam_menu(game, "t6mp", "/steam", "/p", "/c", "/plut", None,
                                        lambda pct, msg: msgs.append(msg))
        self.assertTrue(any("not set" in m for m in msgs))

    def test_clear_compat_tool_says_when_it_could_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_vdf = Path(tmp, "config", "config.vdf")
            config_vdf.parent.mkdir()
            config_vdf.write_text('"InstallConfigStore" { "Software" { ')       # cut off
            self.assertFalse(wrapper.clear_compat_tool([10090], steam_root=tmp))
            self.assertTrue(wrapper.clear_compat_tool([10090], steam_root=os.path.join(tmp, "nope")))


class UpdateLockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "flock is POSIX only")
    def test_second_updater_does_not_start_while_the_first_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            plut = os.path.join(tmp, "Plutonium")
            os.makedirs(plut)
            with plutonium_update._update_lock(plut) as first:
                self.assertTrue(first)
                with plutonium_update._update_lock(plut) as second:
                    self.assertFalse(second)
            with plutonium_update._update_lock(plut) as again:
                self.assertTrue(again)

    @unittest.skipUnless(os.name == "posix", "flock is POSIX only")
    def test_update_skips_the_sync_while_another_run_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            plut = os.path.join(tmp, "Plutonium")
            os.makedirs(plut)
            info = {"revision": 9, "files": []}
            with patch.object(plutonium_update, "fetch", return_value=({}, info)), \
                 patch.object(plutonium_update, "local_revision", return_value=1), \
                 patch.object(plutonium_update, "sync_master") as sync:
                with plutonium_update._update_lock(plut):
                    plutonium_update.update(plut)
                sync.assert_not_called()
                plutonium_update.update(plut)
                sync.assert_called_once()


class FovwatchExitStatusTests(unittest.TestCase):
    """prelaunch.sh starts the game itself when the helper exits 126 or 127."""

    def _run(self, returncode):
        class Child:
            def send_signal(self, _s): pass
            def wait(self): return returncode
        with patch.object(fovwatch.subprocess, "Popen", return_value=Child()), \
             patch.object(fovwatch, "watch"):
            return fovwatch.main(["iw5sp.exe", "cg_fov=90", "--", "game"])

    def test_a_game_that_ended_with_126_or_127_is_not_launched_twice(self):
        self.assertEqual(self._run(127), 1)
        self.assertEqual(self._run(126), 1)

    def test_other_exit_codes_pass_through(self):
        self.assertEqual(self._run(0), 0)
        self.assertEqual(self._run(3), 3)
        self.assertEqual(self._run(-15), 143)

    def test_a_game_that_cannot_be_spawned_still_reports_127(self):
        with patch.object(fovwatch.subprocess, "Popen", side_effect=FileNotFoundError("nope")):
            self.assertEqual(fovwatch.main(["iw5sp.exe", "cg_fov=90", "--", "missing"]), 127)


if __name__ == "__main__":
    unittest.main()
