"""Regression tests for the third sweep: plutonium, plutonium_lcd,
plutonium_update, depot_downgrade and ge_proton."""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import depot_downgrade as depot
import ge_proton
import plutonium
import plutonium_lcd
import plutonium_update


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _tree(root):
    out = []
    for dp, _, fns in os.walk(root):
        for f in fns:
            out.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
    return sorted(out)


class MergeTreeRollbackTests(unittest.TestCase):
    """A failure while moving the staged depot files next to the game must not
    strand them as .deckops-new files with the staging folder emptied."""

    def test_failed_staging_returns_files_and_leaves_no_pending(self):
        real_move = depot.shutil.move
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append(src)
            if len(calls) == 3:
                _write(dst, b"partial")          # a copy cut off by a full disk
                raise OSError(28, "No space left on device")
            return real_move(src, dst, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            stage, game = os.path.join(tmp, "stage"), os.path.join(tmp, "game")
            for n in ("a.bin", "b.bin", "c.bin", "d.bin"):
                _write(os.path.join(stage, n), n.encode())
            _write(os.path.join(game, "a.bin"), b"old-a")
            with patch.object(depot.shutil, "move", side_effect=flaky):
                with self.assertRaises(OSError):
                    depot._merge_tree(stage, game, lambda m: None)
            self.assertEqual(_tree(stage), ["a.bin", "b.bin", "c.bin", "d.bin"])
            self.assertEqual(_tree(game), ["a.bin"])
            with open(os.path.join(game, "a.bin"), "rb") as f:
                self.assertEqual(f.read(), b"old-a")
            with open(os.path.join(stage, "c.bin"), "rb") as f:
                self.assertEqual(f.read(), b"c.bin")


class EmptyMergeTests(unittest.TestCase):
    def test_staging_with_only_downloader_files_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, game = os.path.join(tmp, "stage"), os.path.join(tmp, "game")
            _write(os.path.join(stage, ".DepotDownloader", "state"), b"1")
            _write(os.path.join(stage, "x.manifest"), b"1")
            os.makedirs(game)
            logs = []
            self.assertFalse(depot.merge_depots("iw5", stage, game, on_progress=logs.append))
            self.assertTrue(os.path.isdir(stage))      # kept for inspection / retry
            self.assertTrue(any("no depot files" in m for m in logs))
            self.assertFalse(os.path.exists(depot._receipt_path("iw5", game)))

    def test_real_files_still_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, game = os.path.join(tmp, "stage"), os.path.join(tmp, "game")
            _write(os.path.join(stage, "main", "iw_00.iwd"), b"data")
            os.makedirs(game)
            self.assertTrue(depot.merge_depots("iw5", stage, game))
            self.assertTrue(os.path.isfile(os.path.join(game, "downgrade", "main", "iw_00.iwd")))


class ReceiptShapeTests(unittest.TestCase):
    def _receipt(self, game, text):
        path = depot._receipt_path("iw5", game)
        _write(path, text.encode())
        return path

    def test_non_object_receipt_reads_as_missing(self):
        for text in ("[]", "null", '"x"', '{"game_id": "iw5", "files": [1, 2]}'):
            with tempfile.TemporaryDirectory() as game:
                self._receipt(game, text)
                self.assertIsNone(depot._receipt_status("iw5", game), text)

    def test_write_receipt_replaces_a_malformed_one(self):
        for text in ("[]", "null", '{"game_id": "iw5", "files": [1]}'):
            with tempfile.TemporaryDirectory() as game:
                path = self._receipt(game, text)
                depot._write_receipt("iw5", game, {"main/a.iwd": 5})
                with open(path) as f:
                    data = json.load(f)
                self.assertEqual(data["files"], {"main/a.iwd": 5}, text)

    def test_legacy_migration_tolerates_a_non_object_receipt(self):
        with tempfile.TemporaryDirectory() as game:
            path = os.path.join(game, depot.RECEIPT_NAME)
            _write(path, b"[1]")
            self.assertFalse(depot.migrate_legacy_iw5(game))
            self.assertTrue(os.path.exists(path))      # not ours to delete


class LocalRevisionTests(unittest.TestCase):
    def test_non_object_info_json_has_no_revision(self):
        with tempfile.TemporaryDirectory() as d:
            for text in ("[]", "null", "7"):
                _write(os.path.join(d, "info.json"), text.encode())
                self.assertIsNone(plutonium_update.local_revision(d), text)
            _write(os.path.join(d, "info.json"), b'{"revision": 5}')
            self.assertEqual(plutonium_update.local_revision(d), 5)


class LcdConfigTests(unittest.TestCase):
    def test_config_cut_inside_a_multibyte_character_is_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            raw = '{"t4Path": "Z:\\\\games\\\\J\u00fcrgen"}'.encode("utf-8")[:-4] + b"\xc3"
            _write(os.path.join(d, "config.json"), raw)
            games = {"t4sp": {"install_dir": "/games/waw", "source": "own"}}
            plutonium_lcd._write_plutonium_config_lcd(d, ["t4sp"], games)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["t4Path"], "Z:\\games\\waw")


