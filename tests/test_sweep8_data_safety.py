"""Steam data-safety regressions; all roots and player files are temporary."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import controller_profiles as profiles
import save_backup as saves
import shortcut
import steam_appinfo


class SaveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backup = self.root / "backup"
        self.backup.mkdir()
        p = patch.object(saves, "BACKUP_ROOT", str(self.backup))
        p.start()
        self.addCleanup(p.stop)

    def test_plutonium_mods_and_maps_are_kept_before_restore(self):
        dst = self.root / "steam" / "storage" / "t6"
        for part in ("mods", "usermaps"):
            (dst / part).mkdir(parents=True)
            (dst / part / "custom.cfg").write_text("newer player data")
            src = self.backup / "t6" / "202990" / part
            src.mkdir(parents=True)
            (src / "custom.cfg").write_text("old backup")
        (self.backup / "backup_info.json").write_text(json.dumps({"groups": ["t6"]}))
        with patch.object(saves, "_load_config", return_value={"setup_games": {}}), \
             patch.object(saves, "_plut_restore_dst", return_value=str(dst)):
            self.assertIn("t6", saves.restore_saves(str(self.root / "steam")))
        for part in ("mods", "usermaps"):
            self.assertEqual((dst / (part + ".pre-restore") / "custom.cfg").read_text(), "newer player data")
            self.assertEqual((dst / part / "custom.cfg").read_text(), "old backup")

    def test_failed_pre_restore_copy_preserves_previous_safety_copies(self):
        players = self.root / "game" / "players"
        players.mkdir(parents=True)
        (players / "stats.cfg").write_text("current")
        previous = Path(str(players) + ".pre-restore")
        (previous / "stats.cfg").parent.mkdir()
        (previous / "stats.cfg").write_text("previous")
        older = Path(str(previous) + ".1")
        older.mkdir()
        (older / "stats.cfg").write_text("older")
        with patch.object(saves.shutil, "copytree", side_effect=OSError("disk full")):
            self.assertFalse(saves._keep_pre_restore(str(players)))
        self.assertEqual((previous / "stats.cfg").read_text(), "previous")
        self.assertEqual((older / "stats.cfg").read_text(), "older")
        self.assertEqual((players / "stats.cfg").read_text(), "current")

    def test_failed_safety_copy_rollback_keeps_all_available_generations(self):
        players = self.root / "game" / "players"
        players.mkdir(parents=True)
        (players / "stats.cfg").write_text("current")
        previous = Path(str(players) + ".pre-restore")
        previous.mkdir()
        (previous / "stats.cfg").write_text("previous")
        older = Path(str(previous) + ".1")
        older.mkdir()
        (older / "stats.cfg").write_text("older")
        original = saves.os.replace

        def fail_commit_and_rollback(src, dst):
            if dst == str(previous) and (str(src).endswith("saves") or str(src) == str(older)):
                raise OSError("simulated rename failure")
            return original(src, dst)

        with patch.object(saves.os, "replace", side_effect=fail_commit_and_rollback):
            self.assertFalse(saves._keep_pre_restore(str(players)))
        self.assertEqual((players / "stats.cfg").read_text(), "current")
        self.assertEqual((older / "stats.cfg").read_text(), "previous")
        stages = list(players.parent.glob(".deckops-restore-*"))
        self.assertEqual(len(stages), 1)
        self.assertEqual((stages[0] / "older" / "stats.cfg").read_text(), "older")
        self.assertEqual((stages[0] / "saves" / "stats.cfg").read_text(), "current")

    def test_interrupted_backup_swap_is_visible_to_restore(self):
        # A crash after backup -> .old but before .new -> backup must not hide saves.
        old = self.backup / ".cod4.old" / "stats.cfg"
        old.parent.mkdir()
        old.write_text("saved")
        (self.backup / "backup_info.json").write_text(json.dumps({"groups": ["cod4"]}))
        game = self.root / "game"
        game.mkdir()
        with patch.object(saves, "_load_config", return_value={"setup_games": {"cod4mp": {}}}), \
             patch.object(saves, "_find_install_dir", return_value=str(game)):
            self.assertIn("cod4", saves.restore_saves(str(self.root / "steam")))
        self.assertEqual((game / "players" / "stats.cfg").read_text(), "saved")


class SteamSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_controller_changed_layout_copy_failure_aborts_replacement(self):
        source = self.root / "bundle.vdf"
        target = self.root / "controller_neptune.vdf"
        source.write_bytes(b"deckops")
        target.write_bytes(b"player edits")
        (self.root / "controller_neptune.vdf.deckops-owned").write_text("different hash")
        with patch.object(profiles.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                profiles.install_layout(str(source), str(target))
        self.assertEqual(target.read_bytes(), b"player edits")

    def test_malformed_shortcuts_removal_does_not_claim_success_or_delete_artwork(self):
        userdata = self.root / "userdata"
        config = userdata / "12345" / "config"
        grid = config / "grid"
        grid.mkdir(parents=True)
        vdf = config / "shortcuts.vdf"
        vdf.write_bytes(b"\x00shortcuts\x00bad\x08\x08")
        name, exe = "My Game", '"/fake/game.exe"'
        appid = shortcut._calc_shortcut_appid(exe, name)
        art = grid / f"{appid}_icon.png"
        art.write_bytes(b"player artwork")
        with patch.object(shortcut, "_find_all_steam_uids", return_value=["12345"]), \
             patch.object(shortcut, "_userdata_dir", return_value=str(userdata)):
            self.assertFalse(shortcut.remove_shortcut(name, exe, {"icon_ext": "png"}))
        self.assertEqual(art.read_bytes(), b"player artwork")
        self.assertEqual(vdf.read_bytes(), b"\x00shortcuts\x00bad\x08\x08")

    def test_failed_appinfo_removal_keeps_reapply_record_for_retry(self):
        with patch.object(steam_appinfo, "_edit_launch", return_value=False), \
             patch.object(steam_appinfo, "_record") as record:
            self.assertFalse(steam_appinfo.remove_launch_entries(str(self.root), 123, ["mod.exe"]))
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
