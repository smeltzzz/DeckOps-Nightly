"""Regressions for the bug-and-error sweep: success reported after a failed step,
partial prefixes, ledger loss, bootstrapper overwrite and the CoD4x -> CoD4R order."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cod4r
import controller_profiles
import ge_proton
import plutonium
import save_backup
import shortcut
import wrapper


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name, "ledger.json"))
        p = patch.object(wrapper, "LEDGER_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _corrupt_files(self):
        return [p for p in os.listdir(self.tmp.name) if ".corrupt-" in p]

    def test_corrupt_ledger_is_kept_not_overwritten(self):
        Path(self.path).write_text("{not json")
        wrapper._record_config_vdf("1", "CompatToolMapping", "GE-Proton10-1")
        self.assertEqual(json.loads(Path(self.path).read_text())["config_vdf"]["CompatToolMapping"],
                         {"1": "GE-Proton10-1"})
        kept = self._corrupt_files()
        self.assertEqual(len(kept), 1)
        self.assertEqual(Path(self.tmp.name, kept[0]).read_text(), "{not json")

    def test_non_object_ledger_is_kept_and_does_not_raise(self):
        Path(self.path).write_text("[1, 2]")
        wrapper._record_localconfig("u", "7940", "LaunchOptions", "x")
        self.assertEqual(len(self._corrupt_files()), 1)
        self.assertIn("localconfig", json.loads(Path(self.path).read_text()))

    def test_hand_edited_non_dict_blocks_are_replaced_not_fatal(self):
        Path(self.path).write_text(json.dumps({"config_vdf": [], "configsets": {"f": 3}}))
        wrapper._record_config_vdf("1", "CompatToolMapping", "v")
        wrapper._record_configset("f", "k", "t")
        wrapper._remove_config_vdf("1", "CompatToolMapping")
        data = json.loads(Path(self.path).read_text())
        self.assertEqual(data["configsets"], {"f": {"k": "t"}})

    def test_good_ledger_is_untouched(self):
        wrapper._record_config_vdf("1", "CompatToolMapping", "v")
        wrapper._record_config_vdf("2", "CompatToolMapping", "v")
        self.assertEqual(self._corrupt_files(), [])
        self.assertEqual(set(wrapper._read_ledger()["config_vdf"]["CompatToolMapping"]), {"1", "2"})


class VdfWriteTests(unittest.TestCase):
    def test_a_failed_write_returns_false_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "localconfig.vdf"))
            Path(path).write_text('"a"\n{\n}\n')
            with patch.object(wrapper, "_atomic_write", side_effect=OSError("read-only")):
                self.assertIs(wrapper._write_and_validate_vdf(path, '"b"\n{\n}\n'), False)
            self.assertEqual(Path(path).read_text(), '"a"\n{\n}\n')

    def test_one_unwritable_user_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            users = [("1", str(Path(tmp, "1.vdf")), "x"), ("2", str(Path(tmp, "2.vdf")), "x")]
            for _, p, _ in users:
                Path(p).write_text('"a"\n{\n}\n')

            def write(path, data):
                seen.append(path)
                if path.endswith("1.vdf"):
                    raise OSError("read-only")
            content = ('"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n'
                       '\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"7940"\n\t\t\t\t\t{\n\t\t\t\t\t}\n'
                       '\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')
            users = [(u, p, content) for u, p, _ in users]
            with patch.object(wrapper, "_localconfigs", return_value=users), \
                 patch.object(wrapper, "_atomic_write", side_effect=write), \
                 patch.object(wrapper, "_record_localconfig") as record:
                wrapper.set_launch_options(tmp, 7940, "x %command%")
            self.assertEqual(len(seen), 2)
            self.assertEqual([c.args[0] for c in record.call_args_list], ["2"])


class ControllerLayoutFailureTests(unittest.TestCase):
    def test_layout_error_is_reported_and_skipped(self):
        msgs = []
        with patch.object(controller_profiles, "_write_layout", side_effect=OSError("disk full")):
            self.assertFalse(controller_profiles._write_layout_safe("1", "7940", prog=msgs.append))
        self.assertTrue(any("not written" in m for m in msgs))

    def test_layout_success_returns_true(self):
        with patch.object(controller_profiles, "_write_layout") as write:
            self.assertTrue(controller_profiles._write_layout_safe("1", "7940", ["n"], False))
        write.assert_called_once_with("1", "7940", ["n"], numbered=False)

    def test_assign_continues_with_later_games_after_a_failure(self):
        calls = []

        def write(uid, appid, names=(), numbered=True):
            calls.append(appid)
            if appid == "7940":
                raise OSError("read-only")
        with patch.object(controller_profiles, "_is_steam_deck", return_value=True), \
             patch.object(controller_profiles, "find_all_steam_uids", return_value=["1"]), \
             patch.object(controller_profiles, "_write_layout", side_effect=write), \
             patch.object(controller_profiles, "_shortcut_appids", return_value={}), \
             patch("os.path.isfile", return_value=True):
            controller_profiles.assign_controller_profiles(on_progress=lambda m: None)
        self.assertEqual(set(controller_profiles.APPID_NAMED_KEYS) - {"7940"} <= set(calls), True)


class ShortcutControllerConfigTests(unittest.TestCase):
    def test_controller_config_failure_does_not_escape(self):
        msgs = []
        with patch.object(shortcut, "_assign_controller_config_impl", side_effect=OSError("denied")):
            shortcut._assign_controller_config("1", 5, {}, "on", msgs.append, "T")
        self.assertTrue(any("not assigned" in m for m in msgs))


class CopytreeAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        (self.src / "drive_c").mkdir(parents=True)
        (self.src / "drive_c" / "a").write_text("a")
        (self.src / "system.reg").write_text("reg")

    def test_success_leaves_only_the_final_directory(self):
        dst = self.root / "pfx"
        ge_proton._copytree_atomic(str(self.src), str(dst))
        self.assertEqual((dst / "system.reg").read_text(), "reg")
        self.assertFalse((self.root / "pfx.deckops_new").exists())

    def test_failed_copy_leaves_no_half_built_pfx(self):
        dst = self.root / "pfx"
        real = ge_proton.shutil.copytree

        def partial(src, d, *a, **k):
            real(src, d, *a, **k)
            raise OSError("disk full")
        with patch.object(ge_proton.shutil, "copytree", side_effect=partial):
            with self.assertRaises(OSError):
                ge_proton._copytree_atomic(str(self.src), str(dst))
        self.assertFalse(dst.exists())
        self.assertFalse((self.root / "pfx.deckops_new").exists())

    def test_leftover_staging_dir_from_a_killed_run_is_replaced(self):
        (self.root / "pfx.deckops_new").mkdir()
        (self.root / "pfx.deckops_new" / "junk").write_text("x")
        ge_proton._copytree_atomic(str(self.src), str(self.root / "pfx"))
        self.assertFalse((self.root / "pfx" / "junk").exists())
        self.assertTrue((self.root / "pfx" / "system.reg").exists())

    def test_existing_destination_is_never_merged_into(self):
        (self.root / "pfx").mkdir()
        with self.assertRaises(FileExistsError):
            ge_proton._copytree_atomic(str(self.src), str(self.root / "pfx"))

    def test_clone_failure_is_false_and_retryable(self):
        dest = self.root / "compat"
        with patch.object(ge_proton.shutil, "copytree", side_effect=OSError("disk full")):
            self.assertFalse(ge_proton._clone_prefix(str(self.src), str(dest), None))
        self.assertFalse((dest / "pfx").exists())
        self.assertTrue(ge_proton._clone_prefix(str(self.src), str(dest), None))
        self.assertTrue(ge_proton._prefix_ready(str(dest / "pfx")))

    def test_symlink_clone_links_shared_dlls_before_pfx_appears(self):
        win = self.src / "drive_c" / "windows"
        (win / "system32").mkdir(parents=True)
        (win / "system32" / "big.dll").write_text("x")
        (win / "syswow64").mkdir()
        dest = self.root / "compat"
        try:
            ok = ge_proton._clone_with_symlinks(str(self.src), str(dest), None)
        except OSError:
            self.skipTest("symlinks unavailable here")
        if not ok:
            self.skipTest("symlinks unavailable here")
        self.assertTrue(os.path.islink(dest / "pfx" / "drive_c" / "windows" / "system32"))
        self.assertFalse((dest / "pfx.deckops_new").exists())


class LanPrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lan = os.path.join(self.tmp.name, "lan")
        self.srv = os.path.join(self.tmp.name, "srv")
        for p in (patch.object(plutonium, "LAN_PREFIX", self.lan),
                  patch.object(plutonium, "LAN_SERVER_PREFIX", self.srv),
                  patch("steam_common.default_steam_root", return_value=self.tmp.name),
                  patch("wrapper.get_proton_path", return_value="/proton"),
                  patch("config.get_ge_proton_version", return_value="GE-Proton10-1")):
            p.start()
            self.addCleanup(p.stop)

    def test_dll_copy_alone_does_not_count_as_a_ready_prefix(self):
        # ensure_prefix_deps creates drive_c/windows/system32 before Proton finalizes
        for p in (self.lan, self.srv):
            os.makedirs(os.path.join(p, "pfx", "drive_c", "windows", "system32"))
        with patch.object(ge_proton, "ensure_prefix_deps", return_value=True) as ensure:
            plutonium._lan_prefixes(lambda m: None)
        self.assertEqual(ensure.call_count, 2)

    def test_ready_prefixes_are_not_rebuilt(self):
        for p in (self.lan, self.srv):
            os.makedirs(os.path.join(p, "pfx", "drive_c"))
            Path(p, "pfx", "system.reg").write_text("reg")
        with patch.object(ge_proton, "ensure_prefix_deps") as ensure:
            plutonium._lan_prefixes(lambda m: None)
        ensure.assert_not_called()

    def test_a_prefix_that_cannot_be_built_fails_the_offline_setup(self):
        with patch.object(ge_proton, "ensure_prefix_deps", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "offline prefix"):
                plutonium._lan_prefixes(lambda m: None)

    def test_setup_lan_reports_failure_for_a_broken_prefix(self):
        msgs = []
        with patch.object(ge_proton, "ensure_prefix_deps", return_value=False), \
             patch("config.get_offline_mode", return_value=True):
            result = plutonium.setup_lan({"install_dir": self.tmp.name}, "t6mp", self.tmp.name,
                                         "steam", msgs.append)
        self.assertIs(result, False)


class BootstrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "plutonium.exe")
        Path(self.dest).write_bytes(b"GOOD")

    @staticmethod
    def _is_exe(path):
        try:
            return Path(path).read_bytes().startswith(b"GOOD")
        except OSError:
            return False

    def test_html_error_page_does_not_replace_a_good_exe(self):
        def download(url, dest, **kw):
            Path(dest).write_bytes(b"<html>503</html>")
        with patch.object(plutonium, "download", side_effect=download), \
             patch.object(plutonium, "is_windows_exe", side_effect=self._is_exe):
            self.assertEqual(plutonium.fetch_bootstrapper(self.dest, keep_existing=True), self.dest)
        self.assertEqual(Path(self.dest).read_bytes(), b"GOOD")
        self.assertEqual(os.listdir(self.tmp.name), ["plutonium.exe"])

    def test_failure_without_keep_existing_raises_but_keeps_the_file(self):
        with patch.object(plutonium, "download", side_effect=IOError("offline")), \
             patch.object(plutonium, "is_windows_exe", side_effect=self._is_exe), \
             patch.object(plutonium.time, "sleep"):
            with self.assertRaises(plutonium.DownloadError):
                plutonium.fetch_bootstrapper(self.dest)
        self.assertEqual(Path(self.dest).read_bytes(), b"GOOD")

    def test_a_valid_download_replaces_the_exe(self):
        def download(url, dest, **kw):
            Path(dest).write_bytes(b"GOOD v2")
        with patch.object(plutonium, "download", side_effect=download), \
             patch.object(plutonium, "is_windows_exe", side_effect=self._is_exe):
            plutonium.fetch_bootstrapper(self.dest)
        self.assertEqual(Path(self.dest).read_bytes(), b"GOOD v2")
        self.assertEqual(os.listdir(self.tmp.name), ["plutonium.exe"])


class Cod4rSwitchOrderTests(unittest.TestCase):
    def _run(self, fail_download):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.dll").write_bytes(b"cod4x a")
            new = b"cod4r a"
            manifest = {"client": {"version": "1", "path": "c.exe", "size": 1, "sha256": "0" * 64},
                        "dedi": {"path": "d.exe", "size": 1, "sha256": "0" * 64},
                        "assets": [{"name": "a.dll", "path": "a.dll", "dest": "root", "size": len(new),
                                    "sha256": hashlib.sha256(new).hexdigest()}]}
            files = [(str(root / "a.dll"), manifest["assets"][0])]

            def download(url, dest, *a, **k):
                if fail_download:
                    raise IOError("offline")
                events.append("download")
                Path(dest).write_bytes(new)

            def before():
                events.append("undo")
                self.assertEqual((root / "a.dll").read_bytes(), b"cod4x a")   # nothing swapped yet

            with patch.object(cod4r, "_fetch_manifest", return_value=manifest), \
                 patch.object(cod4r, "_manifest_files", return_value=files), \
                 patch.object(cod4r, "_download", side_effect=download):
                try:
                    cod4r.sync_files(str(root), "", before_swap=before)
                except IOError:
                    events.append("failed")
            return events, (root / "a.dll").read_bytes()

    def test_cod4x_is_undone_only_after_every_download_succeeded(self):
        events, content = self._run(False)
        self.assertEqual(events, ["download", "undo"])
        self.assertEqual(content, b"cod4r a")

    def test_offline_install_leaves_cod4x_alone(self):
        events, content = self._run(True)
        self.assertEqual(events, ["failed"])          # undo never ran
        self.assertEqual(content, b"cod4x a")

    def test_undo_that_changes_files_gets_them_staged_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new = b"cod4r a"
            (root / "a.dll").write_bytes(new)           # already CoD4R's build ...
            manifest = {"client": {"version": "1", "path": "c.exe", "size": 1, "sha256": "0" * 64},
                        "dedi": {"path": "d.exe", "size": 1, "sha256": "0" * 64},
                        "assets": [{"name": "a.dll", "path": "a.dll", "dest": "root", "size": len(new),
                                    "sha256": hashlib.sha256(new).hexdigest()}]}
            files = [(str(root / "a.dll"), manifest["assets"][0])]

            def download(url, dest, *a, **k):
                Path(dest).write_bytes(new)

            def before():
                (root / "a.dll").write_bytes(b"stock")  # ... until the CoD4x undo restores the stock one
            with patch.object(cod4r, "_fetch_manifest", return_value=manifest), \
                 patch.object(cod4r, "_manifest_files", return_value=files), \
                 patch.object(cod4r, "_download", side_effect=download):
                n = cod4r.sync_files(str(root), "", before_swap=before)
            self.assertEqual(n, 1)
            self.assertEqual((root / "a.dll").read_bytes(), new)


class SaveGroupTests(unittest.TestCase):
    def test_bo2_campaign_saves_are_backed_up_and_restored(self):
        group = save_backup.SAVE_GROUPS["t6sp"]
        self.assertEqual((group["keys"], group["type"], group["subdir"]),
                         (["t6sp"], "install_dir", "players"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "game"
            (game / "players").mkdir(parents=True)
            (game / "players" / "user_sp.cgp").write_text("profile")
            with patch.object(save_backup, "BACKUP_ROOT", str(root / "backup")), \
                 patch.object(save_backup, "_load_config", return_value={"setup_games": {"t6sp": {}}}), \
                 patch.object(save_backup, "_find_install_dir", return_value=str(game)):
                failures = []
                result = save_backup.backup_saves(steam_root=str(root), failures=failures,
                                                  on_progress=lambda m: None)
                self.assertEqual((list(result), failures), (["t6sp"], []))
                (game / "players" / "user_sp.cgp").write_text("lost")
                restored = save_backup.restore_saves(steam_root=str(root))
            self.assertEqual(list(restored), ["t6sp"])
            self.assertEqual((game / "players" / "user_sp.cgp").read_text(), "profile")


class ConfigRobustnessTests(unittest.TestCase):
    def setUp(self):
        import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "deckops.json"
        for p in (patch.object(config, "CONFIG_PATH", str(self.path)),
                  patch.object(config, "_cache", None),
                  patch.object(config, "_cache_key", None)):
            p.start()
            self.addCleanup(p.stop)

    def test_null_dict_settings_fall_back_to_defaults(self):
        self.path.write_text(json.dumps({"setup_games": None, "iw_pad": [1], "player_name": "x"}))
        data = self.config.load()
        self.assertEqual((data["setup_games"], data["iw_pad"], data["player_name"]), ({}, {}, "x"))
        self.config.mark_game_setup("t6zm", "plutonium")        # raised TypeError before
        self.assertIn("t6zm", self.config.get_setup_games())

    def test_a_config_replaced_while_being_read_is_not_moved_aside(self):
        good = json.dumps({"player_name": "kept"})
        self.path.write_text("{truncated")
        real_load = json.load
        calls = []

        def racing_load(f):
            if not calls:                      # a writer lands a good file between our stat and read
                calls.append(1)
                self.path.write_text(good + " " * 7)
                raise ValueError("bad json")
            return real_load(f)
        with patch.object(self.config.json, "load", side_effect=racing_load):
            data = self.config.load()
        self.assertEqual(data["player_name"], "kept")
        self.assertTrue(self.path.exists())
        self.assertEqual([p for p in os.listdir(self.tmp.name) if ".corrupt-" in p], [])

    def test_a_really_corrupt_config_is_still_moved_aside(self):
        self.path.write_text("{truncated")
        self.assertEqual(self.config.load()["player_name"], None)
        self.assertFalse(self.path.exists())
        self.assertEqual(len([p for p in os.listdir(self.tmp.name) if ".corrupt-" in p]), 1)


class LogRedactTests(unittest.TestCase):
    def test_redact_survives_a_set_that_grows_in_another_thread(self):
        import log
        with patch.object(log, "_secrets", set()):
            log.redact_also("alpha-secret")

            class Growing(set):
                def __iter__(self):
                    self.add("late-word")      # a worker thread adding a secret mid-iteration
                    return super().__iter__()
            with patch.object(log, "_secrets", Growing({"alpha\\-secret"})):
                self.assertEqual(log.redact("x alpha-secret y"), "x *** y")

    def test_longest_secret_wins(self):
        import log
        with patch.object(log, "_secrets", {"abc", "abc def"}):
            self.assertEqual(log.redact("my abc def here"), "my *** here")


class NetDigestTests(unittest.TestCase):
    def test_a_bad_digest_fails_before_any_download(self):
        import net
        for bad in ("nocolon", "sha256:", "nosuchalgo:abc"):
            with self.subTest(bad=bad), patch.object(net.urllib.request, "urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    net.download("https://example.invalid/x", os.path.join(tempfile.gettempdir(), "x.bin"),
                                 digest=bad, tries=3)
                urlopen.assert_not_called()

    def test_good_digests_are_accepted(self):
        import net
        for ok in ("sha256:" + "a" * 64, "sha1:" + "b" * 40, "gitblob:" + "c" * 40):
            net._validate_digest(ok)

    def test_replaced_files_are_backed_up_without_a_second_copy_when_links_work(self):
        import net
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            stage.mkdir()
            (root / "a.bin").write_bytes(b"old")
            (stage / "a.bin").write_bytes(b"new")
            links = []
            real_link = os.link

            def spy(src, dst, *a, **k):
                links.append(dst)
                return real_link(src, dst, *a, **k)
            with patch.object(net.os, "link", side_effect=spy):
                net.commit_staged_files(str(root), str(stage), ["a.bin"])
            self.assertEqual((root / "a.bin").read_bytes(), b"new")
            self.assertEqual(len(links), 1)

    def test_commit_rolls_back_with_link_backups(self):
        import net
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            stage.mkdir()
            for n in ("a.bin", "b.bin"):
                (root / n).write_bytes(b"old " + n.encode())
                (stage / n).write_bytes(b"new " + n.encode())
            real = os.replace

            def fail(src, dst):
                if str(dst) == str(root / "b.bin"):
                    raise OSError("boom")
                return real(src, dst)
            with patch.object(net.os, "replace", side_effect=fail):
                with self.assertRaises(OSError):
                    net.commit_staged_files(str(root), str(stage), ["a.bin", "b.bin"])
            self.assertEqual((root / "a.bin").read_bytes(), b"old a.bin")
            self.assertEqual((root / "b.bin").read_bytes(), b"old b.bin")

    def test_commit_falls_back_to_copy_when_links_are_unsupported(self):
        import net
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            stage.mkdir()
            (root / "a.bin").write_bytes(b"old")
            (stage / "a.bin").write_bytes(b"new")
            with patch.object(net.os, "link", side_effect=OSError("exFAT")):
                net.commit_staged_files(str(root), str(stage), ["a.bin"])
            self.assertEqual((root / "a.bin").read_bytes(), b"new")


class FovwatchTests(unittest.TestCase):
    def test_out_of_range_settings_are_rejected(self):
        import fovwatch
        for arg in ("cg_fov=500", "cg_fov=5", "com_maxfps=0", "com_maxfps=99999"):
            with self.subTest(arg=arg), self.assertRaises(ValueError):
                fovwatch.parse_settings([arg])
        self.assertEqual(fovwatch.parse_settings(["cg_fov=90", "com_maxfps=144"]),
                         {"cg_fov": 90.0, "com_maxfps": 144})

    def test_hold_does_not_write_into_a_record_that_changed(self):
        import fovwatch

        class Mem:
            def __init__(self):
                self.data = {}
                self.writes = []

            def read(self, addr, n):
                return self.data.get(addr, b"")[:n]

            def write(self, addr, data):
                self.writes.append(addr)
                return True
        mem = Mem()
        addr = 0x1000 + fovwatch.VALUE_OFFSET
        ident = b"\x01" * 8
        mem.data[0x1000] = ident
        mem.data[addr] = fovwatch._encode("f", 70.0)
        self.assertTrue(fovwatch.hold(mem, addr, "cg_fov", 90.0, ident))
        mem.data[0x1000] = b"\x02" * 8                      # the record was freed and reused
        self.assertIsNone(fovwatch.hold(mem, addr, "cg_fov", 90.0, ident))
        self.assertEqual(mem.writes, [addr])
        self.assertTrue(fovwatch.hold(mem, addr, "cg_fov", 90.0))   # no ident: old behaviour


class PlutoniumUpdateCaseTests(unittest.TestCase):
    def test_upper_case_manifest_hashes_do_not_force_a_redownload(self):
        import plutonium_update
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.bin").write_bytes(b"data")
            h = hashlib.sha1(b"data").hexdigest()
            info = {"revision": 1, "baseUrl": "https://example.invalid/",
                    "files": [{"name": "a.bin", "hash": h.upper()}]}
            with patch.object(plutonium_update, "download") as download:
                n = plutonium_update.sync_master(tmp, {}, info)
            self.assertEqual(n, 0)
            download.assert_not_called()


class SpModStagingTests(unittest.TestCase):
    def _install(self, root, fail_on=None):
        import sp_mod

        def download(url, dest, on_progress=None, label="", **kw):
            if fail_on and dest.endswith(fail_on):
                raise IOError(f"{fail_on} failed")
            Path(dest).write_bytes(b"new " + os.path.basename(dest).encode())
        with patch.object(sp_mod, "cdn_digests", return_value={}), \
             patch.object(sp_mod, "download", side_effect=download), \
             patch.object(sp_mod, "_fetch_iw_pad"):
            sp_mod.install_sp_mod("iw4sp", str(root), source="own")

    def test_a_failed_second_download_leaves_the_first_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "iw4x-sp.exe").write_bytes(b"old client")
            (root / "data" / "iw4sp.exe").write_bytes(b"old exe")
            with self.assertRaisesRegex(IOError, "iw4sp.exe failed"):
                self._install(root, fail_on="iw4sp.exe")
            self.assertEqual((root / "iw4x-sp.exe").read_bytes(), b"old client")
            self.assertEqual((root / "data" / "iw4sp.exe").read_bytes(), b"old exe")
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".deckops_")], [])

    def test_success_replaces_the_whole_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._install(root)
            self.assertEqual((root / "iw4x-sp.exe").read_bytes(), b"new iw4x-sp.exe")
            self.assertEqual((root / "data" / "iw4sp.exe").read_bytes(), b"new iw4sp.exe")
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".deckops_")], [])


class DesktopEntryTests(unittest.TestCase):
    def test_one_unwritable_entry_does_not_skip_the_other(self):
        import update_manifest
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            install = str(home / "DeckOps-Nightly")
            Path(install).mkdir()
            Path(install, "launcher.sh").write_text("#!/bin/bash\n")
            old = f"Exec={install}/.venv/bin/python3 {install}/src/main.py"
            app = home / ".local" / "share" / "applications" / "deckops-nightly.desktop"
            desk = home / "Desktop" / "DeckOps-Nightly.desktop"
            for p in (app, desk):
                p.parent.mkdir(parents=True)
                p.write_text(f"[Desktop Entry]\n{old}\n")
            real = update_manifest.tempfile.mkstemp

            def mkstemp(*a, **k):
                if str(k.get("dir")) == str(app.parent):
                    raise PermissionError("read-only")
                return real(*a, **k)
            with patch.object(update_manifest.tempfile, "mkstemp", side_effect=mkstemp):
                changed = update_manifest.upgrade_desktop_entries(install, str(home))
            self.assertEqual(changed, [str(desk)])
            self.assertIn("launcher.sh", desk.read_text())


try:
    from qt import QApplication
    import ui_install
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class InstallResultTests(unittest.TestCase):
    def _screen(self, *rows):
        return SimpleNamespace(_results=[{"key": k, "name": k, "status": s, "reason": ""} for k, s in rows])

    def test_ok_games_are_downgraded_when_a_later_step_fails(self):
        screen = self._screen(("a", "ok"), ("b", "ok"), ("c", "failed"), ("d", "skipped"))
        screen._results[2]["reason"] = "download"
        ui_install._BaseInstallScreen._fail_ok(screen, ["a", "c", "d"], "Steam shortcut not created")
        status = {r["key"]: (r["status"], r["reason"]) for r in screen._results}
        self.assertEqual(status["a"], ("failed", "Steam shortcut not created"))
        self.assertEqual(status["b"], ("ok", ""))
        self.assertEqual(status["c"], ("failed", "download"))      # keeps its own reason
        self.assertEqual(status["d"], ("skipped", ""))


if __name__ == "__main__":
    unittest.main()
