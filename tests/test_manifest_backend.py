"""Manifest updates must not expose unverified bytes or lose the user's originals."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import manifest_mods as mm


class ManifestTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.rel = "Game/mods/file.ff"
        self.dest = Path(self.root, *self.rel.split("/"))
        self.dest.parent.mkdir(parents=True)
        self.cdn_patch = patch.object(mm, "save_cdn")
        self.remember_patch = patch.object(mm, "_remember")
        self.cdn_patch.start(); self.remember = self.remember_patch.start()
        self.addCleanup(self.cdn_patch.stop)
        self.addCleanup(self.remember_patch.stop)

    def manifest(self, version, contents, other=None):
        def entry(path, data):
            return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        files = [entry(self.rel, contents)]
        if other is not None:
            files.append(entry("Game/mods/extra.ff", other))
        return {"id": "testmod", "name": "Test Mod", "version": version, "mode": "options",
                "options": [{"id": "base", "name": "Base", "files": files}]}

    def install_bytes(self, manifest, payloads):
        def download(url, dest, **_kwargs):
            Path(dest).write_bytes(payloads.pop(0))
        with patch.object(mm, "_download", side_effect=download):
            return mm.install_manifest(manifest, "base", "cdn.example.com", self.root)

    def stage_dirs(self):
        folder = Path(self.root, mm.RECEIPT_DIR)
        return list(folder.glob(".staging-*")) if folder.exists() else []

    def test_corrupt_update_keeps_live_file_receipt_and_original_backup(self):
        original, working, updated = b"player data", b"working v1", b"working v2"
        self.dest.write_bytes(original)
        old, new = self.manifest("1", working), self.manifest("2", updated)
        self.assertEqual(self.install_bytes(old, [working]), [])
        backup = Path(mm._backup_path(self.root, "testmod", self.rel))
        receipt = Path(mm._receipt_path(self.root, "testmod"))
        before = receipt.read_bytes()

        # Same size, wrong checksum: even a completed download cannot replace v1.
        self.assertEqual(self.install_bytes(new, [b"bad bytes!"]), [self.rel])
        self.assertEqual(self.dest.read_bytes(), working)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(mm.get_receipt(self.root, "testmod")["files"][self.rel],
                         hashlib.sha256(working).hexdigest())
        self.assertTrue(mm.is_installed(self.root, "testmod"))
        self.assertTrue(mm.needs_update(new, self.root))
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(mm.uninstall_manifest("testmod", self.root), [])
        self.assertEqual(self.dest.read_bytes(), original)

    def test_partial_download_after_valid_staging_rolls_back_whole_update(self):
        working, updated, extra = b"working v1", b"working v2", b"extra bytes"
        old, new = self.manifest("1", working), self.manifest("2", updated, extra)
        self.assertEqual(self.install_bytes(old, [working]), [])
        before = Path(mm._receipt_path(self.root, "testmod")).read_bytes()
        def download(_url, dest, **_kwargs):
            if dest.endswith("0"):
                Path(dest).write_bytes(updated)
            else:
                Path(dest + ".part").write_bytes(b"partial")
                raise mm.DownloadError("https://cdn.example.com/extra", dest, "extra", OSError("offline"))
        with patch.object(mm, "_download", side_effect=download):
            self.assertEqual(mm.install_manifest(new, "base", "cdn.example.com", self.root),
                             ["Game/mods/extra.ff"])
        self.assertEqual(self.dest.read_bytes(), working)
        self.assertFalse(Path(self.root, "Game/mods/extra.ff").exists())
        self.assertEqual(Path(mm._receipt_path(self.root, "testmod")).read_bytes(), before)
        self.assertEqual(self.stage_dirs(), [])

    def test_failed_option_switch_keeps_old_option_and_receipt(self):
        working, replacement = b"working v1", b"new option"
        old = self.manifest("1", working)
        self.assertEqual(self.install_bytes(old, [working]), [])
        before = Path(mm._receipt_path(self.root, "testmod")).read_bytes()
        new = self.manifest("2", working, replacement)
        new["options"] = [{"id": "other", "name": "Other", "files": new["options"][0]["files"][1:]}]
        with patch.object(mm, "_download", side_effect=lambda _url, dest, **_kw: Path(dest).write_bytes(b"bad option")):
            self.assertEqual(mm.install_manifest(new, "other", "cdn.example.com", self.root),
                             ["Game/mods/extra.ff"])
        self.assertEqual(self.dest.read_bytes(), working)
        self.assertEqual(Path(mm._receipt_path(self.root, "testmod")).read_bytes(), before)
        self.assertFalse(Path(self.root, "Game/mods/extra.ff").exists())

    def test_successful_update_preserves_original_backup_for_uninstall(self):
        original, working, updated = b"player data", b"working v1", b"working v2"
        self.dest.write_bytes(original)
        self.assertEqual(self.install_bytes(self.manifest("1", working), [working]), [])
        self.assertEqual(self.install_bytes(self.manifest("2", updated), [updated]), [])
        self.assertEqual(self.dest.read_bytes(), updated)
        self.assertEqual(Path(mm._backup_path(self.root, "testmod", self.rel)).read_bytes(), original)
        receipt = mm.get_receipt(self.root, "testmod")
        self.assertEqual(receipt["files"][self.rel], hashlib.sha256(updated).hexdigest())
        self.assertEqual(receipt["overwritten"], [self.rel])
        self.assertEqual(mm.uninstall_manifest("testmod", self.root), [])
        self.assertEqual(self.dest.read_bytes(), original)

    def test_matching_user_file_is_backed_up_before_later_version_replaces_it(self):
        original, updated = b"matching v1", b"working v2"
        self.dest.write_bytes(original)
        self.assertEqual(self.install_bytes(self.manifest("1", original), []), [])
        backup = Path(mm._backup_path(self.root, "testmod", self.rel))
        self.assertFalse(backup.exists())
        self.assertEqual(mm.get_receipt(self.root, "testmod")["overwritten"], [self.rel])
        self.assertEqual(self.install_bytes(self.manifest("2", updated), [updated]), [])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.dest.read_bytes(), updated)
        self.assertEqual(mm.uninstall_manifest("testmod", self.root), [])
        self.assertEqual(self.dest.read_bytes(), original)

    def test_failed_first_install_does_not_claim_or_change_user_file(self):
        original = b"player data"
        self.dest.write_bytes(original)
        self.assertEqual(self.install_bytes(self.manifest("1", b"working v1"), [b"bad bytes!"]),
                         [self.rel])
        self.assertEqual(self.dest.read_bytes(), original)
        self.assertEqual(mm.get_receipt(self.root, "testmod"), {})
        self.assertFalse(Path(mm._backup_path(self.root, "testmod", self.rel)).exists())

    def test_commit_error_receipt_lists_only_files_on_disk(self):
        original, working, updated, extra = b"player data", b"working v1", b"working v2", b"extra bytes"
        self.dest.write_bytes(original)
        self.assertEqual(self.install_bytes(self.manifest("1", working), [working]), [])
        real_replace = os.replace
        def replace(src, dst):
            if dst.endswith("extra.ff"):
                raise OSError("disk write failed")
            return real_replace(src, dst)
        with patch.object(mm.os, "replace", side_effect=replace):
            self.assertEqual(self.install_bytes(self.manifest("2", updated, extra), [updated, extra]),
                             ["Game/mods/extra.ff"])
        receipt = mm.get_receipt(self.root, "testmod")
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["option_hash"], "")
        self.assertEqual(receipt["files"], {self.rel: hashlib.sha256(updated).hexdigest()})
        self.assertEqual(self.dest.read_bytes(), updated)
        self.assertFalse(Path(self.root, "Game/mods/extra.ff").exists())
        self.assertEqual(Path(mm._backup_path(self.root, "testmod", self.rel)).read_bytes(), original)
        self.assertEqual(mm.uninstall_manifest("testmod", self.root), [])
        self.assertEqual(self.dest.read_bytes(), original)

    def test_existing_backup_is_never_consumed_when_commit_fails(self):
        old_backup, foreign, working = b"original backup", b"current user file", b"working v1"
        self.dest.write_bytes(foreign)
        backup = Path(mm._backup_path(self.root, "testmod", self.rel))
        backup.parent.mkdir(parents=True)
        backup.write_bytes(old_backup)
        self.assertEqual(self.install_bytes(self.manifest("1", working), [working]), [self.rel])
        self.assertEqual(self.dest.read_bytes(), foreign)
        self.assertEqual(backup.read_bytes(), old_backup)
        receipt = mm.get_receipt(self.root, "testmod")
        self.assertEqual(receipt["files"], {})
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["failed"], [self.rel])
        self.assertEqual(self.stage_dirs(), [])


if __name__ == "__main__":
    unittest.main()
