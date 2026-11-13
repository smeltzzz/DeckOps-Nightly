"""Regression tests for uninstaller fixes (sweep 5).

Like test_uninstall_safety.py, these run the Python embedded in
deckops_uninstall.sh against temporary HOME / Steam folders, never real ones.
Tests that run bash itself only run on Linux.
"""
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deckops_uninstall.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def after(marker, opener):
    """The heredoc body of the first `opener` after `marker`."""
    return TEXT.split(marker, 1)[1].split(opener, 1)[1].split("\nPYEOF", 1)[0]


HELPER = after("cat > \"$DECKOPS_PY_HELPERS/deckops_vdf.py\"", "<<'PYEOF'\n")
OWN_RESTORE = after('info "Restoring non-Steam (My Own) game executables..."', "<< 'PYEOF'\n")
LEDGER_MERGE = after('"$HOME/.config/$OTHER_XDG_ID/vdf_edits.json"', "<<'PYEOF'\n")
LEDGER = after("# The VDF edit ledger records", "python3 - << 'PYEOF'\n")
LAUNCH_SWEEP = after('info "Clearing DeckOps launch options from localconfig.vdf..."', "<< 'PYEOF'\n")
ARTWORK = after('info "Removing non-Steam shortcut artwork from Steam grid..."', "<< 'PYEOF'\n")
CONTROLLER = after('info "Removing per-game controller configs..."', "<< 'PYEOF'\n")
LCD_SCAN = after('info "Scanning for DeckOps LCD shortcut appids..."', "<<'PYEOF'\n")
SHARED_DLLS = after('"$SHARED_DLL_DIR" "${COMPAT_DIRS[@]}"', "<<'PYEOF'\n")
OFFLINE_SCRIPTS = after('info "Removing DeckOps offline scripts from game folders..."', "<<'PYEOF'\n")

POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "needs POSIX symlinks")
LINUX_BASH = unittest.skipUnless(sys.platform == "linux" and shutil.which("bash"), "requires Linux bash")


def appid(exe, name):
    return str((binascii.crc32((exe + name).encode()) & 0xFFFFFFFF) | 0x80000000)


def entry(index, name, exe, tag=False):
    raw = b"\x00" + str(index).encode() + b"\x00"
    for key, value in ((b"AppName", name), (b"exe", exe)):
        raw += b"\x01" + key + b"\x00" + value.encode() + b"\x00"
    raw += b"\x02appid\x00\x08\x00\x00\x00"
    if tag:
        raw += b"\x00tags\x00\x010\x00DeckOps\x00\x08"
    return raw + b"\x08"


