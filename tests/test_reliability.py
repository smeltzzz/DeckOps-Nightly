"""Regressions for update and repair paths that must never report false success."""

import hashlib
import json
import os
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

_QAPP = []


def _qapp(QApplication):
    """One QApplication for the whole run. A test-local one could be garbage
    collected when the test's helper returned, deleting every widget with it."""
    if not _QAPP:
        _QAPP.append(QApplication.instance() or QApplication([]))
    return _QAPP[0]


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


class ObsoleteFileTests(unittest.TestCase):
    """A full-archive update or repair must not leave files the new version dropped."""
    SHA = "b" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = Path(self.tmp.name, "DeckOps")
        self.stage = self.install / ".update" / "files"
        for folder in ("src", "assets/music", "logs", ".venv/bin"):
            (self.install / folder).mkdir(parents=True)
        (self.stage / "src").mkdir(parents=True)
        (self.install / "VERSION").write_text("old-version")
        (self.install / "deckops.json").write_text("my settings")
        (self.install / "logs" / "install.log").write_text("log")
        (self.install / "assets" / "music" / "background.mp3").write_bytes(b"music")
        (self.install / ".venv" / "bin" / "python3").write_text("venv")
        (self.install / "notes.txt").write_text("player's own file")
        (self.install / "src" / "main.py").write_text("print('old')")
        (self.install / "src" / "retired.py").write_text("RETIRED = True")
        (self.install / "deckops_identity.sh").write_text("old")
        (self.stage / "src" / "main.py").write_text("print('new')")
        (self.stage / "deckops_identity.sh").write_text("new")
        # What the installed version shipped. Hostile entries must be ignored.
        self.previous = ["src/main.py", "src/retired.py", "src/never_installed.py",
                         "deckops_identity.sh", "deckops.json", "VERSION", "logs/install.log",
                         "assets/music/background.mp3", ".update/files/src/main.py",
                         "../outside", "/etc/passwd", "src/../../outside"]

    def test_full_update_removes_only_files_the_old_version_shipped(self):
        update_manifest.apply_update(self.install, self.stage, self.SHA, full=True,
                                     previous_files=self.previous)
        self.assertFalse((self.install / "src" / "retired.py").exists())
        self.assertEqual((self.install / "src" / "main.py").read_text(), "print('new')")
        for kept in ("deckops.json", "notes.txt", "logs/install.log",
                     "assets/music/background.mp3", ".venv/bin/python3"):
            self.assertTrue((self.install / kept).exists(), kept)
        self.assertEqual((self.install / "VERSION").read_text(), self.SHA + "\n")

    def test_without_an_old_file_list_nothing_is_removed(self):
        update_manifest.apply_update(self.install, self.stage, self.SHA, full=True)
        self.assertTrue((self.install / "src" / "retired.py").exists())

    def test_removed_files_come_back_when_the_update_is_rolled_back(self):
        original = update_manifest._replace_file

        def fail(source, target):
            if source == self.stage / "src" / "main.py":
                raise OSError("disk full")
            original(source, target)

        with patch.object(update_manifest, "_replace_file", side_effect=fail):
            with self.assertRaises(OSError):
                update_manifest.apply_update(self.install, self.stage, self.SHA, full=True,
                                             previous_files=self.previous)
        self.assertEqual((self.install / "src" / "retired.py").read_text(), "RETIRED = True")
        self.assertEqual((self.install / "VERSION").read_text(), "old-version")

    def test_interrupted_update_that_removed_files_recovers(self):
        original = update_manifest._replace_file

        def interrupted(source, target):
            if source == self.stage / "src" / "main.py":
                raise SystemExit("power loss")
            original(source, target)

        with patch.object(update_manifest, "_replace_file", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                update_manifest.apply_update(self.install, self.stage, self.SHA, full=True,
                                             previous_files=self.previous)
        self.assertTrue(update_manifest.recover_update(self.install))
        self.assertEqual((self.install / "src" / "retired.py").read_text(), "RETIRED = True")

    def test_symlinked_obsolete_entry_is_left_alone(self):
        outside = Path(self.tmp.name, "outside.txt")
        outside.write_text("not DeckOps")
        link = self.install / "src" / "linked.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        update_manifest.apply_update(self.install, self.stage, self.SHA, full=True,
                                     previous_files=self.previous + ["src/linked.py"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), "not DeckOps")

    def test_git_tree_must_be_complete(self):
        data = {"tree": [{"type": "blob", "path": "src/a.py"}, {"type": "tree", "path": "src"}]}
        self.assertEqual(update_manifest.tree_paths(data), ["src/a.py"])
        with self.assertRaises(ValueError):
            update_manifest.tree_paths({**data, "truncated": True})
        with self.assertRaises(ValueError):
            update_manifest.tree_paths({})

    def test_prune_for_repair_installs(self):
        # install.sh copies files over the install, then prunes. A broken
        # archive must never delete anything.
        archive = Path(self.tmp.name, "archive")
        (archive / "src").mkdir(parents=True)
        with self.assertRaises(ValueError):
            update_manifest.prune_obsolete(self.install, archive, self.previous)
        self.assertTrue((self.install / "src" / "retired.py").exists())
        (archive / "src" / "main.py").write_text("print('new')")
        with self.assertRaises(ValueError):          # still incomplete
            update_manifest.prune_obsolete(self.install, archive, self.previous)
        (archive / "deckops_identity.sh").write_text("new")
        removed = update_manifest.prune_obsolete(self.install, archive, self.previous)
        self.assertEqual(removed, ["src/retired.py"])
        self.assertTrue((self.install / "notes.txt").exists())

    def test_launcher_commands_tree_prune_and_apply(self):
        lst = Path(self.tmp.name, "previous.txt")
        lst.write_text("\n".join(self.previous) + "\n")
        tree = subprocess.run([sys.executable, update_manifest.__file__, "tree"],
                              input='{"tree": [{"type": "blob", "path": "src/retired.py"}]}',
                              check=True, capture_output=True, text=True)
        self.assertEqual(tree.stdout.split(), ["src/retired.py"])
        subprocess.run([sys.executable, update_manifest.__file__, "apply", str(self.install),
                        str(self.stage), self.SHA, "full", str(lst)],
                       check=True, capture_output=True, text=True)
        self.assertFalse((self.install / "src" / "retired.py").exists())
        self.assertEqual((self.install / "deckops.json").read_text(), "my settings")


