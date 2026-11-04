"""Regression tests for the fifth sweep: prefix overlay, SD card prefix cleanup,
GE-Proton extraction, launch options the client installers set, the FPS/FOV
and deadzone patchers and the host-menu diagnostic report."""

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import game_config as gc
import ge_proton
import iw3sp
import iw4x
import sp_mod
import t6sp_mod


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _builtin(tag):
    """Bytes _is_wine_builtin recognises as a Wine builtin DLL."""
    return b"\0" * 0x40 + b"Wine builtin DLL\0\0\0\0" + tag


class OverlayPrefixTests(unittest.TestCase):
    """_overlay_prefix on an existing prefix only adds what is missing and
    refreshes Wine builtins: it used to replace every file whose size differed
    from the donor's, the prefix's registry and saves included."""

    def _layout(self, tmp):
        donor = os.path.join(tmp, "donor", "pfx")
        dest = os.path.join(tmp, "dest")
        pfx = os.path.join(dest, "pfx")
        sys32 = os.path.join("drive_c", "windows", "system32")
        plut = os.path.join("drive_c", "users", "steamuser", "AppData", "Local", "Plutonium", "mod.cfg")
        for name in ("user.reg", "system.reg", "userdef.reg"):
            _write(os.path.join(donor, name), b"donor registry, much longer than the target's")
            _write(os.path.join(pfx, name), b"mine")
        _write(os.path.join(donor, plut), b"donor default config that is longer")
        _write(os.path.join(pfx, plut), b"my mod")
        _write(os.path.join(donor, sys32, "kernel32.dll"), _builtin(b"new GE-Proton build, longer"))
        _write(os.path.join(pfx, sys32, "kernel32.dll"), _builtin(b"old"))
        _write(os.path.join(donor, sys32, "d3dx9_43.dll"), b"donor native redist dll")
        _write(os.path.join(pfx, sys32, "d3dx9_43.dll"), b"game's own dll")
        _write(os.path.join(donor, sys32, "xinput1_3.dll"), b"missing in target")
        _write(os.path.join(donor, "drive_c", "users", "steamuser", "new.txt"), b"new")
        _write(os.path.join(os.path.dirname(donor), "tracked_files"), b"tracked")
        return donor, dest, pfx, sys32, plut

    def test_existing_registry_saves_and_native_dlls_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            donor, dest, pfx, sys32, plut = self._layout(tmp)
            self.assertTrue(ge_proton._overlay_prefix(donor, dest, "GE-Proton10-9"))
            for name in ("user.reg", "system.reg", "userdef.reg"):
                self.assertEqual(_read(os.path.join(pfx, name)), b"mine", name)
            self.assertEqual(_read(os.path.join(pfx, plut)), b"my mod")
            self.assertEqual(_read(os.path.join(pfx, sys32, "d3dx9_43.dll")), b"game's own dll")

    def test_missing_files_are_added_and_wine_builtins_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            donor, dest, pfx, sys32, _plut = self._layout(tmp)
            self.assertTrue(ge_proton._overlay_prefix(donor, dest, "GE-Proton10-9"))
            self.assertEqual(_read(os.path.join(pfx, sys32, "kernel32.dll")),
                             _builtin(b"new GE-Proton build, longer"))
            self.assertEqual(_read(os.path.join(pfx, sys32, "xinput1_3.dll")), b"missing in target")
            self.assertEqual(_read(os.path.join(pfx, "drive_c", "users", "steamuser", "new.txt")), b"new")
            self.assertEqual(_read(os.path.join(dest, "version")).strip(), b"GE-Proton10-9")
            self.assertEqual(_read(os.path.join(dest, "tracked_files")), b"tracked")

    def test_a_builtin_outside_drive_c_windows_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            donor = os.path.join(tmp, "donor", "pfx")
            dest = os.path.join(tmp, "dest")
            rel = os.path.join("drive_c", "users", "steamuser", "x.dll")
            _write(os.path.join(donor, rel), _builtin(b"donor, longer"))
            _write(os.path.join(dest, "pfx", rel), _builtin(b"mine"))
            self.assertTrue(ge_proton._overlay_prefix(donor, dest, None))
            self.assertEqual(_read(os.path.join(dest, "pfx", rel)), _builtin(b"mine"))


