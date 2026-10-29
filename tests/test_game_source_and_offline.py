"""Configured source selection and Offline Mode failure reporting."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import detect_games
import plutonium


class GameSourceTests(unittest.TestCase):
    def test_steam_root_detection_requires_install_and_accepts_debian_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp, "empty")
            debian = Path(tmp, ".steam", "debian-installation")
            empty.mkdir()
            (debian / "steamapps").mkdir(parents=True)
            with patch.object(detect_games, "STEAM_PATHS", [str(empty), str(debian)]):
                self.assertEqual(detect_games.find_steam_root(), str(debian))

    def test_own_copy_requires_each_modes_executable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(detect_games, "OWN_SCAN_PATHS", []):
            game = Path(tmp, "BO2")
            maps = game / "zone" / "all"
            maps.mkdir(parents=True)
            (maps / "mp_hijacked.ff").write_bytes(b"map")
            (game / "t6mp.exe").write_bytes(b"exe")
            found = detect_games.find_own_installed(extra_paths=[tmp])
            self.assertEqual(set(found), {"t6mp"})
            (game / "T6SP-Mod.exe").write_bytes(b"sp client")
            (game / "t6zm.exe").write_bytes(b"zombies")
            found = detect_games.find_own_installed(extra_paths=[tmp])
            self.assertEqual(set(found), {"t6sp", "t6mp", "t6zm"})

    def test_configured_own_copy_wins_when_steam_copy_is_also_present(self):
        steam = {"t4mp": {"install_dir": "/steam/waw"}}
        own = {"t4mp": {"install_dir": "/my/waw", "source": "own"}}
        with patch.object(detect_games, "parse_library_folders", return_value=[]), \
             patch.object(detect_games, "find_installed_games", return_value=steam), \
             patch.object(detect_games, "find_own_installed", return_value=own):
            default = detect_games.find_all_games("/steam")
            configured = detect_games.find_all_games(
                "/steam", preferred_sources={"t4mp": {"client": "plutonium", "source": "own"}})
        self.assertEqual(default["t4mp"]["install_dir"], "/steam/waw")
        self.assertEqual(configured["t4mp"]["install_dir"], "/my/waw")
        self.assertEqual(configured["t4mp"]["source"], "own")

    def test_preference_falls_back_to_installed_steam_copy(self):
        with patch.object(detect_games, "parse_library_folders", return_value=[]), \
             patch.object(detect_games, "find_installed_games", return_value={"t4mp": {"install_dir": "/steam/waw"}}), \
             patch.object(detect_games, "find_own_installed", return_value={}):
            result = detect_games.find_all_games("/steam", preferred_sources={"t4mp": {"source": "own"}})
        self.assertEqual(result["t4mp"]["source"], "steam")


class OfflineModeTests(unittest.TestCase):
    def test_failed_removal_never_claims_offline_mode_is_ready(self):
        with patch("config.set_offline_mode") as set_mode, \
             patch.object(plutonium, "remove_lan", side_effect=RuntimeError("server busy")):
            with self.assertRaisesRegex(RuntimeError, "server busy"):
                plutonium.apply_offline_mode(False, "/steam")
        self.assertEqual([c.args[0] for c in set_mode.call_args_list], [False, False])

    def test_no_games_cannot_claim_offline_mode_is_ready(self):
        with patch("config.get_offline_mode", return_value=False), \
             patch("config.set_offline_mode") as set_mode, \
             patch("config.get_setup_games", return_value={}), \
             patch("detect_games.find_all_games", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "No configured Plutonium games"):
                plutonium.apply_offline_mode(True, "/steam")
        self.assertEqual([c.args[0] for c in set_mode.call_args_list], [True, False])

    def test_missing_own_copy_does_not_modify_available_steam_copy(self):
        setup = {"t4mp": {"client": "plutonium", "source": "own"}}
        with patch("config.get_offline_mode", return_value=False), \
             patch("config.set_offline_mode") as set_mode, \
             patch("config.get_setup_games", return_value=setup), \
             patch("detect_games.find_all_games", return_value={"t4mp": {
                 "name": "World at War", "install_dir": "/steam/waw", "source": "steam"}}), \
             patch("wrapper.get_proton_path", return_value="/steam/proton"), \
             patch.object(plutonium, "install_plutonium") as install:
            with self.assertRaisesRegex(RuntimeError, "configured own game folder not found"):
                plutonium.apply_offline_mode(True, "/steam")
        install.assert_not_called()
        self.assertEqual([c.args[0] for c in set_mode.call_args_list], [True, False])

    def test_failed_game_reports_error_and_restores_disabled_state(self):
        game = {"name": "World at War", "install_dir": "/my/waw", "source": "own"}
        setup = {"t4mp": {"client": "plutonium", "source": "own"}}
        with patch("config.get_offline_mode", return_value=False), \
             patch("config.set_offline_mode") as set_mode, \
             patch("config.get_setup_games", return_value=setup), \
             patch("detect_games.find_all_games", return_value={"t4mp": game}) as find, \
             patch("wrapper.get_proton_path", return_value="/steam/proton"), \
             patch.object(plutonium, "_plut_compat", return_value="/prefix"), \
             patch.object(plutonium, "install_plutonium", side_effect=RuntimeError("download failed")), \
             patch("shortcut.create_launcher_shortcut") as shortcut:
            with self.assertRaisesRegex(RuntimeError, "t4mp: download failed"):
                plutonium.apply_offline_mode(True, "/steam")
        find.assert_called_once_with("/steam", preferred_sources=setup)
        shortcut.assert_not_called()
        self.assertEqual([c.args[0] for c in set_mode.call_args_list], [True, False])

    def test_success_uses_the_configured_own_copy(self):
        game = {"name": "World at War", "install_dir": "/my/waw", "source": "own"}
        setup = {"t4mp": {"client": "plutonium", "source": "own"}}
        with patch("config.get_offline_mode", return_value=False), \
             patch("config.set_offline_mode") as set_mode, \
             patch("config.get_setup_games", return_value=setup), \
             patch("config.get_deck_model", return_value="oled"), \
             patch("detect_games.find_all_games", return_value={"t4mp": game}), \
             patch("wrapper.get_proton_path", return_value="/steam/proton"), \
             patch.object(plutonium, "_plut_compat", return_value="/prefix"), \
             patch.object(plutonium, "install_plutonium") as install, \
             patch("game_config.apply_game_configs"), \
             patch("shortcut.create_launcher_shortcut"):
            plutonium.apply_offline_mode(True, "/steam")
        self.assertIs(install.call_args.args[0], game)
        self.assertEqual(install.call_args.kwargs["source"], "own")
        self.assertEqual([c.args[0] for c in set_mode.call_args_list], [True])


if __name__ == "__main__":
    unittest.main()
