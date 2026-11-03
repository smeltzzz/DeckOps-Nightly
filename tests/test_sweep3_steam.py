"""Third sweep: Steam helpers (shortcut, save_backup, steam_appinfo) and the
uninstaller's embedded Python passes.

Never touches the real ~/DeckOps-Nightly, Steam userdata or game folders.
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import save_backup as sb  # noqa: E402
import shortcut  # noqa: E402
import steam_appinfo  # noqa: E402

SCRIPT = ROOT / "deckops_uninstall.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
_POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "needs bash")


def _pyeof_after(marker, heredoc="<< 'PYEOF'\n"):
    return TEXT.split(marker, 1)[1].split(heredoc, 1)[1].split("\nPYEOF", 1)[0]


HELPER = TEXT.split("cat > \"$DECKOPS_PY_HELPERS/deckops_vdf.py\" <<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
LEDGER = TEXT.split("# The VDF edit ledger records", 1)[1].split("python3 - << 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
APPINFO = _pyeof_after('info "Removing DeckOps entries from Steam launch menus..."')
HEROIC_LIBRARY = TEXT.split('python3 - "$HGL_LIB" << \'PYEOF\'\n', 1)[1].split("\nPYEOF", 1)[0]


# ── artwork ──────────────────────────────────────────────────────────────────

class ArtworkKeepsWorkingFilesTests(unittest.TestCase):
    def test_failed_download_does_not_delete_existing_artwork(self):
        """Reinstall/Repair with the CDN unreachable used to delete the working
        artwork first and then fail to download it: blank tiles."""
        appid = 2147483700
        sdef = {"icon_ext": "png", "grid_ext": "jpg", "wide_ext": "jpg", "hero_ext": "jpg",
                "logo_ext": "png", "icon_url": "http://x/i.png", "grid_url": "http://x/g.jpg"}
        with tempfile.TemporaryDirectory() as tmp:
            grid = Path(tmp)
            wanted = [f"{appid}_icon.png", f"{appid}p.jpg"]
            stale = [f"{appid}p.png", f"{appid}_icon.jpg"]       # an older extension
            other = [f"{appid}0p.jpg"]                          # another app's art
            for n in wanted + stale + other:
                (grid / n).write_bytes(b"good")
            with patch.object(shortcut, "_download", return_value=False):
                shortcut._download_artwork(str(grid), appid, sdef, lambda m: None,
                                           force=True, clean_stale=True)
            self.assertEqual(sorted(p.name for p in grid.iterdir()), sorted(wanted + other))


# ── remove_shortcut ──────────────────────────────────────────────────────────

class RemoveShortcutArtworkTests(unittest.TestCase):
    NAME = "DeckOps: Test Title"
    EXE = '"/opt/DeckOps/launcher.sh"'
    ART = {"icon_ext": "png", "grid_ext": "jpg", "wide_ext": "jpg", "hero_ext": "jpg", "logo_ext": "png"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.userdata = self.tmp.name
        self.cfg = Path(self.userdata, "12345", "config")
        (self.cfg / "grid").mkdir(parents=True)
        self.appid = shortcut._calc_shortcut_appid(self.EXE, self.NAME)
        self.files = [self.cfg / "grid" / f"{self.appid}{s}"
                      for s in ("_icon.png", "p.jpg", ".jpg", "_hero.jpg", "_logo.png")]
        for f in self.files:
            f.write_bytes(b"art")

    def _run(self):
        with patch.object(shortcut, "_find_all_steam_uids", return_value=["12345"]), \
             patch.object(shortcut, "_userdata_dir", return_value=self.userdata):
            shortcut.remove_shortcut(self.NAME, self.EXE, self.ART)

    def _vdf(self, *names):
        entries = [shortcut._make_shortcut_entry(
            i, shortcut._shortcut_entry(1 + i, n, '"x"', '"y"', "", "")) for i, n in enumerate(names)]
        return b"\x00shortcuts\x00" + b"".join(entries) + b"\x08\x08"

    def test_artwork_is_removed_even_when_the_shortcut_is_already_gone(self):
        vdf = self.cfg / "shortcuts.vdf"
        vdf.write_bytes(self._vdf("Someone else's game"))
        before = vdf.read_bytes()
        self._run()
        self.assertEqual(vdf.read_bytes(), before)
        self.assertEqual([f for f in self.files if f.exists()], [])

    def test_artwork_is_removed_when_there_is_no_shortcuts_file(self):
        self._run()
        self.assertEqual([f for f in self.files if f.exists()], [])

    def test_unreadable_shortcuts_file_leaves_the_user_alone(self):
        vdf = self.cfg / "shortcuts.vdf"
        vdf.write_bytes(b"garbage")
        self._run()
        self.assertEqual(vdf.read_bytes(), b"garbage")
        self.assertTrue(all(f.exists() for f in self.files))

    def test_shortcut_is_still_removed(self):
        vdf = self.cfg / "shortcuts.vdf"
        vdf.write_bytes(self._vdf("Keep me", self.NAME))
        self._run()
        self.assertNotIn(self.NAME.encode(), vdf.read_bytes())
        self.assertIn(b"Keep me", vdf.read_bytes())


# ── save_backup ──────────────────────────────────────────────────────────────

class SaveBackupSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sb, "_DETECTED", None)
        sb._DETECTED = None

    def test_game_scan_is_redone_for_every_backup_and_restore(self):
        sb._DETECTED = ({"cod4sp": {"install_dir": "/stale"}}, {})
        with patch.object(sb, "BACKUP_ROOT", os.path.join(self.tmp.name, "none")):
            sb.restore_saves(steam_root=self.tmp.name)
        self.assertIsNone(sb._DETECTED)
        sb._DETECTED = ({"cod4sp": {"install_dir": "/stale"}}, {})
        with patch.object(sb, "BACKUP_ROOT", os.path.join(self.tmp.name, "b")), \
             patch.object(sb, "_load_config", return_value={"setup_games": {}}):
            sb.backup_saves(steam_root=self.tmp.name)
        self.assertIsNone(sb._DETECTED)

    def test_failed_game_scan_is_not_remembered(self):
        with patch("detect_games.find_steam_root", side_effect=RuntimeError("steam files mid-write")):
            self.assertEqual(sb._detected_games(), ({}, {}))
        self.assertIsNone(sb._DETECTED)
        with patch("detect_games.find_steam_root", return_value=self.tmp.name), \
             patch("detect_games.parse_library_folders", return_value=[]), \
             patch("detect_games.find_installed_games", return_value={"cod4sp": {}}), \
             patch("detect_games.find_own_installed", return_value={}):
            self.assertEqual(sb._detected_games(), ({"cod4sp": {}}, {}))

    def test_write_meta_survives_a_damaged_previous_record(self):
        root = Path(self.tmp.name, "backup")
        (root / "cod4").mkdir(parents=True)
        (root / "backup_info.json").write_text(json.dumps({"groups": 5, "group_times": ["x"]}))
        with patch.object(sb, "BACKUP_ROOT", str(root)):
            sb._write_meta({"cod4": str(root / "cod4")})
        meta = json.loads((root / "backup_info.json").read_text())
        self.assertEqual(meta["groups"], ["cod4"])
        self.assertIn("cod4", meta["group_times"])

    def test_restore_ignores_a_damaged_group_list(self):
        root = Path(self.tmp.name, "backup")
        root.mkdir()
        for groups in ("cod4", [["cod4"], 7, None]):
            (root / "backup_info.json").write_text(json.dumps({"groups": groups}))
            with patch.object(sb, "BACKUP_ROOT", str(root)), \
                 patch.object(sb, "_load_config", return_value={}):
                self.assertEqual(sb.restore_saves(steam_root=self.tmp.name), {})


# ── steam_appinfo launch_menus record ────────────────────────────────────────

class LaunchMenuRecordTests(unittest.TestCase):
    def _config(self, stored):
        saved = []
        cfg = patch.multiple("config", load=MagicMock(return_value={"launch_menus": stored}),
                             save=MagicMock(side_effect=saved.append), transaction=MagicMock())
        return cfg, saved

    def test_record_replaces_a_damaged_launch_menus_value(self):
        for damaged in (["junk"], {"1": "junk"}, {"1": [5, {"description": "no exe"}]}):
            cfg, saved = self._config(damaged)
            with cfg:
                steam_appinfo._record(1, entries=[{"executable": "a.exe"}])
            self.assertEqual(saved[-1]["launch_menus"], {"1": [{"executable": "a.exe"}]})

    def test_reapply_all_ignores_a_damaged_launch_menus_value(self):
        cfg, _ = self._config(["junk"])
        with cfg, patch.object(steam_appinfo, "add_launch_entries") as add:
            steam_appinfo.reapply_all("/steam")
        add.assert_not_called()


# ── uninstaller: embedded Python ─────────────────────────────────────────────

class UninstallEmbeddedPythonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "steam"
        self.config = self.root / "userdata" / "12345" / "config"
        self.config.mkdir(parents=True)
        (self.home / "deckops_vdf.py").write_text(HELPER, encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        DECKOPS_STEAM_ROOT=str(self.root), PYTHONPATH=str(self.home),
                        PYTHONDONTWRITEBYTECODE="1")

    def run_code(self, code, *args, ok=True, timeout=60):
        result = subprocess.run([sys.executable, "-c", code, *args], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _ledger(self, data):
        path = self.home / ".config" / "deckops-nightly" / "vdf_edits.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data))

    def test_ledger_cleanup_handles_escaped_backslash_and_keeps_non_utf8_bytes(self):
        """A value ending in an escaped backslash made the brace check think a quote
        stayed open, so the (correct) edit was refused and the uninstall aborted
        forever; text was read with errors="replace", turning other bytes into U+FFFD."""
        self._ledger({"localconfig": {"12345": {"7940": {"UseSteamControllerConfig": "1"}}}})
        local = self.config / "localconfig.vdf"
        local.write_bytes(
            b'"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
            b'\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"7940"\n\t\t\t\t\t{\n'
            b'\t\t\t\t\t\t"UseSteamControllerConfig"\t\t"1"\n\t\t\t\t\t}\n\t\t\t\t}\n'
            b'\t\t\t\t"InstallPath"\t\t"C:\\\\"\n'
            b'\t\t\t}\n\t\t}\n\t}\n\t"PersonaName"\t\t"caf\xe9"\n}\n')
        self.run_code(LEDGER)
        data = local.read_bytes()
        self.assertIn(b'"UseSteamControllerConfig"\t\t""', data)
        self.assertIn(b'"PersonaName"\t\t"caf\xe9"', data)
        self.assertNotIn("\ufffd".encode(), data)
        self.assertIn(b'"InstallPath"\t\t"C:\\\\"', data)

    def test_ledger_cleanup_reads_non_utf8_config_vdf_and_configset(self):
        self._ledger({"config_vdf": {"CompatToolMapping": {"7940": "GE-Proton-old"}},
                      "configsets": {"configset_controller_neptune.vdf": {"7940": "controller_old_deckops.vdf"}}})
        cfg = self.root / "config" / "config.vdf"
        cfg.parent.mkdir()
        cfg.write_bytes(b'"Name"\t\t"caf\xe9"\n"CompatToolMapping"\n{\n\t"7940"\n\t{\n'
                        b'\t\t"name"\t\t"GE-Proton-old"\n\t}\n}\n')
        cs = self.root / "steamapps/common/Steam Controller Configs/12345/config/configset_controller_neptune.vdf"
        cs.parent.mkdir(parents=True)
        cs.write_bytes(b'\t"title \xe9"\n\t{\n\t\t"template"\t\t"keep.vdf"\n\t}\n'
                       b'\t"7940"\n\t{\n\t\t"template"\t\t"controller_old_deckops.vdf"\n\t}\n')
        self.run_code(LEDGER)
        self.assertNotIn(b'"7940"', cfg.read_bytes())
        self.assertIn(b"caf\xe9", cfg.read_bytes())
        self.assertNotIn(b'"7940"', cs.read_bytes())
        self.assertIn(b'"title \xe9"', cs.read_bytes())

    def test_heroic_library_edit_is_atomic_and_skipped_when_nothing_to_remove(self):
        lib = self.home / "library.json"
        original = ('{\n    "games": [{"app_name": "mine", "title": "Caf\u00e9"}]\n}').encode("utf-8")
        lib.write_bytes(original)
        self.run_code(HEROIC_LIBRARY, str(lib))
        self.assertEqual(lib.read_bytes(), original)       # not re-dumped when nothing changed

        lib.write_text(json.dumps({"games": [
            {"app_name": "do_abc"}, {"app_name": "mine", "title": "Caf\u00e9"}, "odd entry",
            {"app_name": None}]}), encoding="utf-8")
        result = self.run_code(HEROIC_LIBRARY, str(lib))
        self.assertIn("Removed 1", result.stdout)
        games = json.loads(lib.read_text(encoding="utf-8"))["games"]
        self.assertEqual([g.get("app_name") if isinstance(g, dict) else g for g in games],
                         ["mine", "odd entry", None])
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], [])

    def _appinfo_v28(self, kv):
        body = struct.pack("<IIQ20sI20s", 0, 0, 0, b"\0" * 20, 0, b"\0" * 20) + kv
        return struct.pack("<II", 0x07564428, 1) + struct.pack("<II", 10190, len(body)) + body

    @staticmethod
    def _kv(items):
        out = b""
        for t, key, val in items:
            out += bytes([t]) + key.encode() + b"\0"
            if t == 0:
                out += UninstallEmbeddedPythonTests._kv(val)
            elif t == 1:
                out += val + b"\0"
        return out + b"\x08"

    def test_appinfo_cleanup_refuses_a_cut_off_wide_string_instead_of_hanging(self):
        kv = b"\x05key\x00ab"              # wide string with no terminator, file ends
        path = self.root / "appcache" / "appinfo.vdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(self._appinfo_v28(kv))
        before = path.read_bytes()
        self.run_code(APPINFO, str(self.root), ok=False, timeout=30)
        self.assertEqual(path.read_bytes(), before)

    def test_appinfo_cleanup_removes_only_our_entries_and_rewrites_whole(self):
        launch = [(0, "0", [(1, "executable", b"iw4mp.exe")]),
                  (0, "1", [(1, "executable", b"iw4x-launcher.exe")])]
        kv = self._kv([(0, "appinfo", [(0, "config", [(0, "launch", launch)])])])
        path = self.root / "appcache" / "appinfo.vdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(self._appinfo_v28(kv))
        self.run_code(APPINFO, str(self.root))
        data = path.read_bytes()
        self.assertIn(b"iw4mp.exe", data)
        self.assertNotIn(b"iw4x-launcher.exe", data)
        leftovers = sorted(p.name for p in path.parent.iterdir())
        self.assertEqual(leftovers, ["appinfo.vdf", "appinfo.vdf.deckops_uninstall.bak"])


# ── uninstaller: shell text ──────────────────────────────────────────────────

class UninstallShellTests(unittest.TestCase):
    def test_rm_rf_of_install_dir_children_cannot_expand_from_an_empty_variable(self):
        """SC2115: rm -rf "$dir/$name" with an empty $dir would be rm -rf "/name"."""
        bad = re.findall(r'rm -rf "\$\{?\w+_dir\}?/\$[^"]*"', TEXT)
        self.assertEqual(bad, [])

    @_POSIX_ONLY
    def test_incomplete_identity_file_cannot_empty_the_removed_paths(self):
        start = TEXT.index("# An identity file from an older install")
        end = TEXT.index("# ── The other branch")
        guard = TEXT[start:end]
        script = ('EXPECTED_BRANCH=nightly; BRANCH=nightly; INSTALL_DIR_NAME=""; XDG_ID=""; INSTALL_DIR=""\n'
                  + guard + '\necho "$XDG_ID|$INSTALL_DIR_NAME|$INSTALL_DIR"\n')
        out = subprocess.run(["bash", "-c", script], env=dict(os.environ, HOME="/home/test"),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "deckops-nightly|DeckOps-Nightly|/home/test/DeckOps-Nightly")
        out = subprocess.run(["bash", "-c", script], env=dict(os.environ, HOME=""),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout.count("|"), 0)


if __name__ == "__main__":
    unittest.main()
