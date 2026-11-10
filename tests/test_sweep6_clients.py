"""Sweep 6 regressions: DepotDownloader QR success, net rollback folder leaks,
GE-Proton progress band and the IW3SP-MOD manifest check.

Everything runs against temporary folders and fake processes, never real
installs or Steam roots.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import depot_downgrade as depot  # noqa: E402
import ge_proton  # noqa: E402
import iw3sp  # noqa: E402
import net  # noqa: E402


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


# ── CLIENT-1: QR download that never prints the "Success! -username" line ──

class _FakeProc:
    """Popen stand-in: prints the given lines, then exits with returncode."""
    instances = []

    def __init__(self, lines, returncode):
        self._lines = [line + "\n" for line in lines]
        self._rc = returncode
        self.returncode = None
        self.pid = 4242
        self.stdout = iter(self._lines)

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class DepotQrSuccessTests(_TmpCase):
    QR = ["Use the Steam Mobile App to sign in with this QR code:",
          "█▀▀▀▀▀█ ▄▄ █▀▀▀▀▀█", "█ ███ █ ▀▄ █ ███ █", "Done!"]

    def _run(self, runs, **kw):
        """runs: a list of (lines, returncode), one per DepotDownloader start."""
        self.cmds = []
        self.logs = []
        self.progress = []
        queue = list(runs)

        def popen(cmd, **_kw):
            self.cmds.append(cmd)
            lines, rc = queue.pop(0)
            return _FakeProc(lines, rc)

        with patch.object(depot.subprocess, "Popen", side_effect=popen), \
             patch.object(depot, "DEPOTDOWNLOADER_DIR", str(self.root)):
            return depot.run_depot_download_qr(
                staging_dir=str(self.root / "staging"),
                depot_info={"depot": 42681, "manifest": "1", "app": 42680},
                on_qr=lambda text: None,
                on_auth_success=lambda user: None,
                on_progress=self.progress.append,
                on_log=self.logs.append,
                **kw)

    def test_clean_exit_after_download_without_username_line_is_success(self):
        lines = self.QR + ["Got depot key for 42681 result: OK",
                           " 50.00% depot/file.ff", "Total downloaded: 100 bytes"]
        result = self._run([(lines, 0)])
        # Not None: the caller (ui_install) treats only None as a failure
        self.assertIsNotNone(result)
        self.assertEqual(result, "")
        self.assertEqual(len(self.cmds), 1, "the whole download was run again")
        self.assertTrue(any("download complete" in m for m in self.logs), self.logs)
        self.assertFalse(any("expired" in m.lower() for m in self.logs), self.logs)

    def test_empty_string_result_makes_the_next_depot_log_in_by_qr(self):
        # ui_install passes the result back as username=; "" must not become
        # `-username ""` (a password prompt that fails), it must mean QR again
        lines = self.QR + ["Got depot key for 42681 result: OK"]
        self._run([(lines, 0)], username="")
        self.assertIn("-qr", self.cmds[0])
        self.assertNotIn("-username", self.cmds[0])

    def test_username_line_is_still_returned(self):
        lines = self.QR + ["Success! Next time use: -username alice -remember-password",
                           "Got depot key for 42681 result: OK"]
        self.assertEqual(self._run([(lines, 0)]), "alice")

    def test_failure_after_download_started_is_not_retried_as_expired_qr(self):
        lines = self.QR + ["Got depot key for 42681 result: OK", " 10.00% depot/file.ff"]
        self.assertIsNone(self._run([(lines, 1)]))
        self.assertEqual(len(self.cmds), 1)

    def test_nonzero_exit_without_progress_still_retries_and_is_capped(self):
        runs = [(self.QR, 1)] * 3
        self.assertIsNone(self._run(runs, max_retries=3))
        self.assertEqual(len(self.cmds), 3)
        self.assertTrue(any("Max QR login retries reached" in m for m in self.logs), self.logs)

    def test_explicit_expired_text_retries_even_after_exit_zero(self):
        expired = self.QR + ["QR code expired, please try again"]
        good = self.QR + ["Got depot key for 42681 result: OK"]
        self.assertEqual(self._run([(expired, 0), (good, 0)]), "")
        self.assertEqual(len(self.cmds), 2)

    def test_keyword_error_on_clean_exit_is_a_failure(self):
        lines = self.QR + ["Got depot key for 42681 result: OK", "Access denied to depot"]
        self.assertIsNone(self._run([(lines, 0)]))
        self.assertEqual(len(self.cmds), 1)


# ── Skip MW3 on the QR screen: cancel= stops a silent DepotDownloader ──

class _SilentProc:
    """DepotDownloader waiting for a QR scan: prints the code, then nothing until killed."""

    def __init__(self, lines):
        import threading
        self._killed = threading.Event()
        self._lines = [line + "\n" for line in lines]
        self.returncode = None
        self.pid = 4343
        self.stdout = self._read()

    def _read(self):
        yield from self._lines
        self._killed.wait(30)          # silent, like a pending QR login

    def wait(self, timeout=None):
        self._killed.wait(timeout if timeout is not None else 30)
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._killed.set()


class DepotQrCancelTests(_TmpCase):
    def _run(self, proc, cancel):
        logs = []
        with patch.object(depot.subprocess, "Popen", return_value=proc), \
             patch.object(depot, "DEPOTDOWNLOADER_DIR", str(self.root)):
            return depot.run_depot_download_qr(
                staging_dir=str(self.root / "staging"),
                depot_info={"depot": 42681, "manifest": "1", "app": 42680},
                on_qr=lambda text: None, on_auth_success=lambda user: None,
                on_progress=lambda m: None, on_log=logs.append, cancel=cancel)

    def test_cancel_kills_a_silent_depotdownloader_promptly(self):
        proc = _SilentProc(DepotQrSuccessTests.QR)
        started = time.monotonic()
        flag = {"skip": False}

        def cancel():
            # The user presses Skip a moment after the QR code is shown
            if time.monotonic() - started > 0.3:
                flag["skip"] = True
            return flag["skip"]

        with self.assertRaises(depot.DownloadCancelled):
            self._run(proc, cancel)
        self.assertEqual(proc.returncode, -9, "DepotDownloader was not killed")
        self.assertLess(time.monotonic() - started, 5, "cancel waited for DepotDownloader output")

    def test_cancel_before_start_never_runs_depotdownloader(self):
        with patch.object(depot.subprocess, "Popen") as popen, \
             patch.object(depot, "DEPOTDOWNLOADER_DIR", str(self.root)):
            with self.assertRaises(depot.DownloadCancelled):
                depot.run_depot_download_qr(
                    staging_dir=str(self.root / "staging"),
                    depot_info={"depot": 42681, "manifest": "1", "app": 42680},
                    on_qr=lambda t: None, on_auth_success=lambda u: None,
                    on_progress=lambda m: None, on_log=lambda m: None,
                    cancel=lambda: True)
        popen.assert_not_called()

    def test_a_cancel_check_that_raises_is_ignored(self):
        lines = DepotQrSuccessTests.QR + ["Got depot key for 42681 result: OK"]

        def cancel():
            raise RuntimeError("broken check")

        self.assertEqual(self._run(_FakeProc(lines, 0), cancel), "")


# ── CLIENT-2: rollback folders left by a commit that was killed ──

class RollbackSweepTests(_TmpCase):
    def setUp(self):
        super().setUp()
        self.install = self.root / "game"
        self.staged = self.root / "staged"
        self.install.mkdir()
        self.staged.mkdir()
        for name in ("a.dll", "b.dll"):
            (self.install / name).write_bytes(b"old " + name.encode())
            (self.staged / name).write_bytes(b"new " + name.encode())

    def _stale(self, name, age=net.ROLLBACK_STALE_AGE + 60, keep=False):
        d = self.install / (net.ROLLBACK_PREFIX + name)
        d.mkdir()
        (d / "a.dll").write_bytes(b"backup")
        if keep:
            (d / net.ROLLBACK_KEEP_MARKER).write_text("kept")
        old = time.time() - age
        os.utime(d, (old, old))
        return d

    def rollbacks(self):
        return sorted(p.name for p in self.install.iterdir() if p.name.startswith(net.ROLLBACK_PREFIX))

    def test_stale_rollback_folder_is_removed_on_the_next_commit(self):
        stale = self._stale("dead")
        net.commit_staged_files(str(self.install), str(self.staged), ["a.dll", "b.dll"])
        self.assertFalse(stale.exists())
        self.assertEqual(self.rollbacks(), [])
        self.assertEqual((self.install / "a.dll").read_bytes(), b"new a.dll")

    def test_kept_rollback_folder_survives_the_sweep(self):
        kept = self._stale("kept", keep=True)
        net.commit_staged_files(str(self.install), str(self.staged), ["a.dll", "b.dll"])
        self.assertTrue((kept / "a.dll").exists())

    def test_recent_rollback_folder_of_a_running_commit_is_left(self):
        recent = self._stale("busy", age=5)
        net.commit_staged_files(str(self.install), str(self.staged), ["a.dll", "b.dll"])
        self.assertTrue(recent.exists())

    def test_incomplete_rollback_marks_its_folder_as_kept(self):
        real_replace = os.replace
        calls = []

        def replace(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_replace(src, dst)

        real_copy = net.shutil.copy2

        def copy2(src, dst, **kw):
            if calls:           # only the restore after the failed swap
                raise OSError("read-only")
            return real_copy(src, dst, **kw)

        with patch.object(net.os, "replace", side_effect=replace), \
             patch.object(net.shutil, "copy2", side_effect=copy2):
            with self.assertRaisesRegex(RuntimeError, "Rollback incomplete"):
                net.commit_staged_files(str(self.install), str(self.staged), ["a.dll", "b.dll"])
        kept = self.rollbacks()
        self.assertEqual(len(kept), 1)
        self.assertTrue((self.install / kept[0] / net.ROLLBACK_KEEP_MARKER).exists())
        # Even once it is old, the next commit leaves the originals alone
        old = time.time() - net.ROLLBACK_STALE_AGE - 60
        os.utime(self.install / kept[0], (old, old))
        net._sweep_stale_rollbacks(str(self.install))
        self.assertEqual(self.rollbacks(), kept)


# ── CLIENT-3: GE-Proton download progress stays inside its band ──

class GeProtonProgressTests(_TmpCase):
    def test_download_progress_is_scaled_between_10_and_87(self):
        seen = []

        def fake_download(url, dest, on_progress=None, **kw):
            for p in (0, 25, 50, 100):
                on_progress(p, f"{p}%")
            Path(dest).write_bytes(b"tar")

        with patch.object(ge_proton, "_compat_dir", return_value=str(self.root / "compat")), \
             patch.object(ge_proton, "_get_local_version", return_value=None), \
             patch.object(ge_proton, "_get_latest_release",
                          return_value=("10-34", "https://x/t.tar.gz", None, "GE-Proton10-34")), \
             patch.object(ge_proton, "_is_installed", return_value=False), \
             patch.object(ge_proton, "download", side_effect=fake_download), \
             patch.object(ge_proton, "_extract_atomically"):
            ge_proton.install_ge_proton(on_progress=lambda p, m: seen.append(p))

        self.assertEqual(seen, sorted(seen), f"progress went backwards: {seen}")
        download_values = seen[seen.index(10) + 1:seen.index(87)]
        self.assertEqual(len(download_values), 4)
        self.assertTrue(all(10 <= p < 87 for p in download_values), download_values)


# ── CLIENT-4: an empty or incomplete IW3SP-MOD manifest is not a success ──

class Iw3spManifestTests(_TmpCase):
    def _install(self, manifest):
        game = self.root / "cod4"
        game.mkdir(exist_ok=True)
        with patch.object(iw3sp, "_get_manifest", return_value=manifest), \
             patch.object(iw3sp, "_download") as download:
            with self.assertRaises(RuntimeError) as ctx:
                iw3sp.install_iw3sp({"install_dir": str(game)}, "", "", "", source="own")
        download.assert_not_called()
        self.assertFalse((game / iw3sp.METADATA_FILE).exists())
        return str(ctx.exception)

    def test_empty_manifest_raises_without_metadata(self):
        self.assertIn("empty", self._install([]))

    def test_non_list_manifest_raises(self):
        self.assertIn("empty", self._install({}))

    def test_manifest_without_mod_exe_raises(self):
        self.assertIn("iw3sp_mod.exe", self._install([["iw3sp_mod.dll", 3, "a" * 40]]))

    def test_mod_exe_missing_after_install_raises(self):
        game = self.root / "cod4"
        game.mkdir()
        manifest = [["iw3sp_mod.exe", 3, "a" * 40]]
        # The download "succeeds" but the swap never lands the exe
        with patch.object(iw3sp, "_get_manifest", return_value=manifest), \
             patch.object(iw3sp, "_download", side_effect=lambda u, d, *a, **k: Path(d).write_bytes(b"x")), \
             patch.object(iw3sp, "commit_staged_files"):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                iw3sp.install_iw3sp({"install_dir": str(game)}, "", "", "", source="own")
        self.assertFalse((game / iw3sp.METADATA_FILE).exists())


if __name__ == "__main__":
    unittest.main()
