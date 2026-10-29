"""Regression tests for shipped controller deadzones and in-place migration."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import game_config as gc


ROOT = Path(__file__).resolve().parents[1]
MIN = b'seta gpad_stick_deadzone_min "0.05"'
MAX = b'seta gpad_stick_deadzone_max "0.00"'
OLD_MIN = b'seta gpad_stick_deadzone_min "0.2"'
OLD_MAX = b'seta gpad_stick_deadzone_max "0.01"'


class DeadzoneTests(unittest.TestCase):
    def test_every_applicable_shipped_cfg_has_the_defaults(self):
        expected = {
            "MW2/iw4x_config.cfg", "MW3/config_mp.cfg",
            "WaW/config.cfg", "WaW/config_mp.cfg",
            "BO1/config.cfg", "BO1/config_mp.cfg",
            "BO2/plutonium_mp.cfg", "BO2/plutonium_zm.cfg",
        }
        models = ["LCD", "OLED", "Other/own", "Other/1280x720",
                  "Other/1280x800", "Other/1920x1080", "Other/1920x1200",
                  "Other/1920x1200_144hz"]
        for model in models:
            base = ROOT / "assets" / "configs" / model
            for relative in expected:
                with self.subTest(model=model, config=relative):
                    data = (base / relative).read_bytes()
                    self.assertEqual(data.count(MIN), 1)
                    self.assertEqual(data.count(MAX), 1)
                    self.assertNotIn(OLD_MIN, data)
                    self.assertNotIn(OLD_MAX, data)

    def test_supported_configs_only_and_idempotent_preserving_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp = root / "mw2" / "players" / "iw4x_config.cfg"
            mp.parent.mkdir(parents=True)
            before = (b'seta name "Me"\r\nseta sensitivity "7"\r\n'
                      b'seta gpad_stick_deadzone_min "0.2"\r\n'
                      b'seta gpad_stick_deadzone_max "0.01"\r\n'
                      b'seta gpad_stick_deadzone_min_extra "99"\r\n')
            mp.write_bytes(before)
            sp = mp.with_name("config.cfg")
            sp.write_bytes(before)
            mw3 = root / "mw3" / "players2" / "config.cfg"
            mw3.parent.mkdir(parents=True)
            mw3.write_bytes(before)
            binary = root / "bo2" / "players" / "user_sp.cgp"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"\x00\xff" + before)
            setup = {"iw4mp": {}, "iw4sp": {}, "iw5sp": {}, "t6sp": {},
                     "cod4mp": {}, "t6zm": {}}
            games = {"iw4mp": {"install_dir": str(root / "mw2")},
                     "iw4sp": {"install_dir": str(root / "mw2")},
                     "iw5sp": {"install_dir": str(root / "mw3")},
                     "t6sp": {"install_dir": str(root / "bo2")}}
            messages = []
            with patch("config.get_setup_games", return_value=setup), \
                 patch("config.get_player_name", return_value="Me"), \
                 patch("config.is_lcd", return_value=False), \
                 patch.object(gc, "_offline_mirror_paths", return_value=[]):
                self.assertEqual(gc.set_deadzone(tmp, games, on_progress=messages.append), 1)
                self.assertEqual(gc.set_deadzone(tmp, games), 0)
            self.assertEqual(mp.read_bytes(), before.replace(OLD_MIN, MIN).replace(OLD_MAX, MAX))
            self.assertEqual(sp.read_bytes(), before)
            self.assertEqual(mw3.read_bytes(), before)
            self.assertEqual(binary.read_bytes(), b"\x00\xff" + before)
            self.assertFalse((root / "bo2" / "config_sp.cfg").exists())
            self.assertFalse((root / "mw3" / "players2" / "iw5_config.cfg").exists())
            self.assertEqual(len([m for m in messages if "  + " in m]), 1)

    def test_own_prefix_lcd_heroic_and_offline_mirrors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            own = root / "own-prefix"
            local = own / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local"
            primary = local / "Plutonium" / "storage" / "t5" / "players" / "config_mp.cfg"
            heroic_root = root / "heroic"
            heroic = (heroic_root / "pfx" / "drive_c" / "users" / "steamuser" /
                      "AppData" / "Local" / "Plutonium" / "storage" / "t5" /
                      "players" / "config_mp.cfg")
            offline = root / "offline" / "config_mp.cfg"
            old = b'seta sensitivity "5"\n' + OLD_MIN + b'\n' + OLD_MAX + b'\n'
            for path in (primary, heroic, offline):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(old)
            games = {"t5mp": {"source": "own", "compatdata_path": str(own)}}
            messages = []
            with patch("config.get_setup_games", return_value={"t5mp": {}}), \
                 patch("config.get_player_name", return_value="Me"), \
                 patch("config.is_lcd", return_value=True), \
                 patch.object(gc, "HEROIC_DEFAULT_PFX", str(heroic_root)), \
                 patch.object(gc, "_offline_mirror_paths", return_value=[str(offline.parent)]):
                count = gc.set_deadzone(tmp, games, on_progress=messages.append)
                self.assertEqual(count, 3, (messages, gc._heroic_mirror_path(str(primary.parent)),
                                            str(heroic.parent), primary.read_bytes(), heroic.read_bytes(), offline.read_bytes()))
                self.assertEqual(gc.set_deadzone(tmp, games), 0)
            for path in (primary, heroic, offline):
                self.assertEqual(path.read_bytes(), old.replace(OLD_MIN, MIN).replace(OLD_MAX, MAX))
            self.assertFalse((root / "steamapps" / "compatdata" / "42710" / "pfx").exists())

    def test_unsupported_games_do_not_report_success_or_create_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = []
            games = {key: {"install_dir": str(Path(tmp) / key)}
                     for key in ("cod4sp", "cod4mp", "iw4sp", "iw5sp", "t6sp")}
            with patch("config.get_setup_games", return_value=dict.fromkeys(games)):
                self.assertEqual(gc.set_deadzone(tmp, games, on_progress=messages.append), 0)
            self.assertEqual(messages, ["No supported games set up yet."])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_missing_supported_line_is_added_only_to_existing_text_cfg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp = root / "mw2" / "players" / "iw4x_config.cfg"
            mp.parent.mkdir(parents=True)
            mp.write_bytes(b'seta sensitivity "7"\r\n')
            with patch("config.get_setup_games", return_value={"iw4mp": {}, "iw5mp": {}}), \
                 patch("config.get_player_name", return_value="Me"), \
                 patch("config.is_lcd", return_value=False), \
                 patch.object(gc, "_offline_mirror_paths", return_value=[]):
                self.assertEqual(gc.set_deadzone(tmp, {"iw4mp": {"install_dir": str(root / "mw2")}},
                                                 deadzone_min=0.05, deadzone_max=0.0), 1)
            self.assertEqual(mp.read_bytes(), b'seta sensitivity "7"\r\n' + MIN + b'\r\n' + MAX + b'\r\n')
            self.assertFalse((root / "steamapps" / "compatdata" / "42690" / "pfx").exists())

    def test_install_and_refresh_use_updated_assets_and_keep_own_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "mw2"
            cfg = game / "players" / "iw4x_config.cfg"
            games = {"iw4mp": {"install_dir": str(game)}}
            with patch("config.get_model_config_dir", return_value="Other/own"), \
                 patch("config.get_player_name", return_value="Me"):
                self.assertEqual(gc.apply_game_configs(["iw4mp"], games, tmp, "other")[0], 3)
                self.assertIn(MIN, cfg.read_bytes())
                self.assertIn(MAX, cfg.read_bytes())
                cfg.write_bytes(cfg.read_bytes().replace(MIN, OLD_MIN).replace(MAX, OLD_MAX)
                                + b'\r\nseta r_mode "1920x1080"\r\n')
                self.assertEqual(gc.apply_game_configs(["iw4mp"], games, tmp, "other")[0], 3)
            self.assertIn(MIN, cfg.read_bytes())
            self.assertIn(MAX, cfg.read_bytes())
            self.assertIn(b'seta r_mode "1920x1080"', cfg.read_bytes())


if __name__ == "__main__":
    unittest.main()
