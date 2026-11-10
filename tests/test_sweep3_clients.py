"""Regressions for sweep 3: mod client installers, game detection, game config."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import mock_open, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cache_cleanup
import cod4x
import detect_games
import detect_hw
import game_config
import iw3sp
import sp_mod
import t6sp_mod


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class ChainLoaderRelocationTests(_TmpCase):
    """setup.exe that installs nothing must not be reported as a working CoD4x."""

    def _run(self, mss, miles):
        game = self.root / "game"
        game.mkdir()
        if mss is not None:
            (game / "mss32.dll").write_bytes(mss)
        if miles is not None:
            (game / "miles32.dll").write_bytes(miles)
        compat = self.root / "compat"          # no prefix fallback directory at all
        return cod4x._relocate_chainloader(str(compat), str(game))

    def test_stock_mss_next_to_stale_identical_backup_is_a_failure(self):
        self.assertFalse(self._run(b"stock dll", b"stock dll"))

    def test_missing_mss_with_backup_is_a_failure(self):
        self.assertFalse(self._run(None, b"stock dll"))

    def test_same_size_but_different_bytes_is_accepted(self):
        self.assertTrue(self._run(b"loader!!!", b"stock dll"))

    def test_different_sizes_are_accepted(self):
        self.assertTrue(self._run(b"chain-loader dll", b"stock"))


class IwPadArchiveContentTests(_TmpCase):
    def _fetch(self, members):
        game = self.root / "game"
        game.mkdir(exist_ok=True)
        zpath = self.root / "src.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)

        def fake_download(url, dest, *a, **k):
            Path(dest).write_bytes(zpath.read_bytes())

        with patch.object(sp_mod, "download", side_effect=fake_download), patch("config.set_iw_pad"):
            sp_mod._fetch_iw_pad("iw4sp", str(game), "x64", lambda p, m: None,
                                 release=("https://h/r/download/v1/iw-pad-x64.zip", "sha256:a"))
        return game

    def test_archive_without_iw_pad_exe_is_rejected(self):
        game = self.root / "game"
        game.mkdir()
        (game / "iw-pad.exe").write_bytes(b"old")
        with self.assertRaises(RuntimeError):
            self._fetch({"readme.txt": b"hi"})
        self.assertEqual((game / "iw-pad.exe").read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in game.iterdir()), ["iw-pad.exe"])

    def test_empty_archive_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._fetch({})

    def test_good_archive_still_installs(self):
        game = self._fetch({"iw-pad.exe": b"exe", "iw-pad.dll": b"dll"})
        self.assertEqual((game / "iw-pad.exe").read_bytes(), b"exe")


class IwSpStagingTests(_TmpCase):
    def test_iw3sp_removes_staging_left_by_a_killed_run_and_writes_metadata_cleanly(self):
        game = self.root / "game"
        stale = game / ".deckops_iw3sp_dead"
        stale.mkdir(parents=True)
        (stale / "junk.bin").write_bytes(b"x" * 10)
        data = b"mod dll"
        # The manifest must include the exe: install_iw3sp rejects one without it.
        manifest = [["iw3sp_mod.dll", len(data), hashlib.sha1(data).hexdigest()],
                    ["iw3sp_mod.exe", len(data), hashlib.sha1(data).hexdigest()]]

        def fake_download(url, dest, *a, **k):
            Path(dest).write_bytes(data)

        with patch.object(iw3sp, "_get_manifest", return_value=manifest), \
                patch.object(iw3sp, "_download", side_effect=fake_download):
            iw3sp.install_iw3sp({"install_dir": str(game)}, "", "", "", source="own")
        self.assertFalse(stale.exists())
        self.assertEqual((game / "iw3sp_mod.dll").read_bytes(), data)
        self.assertEqual(json.loads((game / iw3sp.METADATA_FILE).read_text())["source"],
                         f"{iw3sp._CDN}/files.json")
        self.assertEqual([p.name for p in game.iterdir() if p.name.endswith(".tmp")], [])

    def test_t6sp_removes_staging_left_by_a_killed_run(self):
        game = self.root / "game"
        stale = game / ".deckops_t6sp_dead"
        stale.mkdir(parents=True)
        (stale / "T6SP-Mod.exe").write_bytes(b"half")

        def fake_download(url, dest, *a, **k):
            Path(dest).write_bytes(b"new")

        with patch.object(t6sp_mod, "github_asset", return_value=("https://h/x", None)), \
                patch.object(t6sp_mod, "_download", side_effect=fake_download):
            t6sp_mod.install_t6sp_mod({"install_dir": str(game)}, "", "", "", source="own")
        self.assertFalse(stale.exists())
        self.assertEqual((game / "T6SP-Mod.exe").read_bytes(), b"new")
        self.assertEqual(json.loads((game / t6sp_mod.METADATA_FILE).read_text())["client"], "t6sp_mod")


class ShaderCacheOwnGameTests(_TmpCase):
    def test_every_game_with_a_steam_appid_has_an_own_shortcut_title(self):
        self.assertEqual(set(cache_cleanup.STEAM_APPIDS) - set(cache_cleanup._OWN_SHORTCUT_TITLES), set())

    def test_own_dedicated_server_cleans_the_shortcut_cache(self):
        appid = cache_cleanup._calc_shortcut_appid(
            '"/usr/bin/flatpak"', cache_cleanup._OWN_SHORTCUT_TITLES["iw5mp"])
        base = self.root / "shadercache"
        target = base / str(appid)
        target.mkdir(parents=True)
        (target / "f").write_bytes(b"x")
        with patch.object(cache_cleanup, "_shadercache_dirs", return_value=[str(base)]):
            cache_cleanup.cleanup_shader_cache("iw5mp_ds", "own")
        self.assertFalse(target.exists())


class DetectGamesRobustnessTests(_TmpCase):
    def _acf(self, steamapps, appid, name):
        steamapps.mkdir(parents=True, exist_ok=True)
        (steamapps / f"appmanifest_{appid}.acf").write_text(
            f'"AppState"\n{{\n\t"installdir"\t\t"{name}"\n\t"StateFlags"\t\t"4"\n}}\n')
        game = steamapps / "common" / name
        game.mkdir(parents=True)
        (game / "iw3mp.exe").write_bytes(b"x")

    def test_unreadable_manifest_in_one_library_does_not_hide_the_game_in_another(self):
        lib2 = self.root / "sd"
        main = self.root / "steam" / "steamapps"
        main.mkdir(parents=True)
        # A directory where the manifest should be: exists() is true, open() raises OSError
        (main / "appmanifest_7940.acf").mkdir()
        (main / "libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n"1"\n{{\n"path" "{lib2.as_posix()}"\n}}\n}}\n')
        self._acf(lib2 / "steamapps", 7940, "Call of Duty 4")
        with patch.object(detect_games, "SD_CARD_PATTERNS", []):
            found = detect_games.find_installed_games([], str(self.root / "steam"))
        self.assertIn("cod4mp", found)
        self.assertTrue(found["cod4mp"]["install_dir"].startswith(str(lib2)))

    def test_unreadable_libraryfolders_vdf_is_ignored(self):
        steam = self.root / "steam"
        (steam / "steamapps" / "libraryfolders.vdf").mkdir(parents=True)
        self.assertEqual(detect_games.parse_library_folders(str(steam)), [str(steam)])
        with patch.object(detect_games, "SD_CARD_PATTERNS", []):
            self.assertEqual(detect_games._all_library_dirs(str(steam)),
                             [os.path.normpath(str(steam / "steamapps"))])


class OsReleaseParsingTests(unittest.TestCase):
    def _os(self, text):
        with patch("builtins.open", mock_open(read_data=text)):
            return detect_hw.detect_os()

    def test_single_quoted_id_is_recognised(self):
        self.assertEqual(self._os("NAME='Bazzite'\nID='bazzite'\n"), "bazzite")

    def test_double_quoted_and_plain_ids_still_work(self):
        self.assertEqual(self._os('ID="steamos"\n'), "steamos")
        self.assertEqual(self._os("ID=cachyos\n"), "cachyos")

    def test_commented_id_line_is_not_used(self):
        self.assertIsNone(self._os("#ID=steamos\nID=debian\n"))


class PatchDvarsTempFileTests(_TmpCase):
    def test_failed_replace_leaves_original_and_no_temp_file(self):
        cfg = self.root / "config.cfg"
        cfg.write_bytes(b'seta cg_fov "65"\r\n')
        with patch.object(game_config.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                game_config._patch_dvars(str(cfg), {"cg_fov": "90"})
        self.assertEqual(cfg.read_bytes(), b'seta cg_fov "65"\r\n')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.cfg"])

    def test_patch_keeps_crlf(self):
        cfg = self.root / "config.cfg"
        cfg.write_bytes(b'seta cg_fov "65"\r\nseta x "1"\r\n')
        self.assertTrue(game_config._patch_dvars(str(cfg), {"cg_fov": "90", "com_maxfps": "90"}))
        self.assertEqual(cfg.read_bytes(),
                         b'seta cg_fov "90"\r\nseta x "1"\r\nseta com_maxfps "90"\r\n')


if __name__ == "__main__":
    unittest.main()
