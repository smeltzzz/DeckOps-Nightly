"""Focused installer/download regressions: remote paths, resume and rollback."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import iw3sp
import iw4x
import net
import t6sp_mod


class PathSafetyTests(unittest.TestCase):
    def test_remote_paths_rejected_on_both_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in ("../escape", "nested/../escape", "/absolute", "C:/absolute",
                        "C:\\absolute", "nested\\escape", "nested//escape", "./escape"):
                with self.subTest(rel=rel), self.assertRaises(ValueError):
                    net.safe_manifest_path(tmp, rel)
                with self.subTest(dlc=rel), self.assertRaises(ValueError):
                    iw4x._remap_dlc_path(rel, tmp)
            # Windows test runners may not have symlink privileges. Simulate
            # an existing symlink parent without relying on that privilege.
            real_islink = os.path.islink
            with patch.object(net.os.path, "islink", side_effect=lambda p:
                              os.path.normpath(p) == os.path.normpath(Path(tmp) / "zone") or real_islink(p)):
                with self.assertRaises(ValueError):
                    iw4x._remap_dlc_path("iw3/zone/dlc/map.ff", tmp)
                with self.assertRaises(ValueError):
                    net.safe_manifest_path(tmp, "zone/map.ff")

    def test_iw3_manifest_rejected_before_hash_or_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_islink = os.path.islink
            with patch.object(net.os.path, "islink", side_effect=lambda p:
                              os.path.normpath(p) == os.path.normpath(root / "safe") or real_islink(p)):
                for bad in ("../outside", "/outside", "C:/outside", "safe/escaped"):
                    with self.subTest(bad=bad), patch.object(iw3sp, "_get_manifest", return_value=[
                        ("iw3sp_mod.exe", 3, "a" * 40), (bad, 3, "b" * 40)]), \
                         patch.object(iw3sp, "_download") as download:
                        with self.assertRaises(ValueError):
                            iw3sp.install_iw3sp({"install_dir": tmp}, "", "", "", source="own")
                        download.assert_not_called()
                        self.assertFalse((root / "iw3sp_mod.exe").exists())

    def test_dlc_manifest_rejected_before_creating_remapped_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "update.json").write_text("player's existing file")
            manifest = {"files": [{"path": "iw3/zone/dlc/map.ff"},
                                  {"path": "../escape.ff"}]}
            def get_manifest(url, dest, *args, **kwargs):
                Path(dest).write_text(json.dumps(manifest))
            with patch.object(iw4x, "_download", side_effect=get_manifest):
                with self.assertRaises(ValueError):
                    iw4x.install_iw4x_dlc(tmp)
            self.assertFalse((Path(tmp) / "zone").exists())
            self.assertEqual((Path(tmp) / "update.json").read_text(), "player's existing file")


class ResumeTests(unittest.TestCase):
    class Response:
        status = 206
        def __init__(self, content_range, body=b"def", length="3"):
            self.headers = {"Content-Range": content_range, "Content-Length": length}
            self.body = io.BytesIO(body)
        def read(self, n=-1):
            return self.body.read(n)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def _part(self, tmp):
        dest = str(Path(tmp) / "client")
        Path(dest + ".part").write_bytes(b"abc")
        Path(dest + ".part.meta").write_text(json.dumps({"if_range": '"v1"'}))
        return dest

    def test_invalid_range_never_splices_or_replaces_installed_file(self):
        for header in (None, "bytes 0-2/6", "bytes 4-5/6", "bytes 3-5/7",
                       "bytes 3-6/6", "bytes 3-5/*", "bytes 3-5/6"):
            # The final range has an inconsistent Content-Length.
            with self.subTest(header=header), tempfile.TemporaryDirectory() as tmp:
                dest = self._part(tmp)
                Path(dest).write_bytes(b"old")
                length = "4" if header == "bytes 3-5/6" else "3"
                with patch.object(net.urllib.request, "urlopen",
                                  return_value=self.Response(header, length=length)):
                    with self.assertRaisesRegex(IOError, "Content-Range"):
                        net.download("https://example.invalid/client", dest, tries=1)
                self.assertEqual(Path(dest).read_bytes(), b"old")
                self.assertFalse(Path(dest + ".part").exists())

    def test_invalid_range_retries_from_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._part(tmp)
            fresh = self.Response(None, body=b"fresh!", length="6")
            fresh.status = 200
            with patch.object(net.time, "sleep"), \
                 patch.object(net.urllib.request, "urlopen", side_effect=[
                     self.Response("bytes 0-2/6"), fresh]) as urlopen:
                net.download("https://example.invalid/client", dest, tries=2)
            self.assertEqual(Path(dest).read_bytes(), b"fresh!")
            self.assertIsNone(urlopen.call_args.args[0].get_header("Range"))

    def test_valid_resume_reconstructs_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._part(tmp)
            with patch.object(net.urllib.request, "urlopen",
                              return_value=self.Response("bytes 3-5/6")) as urlopen:
                net.download("https://example.invalid/client", dest, tries=1)
            self.assertEqual(Path(dest).read_bytes(), b"abcdef")
            self.assertEqual(urlopen.call_args.args[0].get_header("Range"), "bytes=3-")


class StagedInstallTests(unittest.TestCase):
    @staticmethod
    def _zip(files):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        return out.getvalue()

    def _t6(self, root, failure=None):
        def download(url, dest, *args, **kwargs):
            if failure == "download" and dest.endswith(".dll"):
                raise IOError("dll failed")
            Path(dest).write_bytes(b"new exe" if dest.endswith(".exe") else b"new dll")
        with patch.object(t6sp_mod, "github_asset", return_value=("https://example.invalid/asset", None)), \
             patch.object(t6sp_mod, "_download", side_effect=download):
            t6sp_mod.install_t6sp_mod({"install_dir": str(root)}, "", "", "", source="own")

    def test_t6_failed_second_download_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / t6sp_mod.MOD_EXE).write_bytes(b"old exe")
            (root / "t6sp-mod.dll").write_bytes(b"old dll")
            with self.assertRaisesRegex(IOError, "dll failed"):
                self._t6(root, "download")
            self.assertEqual((root / t6sp_mod.MOD_EXE).read_bytes(), b"old exe")
            self.assertEqual((root / "t6sp-mod.dll").read_bytes(), b"old dll")
            self.assertEqual(sorted(p.name for p in root.iterdir()),
                             ["T6SP-Mod.exe", "t6sp-mod.dll"])

    def test_t6_failed_commit_restores_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / t6sp_mod.MOD_EXE).write_bytes(b"old exe")
            (root / "t6sp-mod.dll").write_bytes(b"old dll")
            original = os.replace
            def fail(source, dest):
                if str(source).endswith("t6sp-mod.dll") and str(dest) == str(root / "t6sp-mod.dll"):
                    raise OSError("commit failed")
                return original(source, dest)
            with patch.object(net.os, "replace", side_effect=fail):
                with self.assertRaisesRegex(OSError, "commit failed"):
                    self._t6(root)
            self.assertEqual((root / t6sp_mod.MOD_EXE).read_bytes(), b"old exe")
            self.assertEqual((root / "t6sp-mod.dll").read_bytes(), b"old dll")

    def _iw4x(self, root, release=None, launcher=None, failure=None):
        release = release if release is not None else self._zip({
            "iw4x/old.dat": b"new asset", "iw4x/new.iwd": b"new iwd",
            "zone/patch/map.ff": b"new map"})
        launcher = launcher if launcher is not None else self._zip({"iw4x-launcher.exe": b"new launcher"})
        def download(url, dest, *args, **kwargs):
            if failure == "download" and dest.endswith("release.zip"):
                raise IOError("zip failed")
            Path(dest).write_bytes(release if dest.endswith("release.zip") else
                                   launcher if dest.endswith("launcher.zip") else b"new dll")
        with patch.object(iw4x, "github_asset", return_value=("https://example.invalid/asset", None)), \
             patch.object(iw4x, "_download", side_effect=download):
            iw4x.install_iw4x({"install_dir": str(root)}, "", "", "", source="own")

    def _old_iw4x(self, root):
        (root / "main/iw4x/x86").mkdir(parents=True)
        (root / "main/iw4x/x86/old.dat").write_bytes(b"old asset")
        (root / "main/iw4x/x86/keep.iwd").write_bytes(b"old dlc")
        (root / "iw4x.dll").write_bytes(b"old dll")
        (root / "iw4x-launcher.exe").write_bytes(b"old launcher")

    def _assert_old(self, root):
        self.assertEqual((root / "main/iw4x/x86/old.dat").read_bytes(), b"old asset")
        self.assertEqual((root / "main/iw4x/x86/keep.iwd").read_bytes(), b"old dlc")
        self.assertEqual((root / "iw4x.dll").read_bytes(), b"old dll")
        self.assertEqual((root / "iw4x-launcher.exe").read_bytes(), b"old launcher")
        self.assertFalse((root / "main/iw4x/x86/new.iwd").exists())

    def test_iw4x_failed_download_and_unsafe_zip_preserve_install(self):
        for failure, archive in (("download", None),
                                 (None, self._zip({"../escape": b"bad"})),
                                 (None, self._zip({"main/iw4x/x86/old.dat": b"okay", "C:/escape": b"bad"}))):
            with self.subTest(failure=failure, archive=archive), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._old_iw4x(root)
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    self._iw4x(root, release=archive, failure=failure)
                self._assert_old(root)
                self.assertFalse(any(p.name.startswith(".deckops_iw4x_") for p in root.iterdir()))

    def test_iw4x_failed_commit_rolls_back_removed_and_replaced_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._old_iw4x(root)
            original = os.replace
            def fail(source, dest):
                if str(dest) == str(root / "iw4x.dll"):
                    raise OSError("commit failed")
                return original(source, dest)
            with patch.object(net.os, "replace", side_effect=fail):
                with self.assertRaisesRegex(OSError, "commit failed"):
                    self._iw4x(root)
            self._assert_old(root)
            self.assertFalse((root / "zone/iw4x/x86/patch/map.ff").exists())

    def test_iw4x_success_preserves_dlc_and_relocates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._old_iw4x(root)
            self._iw4x(root)
            self.assertEqual((root / "main/iw4x/x86/keep.iwd").read_bytes(), b"old dlc")
            self.assertEqual((root / "main/iw4x/x86/old.dat").read_bytes(), b"new asset")
            self.assertEqual((root / "zone/iw4x/x86/patch/map.ff").read_bytes(), b"new map")
            self.assertEqual((root / "iw4x.dll").read_bytes(), b"new dll")
            self.assertEqual((root / "iw4x-launcher.exe").read_bytes(), b"new launcher")


if __name__ == "__main__":
    unittest.main()