def shortcuts(*entries):
    return b"\x00shortcuts\x00" + b"".join(entries) + b"\x08\x08"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "steam"
        self.config = self.root / "userdata" / "12345" / "config"
        self.config.mkdir(parents=True)
        (self.home / "deckops_vdf.py").write_text(HELPER, encoding="utf-8")
        self.install = self.home / "DeckOps-Nightly"
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        DECKOPS_STEAM_ROOT=str(self.root), PYTHONPATH=str(self.home),
                        DECKOPS_INSTALL_DIR=str(self.install), PYTHONIOENCODING="utf-8")
        for k in ("HAS_VDF_LEDGER", "SHORTCUTS_SNAPSHOT"):
            self.env.pop(k, None)

    def run_code(self, code, *args, rc=0):
        result = subprocess.run([sys.executable, "-c", code, *args], env=self.env,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        return result


class SharedDllTests(Base):
    """F1: prefixes whose system32/syswow64 link into shared_dlls get real copies."""

    def make_shared(self):
        shared = self.home / ".local/share/deckops/shared_dlls"
        sys32 = shared / "system32"
        sys32.mkdir(parents=True)
        (sys32 / "a.dll").write_bytes(b"real dll")
        os.symlink("/nonexistent/compatibilitytools.d/GE-Proton/files/b.dll", sys32 / "b.dll")
        os.symlink(str(sys32 / "a.dll"), sys32 / "c.dll")   # back into shared_dlls
        (shared / "syswow64").mkdir()
        (shared / "syswow64" / "w.dll").write_bytes(b"wow")
        return shared

    def windows(self, appid_):
        w = self.root / "steamapps/compatdata" / appid_ / "pfx/drive_c/windows"
        w.mkdir(parents=True)
        return w

    @POSIX_ONLY
    def test_linked_prefix_gets_a_real_copy_and_survives_removal(self):
        shared = self.make_shared()
        w = self.windows("42700")
        os.symlink(str(shared / "system32"), w / "system32")
        os.symlink(str(shared / "syswow64"), w / "syswow64")
        donor = self.windows("10090")
        (donor / "system32").mkdir()
        (donor / "system32" / "own.dll").write_bytes(b"donor")

        self.run_code(SHARED_DLLS, str(shared), str(self.root / "steamapps/compatdata"))
        shutil.rmtree(shared)

        sys32 = w / "system32"
        self.assertFalse(sys32.is_symlink())
        self.assertEqual((sys32 / "a.dll").read_bytes(), b"real dll")
        self.assertTrue((sys32 / "b.dll").is_symlink())       # Proton's link kept as a link
        self.assertFalse((sys32 / "c.dll").is_symlink())
        self.assertEqual((sys32 / "c.dll").read_bytes(), b"real dll")
        self.assertEqual((w / "syswow64" / "w.dll").read_bytes(), b"wow")
        self.assertEqual((donor / "system32" / "own.dll").read_bytes(), b"donor")
        self.assertFalse(any(p.name.endswith(".deckops-copy") for p in w.iterdir()))

    @POSIX_ONLY
    def test_failed_copy_keeps_the_link_and_reports_failure(self):
        shared = self.make_shared()
        shutil.rmtree(shared / "syswow64")
        w = self.windows("42700")
        os.symlink(str(shared / "syswow64"), w / "syswow64")
        self.run_code(SHARED_DLLS, str(shared), str(self.root / "steamapps/compatdata"), rc=2)
        self.assertTrue((w / "syswow64").is_symlink())
        self.assertTrue(w.parent.parent.parent.is_dir())      # prefix never removed

    def test_shell_keeps_shared_dlls_when_a_copy_fails(self):
        self.assertIn("KEEP_SHARED_DLLS=1", TEXT)
        self.assertIn("keep+=(shared_dlls)", TEXT)
        # Runs before the data dir goes
        self.assertLess(TEXT.index('"$SHARED_DLL_DIR" "${COMPAT_DIRS[@]}"'),
                        TEXT.index('info "Removing DeckOps install directory and config..."'))


class LauncherIdTests(Base):
    """F2: the current launcher_offline.sh-based launcher appid is cleaned."""

    def launcher_id(self):
        exe = os.path.join(str(self.install), "assets", "LAN", "launcher_offline.sh")
        return appid(f'"{exe}"', "DeckOps: Plutonium Offline")

    def test_artwork_of_current_launcher_is_removed(self):
        grid = self.config / "grid"
        grid.mkdir()
        lid = self.launcher_id()
        for n in (f"{lid}p.png", f"{lid}_hero.png", "999.png"):
            (grid / n).write_bytes(b"x")
        self.run_code(ARTWORK)
        self.assertEqual(sorted(p.name for p in grid.iterdir()), ["999.png"])

    def test_controller_config_of_current_launcher_is_removed(self):
        cfg = self.root / "steamapps/common/Steam Controller Configs/12345/config"
        d = cfg / self.launcher_id()
        d.mkdir(parents=True)
        data = b"deckops layout"
        (d / "controller_neptune.vdf").write_bytes(data)
        (d / "controller_neptune.vdf.deckops-owned").write_text(hashlib.sha256(data).hexdigest())
        self.run_code(CONTROLLER)
        self.assertFalse(d.exists())


class SnapshotTests(Base):
    """F4: the snapshot persists and is read together with the live file."""

    def test_snapshot_lives_in_config_and_is_not_overwritten(self):
        self.assertIn('SHORTCUTS_SNAPSHOT="$HOME/.config/$XDG_ID/uninstall_shortcuts_snapshot"', TEXT)
        self.assertNotIn('SHORTCUTS_SNAPSHOT="$(mktemp -d)"', TEXT)
        self.assertNotIn('rm -rf "$SHORTCUTS_SNAPSHOT"', TEXT)
        self.assertIn('if [ ! -f "$SHORTCUTS_SNAPSHOT/$_uid.vdf" ]; then', TEXT)
        # Keep the installer and config (snapshot + applied ledger) until shared cleanup succeeds.
        self.assertIn('DECKOPS_DIRS+=("$INSTALL_DIR" "$HOME/.config/$XDG_ID")\n\nfor d in "${DECKOPS_DIRS[@]}"', TEXT)

    def test_scans_read_snapshot_and_live_file(self):
        snap = self.home / "snap"
        snap.mkdir()
        old_exe = '"/games/old/t6mp.exe"'
        new_exe = '"/games/new/codwaw.exe"'
        (snap / "12345.vdf").write_bytes(shortcuts(entry(0, "x", old_exe, True)))
        (self.config / "shortcuts.vdf").write_bytes(shortcuts(entry(0, "y", new_exe, True)))
        grid = self.config / "grid"
        grid.mkdir()
        a = appid(old_exe, "Call of Duty: Black Ops II - Multiplayer")
        b = appid(new_exe, "Call of Duty: World at War")
        (grid / f"{a}p.png").write_bytes(b"x")
        (grid / f"{b}p.png").write_bytes(b"x")
        self.env["SHORTCUTS_SNAPSHOT"] = str(snap)
        self.run_code(ARTWORK)
        self.assertEqual(list(grid.iterdir()), [])

    def test_lcd_scan_reads_snapshot_and_live_file(self):
        snap = self.home / "snap"
        snap.mkdir()
        (snap / "12345.vdf").write_bytes(shortcuts(entry(0, "A", "/x/lcd_wrappers/plut_a.sh")))
        (self.config / "shortcuts.vdf").write_bytes(shortcuts(entry(0, "B", "/x/lcd_wrappers/plut_b.sh")))
        self.env["SHORTCUTS_SNAPSHOT"] = str(snap)
        out = self.home / "ids.txt"
        self.run_code(LCD_SCAN, str(self.root), str(out))
        self.assertEqual(len(out.read_text().split()), 2)

    def test_controller_step_survives_bad_marker_and_reports_partial(self):
        cfg = self.root / "steamapps/common/Steam Controller Configs/12345/config"
        bad = cfg / "7940"
        good = cfg / "10090"
        for d in (bad, good):
            d.mkdir(parents=True)
        (bad / "controller_neptune.vdf").write_bytes(b"layout")
        (bad / "controller_neptune.vdf.deckops-owned").write_bytes(b"\xff\xfe not ascii")
        data = b"deckops layout"
        (good / "controller_neptune.vdf").write_bytes(data)
        (good / "controller_neptune.vdf.deckops-owned").write_text(hashlib.sha256(data).hexdigest())
        result = self.run_code(CONTROLLER, rc=4)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("WARNING", result.stdout)
        self.assertFalse(good.exists())
        self.assertTrue((bad / "controller_neptune.vdf").exists())


class LedgerMergeTests(Base):
    """F5: side-by-side uninstall hands this ledger's records to the other branch."""

    def test_records_only_in_ours_are_added_and_theirs_win(self):
        ours = self.home / "ours.json"
        theirs = self.home / "deckops" / "vdf_edits.json"
        theirs.parent.mkdir()
        ours.write_text(json.dumps({
            "localconfig": {"12345": {"7940": {"LaunchOptions": "ours", "UseSteamControllerConfig": "1"}}},
            "configsets": {"configset_controller_neptune.vdf": {"7940": "t_ours"}},
        }))
        theirs.write_text(json.dumps({
            "localconfig": {"12345": {"7940": {"LaunchOptions": "theirs"}}},
            "config_vdf": {"CompatToolMapping": {"10090": "GE"}},
        }))
        self.run_code(LEDGER_MERGE, str(ours), str(theirs))
        merged = json.loads(theirs.read_text())
        self.assertEqual(merged["localconfig"]["12345"]["7940"],
                         {"LaunchOptions": "theirs", "UseSteamControllerConfig": "1"})
        self.assertEqual(merged["config_vdf"], {"CompatToolMapping": {"10090": "GE"}})
        self.assertEqual(merged["configsets"], {"configset_controller_neptune.vdf": {"7940": "t_ours"}})

    def test_missing_other_ledger_is_created(self):
        ours = self.home / "ours.json"
        theirs = self.home / "deckops" / "vdf_edits.json"
        ours.write_text(json.dumps({"config_vdf": {"CompatToolMapping": {"7940": "GE"}}}))
        self.run_code(LEDGER_MERGE, str(ours), str(theirs))
        self.assertEqual(json.loads(theirs.read_text()), json.loads(ours.read_text()))

    def test_damaged_other_ledger_fails_and_is_left_alone(self):
        ours = self.home / "ours.json"
        theirs = self.home / "theirs.json"
        ours.write_text(json.dumps({"config_vdf": {}}))
        theirs.write_text("{broken")
        result = subprocess.run([sys.executable, "-c", LEDGER_MERGE, str(ours), str(theirs)],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(theirs.read_text(), "{broken")

    def test_shell_keeps_own_config_when_merge_fails(self):
        side = TEXT.split('if [ -n "$KEEP_OTHER" ]; then\n    if [ ! -f "$OTHER_DIR/src/main.py" ]', 1)[1]
        side = side.split("\n    exit 0\nfi", 1)[0]
        self.assertIn("KEEP_OWN_CONFIG=1", side)
        self.assertIn('rm -rf "$INSTALL_DIR" \\\n', side)


class RestoreExeTests(Base):
    """F6: a .bak only goes back over a missing exe or a DeckOps script."""

    def setup_game(self, exe_name, content):
        game = self.home / "games" / exe_name.split(".")[0]
        game.mkdir(parents=True)
        exe = game / exe_name
        if content is not None:
            exe.write_bytes(content)
        (game / (exe_name + ".bak")).write_bytes(b"original")
        return exe

    def test_own_copy_restore_respects_real_exes(self):
        real = self.setup_game("t6mp.exe", b"MZ verified by steam")
        script = self.setup_game("t6zm.exe", b"#!/bin/bash\nwrapper\n")
        missing = self.setup_game("CoDWaW.exe", None)
        legacy = self.setup_game("iw4mp.exe", b"MZ iw4x binary swap")
        (self.config / "shortcuts.vdf").write_bytes(shortcuts(
            *[entry(i, "g", f'"{p}"', True) for i, p in enumerate((real, script, missing, legacy))]))
        self.run_code(OWN_RESTORE)
        self.assertEqual(real.read_bytes(), b"MZ verified by steam")
        self.assertTrue(Path(str(real) + ".bak").exists())
        for p in (script, missing, legacy):
            self.assertEqual(p.read_bytes(), b"original")
            self.assertFalse(Path(str(p) + ".bak").exists())

    @LINUX_BASH
    def test_bash_restore_exe(self):
        func = re.search(r"^restore_exe\(\) \{\n.*?^\}\n", TEXT, re.S | re.M).group(0)
        real = self.setup_game("t6mp.exe", b"MZ real")
        script = self.setup_game("t6zm.exe", b"#!/bin/bash\n")
        code = ("success() { :; }; warn() { :; }; skip() { :; }; FAILED=0\n" + func +
                f'restore_exe "{real.parent}" t6mp.exe\nrestore_exe "{script.parent}" t6zm.exe\n')
        subprocess.run(["bash", "-c", code], check=True)
        self.assertEqual(real.read_bytes(), b"MZ real")
        self.assertTrue(Path(str(real) + ".bak").exists())
        self.assertEqual(script.read_bytes(), b"original")

    def test_no_remove_then_rename(self):
        self.assertNotIn("os.remove(candidate)", OWN_RESTORE)
        self.assertNotIn('mv "$bak_path" "$exe_path"', TEXT)


LOCALCONFIG = ('"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
               '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"311210"\n\t\t\t\t\t{\n'
               '\t\t\t\t\t\t"LaunchOptions"\t\t"WINEDLLOVERRIDES=\\"d3d11=n,b\\" %command%"\n'
               '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')


class LaunchSweepTests(Base):
    """F7: with a ledger, the heuristic sweep leaves player options alone."""

    def test_ledger_install_keeps_reshade_override(self):
        path = self.config / "localconfig.vdf"
        path.write_text(LOCALCONFIG)
        self.env["HAS_VDF_LEDGER"] = "1"
        self.run_code(LAUNCH_SWEEP)
        self.assertEqual(path.read_text(), LOCALCONFIG)

    def test_no_ledger_install_still_swept(self):
        path = self.config / "localconfig.vdf"
        path.write_text(LOCALCONFIG)
        self.run_code(LAUNCH_SWEEP)
        self.assertNotIn("d3d11=n,b", path.read_text())

    def test_applied_ledger_still_counts_on_rerun(self):
        ledger = self.home / ".config/deckops-nightly/vdf_edits.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({}))
        self.run_code(LEDGER)
        self.assertFalse(ledger.exists())
        self.assertTrue(Path(str(ledger) + ".applied").exists())
        self.assertIn('[ -f "$HOME/.config/$XDG_ID/vdf_edits.json.applied" ]', TEXT)


class OfflineScriptTests(Base):
    """F3: offline scripts recorded in deckops.json are removed from own copies."""

    def test_only_deckops_scripts_are_removed(self):
        game = self.home / "Games" / "BO2"
        game.mkdir(parents=True)
        lan = game / "t6plut_lan_mp.sh"
        lan.write_text("#!/bin/bash\n# DeckOps offline t6mp (plutonium._write_offline_wrapper)\nexec x\n")
        foreign = game / "t5plut_lan_mp.sh"
        foreign.write_text("#!/bin/bash\necho mine\n")
        online = game / "t6plutmp.exe"
        online.write_text('#!/bin/bash\n"proton" run "l.exe" "plutonium://play/t6mp" &\n')
        exe = game / "t6zmplut.exe"
        exe.write_bytes(b"MZ not a script")
        self.install.mkdir()
        config = self.install / "deckops.json"
        config.write_text(json.dumps({"setup_games": {
            "t6mp": {"lan_wrapper_path": str(lan), "wrapper_path": str(online)},
            "t5mp": {"lan_wrapper_path": str(foreign), "wrapper_path": str(exe)},
            "bad": "not a dict",
        }}))
        self.run_code(OFFLINE_SCRIPTS, str(config))
        self.assertFalse(lan.exists())
        self.assertFalse(online.exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(exe.exists())

    def test_runs_before_install_dir_is_removed(self):
        self.assertLess(TEXT.index('info "Removing DeckOps offline scripts from game folders..."'),
                        TEXT.index('info "Removing DeckOps install directory and config..."'))


class ControllerBackupTests(Base):
    """F10: the player's .deckops-user-<time> layout comes back."""

    def test_newest_user_backup_is_restored(self):
        d = self.root / "steamapps/common/Steam Controller Configs/12345/config/7940"
        d.mkdir(parents=True)
        data = b"deckops layout"
        canon = d / "controller_neptune.vdf"
        canon.write_bytes(data)
        Path(str(canon) + ".deckops-owned").write_text(hashlib.sha256(data).hexdigest())
        Path(str(canon) + ".deckops-user-100").write_bytes(b"older")
        Path(str(canon) + ".deckops-user-200").write_bytes(b"newest")
        Path(str(canon) + ".deckops-original").write_bytes(b"before deckops")
        result = self.run_code(CONTROLLER)
        self.assertEqual(canon.read_bytes(), b"newest")
        self.assertFalse(Path(str(canon) + ".deckops-owned").exists())
        self.assertTrue(Path(str(canon) + ".deckops-user-100").exists())
        self.assertIn("deckops-user-100", result.stdout)
        self.assertIn("deckops-original", result.stdout)

    def test_without_user_backup_original_comes_back(self):
        d = self.root / "steamapps/common/Steam Controller Configs/12345/config/7940"
        d.mkdir(parents=True)
        data = b"deckops layout"
        canon = d / "controller_neptune.vdf"
        canon.write_bytes(data)
        Path(str(canon) + ".deckops-owned").write_text(hashlib.sha256(data).hexdigest())
        Path(str(canon) + ".deckops-original").write_bytes(b"before deckops")
        self.run_code(CONTROLLER)
        self.assertEqual(canon.read_bytes(), b"before deckops")


class ShellFixTests(unittest.TestCase):
    """F8, F11 and the Snap Steam root."""

    def test_no_rm_and_success_or_skip_chains(self):
        self.assertIsNone(re.search(r"&& rm -r?f [^\n]*&& success [^\n]*\|\| skip", TEXT))

    def test_final_message_reflects_failures(self):
        self.assertIn('if [ "$FAILED" -ne 0 ]; then', TEXT)
        self.assertIn("uninstalled with warnings", TEXT)
        decky = TEXT.split('warn "Could not remove the Decky plugin', 1)[1].split("else", 1)[0]
        self.assertIn("FAILED=1", decky)
        heroic = TEXT.split('warn "Heroic library.json edit failed"', 1)[1].split("else", 1)[0]
        self.assertIn("FAILED=1", heroic)

    def test_mw3_dir_checked_for_empty(self):
        self.assertNotIn('mw3_dir=$(find_install_dir "$appid") || continue', TEXT)
        self.assertIn('[ -n "$mw3_dir" ] || continue', TEXT)

    def test_snap_steam_root(self):
        roots = TEXT.split("STEAM_ROOTS=(", 1)[1].split("\n)", 1)[0]
        self.assertIn('"$HOME/snap/steam/common/.local/share/Steam"', roots)
        self.assertLess(roots.index("com.valvesoftware.Steam/.local/share/Steam"), roots.index("snap/steam"))

    @LINUX_BASH
    def test_rm_path_failure_sets_failed(self):
        func = re.search(r"^rm_path\(\) \{\n.*?^\}\n", TEXT, re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "f"
            target.write_text("x")
            code = ("success() { echo OK; }; warn() { echo WARN; }; skip() { echo SKIP; }; FAILED=0\n"
                    + func + "rm() { return 1; }\n"
                    f'rm_path "{target}" f\necho "FAILED=$FAILED"\n'
                    f'rm_path "{tmp}/missing" m\n')
            out = subprocess.run(["bash", "-c", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.split(), ["WARN", "FAILED=1", "SKIP"])

    def test_embedded_python_compiles(self):
        for body in (OWN_RESTORE, LEDGER_MERGE, SHARED_DLLS, OFFLINE_SCRIPTS, CONTROLLER,
                     ARTWORK, LCD_SCAN, LAUNCH_SWEEP):
            compile(body, "deckops_uninstall.sh", "exec")


if __name__ == "__main__":
    unittest.main()
