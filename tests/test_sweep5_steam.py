"""Regression tests for the fifth sweep: shortcut write results, compat tool
clearing, unparsable shortcuts.vdf bodies, own-copy detection and layouts,
Steam artwork and Steam roots."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import controller_profiles as profiles
import detect_games
import shortcut


def _quiet():
    msgs = []
    return msgs, msgs.append


class _SteamUser(unittest.TestCase):
    """A temporary userdata folder with one or more Steam users."""

    uids = ("12345",)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.userdata = os.path.join(self.root, "userdata")
        for uid in self.uids:
            os.makedirs(os.path.join(self.userdata, uid, "config"))
        for p in (patch.object(shortcut, "_find_all_steam_uids", return_value=list(self.uids)),
                  patch.object(shortcut, "_userdata_dir", return_value=self.userdata),
                  patch.object(shortcut, "default_steam_root", return_value=self.root),
                  patch.object(shortcut, "_download_artwork"),
                  patch.object(shortcut, "_assign_controller_config")):
            p.start()
            self.addCleanup(p.stop)

    def vdf(self, uid="12345"):
        return Path(self.userdata, uid, "config", "shortcuts.vdf")


class ClearCompatToolResultTests(unittest.TestCase):
    def test_result_of_the_wrapper_is_returned(self):
        with patch("wrapper.clear_compat_tool", return_value=False):
            self.assertFalse(shortcut._clear_compat_tool("1"))
        with patch("wrapper.clear_compat_tool", return_value=True):
            self.assertTrue(shortcut._clear_compat_tool("1"))


class AddShortcutTests(_SteamUser):
    def _add(self, prog, **kw):
        return shortcut.add_shortcut("Game", '"/x"', '"/"', "", {}, "standard", "on",
                                     on_progress=prog, **kw)

    def test_compat_tool_left_in_place_is_a_warning(self):
        msgs, prog = _quiet()
        with patch.object(shortcut, "_clear_compat_tool", return_value=False):
            self._add(prog, clear_compat_tool=True)
        self.assertTrue(any("⚠ Could not clear compat tool" in m for m in msgs))
        self.assertFalse(any(m.strip() == "Cleared compat tool for shortcut" for m in msgs))

    def test_body_whose_entries_dont_parse_is_left_alone(self):
        # Header and footer are fine, the entry inside is cut off
        data = b"\x00shortcuts\x00\x000\x00\x01AppName\x00Game\x00\x08\x08"
        self.vdf().write_bytes(data)
        msgs, prog = _quiet()
        with patch.object(shortcut, "_write_shortcuts_vdf") as write:
            self._add(prog)
        write.assert_not_called()
        self.assertEqual(self.vdf().read_bytes(), data)
        self.assertTrue(any("Skipped Steam user 12345" in m for m in msgs))


class CreateShortcutsResultTests(_SteamUser):
    def setUp(self):
        super().setUp()
        self.game_dir = os.path.join(self.root, "cod4")
        os.makedirs(self.game_dir)
        for p in (patch.object(shortcut, "remove_merged_mp_shortcuts", return_value=[]),
                  patch.object(shortcut, "mp_on_main_tile", return_value=False),
                  patch("wrapper.find_compatdata", return_value="/compat"),
                  patch("config.is_lcd", return_value=False),
                  patch("config.get_ge_proton_version", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def _create(self, prog, keys=("cod4mp",)):
        games = {k: {"install_dir": self.game_dir} for k in keys}
        return shortcut.create_shortcuts(games, list(keys), "on", prog, steam_root=self.root)

    def test_written_keys_are_returned(self):
        msgs, prog = _quiet()
        self.assertEqual(self._create(prog), {"cod4mp"})
        self.assertIn(b"Call of Duty 4: Modern Warfare - Multiplayer", self.vdf().read_bytes())
        self.assertEqual(msgs[-1], "✓ Non-Steam shortcuts created.")

    def test_failed_write_returns_nothing_and_warns(self):
        msgs, prog = _quiet()
        with patch.object(shortcut, "_write_shortcuts_vdf", side_effect=OSError("read-only")):
            self.assertEqual(self._create(prog), set())
        self.assertTrue(msgs[-1].startswith("⚠"))

    def test_unreadable_user_returns_nothing_and_warns(self):
        msgs, prog = _quiet()
        with patch.object(shortcut, "_read_shortcuts_or_skip", return_value=None):
            self.assertEqual(self._create(prog), set())
        self.assertTrue(msgs[-1].startswith("⚠"))

    def test_no_user_returns_nothing(self):
        with patch.object(shortcut, "_find_all_steam_uids", return_value=[]):
            self.assertEqual(self._create(lambda m: None), set())

    def test_waw_mp_shortcut_is_never_written_off_lcd(self):
        # mp_on_main_tile is patched to False, as when it could not read the config
        with patch.object(shortcut, "_write_shortcuts_vdf") as write:
            self.assertEqual(self._create(lambda m: None, keys=("t4mp",)), set())
        write.assert_not_called()

    def test_waw_mp_shortcut_is_written_on_lcd_without_compat_tool(self):
        msgs, prog = _quiet()
        with patch("config.is_lcd", return_value=True), \
             patch.object(shortcut, "_clear_compat_tool", return_value=False):
            self.assertEqual(self._create(prog, keys=("t4mp",)), {"t4mp"})
        self.assertIn(b"cache_cleanup.py", self.vdf().read_bytes())
        self.assertTrue(any("⚠ Could not clear compat tool" in m for m in msgs))

    def test_cod4_shortcut_has_no_prelaunch_hook(self):
        self._create(lambda m: None)
        self.assertNotIn(b"prelaunch", self.vdf().read_bytes())


class WriteOwnShortcutsResultTests(_SteamUser):
    def setUp(self):
        super().setUp()
        self.game_dir = os.path.join(self.root, "cod4")
        os.makedirs(self.game_dir)
        exe = os.path.join(self.game_dir, "iw3sp_mod.exe")
        Path(exe).write_bytes(b"x")
        self.own = {"cod4sp": {"install_dir": self.game_dir, "_own_actual_exe": exe,
                               "_own_launch_options": "", "shortcut_appid": 2147483649}}
        for p in (patch.object(shortcut, "cleanup_orphan_shortcuts"),
                  patch("config.get_ge_proton_version", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_written_keys_are_returned(self):
        msgs, prog = _quiet()
        self.assertEqual(shortcut.write_own_shortcuts(self.own, ["cod4sp"], "on", prog), {"cod4sp"})
        self.assertEqual(msgs[-1], "✓ Own game shortcuts written.")

    def test_failed_write_returns_nothing_and_warns(self):
        msgs, prog = _quiet()
        with patch.object(shortcut, "_write_shortcuts_vdf", side_effect=OSError("read-only")):
            self.assertEqual(shortcut.write_own_shortcuts(self.own, ["cod4sp"], "on", prog), set())
        self.assertTrue(msgs[-1].startswith("⚠"))

    def test_no_user_returns_nothing(self):
        with patch.object(shortcut, "_find_all_steam_uids", return_value=[]):
            self.assertEqual(shortcut.write_own_shortcuts(self.own, ["cod4sp"], "on"), set())

    def test_body_whose_entries_dont_parse_is_left_alone(self):
        data = b"\x00shortcuts\x00\x000\x00\x01AppName\x00x\x00\x08\x08"
        self.vdf().write_bytes(data)
        msgs, prog = _quiet()
        self.assertEqual(shortcut.write_own_shortcuts(self.own, ["cod4sp"], "on", prog), set())
        self.assertEqual(self.vdf().read_bytes(), data)
        self.assertTrue(msgs[-1].startswith("⚠"))


class TwoUsersTests(_SteamUser):
    uids = ("1", "2")

    def test_key_failed_for_one_user_is_not_reported_written(self):
        own_dir = os.path.join(self.root, "g")
        os.makedirs(own_dir)
        own = {"cod4sp": {"install_dir": own_dir, "_own_actual_exe": "/x.exe",
                          "_own_launch_options": "", "shortcut_appid": 2147483649}}
        real = shortcut._write_shortcuts_vdf

        def write(path, *a):
            if os.sep + "2" + os.sep in path:
                raise OSError("read-only")
            return real(path, *a)

        msgs, prog = _quiet()
        with patch.object(shortcut, "cleanup_orphan_shortcuts"), \
             patch("config.get_ge_proton_version", return_value=None), \
             patch.object(shortcut, "_write_shortcuts_vdf", side_effect=write):
            self.assertEqual(shortcut.write_own_shortcuts(own, ["cod4sp"], "on", prog), set())
        self.assertTrue(self.vdf("1").is_file())
        self.assertTrue(msgs[-1].startswith("⚠"))


class RepairResultTests(unittest.TestCase):
    def _repair(self, created, launcher):
        name = shortcut.SHORTCUTS["cod4mp"]["name"]
        with patch("config.get_setup_games", return_value={}), \
             patch("config.get_gyro_mode", return_value="on"), \
             patch.object(shortcut, "expected_shortcuts",
                          return_value=({"cod4mp": ("steam", name)}, True)), \
             patch.object(shortcut, "_find_all_steam_uids", return_value=["1"]), \
             patch.object(shortcut, "remove_merged_mp_shortcuts", return_value=[]), \
             patch.object(shortcut, "present_shortcut_names", return_value=set()), \
             patch.object(shortcut, "_remap_shortcut_ge", return_value=[]), \
             patch("plutonium.refresh_steam_menus"), patch("iw4x.refresh_steam_menu"), \
             patch("sp_mod.repair_sp_launch_options", return_value=[]), \
             patch("detect_games.parse_library_folders", return_value=[]), \
             patch("detect_games.find_installed_games", return_value={"cod4mp": {"install_dir": "/g"}}), \
             patch.object(shortcut, "create_shortcuts", return_value=created), \
             patch.object(shortcut, "create_launcher_shortcut", return_value=launcher):
            return shortcut.repair_shortcuts("/steam", lambda m: None)

    def test_shortcuts_that_were_not_written_are_not_reported_readded(self):
        self.assertEqual(self._repair(set(), False), [])

    def test_written_shortcuts_are_reported(self):
        self.assertEqual(self._repair({"cod4mp"}, True),
                         [shortcut.SHORTCUTS["cod4mp"]["name"], shortcut.LAUNCHER_TITLE])

    def test_t6sp_move_that_was_not_written_is_false(self):
        with patch.object(shortcut, "get_shortcut_appid", return_value=1), \
             patch.object(shortcut, "_old_t6sp_appid", return_value=1), \
             patch.object(shortcut, "enrich_own_games", side_effect=lambda g, *a: g), \
             patch.object(shortcut, "write_own_shortcuts", return_value=set()), \
             patch("config.get_gyro_mode", return_value="on"):
            self.assertFalse(shortcut.migrate_own_t6sp({"install_dir": "/g"}))


class SteamArtworkTests(_SteamUser):
    uids = ("bad", "good")

    def test_one_account_folder_that_cannot_be_made_does_not_stop_the_rest(self):
        # A file where the grid folder should go
        Path(self.userdata, "bad", "config", "grid").write_text("not a folder")
        msgs, prog = _quiet()
        shortcut.apply_steam_artwork(["iw4mp"], prog)
        grids = [c.args[0] for c in shortcut._download_artwork.call_args_list]
        self.assertEqual(grids, [os.path.join(self.userdata, "good", "config", "grid")])
        self.assertTrue(any("Artwork skipped for Steam user bad" in m for m in msgs))


class SentinelTests(unittest.TestCase):
    def test_bo1_folder_that_cannot_be_listed_is_not_a_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "zone", "English"))
            with patch.object(detect_games.os, "listdir", side_effect=PermissionError("denied")):
                self.assertFalse(detect_games._check_sentinel(tmp, "bo1"))

    def test_snap_steam_root_is_searched(self):
        self.assertIn(os.path.expanduser("~/snap/steam/common/.local/share/Steam"),
                      detect_games.STEAM_PATHS)


class OwnLayoutTests(unittest.TestCase):
    def test_only_own_copies_that_were_set_up_get_a_layout(self):
        found = {"cod4sp": {"exe_path": "/a/iw3sp.exe", "install_dir": "/a"},
                 "iw4mp": {"exe_path": "/b/iw4mp.exe", "install_dir": "/b"},
                 "t5mp": {"exe_path": "/c/BlackOpsMP.exe", "install_dir": "/c"}}
        setup = {"cod4sp": {"source": "own"}, "iw4mp": {"source": "steam"}, "t5mp": "garbage"}
        own_calls = []

        def write(uid, appid, names=(), numbered=True, prog=None):
            if not numbered:
                own_calls.append(names[0])
            return True

        with patch.object(profiles, "_is_steam_deck", return_value=True), \
             patch.object(profiles, "find_all_steam_uids", return_value=["1"]), \
             patch.object(profiles, "_write_layout_safe", side_effect=write), \
             patch.object(profiles, "_shortcut_appids", return_value={}), \
             patch("shortcut.get_shortcut_appid", return_value=None), \
             patch("detect_games.find_own_installed", return_value=found), \
             patch("config.get_setup_games", return_value=setup):
            profiles.assign_controller_profiles()
        self.assertEqual(own_calls,
                         [profiles.shortcut_title_key(shortcut.OWN_SHORTCUTS["cod4sp"]["name"])])


if __name__ == "__main__":
    unittest.main()
