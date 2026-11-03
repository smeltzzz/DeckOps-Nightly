"""Regression tests for the fourth sweep: controller layouts, shortcuts, game configs,
config getters, steam_common and net helpers."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
import controller_profiles as profiles
import detect_games
import game_config
import net
import shortcut
import steam_common


class TemplatesFolderTests(unittest.TestCase):
    def test_follows_the_steam_install_in_use(self):
        with patch.object(profiles, "TEMPLATES_DIR", None), \
             patch.object(profiles, "default_steam_root", return_value="/home/u/.var/app/steam/Steam"):
            self.assertEqual(Path(profiles.templates_dir()),
                             Path("/home/u/.var/app/steam/Steam/controller_base/templates"))

    def test_override_wins(self):
        with patch.object(profiles, "TEMPLATES_DIR", "/elsewhere"):
            self.assertEqual(profiles.templates_dir(), "/elsewhere")

    def test_template_is_installed_into_that_folder_not_the_home(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(profiles, "TEMPLATES_DIR", None), \
             patch.object(profiles, "default_steam_root", return_value=root), \
             patch.object(profiles, "_is_steam_deck", return_value=True):
            profiles.install_controller_templates()
            self.assertTrue(Path(root, "controller_base", "templates", profiles.TEMPLATE).is_file())

    def test_shortcut_lookup_uses_the_steam_the_layouts_are_written_into(self):
        seen = []
        with patch.object(profiles, "default_steam_root", return_value="/the/root"), \
             patch("detect_games.find_installed_games", side_effect=lambda libs, sr: seen.append(sr) or {}), \
             patch("detect_games.parse_library_folders", return_value=[]), \
             patch("shortcut.mp_on_main_tile", return_value=False):
            profiles._shortcut_appids(lambda m: None)
        self.assertEqual(seen, ["/the/root"])


class LayoutOverwriteTests(unittest.TestCase):
    def _src(self, tmp, text):
        src = Path(tmp, "layout.vdf")
        src.write_text(text)
        return str(src)

    def test_a_layout_the_player_changed_is_kept_before_it_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._src(tmp, "deckops layout")
            canonical = os.path.join(tmp, "cfg", "7940", "controller_neptune.vdf")
            os.makedirs(os.path.dirname(canonical))
            Path(canonical).write_text("player's first layout")
            profiles.install_layout(src, canonical)                    # first: keep_original
            Path(canonical).write_text("player edited it in Steam")    # then Steam rewrites it
            profiles.install_layout(src, canonical)
            kept = [n for n in os.listdir(os.path.dirname(canonical)) if ".deckops-user-" in n]
            self.assertEqual(len(kept), 1)
            self.assertEqual(Path(os.path.dirname(canonical), kept[0]).read_text(), "player edited it in Steam")
            self.assertEqual(Path(canonical).read_text(), "deckops layout")
            self.assertEqual(Path(canonical + ".deckops-original").read_text(), "player's first layout")

    def test_an_unchanged_layout_is_replaced_without_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._src(tmp, "deckops layout")
            canonical = os.path.join(tmp, "cfg", "controller_neptune.vdf")
            os.makedirs(os.path.dirname(canonical))
            profiles.install_layout(src, canonical)
            profiles.install_layout(src, canonical)
            self.assertEqual([n for n in os.listdir(os.path.dirname(canonical)) if ".deckops-user-" in n], [])

    def test_shortcut_layout_goes_through_the_same_helper(self):
        with tempfile.TemporaryDirectory() as root, \
             patch("controller_profiles._is_steam_deck", return_value=True), \
             patch.object(shortcut, "default_steam_root", return_value=root), \
             patch.object(shortcut, "_patch_configset"), \
             patch.object(shortcut, "_get_deck_serial", return_value=None):
            shortcut._assign_controller_config_impl("12345", 2147483649, {}, "on", lambda m: None, "Title")
            canonical = Path(root, "steamapps", "common", "Steam Controller Configs", "12345", "config",
                             "2147483649", profiles.CANONICAL)
            self.assertTrue(canonical.is_file())
            self.assertTrue(Path(str(canonical) + ".deckops-owned").is_file())


class AssignResultTests(unittest.TestCase):
    def test_no_account_is_a_zero_not_an_applied_layout(self):
        with patch.object(profiles, "_is_steam_deck", return_value=True), \
             patch.object(profiles, "find_all_steam_uids", return_value=[]):
            self.assertEqual(profiles.assign_controller_profiles(), 0)

    def test_other_devices_have_nothing_to_apply(self):
        with patch.object(profiles, "_is_steam_deck", return_value=False):
            self.assertIsNone(profiles.assign_controller_profiles())

    def test_counts_what_was_written(self):
        with patch.object(profiles, "_is_steam_deck", return_value=True), \
             patch.object(profiles, "find_all_steam_uids", return_value=["1"]), \
             patch.object(profiles, "_write_layout_safe", return_value=True), \
             patch.object(profiles, "_shortcut_appids", return_value={}), \
             patch("shortcut.get_shortcut_appid", return_value=None), \
             patch("detect_games.find_own_installed", return_value={}):
            self.assertEqual(profiles.assign_controller_profiles(), len(profiles.APPID_NAMED_KEYS))


def _entry(index, name, exe="x"):
    return (b"\x00" + str(index).encode() + b"\x00"
            + b"\x01AppName\x00" + name.encode() + b"\x00"
            + b"\x01Exe\x00" + exe.encode() + b"\x00\x08")


class ShortcutRemovalTests(unittest.TestCase):
    def test_a_shortcut_that_could_not_be_written_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "userdata", "12345", "config", "shortcuts.vdf")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\x00shortcuts\x00" + _entry(0, "DeckOps: Plutonium Offline") + b"\x08\x08")
            with patch.object(shortcut, "default_steam_root", return_value=root), \
                 patch.object(shortcut, "_find_all_steam_uids", return_value=["12345"]), \
                 patch.object(shortcut, "_userdata_dir", return_value=os.path.join(root, "userdata")):
                with patch.object(shortcut, "_write_shortcuts_vdf", side_effect=OSError("read-only")):
                    self.assertFalse(shortcut.remove_shortcut("DeckOps: Plutonium Offline", '"x"'))
                self.assertTrue(shortcut.remove_shortcut("DeckOps: Plutonium Offline", '"x"'))
                self.assertNotIn(b"DeckOps: Plutonium Offline", path.read_bytes())

    def test_unreadable_user_is_reported_as_not_removed(self):
        with patch.object(shortcut, "_find_all_steam_uids", return_value=["1"]), \
             patch.object(shortcut, "_userdata_dir", return_value="/nowhere"), \
             patch.object(shortcut, "_read_shortcuts_or_skip", return_value=None):
            self.assertFalse(shortcut.remove_shortcut("x", '"x"'))

    def test_merged_mp_shortcut_keeps_its_compat_tool_when_it_stays_in_steam(self):
        game_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(game_dir, ignore_errors=True))
        Path(game_dir, "deckops_cod4r.json").write_text("{}")
        with patch("config.get_setup_games", return_value={"cod4mp": {"source": "steam", "client": "cod4r"}}), \
             patch("detect_games.parse_library_folders", return_value=[]), \
             patch("detect_games.find_installed_games", return_value={"cod4mp": {"install_dir": game_dir}}), \
             patch.object(shortcut, "get_shortcut_appid", return_value=111), \
             patch.object(shortcut, "remove_shortcut", return_value=False), \
             patch.object(shortcut, "_clear_compat_tool") as clear:
            removed = shortcut.remove_merged_mp_shortcuts("/steam", lambda m: None)
        self.assertEqual(removed, [])
        clear.assert_not_called()


class OrphanArtworkTests(unittest.TestCase):
    def test_artwork_goes_even_when_the_account_has_no_shortcuts(self):
        with tempfile.TemporaryDirectory() as root:
            userdata = os.path.join(root, "userdata")
            grid = Path(userdata, "12345", "config", "grid")
            grid.mkdir(parents=True)
            appid = shortcut._calc_shortcut_appid(*shortcut._ORPHAN_PATTERNS[0])
            (grid / f"{appid}p.png").write_bytes(b"art")
            (grid / "99999p.png").write_bytes(b"other game")
            Path(userdata, "12345", "config", "shortcuts.vdf").write_bytes(b"")
            with patch.object(shortcut, "_find_all_steam_uids", return_value=["12345"]), \
                 patch.object(shortcut, "_userdata_dir", return_value=userdata), \
                 patch.object(shortcut, "default_steam_root", return_value=root):
                shortcut.cleanup_orphan_shortcuts(lambda m: None)
            self.assertFalse((grid / f"{appid}p.png").exists())
            self.assertTrue((grid / "99999p.png").exists())


class LauncherShortcutTests(unittest.TestCase):
    def test_no_launcher_files_is_not_a_created_shortcut(self):
        with patch.object(shortcut, "INSTALL_DIR", "/nonexistent-deckops"):
            self.assertFalse(shortcut.create_launcher_shortcut(lambda m: None))

    def test_no_steam_account_is_not_a_created_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in (shortcut._LAUNCHER_SH_REL, shortcut._LAUNCHER_EXE_REL):
                p = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                Path(p).write_bytes(b"x")
            with patch.object(shortcut, "INSTALL_DIR", tmp), \
                 patch.object(shortcut, "_find_all_steam_uids", return_value=[]):
                self.assertFalse(shortcut.create_launcher_shortcut(lambda m: None))

    def test_repair_continues_when_the_old_mp_shortcut_cleanup_fails(self):
        with patch("config.get_setup_games", return_value={}), \
             patch.object(shortcut, "expected_shortcuts", return_value=({}, False)), \
             patch.object(shortcut, "_find_all_steam_uids", return_value=["1"]), \
             patch.object(shortcut, "remove_merged_mp_shortcuts", side_effect=OSError("config.vdf")), \
             patch.object(shortcut, "present_shortcut_names", return_value=set()), \
             patch.object(shortcut, "_remap_shortcut_ge", return_value=[]), \
             patch("plutonium.refresh_steam_menus"), patch("iw4x.refresh_steam_menu"), \
             patch("sp_mod.repair_sp_launch_options", return_value=[]):
            self.assertEqual(shortcut.repair_shortcuts("/steam", lambda m: None), [])


class GameConfigTests(unittest.TestCase):
    def test_a_destination_that_cannot_be_created_counts_as_failed_and_the_loop_goes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "file")
            Path(blocker).write_text("not a folder")
            good = os.path.join(tmp, "good")
            asset = Path(game_config.CONFIGS_DIR, "OLED")
            self.assertTrue(asset.is_dir())
            src = next(p for p in asset.rglob("*.cfg"))
            rel = str(src.relative_to(asset)).replace("\\", "/")
            config_map = {"t4sp": [(rel, os.path.join(blocker, "sub"))],
                          "t4mp": [(rel, good)]}
            with patch.object(game_config, "_build_config_map", return_value=config_map), \
                 patch("config.get_player_name", return_value=None), \
                 patch.object(game_config, "_own_compatdata", return_value=None), \
                 patch.object(game_config, "_own_pfx_dest", side_effect=lambda d, p: d), \
                 patch.object(game_config, "_offline_mirror_paths", return_value=[]):
                applied, skipped, failed = game_config.apply_game_configs(
                    selected_keys=["t4sp", "t4mp"],
                    installed_games={"t4sp": {"install_dir": tmp, "source": "steam"},
                                     "t4mp": {"install_dir": tmp, "source": "steam"}},
                    steam_root=tmp, deck_model="oled", on_progress=lambda m: None)
            self.assertEqual((applied, failed), (1, 1))
            self.assertTrue(os.path.isfile(os.path.join(good, os.path.basename(rel))))

    def test_patch_dvars_writes_whole_and_keeps_the_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config_mp.cfg")
            Path(path).write_text('seta cg_fov "65"\n')
            self.assertTrue(game_config._patch_dvars(path, {"cg_fov": "90"}))
            self.assertEqual(Path(path).read_text(), 'seta cg_fov "90"\n')
            self.assertEqual(os.listdir(tmp), ["config_mp.cfg"])

    def test_cod4_fps_fov_skips_active_txt_in_the_profiles_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles_dir = Path(tmp, "profiles")
            (profiles_dir / "Player").mkdir(parents=True)
            (profiles_dir / "Player" / "config_mp.cfg").write_text('seta cg_fov "65"\n')
            (profiles_dir / "active.txt").write_text("Player")
            msgs = []
            install = Path(tmp, "cod4")
            install.mkdir()
            with patch("config.get_setup_games", return_value={"cod4mp": {}}), \
                 patch("config.get_player_name", return_value=None), \
                 patch("config.is_lcd", return_value=False), \
                 patch.object(game_config, "_build_config_map",
                              return_value={"cod4mp": [("config_mp.cfg", None)]}), \
                 patch.object(game_config, "_own_compatdata", return_value=None), \
                 patch.object(game_config, "_dest_from_install", return_value=str(install / "players")), \
                 patch.object(game_config, "_offline_mirror_paths", return_value=[]), \
                 patch.object(game_config, "_cod4_appdata_profiles", return_value=str(profiles_dir)):
                game_config.set_fps_fov(tmp, {"cod4mp": {"install_dir": str(install)}},
                                        on_progress=msgs.append)
            self.assertEqual((profiles_dir / "Player" / "config_mp.cfg").read_text(), 'seta cg_fov "90"\nseta com_maxfps "90"\n')
            self.assertFalse([m for m in msgs if "active.txt" in m])


class DetectGamesTests(unittest.TestCase):
    def test_every_game_has_a_sentinel_group(self):
        self.assertEqual(set(detect_games.GAMES) - set(detect_games.KEY_TO_SENTINEL), set())
        self.assertEqual(detect_games.KEY_TO_SENTINEL["iw5mp_ds"], "mw3")


class ConfigGetterTests(unittest.TestCase):
    def _with_config(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "deckops.json")
        with open(path, "w") as f:
            json.dump(data, f)
        p = patch.object(config, "CONFIG_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        config._cache = config._cache_key = None
        self.addCleanup(lambda: setattr(config, "_cache", None))

    def test_hand_edited_music_values_fall_back(self):
        self._with_config({"music_volume": "loud", "music_enabled": "no"})
        self.assertEqual(config.get_music_volume(), 0.4)
        self.assertTrue(config.get_music_enabled())

    def test_out_of_range_volume_is_clamped(self):
        self._with_config({"music_volume": 7})
        self.assertEqual(config.get_music_volume(), 1.0)

    def test_damaged_setup_entry_does_not_crash_the_lookups(self):
        self._with_config({"setup_games": {"t4mp": "garbage", "t5mp": {"source": "own"}}})
        self.assertTrue(config.is_game_setup_for_source("t4mp", "steam"))
        self.assertFalse(config.is_game_setup_for_source("t4mp", "own"))
        self.assertTrue(config.is_game_setup_for_source("t5mp", "own"))
        self.assertFalse(config.get_offline_mode())


class WriteJsonTests(unittest.TestCase):
    def test_writes_whole_and_syncs_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.json")
            steam_common.write_json(path, {"a": [1, 2]})
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": [1, 2]})
            self.assertEqual(os.listdir(tmp), ["x.json"])

    def test_data_that_cannot_be_dumped_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.json")
            steam_common.write_json(path, {"a": 1})
            with self.assertRaises(TypeError):
                steam_common.write_json(path, {"a": object()})
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 1})
            self.assertEqual(os.listdir(tmp), ["x.json"])

    def test_relative_path_in_the_working_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                steam_common.write_json("rel.json", {"ok": True})
            finally:
                os.chdir(old)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "rel.json")))


class GithubAssetTests(unittest.TestCase):
    def setUp(self):
        net._LATEST_RELEASES.clear()
        self.addCleanup(net._LATEST_RELEASES.clear)

    def _answer(self, payload):
        class Resp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return json.dumps(payload).encode()
        return patch.object(net.urllib.request, "urlopen", return_value=Resp())

    def test_unexpected_answer_uses_the_fallback(self):
        with self._answer([1, 2, 3]):
            self.assertEqual(net.github_asset("o/r", ".zip", fallback="http://fb"), ("http://fb", None))

    def test_unexpected_answer_without_fallback_says_what_happened(self):
        with self._answer(["x"]):
            with self.assertRaisesRegex(RuntimeError, "Unexpected answer from GitHub"):
                net.github_asset("o/r", ".zip")

    def test_asset_without_a_name_is_skipped(self):
        assets = [{"browser_download_url": "http://x"}, "junk",
                  {"name": "a.zip", "browser_download_url": "http://a", "digest": "sha256:1"}]
        with self._answer({"assets": assets}):
            self.assertEqual(net.github_asset("o/r", ".zip"), ("http://a", "sha256:1"))


class CommitStagedFilesTests(unittest.TestCase):
    def test_still_commits_and_rolls_back_with_strict_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            install, stage = os.path.join(tmp, "game"), os.path.join(tmp, "stage")
            os.makedirs(install)
            for n in ("a", "b"):
                Path(stage).mkdir(exist_ok=True)
                Path(stage, n).write_text(f"new {n}")
            Path(install, "a").write_text("old a")
            net.commit_staged_files(install, stage, ["a", "b"])
            self.assertEqual(Path(install, "a").read_text(), "new a")
            self.assertEqual(Path(install, "b").read_text(), "new b")


if __name__ == "__main__":
    unittest.main()