class HeroicFlatpakTests(unittest.TestCase):
    def test_install_waits_longer_than_five_minutes(self):
        seen = {}

        def run(cmd, **kw):
            seen[cmd[1]] = kw.get("timeout")
            return SimpleNamespace(returncode=0, stderr=b"")

        with patch.object(plutonium_lcd, "is_heroic_installed", return_value=False), \
                patch.object(plutonium_lcd, "_ensure_flathub_remote"), \
                patch.object(plutonium_lcd.subprocess, "run", side_effect=run):
            self.assertTrue(plutonium_lcd.install_heroic())
        self.assertGreaterEqual(seen["install"], 20 * 60)

    def test_refused_override_is_reported(self):
        msgs = []

        def run(cmd, **kw):
            return SimpleNamespace(returncode=1, stderr=b"bad path")

        with patch.object(plutonium_lcd.subprocess, "run", side_effect=run), \
                patch("steam_common.default_steam_root", return_value="/steam"):
            plutonium_lcd._grant_heroic_filesystem_access(["/mnt/odd"], on_progress=msgs.append)
        self.assertTrue(any("/mnt/odd" in m and "bad path" in m for m in msgs), msgs)


class StashFailureTests(unittest.TestCase):
    def test_partial_stash_is_put_back(self):
        real_move = plutonium.shutil.move
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append(src)
            if len(calls) == 2:
                raise OSError(5, "I/O error")
            return real_move(src, dst, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "Plutonium")
            _write(os.path.join(dest, "storage", "t6", "players", "stats.dat"), b"stats")
            _write(os.path.join(dest, "storage", "t6", "mods", "m", "mod.ff"), b"mod")
            with patch.object(plutonium.shutil, "move", side_effect=flaky):
                with self.assertRaises(OSError):
                    plutonium._copy_plut_to_prefix(os.path.join(tmp, "master"), dest, "t6mp")
            self.assertEqual(_tree(dest), ["storage/t6/mods/m/mod.ff", "storage/t6/players/stats.dat"])
            self.assertFalse(os.path.exists(dest + ".deckops_keep"))


class LanDownloadTests(unittest.TestCase):
    def test_zip_without_the_wanted_file_raises_a_readable_error(self):
        def fake_download(url, dest, **kw):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("other.iwd", b"x")
            _write(dest, buf.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "z_svr_bots.iwd")
            with patch.object(plutonium, "download", side_effect=fake_download):
                with self.assertRaises(RuntimeError) as cm:
                    plutonium._lan_download(dst, "https://example.invalid/b.zip", None, print)
            self.assertIn("z_svr_bots.iwd", str(cm.exception))
            self.assertFalse(os.path.exists(dst))
            self.assertFalse(os.path.exists(dst + ".part"))


class StaleFinalizingMarkerTests(unittest.TestCase):
    def _donor(self, tmp):
        donor = os.path.join(tmp, "donor")
        _write(os.path.join(donor, "pfx", "user.reg"), b"reg")
        _write(os.path.join(donor, "pfx", "drive_c", "windows", "x.dll"), b"dll")
        return os.path.join(donor, "pfx")

    def test_overlay_clears_a_stale_marker_when_the_prefix_is_now_real(self):
        with tempfile.TemporaryDirectory() as tmp:
            donor_pfx = self._donor(tmp)
            dest = os.path.join(tmp, "dest")
            _write(os.path.join(dest, "pfx", "drive_c", "windows", "system32", "a.dll"), b"a")
            _write(os.path.join(dest, ge_proton._FINALIZING_MARKER), b"")
            self.assertFalse(ge_proton._prefix_ready(os.path.join(dest, "pfx")))
            self.assertTrue(ge_proton._overlay_prefix(donor_pfx, dest, "GE-Proton10-1"))
            self.assertTrue(ge_proton._prefix_ready(os.path.join(dest, "pfx")))
            self.assertFalse(os.path.exists(os.path.join(dest, ge_proton._FINALIZING_MARKER)))

    def test_marker_stays_when_the_prefix_has_no_wine_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "dest")
            _write(os.path.join(dest, "pfx", "drive_c", "windows", "system32", "a.dll"), b"a")
            marker = os.path.join(dest, ge_proton._FINALIZING_MARKER)
            _write(marker, b"")
            ge_proton._finish_clone(os.path.join(tmp, "nodonor", "pfx"), dest, "GE-Proton10-1")
            self.assertTrue(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
