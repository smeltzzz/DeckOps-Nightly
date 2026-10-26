"""Regressions for update and repair paths that must never report false success."""

import hashlib
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import depot_downgrade as depot
import update_manifest
import zombies_declassified as zd


class UpdateManifestTests(unittest.TestCase):
    def test_renames_and_deletions_are_explicit(self):
        sha = hashlib.sha1(b"blob 4\0test").hexdigest()
        data = {"status": "ahead", "files": [
            {"status": "renamed", "filename": "src/new.py",
             "previous_filename": "src/old.py", "sha": sha},
            {"status": "removed", "filename": "src/gone.py"},
        ]}
        self.assertEqual(list(update_manifest.compare(data)), [
            ("D", "src/old.py", "-"), ("M", "src/new.py", sha),
            ("D", "src/gone.py", "-"),
        ])

    def test_unsafe_or_incomplete_comparisons_fail_closed(self):
        for path in ("../outside", "/outside", "src/../outside", "src\\bad", "C:/outside", ".update/staged"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                list(update_manifest.compare({"status": "ahead", "files": [
                    {"status": "removed", "filename": path}]}))
        with self.assertRaises(ValueError):
            list(update_manifest.compare({"status": "ahead", "files": [{}] * 300}))

    def test_verified_file_rejects_corrupt_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            path.write_bytes(b"test")
            sha = hashlib.sha1(b"blob 4\0test").hexdigest()
            self.assertTrue(update_manifest.verify(str(path), sha))
            path.write_bytes(b"FAIL")
            self.assertFalse(update_manifest.verify(str(path), sha))


class TransactionTests(unittest.TestCase):
    SHA = "a" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = Path(self.tmp.name, "DeckOps")
        self.stage = self.install / ".update" / "files"
        (self.install / "src").mkdir(parents=True)
        self.stage.mkdir(parents=True)
        (self.install / "VERSION").write_text("old-version")
        (self.install / "deckops.json").write_text("my settings")
        (self.install / "assets" / "music").mkdir(parents=True)
        (self.install / "assets" / "music" / "background.mp3").write_bytes(b"my music")
        (self.install / "src" / "main.py").write_text("print('old app')")
        (self.install / "deckops_identity.sh").write_text("old identity")
        (self.stage / "src").mkdir()
        (self.stage / "src" / "main.py").write_text("print('new app')")
        (self.stage / "deckops_identity.sh").write_text("new identity")
        (self.stage / "VERSION").write_text("stale archive version")
        (self.stage / "deckops.json").write_text("do not use")
        (self.stage / "assets" / "music").mkdir(parents=True)
        (self.stage / "assets" / "music" / "background.mp3").write_bytes(b"replace music")

    def test_full_archive_preserves_settings_and_records_target_version(self):
        update_manifest.apply_update(self.install, self.stage, self.SHA, full=True)
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('new app')")
        self.assertEqual((self.install / "deckops.json").read_text(), "my settings")
        self.assertEqual((self.install / "assets" / "music" / "background.mp3").read_bytes(), b"my music")
        self.assertEqual((self.install / "VERSION").read_text(), self.SHA + "\n")
        self.assertFalse((self.install / ".update" / "transaction.json").exists())

    def test_desktop_launcher_apply_command(self):
        subprocess.run([sys.executable, update_manifest.__file__, "apply",
                        str(self.install), str(self.stage), self.SHA, "full"],
                       check=True, capture_output=True, text=True)
        self.assertEqual((self.install / "VERSION").read_text(), self.SHA + "\n")
        self.assertEqual((self.install / "deckops.json").read_text(), "my settings")

    def test_invalid_python_is_rejected_before_installing_anything(self):
        (self.stage / "src" / "main.py").write_text("def broken(:\n")
        with self.assertRaises(SyntaxError):
            update_manifest.apply_update(self.install, self.stage, self.SHA, full=True)
        self.assertEqual((self.install / "VERSION").read_text(), "old-version")
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('old app')")
        self.assertFalse((self.install / ".update" / "transaction.json").exists())

    def test_failed_full_apply_restores_all_old_files(self):
        original = update_manifest._replace_file

        def fail(source, target):
            if source == self.stage / "src" / "main.py":
                raise OSError("disk full")
            original(source, target)

        with patch.object(update_manifest, "_replace_file", side_effect=fail):
            with self.assertRaisesRegex(OSError, "disk full"):
                update_manifest.apply_update(self.install, self.stage, self.SHA, full=True)
        self.assertEqual((self.install / "deckops_identity.sh").read_text(), "old identity")
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('old app')")
        self.assertEqual((self.install / "VERSION").read_text(), "old-version")
        self.assertFalse((self.install / ".update" / "transaction.json").exists())

    def test_interrupted_update_recovers_on_next_launch(self):
        original = update_manifest._replace_file

        def interrupted(source, target):
            if source == self.stage / "src" / "main.py":
                raise SystemExit("power loss")
            original(source, target)

        with patch.object(update_manifest, "_replace_file", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                update_manifest.apply_update(self.install, self.stage, self.SHA, full=True)
        self.assertTrue((self.install / ".update" / "transaction.json").exists())
        self.assertTrue(update_manifest.recover_update(self.install))
        self.assertEqual((self.install / "deckops_identity.sh").read_text(), "old identity")
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('old app')")
        self.assertEqual((self.install / "VERSION").read_text(), "old-version")
        self.assertFalse((self.install / ".update" / "transaction.json").exists())

    def test_crash_after_version_write_still_rolls_back(self):
        original = update_manifest.os.replace
        version = self.install / "VERSION"

        def crash_after_commit(source, target):
            original(source, target)
            if Path(target) == version:
                raise SystemExit("power loss after recording version")

        with patch.object(update_manifest.os, "replace", side_effect=crash_after_commit):
            with self.assertRaises(SystemExit):
                update_manifest.apply_update(self.install, self.stage, self.SHA, full=True)
        self.assertEqual(version.read_text(), self.SHA + "\n")
        self.assertTrue(update_manifest.recover_update(self.install))
        self.assertEqual(version.read_text(), "old-version")
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('old app')")

    def test_version_only_update_does_not_touch_user_files(self):
        update_manifest.apply_update(self.install, self.stage, self.SHA, [])
        self.assertEqual((self.install / "VERSION").read_text(), self.SHA + "\n")
        self.assertEqual((self.install / "deckops.json").read_text(), "my settings")
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('old app')")

    def test_failed_partial_update_restores_deleted_files(self):
        old = self.install / "src" / "removed.py"
        old.write_text("keep on failure")
        staged = self.stage / "src" / "added.py"
        staged.write_text("print('new')")
        payload = staged.read_bytes()
        sha = hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()
        original = update_manifest._replace_file

        def fail(source, target):
            if source == staged:
                raise OSError("disk full")
            original(source, target)

        with patch.object(update_manifest, "_replace_file", side_effect=fail):
            with self.assertRaisesRegex(OSError, "disk full"):
                update_manifest.apply_update(self.install, self.stage, self.SHA,
                    [("D", "src/removed.py", "-"), ("M", "src/added.py", sha)])
        self.assertEqual(old.read_text(), "keep on failure")
        self.assertFalse((self.install / "src" / "added.py").exists())
        self.assertEqual((self.install / "VERSION").read_text(), "old-version")

    def test_partial_update_removes_only_explicit_deletions(self):
        old = self.install / "src" / "removed.py"
        old.write_text("old module")
        new = self.stage / "src" / "added.py"
        new.write_text("print('new module')")
        payload = new.read_bytes()
        sha = hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()
        update_manifest.apply_update(self.install, self.stage, self.SHA,
            [("D", "src/removed.py", "-"), ("M", "src/added.py", sha)])
        self.assertFalse(old.exists())
        self.assertEqual((self.install / "src" / "added.py").read_bytes(), payload)
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('old app')")


class DepotLoginTests(unittest.TestCase):
    def test_cleanup_removes_only_private_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp, "depotdownloader")
            other = Path(tmp, "other-app", "IsolatedStorage", "account.config")
            other.parent.mkdir(parents=True)
            other.write_text("keep")
            with patch.object(depot, "DEPOTDOWNLOADER_DIR", str(private)):
                env = depot._depot_env()
                self.assertEqual(env["HOME"], str(private / ".home"))
                token = Path(env["XDG_DATA_HOME"], "IsolatedStorage", "account.config")
                token.parent.mkdir(parents=True)
                token.write_text("private-login")
                self.assertEqual(depot.cleanup_depotdownloader(), 1)
            self.assertFalse(private.exists())
            self.assertEqual(other.read_text(), "keep")


class ZombiesRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.relpath = "mods/dlc5/test.ff"
        self.file = Path(self.root, self.relpath)
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"good")
        zd._write_metadata(self.root, [self.relpath])
        self.etag = hashlib.md5(b"good").hexdigest()

    def test_unchanged_missing_and_same_size_corruption(self):
        downloads = []

        def download(url, dest, **kwargs):
            downloads.append(url)
            Path(dest).write_bytes(b"good")

        with patch.object(zd, "fetch_manifest", return_value=[self.relpath]), \
             patch.object(zd, "_remote_info", return_value=(4, self.etag)), \
             patch.object(zd, "_download", side_effect=download):
            self.assertFalse(zd.update_zd(self.root))
            self.assertEqual(downloads, [])
            self.file.write_bytes(b"baad")
            self.assertTrue(zd.update_zd(self.root))
            self.assertEqual(self.file.read_bytes(), b"good")
            self.file.unlink()
            self.assertTrue(zd.update_zd(self.root))
            self.assertEqual(len(downloads), 2)

    def test_failed_repair_does_not_claim_success(self):
        self.file.unlink()
        with patch.object(zd, "fetch_manifest", return_value=[self.relpath]), \
             patch.object(zd, "_remote_info", return_value=(4, self.etag)), \
             patch.object(zd, "_download", side_effect=OSError("offline")):
            with self.assertRaisesRegex(RuntimeError, "could not be repaired"):
                zd.update_zd(self.root)
        self.assertTrue(zd.get_zd_info(self.root)["manifest_hash"].startswith("incomplete-"))

    def test_unverifiable_file_is_kept_but_not_called_current(self):
        with patch.object(zd, "fetch_manifest", return_value=[self.relpath]), \
             patch.object(zd, "_remote_info", return_value=(None, None)), \
             patch.object(zd, "_download") as download:
            with self.assertRaisesRegex(RuntimeError, "Could not verify"):
                zd.update_zd(self.root)
            download.assert_not_called()
        self.assertEqual(self.file.read_bytes(), b"good")


if __name__ == "__main__":
    unittest.main()