class GeProtonFallbackTests(unittest.TestCase):
    """A failed GE-Proton download must not leave games without a compat tool."""

    def _tool(self, root, name):
        tool = Path(root, name)
        (tool / "files" / "share" / "default_pfx").mkdir(parents=True)
        (tool / "proton").write_text("")
        (tool / "compatibilitytool.vdf").write_text("")

    def test_falls_back_to_pinned_then_newest_complete_install(self):
        import ge_proton
        with tempfile.TemporaryDirectory() as tmp, patch.object(ge_proton, "COMPAT_DIR", tmp):
            self.assertIsNone(ge_proton.resolve_ge_version())
            self._tool(tmp, "GE-Proton10-9")
            self._tool(tmp, "GE-Proton10-30")
            Path(tmp, "GE-Proton10-99").mkdir()          # half-extracted: ignored
            self.assertEqual(ge_proton.resolve_ge_version(), "GE-Proton10-30")
            with patch("config.get_ge_proton_version", return_value="GE-Proton10-9"):
                self.assertEqual(ge_proton.resolve_ge_version(), "GE-Proton10-9")
            self.assertEqual(ge_proton.resolve_ge_version("GE-Proton11-1"), "GE-Proton11-1")


class SaveBackupFailureTests(unittest.TestCase):
    def test_skipped_group_is_reported_not_called_complete(self):
        import save_backup as sb
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp, "game")
            (game / "players").mkdir(parents=True)
            (game / "players" / "profile.cfg").write_text("stats")
            backup = Path(tmp, "backup")
            config = {"setup_games": {"cod4mp": {}}}
            with patch.object(sb, "BACKUP_ROOT", str(backup)), \
                 patch.object(sb, "_load_config", return_value=config), \
                 patch.object(sb, "_find_install_dir", return_value=str(game)):
                failures = []
                with patch.object(sb, "_has_room", return_value=False):
                    self.assertEqual(sb.backup_saves(steam_root=tmp, failures=failures), {})
                self.assertEqual(failures, ["cod4"])
                failures = []
                self.assertEqual(list(sb.backup_saves(steam_root=tmp, failures=failures)), ["cod4"])
                self.assertEqual(failures, [])
                self.assertEqual((backup / "cod4" / "profile.cfg").read_text(), "stats")


class SaveBackupWithoutSettingsTests(unittest.TestCase):
    """No readable deckops.json must never be reported as a complete backup:
    the uninstaller would then offer to delete Plutonium player data."""

    def _cli(self, config_text):
        import save_backup as sb
        with tempfile.TemporaryDirectory() as home:
            install = Path(home, "DeckOps-Nightly")
            install.mkdir()
            if config_text is not None:
                (install / "deckops.json").write_text(config_text)
            env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONIOENCODING="utf-8")
            return subprocess.run([sys.executable, sb.__file__, "backup", home],
                                  cwd=str(Path(sb.__file__).parent), env=env,
                                  capture_output=True, text=True)

    def test_missing_or_broken_settings_fail_the_backup(self):
        for text in (None, '{"setup_games": {"t6mp"', "[1, 2]", ""):
            with self.subTest(config=text):
                r = self._cli(text)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("NOT backed up: settings", r.stdout)

    def test_readable_settings_without_games_is_fine(self):
        r = self._cli('{"setup_games": {}}')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("No saves to back up", r.stdout)

    def test_message_explains_unreadable_settings(self):
        import save_backup as sb
        self.assertIn("could not tell which games", sb.describe_backup({}, ["settings"]))


