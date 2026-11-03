"""Save backup regressions: a partial copy is never a usable backup."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import save_backup as sb


class SaveBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backup = self.root / "backup"
        self.storage = self.root / "storage"
        (self.storage / "players").mkdir(parents=True)
        (self.storage / "mods").mkdir()
        (self.storage / "players" / "stats.cfg").write_text("old stats")
        (self.storage / "mods" / "custom.gsc").write_text("old mod")
        for p in (patch.object(sb, "BACKUP_ROOT", str(self.backup)),
                  patch.object(sb, "_load_config", return_value={"setup_games": {"t6zm": {}}}),
                  patch.object(sb, "_find_plutonium_storages", return_value={"212910": str(self.storage)})):
            p.start()
            self.addCleanup(p.stop)

    def _backup(self):
        failures, messages = [], []
        result = sb.backup_saves(steam_root=str(self.root), failures=failures,
                                 on_progress=messages.append)
        return result, failures, messages

    def _partial_copy(self):
        original = sb.shutil.copytree

        def copy_then_fail(src, dst, *args, **kwargs):
            if Path(src) == self.storage / "mods":
                Path(dst).mkdir(parents=True, exist_ok=True)
                (Path(dst) / "partial.gsc").write_text("incomplete")
                raise OSError("disk full")
            return original(src, dst, *args, **kwargs)

        return patch.object(sb.shutil, "copytree", side_effect=copy_then_fail)

    def test_partial_first_backup_is_failure_not_committed_or_listed(self):
        with self._partial_copy():
            result, failures, messages = self._backup()
        self.assertEqual(result, {})
        self.assertEqual(failures, ["t6"])
        self.assertFalse((self.backup / "t6").exists())
        self.assertFalse((self.backup / ".t6.new").exists())
        self.assertFalse((self.backup / "backup_info.json").exists())
        self.assertTrue(any("incomplete" in msg for msg in messages))
        self.assertNotIn("No save data found", "\n".join(messages))
        self.assertIn("Backup incomplete", sb.describe_backup(result, failures))

    def test_partial_update_keeps_the_previous_complete_backup_and_metadata(self):
        result, failures, _ = self._backup()
        self.assertEqual(list(result), ["t6"])
        self.assertEqual(failures, [])
        before = (self.backup / "backup_info.json").read_bytes()
        (self.storage / "players" / "stats.cfg").write_text("new stats")
        (self.storage / "mods" / "custom.gsc").write_text("new mod")
        with self._partial_copy():
            result, failures, messages = self._backup()
        self.assertEqual(result, {})
        self.assertEqual(failures, ["t6"])
        self.assertEqual((self.backup / "t6" / "212910" / "players" / "stats.cfg").read_text(), "old stats")
        self.assertEqual((self.backup / "t6" / "212910" / "mods" / "custom.gsc").read_text(), "old mod")
        self.assertFalse((self.backup / "t6" / "212910" / "mods" / "partial.gsc").exists())
        self.assertFalse((self.backup / ".t6.new").exists())
        self.assertEqual((self.backup / "backup_info.json").read_bytes(), before)
        self.assertTrue(any("previous backup is kept" in msg for msg in messages))

    def test_custom_usermaps_and_mods_round_trip(self):
        custom = self.storage / "usermaps" / "zm_my_custom_map" / "map.ff"
        custom.parent.mkdir(parents=True)
        custom.write_text("my map")
        (self.storage / "mods" / "dlc5").mkdir()
        (self.storage / "mods" / "dlc5" / "mine.gsc").write_text("custom mod")
        second = self.root / "second_storage"
        other = second / "usermaps" / "zm_other_map" / "mine.ff"
        other.parent.mkdir(parents=True)
        other.write_text("other map")
        with patch.object(sb, "_find_plutonium_storages", return_value={
                "212910": str(self.storage), "202990": str(second)}):
            result, failures, messages = self._backup()
        self.assertEqual(failures, [])
        self.assertEqual(list(result), ["t6"])
        saved = self.backup / "t6"
        self.assertEqual((saved / "212910" / "usermaps" / "zm_my_custom_map" / "map.ff").read_text(), "my map")
        self.assertEqual((saved / "212910" / "mods" / "dlc5" / "mine.gsc").read_text(), "custom mod")
        self.assertEqual((saved / "202990" / "usermaps" / "zm_other_map" / "mine.ff").read_text(), "other map")
        target = self.root / "restored_storage"
        with patch.object(sb, "_plut_restore_dst", return_value=str(target)):
            self.assertEqual(list(sb.restore_saves(steam_root=str(self.root))), ["t6"])
        self.assertEqual((target / "usermaps" / "zm_my_custom_map" / "map.ff").read_text(), "my map")
        self.assertEqual((target / "usermaps" / "zm_other_map" / "mine.ff").read_text(), "other map")
        self.assertEqual((target / "mods" / "dlc5" / "mine.gsc").read_text(), "custom mod")

    def test_unrecorded_backup_is_reported_as_a_failure(self):
        with patch.object(sb, "_write_meta", side_effect=OSError("read-only")):
            result, failures, messages = self._backup()
        self.assertEqual(list(result), ["t6"])
        self.assertEqual(failures, ["backup list"])
        self.assertFalse(sb.has_backups())
        self.assertTrue(any("backup incomplete" in m.lower() for m in messages))

    def test_restore_is_offered_once_per_backup(self):
        self._backup()
        self.assertTrue(sb.restore_offer_pending())
        sb.mark_restore_offer_handled()
        self.assertFalse(sb.restore_offer_pending())
        self.assertTrue(sb.has_backups())           # still there to restore by hand
        (self.storage / "players" / "stats.cfg").write_text("newer")
        import time
        time.sleep(0.01)                            # a new backup gets a new timestamp
        self._backup()
        self.assertTrue(sb.restore_offer_pending())

    def test_valid_json_of_the_wrong_shape_does_not_crash_meta_handling(self):
        self.backup.mkdir()
        (self.backup / "backup_info.json").write_text("[1, 2, 3]")
        self.assertFalse(sb.restore_offer_pending())
        self.assertEqual(sb.restore_saves(steam_root=str(self.root)), {})
        result, failures, _ = self._backup()
        self.assertEqual((list(result), failures), (["t6"], []))

    def test_a_second_restore_keeps_the_first_ones_pre_restore_copy(self):
        live = self.root / "live"
        live.mkdir()
        (live / "stats.cfg").write_text("v1")
        sb._keep_pre_restore(str(live))
        (live / "stats.cfg").write_text("v2")
        sb._keep_pre_restore(str(live))
        self.assertEqual((Path(str(live) + ".pre-restore") / "stats.cfg").read_text(), "v2")
        self.assertEqual((Path(str(live) + ".pre-restore.1") / "stats.cfg").read_text(), "v1")

    def test_mw3_dedicated_server_saves_are_in_the_mw3_group(self):
        group = sb.SAVE_GROUPS["iw5"]
        self.assertIn("iw5mp_ds", group["keys"])
        self.assertIn("42750", group["appids"])


if __name__ == "__main__":
    unittest.main()
