"""Sweep 7 data-safety regressions against temporary backup roots only."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import save_backup as sb


class BackupCommitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(sb, "BACKUP_ROOT", str(self.root))
        p.start()
        self.addCleanup(p.stop)
        self.dst = self.root / "t6"
        self.old = self.root / ".t6.old"
        self.staging = self.root / ".t6.new"
        for folder, text in ((self.old, "last complete backup"), (self.staging, "new complete backup")):
            (folder / "players").mkdir(parents=True)
            (folder / "players" / "stats.cfg").write_text(text)

    def test_interrupted_swap_keeps_the_last_complete_backup_when_the_next_commit_fails(self):
        # A crash after dst -> old but before staging -> dst leaves only .old.
        self.assertFalse(self.dst.exists())
        original = sb.os.replace

        def fail_commit(src, dst):
            if Path(src) == self.staging and Path(dst) == self.dst:
                raise OSError("read-only destination")
            return original(src, dst)

        with patch.object(sb.os, "replace", side_effect=fail_commit):
            ok = sb._commit(str(self.staging), str(self.dst), "t6")
        self.assertFalse(ok)
        self.assertEqual((self.dst / "players" / "stats.cfg").read_text(), "last complete backup")

    def test_interrupted_swap_is_recovered_before_a_later_successful_commit(self):
        self.assertFalse(self.dst.exists())
        ok = sb._commit(str(self.staging), str(self.dst), "t6")
        self.assertTrue(ok)
        self.assertEqual((self.dst / "players" / "stats.cfg").read_text(), "new complete backup")
        self.assertFalse(self.old.exists())
        self.assertFalse(self.staging.exists())


if __name__ == "__main__":
    unittest.main()