class T6spConsistentPairTests(unittest.TestCase):
    def test_exe_and_dll_always_come_from_the_same_release(self):
        import t6sp_mod
        latest = "https://github.com/Rattpak/T6SP-Mod-Release/releases/download/v2.0/"
        cases = [
            ([(latest + "T6SP-Mod.exe", "sha256:a"), (t6sp_mod._PIN + "t6sp-mod.dll", None)], t6sp_mod._PIN),
            ([(t6sp_mod._PIN + "T6SP-Mod.exe", None), (latest + "t6sp-mod.dll", "sha256:b")], t6sp_mod._PIN),
            ([(latest + "T6SP-Mod.exe", "sha256:a"), (latest + "t6sp-mod.dll", "sha256:b")], latest),
        ]
        for lookups, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as game:
                urls = []

                def fake_download(url, dest, *a, **kw):
                    urls.append(url)
                    Path(dest).write_bytes(b"x")
                with patch.object(t6sp_mod, "github_asset", side_effect=lookups), \
                     patch.object(t6sp_mod, "_download", side_effect=fake_download):
                    t6sp_mod.install_t6sp_mod({"install_dir": game}, game, "", "", source="own")
                self.assertTrue(all(u.startswith(expected) for u in urls), urls)


class InstallStopsWhenSteamStaysOpenTests(unittest.TestCase):
    def test_no_steam_config_written_if_steam_cannot_be_closed(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PyQt6 not installed")
        app = _qapp(QApplication)
        import ui_install
        screen = ui_install.InstallScreen(None)
        screen.steam_selected, screen.own_selected = [], {}
        screen.steam_root = tempfile.gettempdir()
        screen.offline_mode = None
        done, logs = [], []
        screen._s.done.connect(done.append)
        screen._s.log.connect(logs.append)
        with patch.object(ui_install, "_log_to_file", create=True), \
             patch.object(ui_install.cfg, "get_offline_mode", return_value=False), \
             patch("wrapper.kill_steam", side_effect=TimeoutError("Steam did not exit")), \
             patch("wrapper.get_proton_path", return_value="proton"), \
             patch("wrapper.clear_launch_options") as clear_lo, \
             patch("wrapper.clear_compat_tool") as clear_ct, \
             patch("wrapper.set_compat_tool") as set_ct, \
             patch("ge_proton.install_ge_proton", return_value="GE-Proton10-1"), \
             patch.object(ui_install.cfg, "set_ge_proton_version"):
            screen._run()
        app.processEvents()
        self.assertEqual(done, [False])
        clear_lo.assert_not_called(); clear_ct.assert_not_called(); set_ct.assert_not_called()
        self.assertTrue(any("Steam's settings were not changed" in m for m in logs), logs[-3:])


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


class PlutoniumLeakCleanupTests(unittest.TestCase):
    """Update Games' leaked-Plutonium cleanup must only remove DeckOps's folders."""

    def setUp(self):
        import plutonium
        self.plut = plutonium
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.master = self.root / "master" / "Plutonium"
        for sub in ("bin", "launcher", "games"):
            (self.master / sub).mkdir(parents=True)
        probe = self.root / "probe"
        try:
            probe.symlink_to(self.master)
        except OSError:
            self.skipTest("symlinks unavailable here")
        for p in (patch.object(plutonium, "get_dedicated_plut_dir", return_value=str(self.master)),
                  patch.object(plutonium, "SHARED_PLUT_DIR", str(self.root / "no-shared"))):
            p.start()
            self.addCleanup(p.stop)
        # CoD4 (7940) never runs Plutonium
        self.dir = Path(plutonium._plut_dir_in_compatdata(str(self.root), 7940))
        self.dir.mkdir(parents=True)

    def _clean(self):
        return self.plut.cleanup_plut_from_non_plut_prefixes(str(self.root))

    def test_deckops_leak_without_user_files_is_removed(self):
        (self.dir / "bin").symlink_to(self.master / "bin")
        (self.dir / "storage" / "t6").mkdir(parents=True)
        (self.dir / "storage" / "t6" / "x.ff").symlink_to(self.master / "bin")
        self.assertEqual(self._clean(), 1)
        self.assertFalse(self.dir.exists())

    def test_deckops_leak_keeps_real_storage_files(self):
        (self.dir / "bin").symlink_to(self.master / "bin")
        (self.dir / "config.json").write_text("{}")
        stats = self.dir / "storage" / "t6" / "players" / "stats.cfg"
        stats.parent.mkdir(parents=True)
        stats.write_text("prestige 10")
        self.assertEqual(self._clean(), 1)
        self.assertEqual(stats.read_text(), "prestige 10")
        self.assertFalse(os.path.lexists(self.dir / "bin"))
        self.assertFalse((self.dir / "config.json").exists())
        self.assertTrue(self.master.joinpath("bin").is_dir())      # the link target survives

    def test_unidentified_plutonium_is_left_alone(self):
        stats = self.dir / "storage" / "t6" / "players" / "stats.cfg"
        stats.parent.mkdir(parents=True)
        stats.write_text("mine")
        (self.dir / "bin").mkdir()
        other = self.root / "other" / "bin"
        other.mkdir(parents=True)
        (self.dir / "launcher").symlink_to(other)
        self.assertEqual(self._clean(), 0)
        self.assertEqual(stats.read_text(), "mine")
        self.assertTrue((self.dir / "bin").is_dir())


class DesktopIconUpgradeTests(unittest.TestCase):
    """Old icons ran the venv's python directly and could not repair it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.install = str(self.home / "DeckOps-Nightly")
        Path(self.install).mkdir()
        Path(self.install, "launcher.sh").write_text("#!/bin/bash\n")
        self.old = f"Exec={self.install}/.venv/bin/python3 {self.install}/src/main.py"
        self.app = self.home / ".local" / "share" / "applications" / "deckops-nightly.desktop"
        self.desk = self.home / "Desktop" / "DeckOps-Nightly.desktop"
        for p in (self.app, self.desk):
            p.parent.mkdir(parents=True)
            p.write_text(f"[Desktop Entry]\nName=DeckOps Nightly\n{self.old}\nTerminal=false\n")

    def test_old_icons_open_through_the_launcher(self):
        changed = update_manifest.upgrade_desktop_entries(self.install, str(self.home))
        self.assertEqual(sorted(changed), sorted([str(self.app), str(self.desk)]))
        for p in (self.app, self.desk):
            text = p.read_text()
            self.assertIn(f'Exec=bash "{self.install}/launcher.sh" --desktop\n', text)
            self.assertNotIn("main.py", text)
            self.assertIn("Name=DeckOps Nightly\n", text)
        self.assertEqual(update_manifest.upgrade_desktop_entries(self.install, str(self.home)), [])

    def test_edited_icon_or_missing_launcher_is_left_alone(self):
        Path(self.install, "launcher.sh").unlink()
        self.assertEqual(update_manifest.upgrade_desktop_entries(self.install, str(self.home)), [])
        self.assertIn(self.old, self.app.read_text())
        Path(self.install, "launcher.sh").write_text("#!/bin/bash\n")
        self.desk.write_text("[Desktop Entry]\nExec=my-own-command\n")
        self.assertEqual(update_manifest.upgrade_desktop_entries(self.install, str(self.home)), [str(self.app)])
        self.assertEqual(self.desk.read_text(), "[Desktop Entry]\nExec=my-own-command\n")


_LOCALCONFIG = '''"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"10190"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"MINE=1 \\"quoted\\" %command%"
\t\t\t\t\t}
\t\t\t\t\t"42690"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"other %command%"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
'''


class _FakeResponse:
    def __init__(self, data): self.data = data
    def read(self): return json.dumps(self.data).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _http_error(code):
    import urllib.error
    return urllib.error.HTTPError("https://api.github.com/x", code, "rate limit exceeded", {}, None)


class UpdateNoticeAndRateLimitTests(unittest.TestCase):
    def _ui(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PyQt6 not installed")
        _qapp(QApplication)
        import ui_manage
        return ui_manage

    def test_quick_check_compares_one_request_with_version(self):
        ui = self._ui()
        with tempfile.TemporaryDirectory() as root:
            Path(root, "VERSION").write_text("a" * 40 + "\n")
            calls = []

            def opener(req, timeout=0):
                calls.append(req.full_url)
                return _FakeResponse({"sha": sha})
            with patch.object(ui, "PROJECT_ROOT", root), patch("urllib.request.urlopen", side_effect=opener):
                sha = "a" * 40
                self.assertIs(ui._quick_update_check(), False)
                sha = "b" * 40
                self.assertIs(ui._quick_update_check(), True)
                sha = "not-a-sha"
                self.assertIsNone(ui._quick_update_check())
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(u.endswith("/commits/main") for u in calls))
            with patch.object(ui, "PROJECT_ROOT", root), \
                 patch("urllib.request.urlopen", side_effect=_http_error(403)):
                self.assertIsNone(ui._quick_update_check())

    def test_startup_notice_shown_once_and_never_over_a_running_check(self):
        ui = self._ui()
        screen = ui.ManagementScreen(None)
        screen._on_startup_update_check(False)
        self.assertEqual(screen._deckops_update_status.text(), "")
        screen._on_startup_update_check(True)
        self.assertIn("update is available", screen._deckops_update_status.text())
        screen._deckops_update_status.setText("Checking...")
        screen._on_startup_update_check(True)
        self.assertEqual(screen._deckops_update_status.text(), "Checking...")

    def test_rate_limit_is_named_not_blamed_on_the_connection(self):
        ui = self._ui()
        for code in (403, 429):
            with patch.object(ui, "_remote_changes", side_effect=_http_error(code)):
                msg = ui._check_update_status()
            self.assertTrue(msg.startswith("FAIL|") and "limiting requests" in msg, msg)
        with patch.object(ui, "_remote_changes", side_effect=OSError("Network is unreachable")):
            self.assertIn("internet connection", ui._check_update_status())

    def test_release_lookups_are_reused_within_a_session(self):
        import net
        calls = []

        release = {"assets": [{"name": "a.exe", "browser_download_url": "u1", "digest": "d1"},
                              {"name": "b.dll", "browser_download_url": "u2", "digest": "d2"}]}

        def opener(req, timeout=0):
            calls.append(req.full_url)
            return _FakeResponse(release)
        with patch.dict(net._LATEST_RELEASES, clear=True), patch("urllib.request.urlopen", side_effect=opener):
            self.assertEqual(net.github_asset("o/r", "a.exe"), ("u1", "d1"))
            self.assertEqual(net.github_asset("o/r", "b.dll"), ("u2", "d2"))
        self.assertEqual(len(calls), 1)
        with patch.dict(net._LATEST_RELEASES, clear=True), \
             patch("urllib.request.urlopen", side_effect=[_http_error(403), _FakeResponse(release)]) as op:
            self.assertEqual(net.github_asset("o/r", "a.exe", fallback="pin"), ("pin", None))
            self.assertEqual(net.github_asset("o/r", "a.exe", fallback="pin"), ("u1", "d1"))   # failures aren't kept
            self.assertEqual(op.call_count, 2)


class SettingsFileSafetyTests(unittest.TestCase):
    def test_settings_save_is_flushed_before_it_replaces_the_file(self):
        from steam_common import write_json
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "deckops.json")
            path.write_text('{"setup_games": {"t6mp": {}}}')
            with patch("os.fsync", side_effect=OSError(5, "I/O error")):
                with self.assertRaises(OSError):
                    write_json(str(path), {"setup_games": {}})
            self.assertEqual(json.loads(path.read_text()), {"setup_games": {"t6mp": {}}})
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])

    def test_damaged_settings_are_reported_once_and_kept(self):
        import config
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "deckops.json")
            Path(path).write_text('{"setup_games": {"t6')
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "_cache", None):
                self.assertEqual(config.load().get("setup_games"), config.DEFAULTS.get("setup_games"))
                damaged = config.unnoticed_corrupt_files()
                self.assertEqual(len(damaged), 1)
                self.assertEqual(Path(damaged[0]).read_text(), '{"setup_games": {"t6')
                config.mark_corrupt_noticed(damaged)
                self.assertEqual(config.unnoticed_corrupt_files(), [])
                self.assertTrue(os.path.isfile(damaged[0] + ".noticed"))


class DamagedSettingsNoticeWiringTests(unittest.TestCase):
    def test_startup_tells_the_player_then_continues_to_setup(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PyQt6 not installed")
        _qapp(QApplication)
        import ui_qt
        screen = ui_qt.BootstrapScreen(None)
        with patch.object(ui_qt, "go_to") as go, patch.object(ui_qt, "_load_font"), \
             patch.object(ui_qt.cfg, "is_first_run", return_value=True), \
             patch.object(ui_qt.cfg, "unnoticed_corrupt_files", return_value=["/x/deckops.json.corrupt-1"]), \
             patch.object(ui_qt.cfg, "mark_corrupt_noticed") as mark, \
             patch.object(ui_qt.QMessageBox, "warning") as warn:
            screen._proceed()
        self.assertIn("/x/deckops.json.corrupt-1", warn.call_args[0][2])
        mark.assert_called_once_with(["/x/deckops.json.corrupt-1"])
        self.assertEqual(go.call_args[0][1], "SetupFlowScreen")


class SetupCheckTests(unittest.TestCase):
    """Check My Setup: read-only, and only reports real problems."""

    CONFIG = ('"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
              '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n%s\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')
    MAP = '\t\t\t\t\t"%s"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"%s"\n\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t}\n'
    LOCAL = ('"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
             '\t\t\t\t"apps"\n\t\t\t\t{\n%s\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')
    APP = '\t\t\t\t\t"%s"\n\t\t\t\t\t{\n\t\t\t\t\t\t"LaunchOptions"\t\t"%s"\n\t\t\t\t\t}\n'

    def setUp(self):
        import setup_check
        self.sc = setup_check
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "Steam")
        self.inst = Path(self.tmp.name, "DeckOps-Nightly")
        self.inst.mkdir()
        (self.inst / "prelaunch.sh").write_text("#!/bin/bash\n")
        p = patch("identity.INSTALL_DIR", self.inst.as_posix())
        p.start(); self.addCleanup(p.stop)
        (self.root / "config").mkdir(parents=True)
        (self.root / "userdata" / "1" / "config").mkdir(parents=True)
        (self.root / "compatibilitytools.d" / "GE-Proton10-1").mkdir(parents=True)
        self.mw2 = Path(self.tmp.name, "games", "MW2")
        self.mw2.mkdir(parents=True)
        (self.mw2 / "iw4x-launcher.exe").write_text("x")
        self.setup = {"iw4mp": {"client": "iw4x", "source": "steam"}}
        self.installed = {"iw4mp": {"install_dir": str(self.mw2)}}
        self.write(tool="GE-Proton10-1",
                   option='DECKOPS_MENU=\\"x\\" bash \\"%s/prelaunch.sh\\" menu %%command%%' % self.inst.as_posix())

    def write(self, tool=None, option="", appid="10190"):
        maps = self.MAP % (appid, tool) if tool is not None else ""
        (self.root / "config" / "config.vdf").write_text(self.CONFIG % maps)
        (self.root / "userdata" / "1" / "config" / "localconfig.vdf").write_text(
            self.LOCAL % (self.APP % (appid, option)))

    def check(self):
        [r] = self.sc.check_setup(str(self.root), self.installed, self.setup)
        return r

    def test_healthy_game_has_no_problems(self):
        r = self.check()
        self.assertEqual(r["problems"], [])
        self.assertFalse(r["fixable"])

    def test_missing_launch_option_is_reported_and_fixable(self):
        self.write(tool="GE-Proton10-1", option="")
        r = self.check()
        self.assertTrue(any("launch option is missing" in p for p in r["problems"]), r)
        self.assertTrue(r["fixable"])

    def test_launch_option_calling_a_missing_deckops_file(self):
        (self.inst / "prelaunch.sh").unlink()
        r = self.check()
        self.assertTrue(any("no longer exists" in p and "prelaunch.sh" in p for p in r["problems"]), r)

    def test_ge_proton_mapping(self):
        opt = 'bash \\"%s/prelaunch.sh\\" menu %%command%%' % self.inst.as_posix()
        self.write(tool="GE-Proton9-99", option=opt)
        self.assertTrue(any("not installed" in p for p in self.check()["problems"]))
        self.write(tool=None, option=opt)
        self.assertTrue(any("GE-Proton" in p for p in self.check()["problems"]))
        self.write(tool="proton_9", option=opt)            # the player's own choice
        self.assertEqual(self.check()["problems"], [])

    def test_missing_mod_files_and_missing_folder(self):
        (self.mw2 / "iw4x-launcher.exe").unlink()
        r = self.check()
        self.assertTrue(any("iw4x files are missing" in p for p in r["problems"]) and r["fixable"], r)
        self.installed = {}
        r = self.check()
        self.assertTrue(any("Game folder not found" in p for p in r["problems"]), r)
        self.assertFalse(r["fixable"])

    def test_cod4x_alone_needs_no_launch_option(self):
        cod4 = Path(self.tmp.name, "games", "CoD4"); cod4.mkdir()
        (cod4 / "deckops_cod4x.json").write_text("{}")
        self.installed = {"cod4mp": {"install_dir": str(cod4)}}
        self.setup = {"cod4mp": {"client": "cod4x", "source": "steam"}}
        self.write(tool="GE-Proton10-1", option="", appid="7940")
        self.assertEqual(self.check()["problems"], [])
        self.setup = {"cod4mp": {"client": "cod4r", "source": "steam"}}
        (cod4 / "deckops_cod4r.json").write_text("{}")
        self.assertTrue(any("launch option is missing" in p for p in self.check()["problems"]))

    def test_own_copy_skips_steam_checks(self):
        self.setup = {"iw4mp": {"client": "iw4x", "source": "own"}}
        self.write(tool=None, option="")
        self.assertEqual(self.check()["problems"], [])

    def test_check_writes_nothing(self):
        files = sorted(self.root.rglob("*"))
        before = {p: p.read_bytes() for p in files if p.is_file()}
        self.write(tool="GE-Proton9-99", option="")
        before = {p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}
        self.check()
        after = {p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)

    def test_report_window_and_fix_button(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication, QPushButton
        except ImportError:
            self.skipTest("PyQt6 not installed")
        app = _qapp(QApplication)
        import ui_manage
        screen = ui_manage.ConfigureScreen(None)
        mgmt = type("M", (), {})()
        mgmt.installed = {}
        mgmt._update = unittest.mock.MagicMock()
        results = [{"key": "iw4mp", "name": "MW2 MP", "problems": ["Its Steam launch option is missing."], "fixable": True},
                   {"key": "t6mp", "name": "BO2 MP", "problems": [], "fixable": False}]
        with patch.object(ui_manage, "get_screen", return_value=mgmt), \
             patch.object(ui_manage.QDialog, "exec", return_value=0):
            screen._show_setup_report(results, self.installed)
            fixes = [b for b in screen._setup_dialog.findChildren(QPushButton) if b.text() == "Fix"]
            self.assertEqual(len(fixes), 1)
            fixes[0].click()
        app.processEvents()
        gd, keys = mgmt._update.call_args[0]
        self.assertEqual(keys, ["iw4mp"])
        self.assertIn("iw4mp", gd["keys"])


class SteamFileSafetyTests(unittest.TestCase):
    """Steam's files are replaced all at once, checked before writing, keep
    bytes that aren't UTF-8, and keep the player's original copy."""

    def setUp(self):
        import wrapper
        self.w = wrapper
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.vdf = Path(self.root, "userdata", "123", "config", "localconfig.vdf")
        self.vdf.parent.mkdir(parents=True)
        self.vdf.write_bytes(_LOCALCONFIG.encode())
        self.ledger = Path(self.root, "ledger.json")
        p = patch.object(wrapper, "LEDGER_PATH", str(self.ledger))
        p.start()
        self.addCleanup(p.stop)

    def _leftovers(self, folder):
        return [n for n in os.listdir(folder) if n.endswith(".tmp")]

    def test_disk_error_mid_write_leaves_localconfig_untouched(self):
        before = self.vdf.read_bytes()
        with patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            try:
                self.w.set_launch_options(self.root, "10190", "new %command%")
            except OSError:
                pass
        self.assertEqual(self.vdf.read_bytes(), before)
        self.assertEqual(self._leftovers(self.vdf.parent), [])

    def test_disk_error_mid_write_leaves_shortcuts_untouched(self):
        import shortcut
        path = Path(self.root, "shortcuts.vdf")
        before = b"\x00shortcuts\x00" + b"\x000\x00\x01AppName\x00Mine\x00\x08" + b"\x08\x08"
        path.write_bytes(before)
        raw = shortcut._read_shortcuts_raw(str(path))
        with patch("os.fsync", side_effect=OSError(28, "No space left on device")):
            try:
                shortcut._write_shortcuts_vdf(str(path), raw, [b"\x001\x00\x01AppName\x00New\x00\x08"])
            except OSError:
                pass
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self._leftovers(self.root), [])

    def test_bytes_that_are_not_utf8_survive_an_edit(self):
        data = self.vdf.read_bytes().replace(b'"other %command%"', b'"caf\xe9 %command%"')
        self.vdf.write_bytes(data)
        self.w.set_launch_options(self.root, "10190", "new %command%")
        out = self.vdf.read_bytes()
        self.assertIn(b'"caf\xe9 %command%"', out)
        self.assertIn(b"new %command%", out)

    def test_value_ending_in_backslash_does_not_block_the_edit(self):
        data = self.vdf.read_text().replace('"other %command%"', '"Z:\\\\games\\\\"')
        self.vdf.write_text(data)
        self.w.set_launch_options(self.root, "10190", "new %command%")
        self.assertIn("new %command%", self.vdf.read_text())

    def test_unbalanced_edit_is_never_written_or_recorded(self):
        before = self.vdf.read_bytes()
        self.assertFalse(self.w._write_and_validate_vdf(str(self.vdf), '"a" { "b" { }', errors="surrogateescape"))
        self.assertEqual(self.vdf.read_bytes(), before)
        with patch.object(self.w, "_vdf_balanced", create=True, return_value=False):
            self.w.set_launch_options(self.root, "10190", "new %command%")
        self.assertEqual(self.vdf.read_bytes(), before)
        self.assertNotIn("new %command%", self.ledger.read_text() if self.ledger.exists() else "")

    def test_players_original_is_kept_once(self):
        original = self.vdf.read_bytes()
        self.w.set_launch_options(self.root, "10190", "first %command%")
        after_first = self.vdf.read_bytes()
        self.w.clear_launch_options(self.root, "10190")
        orig = Path(str(self.vdf) + ".deckops-original")
        self.assertEqual(orig.read_bytes(), original)
        self.assertEqual(Path(str(self.vdf) + ".bak").read_bytes(), after_first)


class LaunchOptionRestoreTests(unittest.TestCase):
    """A game that fails during Update Games must keep its launch options."""

    def setUp(self):
        import wrapper
        self.wrapper = wrapper
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.vdf = Path(self.root, "userdata", "123", "config", "localconfig.vdf")
        self.vdf.parent.mkdir(parents=True)
        self.vdf.write_text(_LOCALCONFIG)
        ledger = patch.object(wrapper, "LEDGER_PATH", str(Path(self.root, "ledger.json")))
        ledger.start()
        self.addCleanup(ledger.stop)

    def _options(self, appid):
        import wrapper
        content = self.vdf.read_text()
        found = wrapper._flat_values(content, wrapper._app_block(content, appid), "LaunchOptions")
        return content[found[0][1]:found[0][2]] if found else None

    def test_snapshot_and_restore_round_trip(self):
        w = self.wrapper
        snap = w.snapshot_launch_options(self.root, ["10190", "42690", "7940"])
        self.assertEqual(snap, {"10190": {"123": 'MINE=1 \\"quoted\\" %command%'},
                                "42690": {"123": "other %command%"}})
        w.clear_launch_options(self.root, "10190")
        self.assertEqual(self._options("10190"), "")
        self.assertEqual(w.restore_launch_options(self.root, snap, "10190"), ["123"])
        self.assertEqual(self._options("10190"), 'MINE=1 \\"quoted\\" %command%')

    def test_restore_never_overwrites_a_newer_value(self):
        w = self.wrapper
        snap = w.snapshot_launch_options(self.root, ["10190"])
        w.clear_launch_options(self.root, "10190")
        w.set_launch_options(self.root, "10190", "new %command%")
        self.assertEqual(w.restore_launch_options(self.root, snap, "10190"), [])
        self.assertEqual(self._options("10190"), "new %command%")

    def _run_update_games(self, backup=None, confirm=None):
        """Run UpdateScreen's real Update Games steps for MW2 MP with Steam,
        downloads and the MW2 installer (which fails) faked. Returns
        (screen, order of the backup / Steam-close / install steps)."""
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PyQt6 not installed")
        app = _qapp(QApplication)
        import ui_manage
        from ui_constants import ALL_GAMES
        gd = next(g for g in ALL_GAMES if "iw4mp" in g.get("keys", []))
        screen = ui_manage.UpdateScreen(None)
        screen.steam_root = self.root
        screen.selected = [("iw4mp", gd, {"install_dir": str(Path(self.root, "mw2")), "source": "steam"})]
        order = []

        def fake_backup(*a, failures=None, **kw):
            order.append("backup")
            return backup(failures) if backup else {}

        def fake_install(*a, **kw):
            order.append("install")
            raise RuntimeError("download failed")
        with patch.object(ui_manage, "_log_to_file"), \
             patch.object(ui_manage, "inhibit"), \
             patch.object(ui_manage.cfg, "is_lcd", return_value=False), \
             patch.object(ui_manage.cfg, "get_setup_games", return_value={"iw4mp": {"client": "iw4x"}}), \
             patch.object(screen, "_confirm", create=True, side_effect=lambda text: confirm), \
             patch("save_backup.backup_saves", side_effect=fake_backup), \
             patch("wrapper.kill_steam", side_effect=lambda **kw: order.append("close steam")), \
             patch("wrapper.get_proton_path", return_value="proton"), \
             patch("wrapper.find_compatdata", return_value=self.root), \
             patch("wrapper.set_compat_tool"), patch("wrapper.set_steam_input_enabled"), \
             patch("ge_proton.install_ge_proton", side_effect=OSError("offline")), \
             patch("ge_proton.resolve_ge_version", return_value=None), \
             patch("iw4x.install_iw4x", side_effect=fake_install):
            screen._run()
        app.processEvents()
        return screen, order

    def test_failed_game_in_update_games_keeps_its_launch_options(self):
        screen, _ = self._run_update_games()
        self.assertEqual([k for k, _, _ in screen._failed_games], ["iw4mp"])
        self.assertEqual(self._options("10190"), 'MINE=1 \\"quoted\\" %command%')
        self.assertEqual(self._options("42690"), "other %command%")

    def test_update_games_backs_up_saves_before_changing_anything(self):
        _, order = self._run_update_games()
        self.assertEqual(order, ["backup", "close steam", "install"])

    def test_incomplete_backup_and_player_says_no_changes_nothing(self):
        before = self.vdf.read_text()
        screen, order = self._run_update_games(backup=lambda f: f.append("cod4") or {}, confirm=False)
        self.assertEqual(order, ["backup"])
        self.assertEqual(self.vdf.read_text(), before)
        self.assertEqual(screen.cur.text(), "Update cancelled. Nothing was changed.")

    def test_incomplete_backup_and_player_says_yes_continues(self):
        _, order = self._run_update_games(backup=lambda f: f.append("cod4") or {}, confirm=True)
        self.assertEqual(order, ["backup", "close steam", "install"])

    def test_backup_message_is_honest(self):
        import save_backup as sb
        self.assertIn("incomplete for: cod4", sb.describe_backup({"mw2": "x"}, ["cod4"]))
        self.assertIn("Could not create", sb.describe_backup({}, ["*"]))
        self.assertTrue(sb.describe_backup({"mw2": "x"}, []).startswith("✓"))
        self.assertEqual(sb.describe_backup({}, []), "No save data found to back up.")


if __name__ == "__main__":
    unittest.main()