class SdCardPrefixCleanupTests(unittest.TestCase):
    """Prefixes on every non-internal library are set aside, not only
    /run/media/deck/* and mmcblk0p1."""

    def test_every_external_library_is_checked_and_the_internal_one_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            sr = os.path.join(tmp, "Steam")
            internal = os.path.join(sr, "steamapps", "compatdata")
            ext = os.path.join(tmp, "ext")
            card = os.path.join(tmp, "media", "bazzite", "card1")
            for root in (internal, os.path.join(ext, "steamapps", "compatdata"),
                         os.path.join(card, "steamapps", "compatdata")):
                _write(os.path.join(root, "7940", "pfx", "user.reg"), b"reg")
            _write(os.path.join(sr, "steamapps", "libraryfolders.vdf"),
                   ('"libraryfolders"\n{\n "0"\n {\n  "path" "%s"\n }\n "1"\n {\n  "path" "%s"\n }\n}\n'
                    % (sr.replace("\\", "/"), ext.replace("\\", "/"))).encode())
            with patch("detect_games.SD_CARD_PATTERNS", [os.path.join(tmp, "media", "*", "*")]), \
                 patch.object(ge_proton, "_nvme_compatdata", side_effect=lambda a: os.path.join(internal, str(a))):
                moved = ge_proton._cleanup_sd_card_prefixes(["7940"], steam_root=sr)
            self.assertTrue(os.path.isdir(os.path.join(internal, "7940")))
            self.assertEqual(sorted(os.path.normcase(os.path.normpath(p)) for p in moved), sorted(
                os.path.normcase(os.path.normpath(os.path.join(r, "steamapps", "compatdata", "7940")))
                + ge_proton.SD_PREFIX_ASIDE_SUFFIX for r in (ext, card)))
            self.assertFalse(os.path.exists(os.path.join(ext, "steamapps", "compatdata", "7940")))


class TarTimeoutTests(unittest.TestCase):
    def test_a_hung_tar_falls_back_to_tarfile_after_its_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            compat = os.path.join(tmp, "compatibilitytools.d")
            os.makedirs(compat)
            archive = os.path.join(tmp, "ge.tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                for name in ("GE-Proton10-9/proton", "GE-Proton10-9/compatibilitytool.vdf",
                             "GE-Proton10-9/files/share/default_pfx/user.reg"):
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
            calls = []

            def hung_tar(cmd, **kwargs):
                calls.append(kwargs)
                _write(os.path.join(cmd[-1], "partial"), b"half")   # tar's partial output
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

            with patch.object(ge_proton, "_compat_dir", return_value=compat), \
                 patch("subprocess.run", side_effect=hung_tar):
                ge_proton._extract_atomically(archive, "GE-Proton10-9")
            self.assertEqual(calls[0]["timeout"], ge_proton.TAR_TIMEOUT)
            self.assertTrue(ge_proton._is_complete(os.path.join(compat, "GE-Proton10-9")))
            self.assertEqual(os.listdir(compat), ["GE-Proton10-9"])


