"""Only the bundled Steam Deck layout may be assigned by DeckOps."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import controller_profiles as profiles
import shortcut


class BundledLayoutTests(unittest.TestCase):
    def test_bundle_is_neptune_and_has_no_personal_workshop_identifiers(self):
        text = (Path(profiles.ASSETS_DIR) / profiles.TEMPLATE).read_text(encoding="utf-8")
        self.assertIn('"title"\t\t"Call of Duty with controller support"', text)
        self.assertIn('"controller_type"\t\t"controller_neptune"', text)
        self.assertNotIn('"creator"', text)
        self.assertNotIn('workshop://', text)
        self.assertEqual(shortcut._get_template_filename("other", "toggle"), profiles.TEMPLATE)

    def test_fresh_install_assigns_same_layout_to_game_and_shortcut(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(profiles, "STEAM_ROOT", folder), \
             patch.object(profiles, "TEMPLATES_DIR", os.path.join(folder, "templates")), \
             patch.object(profiles, "_is_steam_deck", return_value=True), \
             patch.object(profiles, "find_all_steam_uids", return_value=["12345"]), \
             patch.object(profiles, "get_deck_serial", return_value="SERIAL"), \
             patch.object(profiles, "_shortcut_appids", return_value={"2147483649": "WaW MP"}), \
             patch.object(profiles, "patch_configset") as patch_config, \
             patch("shortcut.get_shortcut_appid", return_value=None), \
             patch("detect_games.find_own_installed", return_value={}):
            profiles.install_controller_templates()
            profiles.assign_controller_profiles()
            template = Path(folder, "templates", profiles.TEMPLATE)
            self.assertTrue(template.is_file())
            canonical = Path(folder, "steamapps", "common", "Steam Controller Configs", "12345",
                             "config", "7940", profiles.CANONICAL)
            self.assertEqual(canonical.read_bytes(), template.read_bytes())
            user_copy = Path(folder, "userdata", "12345", "241100", "remote", "controller_config",
                             "2147483649", profiles.TEMPLATE)
            self.assertEqual(user_copy.read_bytes(), template.read_bytes())
            self.assertTrue(any(call.args[1:] == ("7940", profiles.TEMPLATE)
                                for call in patch_config.call_args_list))
            self.assertTrue(any(call.args[1:] == ("2147483649", profiles.TEMPLATE)
                                for call in patch_config.call_args_list))
            self.assertTrue(any("configset_SERIAL.vdf" in call.args[0]
                                for call in patch_config.call_args_list))

    def test_assignment_preserves_existing_canonical_layout_for_uninstall(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(profiles, "STEAM_ROOT", folder), \
             patch.object(profiles, "get_deck_serial", return_value=None), \
             patch.object(profiles, "patch_configset"):
            canonical = Path(folder, "steamapps", "common", "Steam Controller Configs", "12345",
                             "config", "7940", profiles.CANONICAL)
            canonical.parent.mkdir(parents=True)
            canonical.write_text("my original Steam layout")
            profiles._write_layout("12345", "7940")
            self.assertEqual(Path(str(canonical) + ".deckops-original").read_text(),
                             "my original Steam layout")
            self.assertTrue(Path(str(canonical) + ".deckops-owned").is_file())
            profiles._write_layout("12345", "7940")
            self.assertEqual(Path(str(canonical) + ".deckops-original").read_text(),
                             "my original Steam layout")

    def test_old_deckops_templates_removed_but_player_layout_kept(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(profiles, "TEMPLATES_DIR", folder), \
             patch.object(profiles, "_is_steam_deck", return_value=True):
            old = Path(folder, "controller_neptune_deckops_ads.vdf")
            own = Path(folder, "controller_neptune.vdf")
            old.write_text("old DeckOps template")
            own.write_text("my own layout")
            profiles.install_controller_templates()
            self.assertFalse(old.exists())
            self.assertEqual(own.read_text(), "my own layout")
            self.assertTrue(Path(folder, profiles.TEMPLATE).exists())

    def test_non_deck_does_not_install_or_assign_a_neptune_layout(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(profiles, "TEMPLATES_DIR", folder), \
             patch.object(profiles, "_is_steam_deck", return_value=False), \
             patch.object(profiles, "find_all_steam_uids") as find_users:
            profiles.install_controller_templates()
            profiles.assign_controller_profiles()
            self.assertEqual(os.listdir(folder), [])
            find_users.assert_not_called()


if __name__ == "__main__":
    unittest.main()