class LaunchOptionResultTests(unittest.TestCase):
    """wrapper.set_launch_options returns how many accounts it wrote: 0 must
    not be reported as a launch option set."""

    def test_cod4_option_is_only_reported_when_written(self):
        setup = {"cod4sp": {"client": "iw3sp", "source": "steam"}}
        with patch("config.get_setup_games", return_value=setup):
            with patch("wrapper.set_launch_options", return_value=0):
                self.assertFalse(iw3sp.apply_cod4_launch_option("steam"))
            with patch("wrapper.set_launch_options", return_value=1):
                self.assertTrue(iw3sp.apply_cod4_launch_option("steam"))

    def _t6(self, root, written):
        def download(url, dest, *args, **kwargs):
            Path(dest).write_bytes(b"new")
        with patch.object(t6sp_mod, "github_asset", return_value=("https://example.invalid/asset", None)), \
             patch.object(t6sp_mod, "_download", side_effect=download), \
             patch("wrapper.set_launch_options", return_value=written):
            t6sp_mod.install_t6sp_mod({"install_dir": str(root)}, "steam", "", "", source="steam")

    def test_t6sp_fails_when_no_account_got_the_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "launch option not set"):
                self._t6(Path(tmp), 0)
            self.assertFalse((Path(tmp) / t6sp_mod.METADATA_FILE).exists())
        with tempfile.TemporaryDirectory() as tmp:
            self._t6(Path(tmp), 1)
            self.assertTrue((Path(tmp) / t6sp_mod.METADATA_FILE).exists())

    def test_iw_pad_steam_install_fails_when_no_account_got_the_option(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sp_mod, "_fetch_iw_pad"), \
             patch("config.get_dxvk_frame_cap", return_value=True), \
             patch("wrapper.clear_launch_options"):
            with patch("wrapper.set_launch_options", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "launch option not set"):
                    sp_mod.install_sp_mod("iw4sp", tmp, source="steam", steam_root="steam")
            with patch("wrapper.set_launch_options", return_value=1):
                sp_mod.install_sp_mod("iw4sp", tmp, source="steam", steam_root="steam")

    def test_repair_only_lists_games_whose_option_was_written(self):
        setup = {"iw4sp": {"client": "iw-pad", "source": "steam"},
                 "iw5sp": {"client": "iw-pad", "source": "steam"}}
        logged = []
        with patch("config.get_setup_games", return_value=setup), \
             patch("config.get_dxvk_frame_cap", return_value=True), \
             patch("wrapper.snapshot_launch_options", return_value={}), \
             patch("wrapper.clear_launch_options"), \
             patch("wrapper.set_launch_options",
                   side_effect=lambda sr, appid, opt: 1 if appid == 42680 else 0):
            fixed = sp_mod.repair_sp_launch_options("steam", logged.append)
        self.assertNotIn("iw4sp", fixed)
        self.assertEqual(fixed, ["iw5sp"])
        self.assertTrue(any("iw4sp" in m and "launch option not set" in m for m in logged))

    def test_iw4x_warns_when_no_account_got_the_option(self):
        def zip_bytes(files):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as zf:
                for name, data in files.items():
                    zf.writestr(name, data)
            return out.getvalue()
        release = zip_bytes({"iw4x/new.iwd": b"iwd"})
        launcher = zip_bytes({"iw4x-launcher.exe": b"launcher"})

        def download(url, dest, *args, **kwargs):
            Path(dest).write_bytes(release if dest.endswith("release.zip") else
                                   launcher if dest.endswith("launcher.zip") else b"dll")
        messages = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(iw4x, "github_asset", return_value=("https://example.invalid/asset", None)), \
             patch.object(iw4x, "_download", side_effect=download), \
             patch("wrapper.clear_launch_options"), \
             patch("wrapper.set_launch_options", return_value=0), \
             patch("steam_appinfo.add_launch_entries", return_value=True):
            iw4x.install_iw4x({"install_dir": tmp}, "steam", "", "",
                              on_progress=lambda p, m: messages.append(m), source="steam")
        self.assertTrue(any("launch option not set" in m for m in messages))


class FpsFovAndDeadzoneTests(unittest.TestCase):
    def _run(self, func, key, tmp, **kwargs):
        dest = os.path.join(tmp, "pfx", "drive_c", "players")
        asset = "MW3/config_mp.cfg"
        with patch("config.get_setup_games", return_value={key: {}}), \
             patch("config.get_player_name", return_value="Me"), \
             patch("config.is_lcd", return_value=False), \
             patch.object(gc, "_build_config_map", return_value={key: [(asset, dest)]}), \
             patch.object(gc, "_offline_mirror_paths", return_value=[]):
            return func(tmp, {key: {"install_dir": tmp}}, **kwargs), os.path.join(dest, "config_mp.cfg")

    def test_mw3_dedicated_server_gets_fps_and_fov(self):
        self.assertIn("iw5mp_ds", gc.FPS_FOV_KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pfx", "drive_c", "players", "config_mp.cfg")
            _write(path, b'seta cg_fov "65"\nseta com_maxfps "85"\n')
            count, path = self._run(gc.set_fps_fov, "iw5mp_ds", tmp)
            self.assertEqual(count, 1)
            self.assertEqual(_read(path), b'seta cg_fov "90"\nseta com_maxfps "90"\n')

    def test_failed_writes_are_reported_to_the_caller(self):
        for func, key in ((gc.set_fps_fov, "iw5mp"), (gc.set_deadzone, "iw5mp")):
            with self.subTest(func=func.__name__), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(gc, "_patch_dvars", side_effect=PermissionError("read-only")):
                failures = []
                count, path = self._run(func, key, tmp, failures=failures)
                self.assertEqual(count, 0)
                self.assertEqual(failures, [(path, "read-only")])
                # Without the list it still returns the count, as before
                self.assertEqual(self._run(func, key, tmp)[0], 0)


class DiagReportRedactionTests(unittest.TestCase):
    def test_report_lines_are_redacted(self):
        import diag_host_menus
        before = list(diag_host_menus.LINES)
        try:
            with patch("builtins.print"):
                diag_host_menus.say("dir", "/home/alice/.local/share/Steam/userdata/12345678/x")
            self.assertEqual(diag_host_menus.LINES[-1], "dir ~/.local/share/Steam/userdata/<uid>/x")
        finally:
            diag_host_menus.LINES[:] = before


if __name__ == "__main__":
    unittest.main()
