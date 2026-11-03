"""Regressions for update and repair paths that must never report false success."""

import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import depot_downgrade as depot
import update_manifest

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
        with tempfile.TemporaryDirectory() as tmp, patch.object(ge_proton, "COMPAT_DIR", tmp), \
             patch("wrapper._configured_steam_root", return_value=None):
            self.assertIsNone(ge_proton.resolve_ge_version())
            self._tool(tmp, "GE-Proton10-9")
            self._tool(tmp, "GE-Proton10-30")
            Path(tmp, "GE-Proton10-99").mkdir()          # half-extracted: ignored
            incomplete = Path(tmp, "GE-Proton10-98")
            incomplete.mkdir()
            (incomplete / "proton").write_text("")       # script alone is not a usable tool
            self.assertEqual(ge_proton.resolve_ge_version(), "GE-Proton10-30")
            with patch("config.get_ge_proton_version", return_value="GE-Proton10-9"):
                self.assertEqual(ge_proton.resolve_ge_version(), "GE-Proton10-9")
            self.assertEqual(ge_proton.resolve_ge_version("GE-Proton11-1"), "GE-Proton11-1")


class PrelaunchHelperFallbackTests(unittest.TestCase):
    def test_helper_startup_failure_runs_game_but_other_failures_do_not_duplicate_it(self):
        """prelaunch.sh runs fovwatch.py as the game's parent, so the game must
        start exactly once whether or not the helper could start it:

          * helper cannot start the game at all (126/127) -> prelaunch.sh
            starts it, and DeckOps reports success;
          * helper starts the game and the game returns s -> s is passed on and
            prelaunch.sh must not start the game a second time.

        The previous version of this test used a fake python that failed the
        helper's import check with the same status it was meant to return for
        the game, so it asserted that a broken helper must NOT start the game.
        That contradicts prelaunch.sh's contract ("the game always starts") and
        tests/test_prelaunch_shell.py, and it failed on a clean checkout.
        """
        if not shutil.which("bash"):
            self.skipTest("bash unavailable")
        try:
            if subprocess.run(["bash", "--version"], capture_output=True).returncode:
                self.skipTest("bash unavailable on this host")
        except OSError:
            self.skipTest("bash unavailable on this host")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / "src").mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / "prelaunch.sh", root / "prelaunch.sh")
            (root / "src" / "fovwatch.py").write_text("placeholder")
            python = root / ".venv" / "bin" / "python3"
            game = root / "game.sh"
            game.write_text('#!/bin/sh\necho launched >> "$1"\n')
            game.chmod(0o755)
            marker = root / "launched"
            for status in (127, 1):
                # prelaunch.sh calls python three times: the silent prelaunch.py
                # update, the `python -c 'import fovwatch'` import check, then
                # the helper itself. The first two succeed, so the helper is
                # used. On the helper run: 127 = it never got the game started,
                # any other status = the game ran and returned that status.
                python.write_text(
                    "#!/bin/sh\n"
                    "case \"$*\" in *prelaunch.py*|*\"import fovwatch\"*) exit 0 ;; esac\n"
                    + ("exit 127\n" if status == 127 else "")
                    + f'"{game}" "{marker}"\n'
                    + f"exit {status}\n"
                )
                python.chmod(0o755)
                result = subprocess.run(["bash", str(root / "prelaunch.sh"), "iw4sp",
                                         str(game), str(marker)], capture_output=True)
                self.assertEqual(result.returncode, 0 if status == 127 else 1)
                self.assertEqual(marker.read_text().count("launched") if marker.exists() else 0, 1)
                marker.unlink(missing_ok=True)


class PrefixFinalizeTests(unittest.TestCase):
    """`proton run cmd /c exit` is the step that turns a bare directory into a
    Wine prefix. Its exit status used to be ignored, so a prefix that was never
    created was reported as "✓ Prefix finalized by Proton", counted as ready,
    and the game then failed on first launch instead of setup saying so."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A minimal GE-Proton: default_pfx with the DLL folders the copy step reads.
        self.tool = self.root / "GE-Proton10-30"
        for d in ("system32", "syswow64"):
            (self.tool / "files" / "share" / "default_pfx" / "drive_c" / "windows" / d).mkdir(parents=True)
        (self.tool / "files" / "share" / "default_pfx" / "drive_c" / "windows" / "system32" / "d3dx9_43.dll").write_bytes(b"dll")
        (self.tool / "proton").write_text("")
        (self.tool / "compatibilitytool.vdf").write_text("")

    def _fake_proton(self, body: str) -> str:
        """A proton script that runs `body` when asked to finalize a prefix."""
        path = self.root / "proton"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return str(path)

    def _deps(self, proton):
        import ge_proton
        prefix = self.root / "compatdata" / "10090"
        with patch.object(ge_proton, "COMPAT_DIR", str(self.root)), \
             patch.object(ge_proton, "_find_default_pfx",
                          return_value=str(self.tool / "files" / "share" / "default_pfx")):
            return ge_proton.ensure_prefix_deps(
                "GE-Proton10-30", str(prefix), on_progress=lambda m: None,
                proton_path=proton, steam_root=str(self.root))

    def test_failed_proton_run_is_not_reported_as_finalized(self):
        # Exits non-zero and creates nothing, like a missing steamclient.so
        ok = self._deps(self._fake_proton("exit 1"))
        self.assertIs(ok, False)

    def test_successful_proton_run_is_finalized(self):
        # Wine boot order: drive_c plus the registry it writes on first boot
        proton = self._fake_proton(
            'mkdir -p "$STEAM_COMPAT_DATA_PATH/pfx/drive_c"\n'
            ': > "$STEAM_COMPAT_DATA_PATH/pfx/system.reg"')
        self.assertIs(self._deps(proton), True)

    def test_zero_exit_without_a_prefix_does_not_count(self):
        # Exits 0 but never boots wine -- a launcher that silently does nothing
        ok = self._deps(self._fake_proton("exit 0"))
        self.assertIs(ok, False)

    def test_a_failed_finalize_is_retried_on_the_next_run(self):
        """The DLL copy creates drive_c before Proton runs, so a prefix whose
        finalize failed used to look initialized forever: every later run said
        "already initialized — skipped Proton run" and never retried."""
        self.assertIs(self._deps(self._fake_proton("exit 1")), False)
        proton = self._fake_proton(
            'mkdir -p "$STEAM_COMPAT_DATA_PATH/pfx/drive_c"\n'
            ': > "$STEAM_COMPAT_DATA_PATH/pfx/system.reg"')
        self.assertIs(self._deps(proton), True)

    def test_finalized_helper_checks_the_disk_not_only_the_status(self):
        import ge_proton
        pfx = self.root / "pfx"
        # A drive_c made by the DLL copy does not mean wine ever ran
        (pfx / "drive_c" / "windows" / "system32").mkdir(parents=True)
        self.assertFalse(ge_proton._prefix_ready(str(pfx)))
        self.assertFalse(ge_proton._finalized(0, str(pfx)))
        (pfx / "system.reg").write_text("WINE REGISTRY Version 2\n")
        self.assertTrue(ge_proton._prefix_ready(str(pfx)))
        self.assertTrue(ge_proton._finalized(0, str(pfx)))
        self.assertFalse(ge_proton._finalized(1, str(pfx)))       # failed even though it booted

    def test_a_timed_out_finalize_leaves_the_prefix_not_ready(self):
        """Wine writes its registry early, so a run killed by the timeout left files
        that made _prefix_ready() true and the next run skipped Proton for good."""
        import ge_proton
        prefix = self.root / "compatdata" / "10090"
        pfx = prefix / "pfx"
        prefix.mkdir(parents=True)

        def boot_then_hang(*a, **k):
            pfx.mkdir(parents=True, exist_ok=True)
            (pfx / "system.reg").write_text("")
            raise subprocess.TimeoutExpired(a[0], 1)

        with patch("subprocess.run", side_effect=boot_then_hang):
            with self.assertRaises(subprocess.TimeoutExpired):
                ge_proton._run_proton_finalize(str(self.tool / "proton"), str(prefix), str(self.root), 1)
        self.assertTrue((pfx / "system.reg").exists())
        self.assertFalse(ge_proton._prefix_ready(str(pfx)))

    def test_a_clean_finalize_clears_the_marker(self):
        import ge_proton
        prefix = self.root / "compatdata" / "10091"
        pfx = prefix / "pfx"
        prefix.mkdir(parents=True)

        def boot(*a, **k):
            pfx.mkdir(parents=True, exist_ok=True)
            (pfx / "system.reg").write_text("")
            return subprocess.CompletedProcess(a[0], 0)

        with patch("subprocess.run", side_effect=boot):
            r = ge_proton._run_proton_finalize(str(self.tool / "proton"), str(prefix), str(self.root), 1)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(ge_proton._prefix_ready(str(pfx)))


class ShellSyntaxTests(unittest.TestCase):
    """The updater refuses any .sh file that fails `bash -n`, so a syntax error
    in a shipped script blocks every user's update."""

    def test_all_shell_scripts_pass_bash_syntax_check(self):
        try:
            if subprocess.run(["bash", "--version"], capture_output=True).returncode:
                self.skipTest("bash unavailable on this host")
        except OSError:
            self.skipTest("bash unavailable on this host")
        root = Path(__file__).resolve().parents[1]
        skip = {".git", ".venv", ".update", "node_modules"}
        scripts = [p for p in root.rglob("*.sh")
                   if not skip.intersection(p.relative_to(root).parts)]
        self.assertTrue(scripts)
        for script in scripts:
            with self.subTest(script=str(script.relative_to(root))):
                result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


class SteamRootGeProtonTests(unittest.TestCase):
    """A separate Steam install must never use the default install's GE tool."""

    def setUp(self):
        import ge_proton
        import wrapper
        self.ge, self.wrapper = ge_proton, wrapper
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.default = Path(self.tmp.name, "default")
        self.selected = Path(self.tmp.name, "selected")
        self.override = Path(self.tmp.name, "override")
        for root in (self.default, self.selected, self.override):
            (root / "config").mkdir(parents=True)
            (root / "config" / "config.vdf").write_text(
                '"InstallConfigStore" { "Software" { "Valve" { "Steam" { } } } }')
        for p in (patch("config.load", return_value={"steam_root": str(self.selected)}),
                  patch.object(wrapper, "STEAM_CONFIG", str(self.default / "config" / "config.vdf")),
                  patch.object(wrapper, "LEDGER_PATH", str(Path(self.tmp.name, "ledger.json")))):
            p.start(); self.addCleanup(p.stop)

    def tool(self, root, name):
        tool = root / "compatibilitytools.d" / name
        (tool / "files" / "share" / "default_pfx").mkdir(parents=True)
        (tool / "proton").write_text("")
        (tool / "compatibilitytool.vdf").write_text("")
        return tool

    def test_configured_root_and_explicit_override_for_lookup_and_mapping(self):
        self.tool(self.default, "GE-Proton10-99")
        selected = self.tool(self.selected, "GE-Proton10-2")
        override = self.tool(self.override, "GE-Proton10-3")
        with patch("config.get_ge_proton_version", return_value="GE-Proton10-99"):
            self.assertEqual(self.ge.resolve_ge_version(), "GE-Proton10-2")
            self.assertEqual(self.wrapper.get_proton_path(), str(selected / "proton"))
            self.assertEqual(self.wrapper.get_proton_path(str(self.override)), str(override / "proton"))
            self.assertEqual(self.ge.resolve_ge_version(steam_root=str(self.override)), "GE-Proton10-3")
            self.assertEqual(self.ge._find_default_pfx("GE-Proton10-99"),
                             str(selected / "files" / "share" / "default_pfx"))
        self.assertTrue(self.wrapper.set_compat_tool([10190], "GE-Proton10-2"))
        self.assertTrue(self.ge.set_compat_tool([10090], "GE-Proton10-3", str(self.override)))
        self.assertNotIn("CompatToolMapping", (self.default / "config" / "config.vdf").read_text())
        self.assertIn('"10190"', (self.selected / "config" / "config.vdf").read_text())
        self.assertIn('"10090"', (self.override / "config" / "config.vdf").read_text())
        self.wrapper.clear_compat_tool([10190])
        self.wrapper.clear_compat_tool([10090], steam_root=str(self.override))
        self.assertNotIn('"10190"', (self.selected / "config" / "config.vdf").read_text())
        self.assertNotIn('"10090"', (self.override / "config" / "config.vdf").read_text())

    def test_compat_edits_stay_inside_compat_tool_mapping(self):
        """The same quoted number can be a block key elsewhere in config.vdf; set and
        clear used to rewrite or delete every block with that key."""
        cfg = self.selected / "config" / "config.vdf"
        decoy = '\t\t\t"10190"\n\t\t\t{\n\t\t\t\t"name"\t\t"not a compat tool"\n\t\t\t}\n'
        cfg.write_text('"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
                       + decoy +
                       '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')
        self.assertTrue(self.wrapper.set_compat_tool([10190], "GE-Proton10-2"))
        text = cfg.read_text()
        self.assertIn(decoy, text)
        self.assertEqual(text.count("GE-Proton10-2"), 1)
        self.assertTrue(self.wrapper.set_compat_tool([10190], "GE-Proton10-3"))   # replaced in place
        text = cfg.read_text()
        self.assertIn(decoy, text)
        self.assertNotIn("GE-Proton10-2", text)
        self.assertEqual(text.count("GE-Proton10-3"), 1)
        self.wrapper.clear_compat_tool([10190])
        text = cfg.read_text()
        self.assertIn(decoy, text)
        self.assertNotIn("GE-Proton10-3", text)

    def test_config_without_a_steam_block_is_left_alone_and_not_recorded(self):
        cfg = self.selected / "config" / "config.vdf"
        cfg.write_text('"SomethingElse"\n{\n}\n')
        self.assertFalse(self.wrapper.set_compat_tool([10190], "GE-Proton10-2"))
        self.assertEqual(cfg.read_text(), '"SomethingElse"\n{\n}\n')
        ledger = Path(self.tmp.name, "ledger.json")
        self.assertNotIn("10190", ledger.read_text() if ledger.exists() else "")

    def test_non_utf8_bytes_in_config_vdf_survive_a_compat_edit(self):
        cfg = self.selected / "config" / "config.vdf"
        cfg.write_bytes(b'"InstallConfigStore" { "Software" { "Valve" { "Steam" { "name" "caf\xe9" } } } }')
        self.assertTrue(self.wrapper.set_compat_tool([10190], "GE-Proton10-2"))
        self.assertIn(b"caf\xe9", cfg.read_bytes())

    def test_default_pfx_fallback_picks_the_numerically_newest_ge(self):
        # A string sort ranks GE-Proton9-27 and GE-Proton10-4 above GE-Proton10-34
        for name in ("GE-Proton9-27", "GE-Proton10-4", "GE-Proton10-34"):
            self.tool(self.selected, name)
        newest = self.selected / "compatibilitytools.d" / "GE-Proton10-34"
        self.assertEqual(self.ge._find_default_pfx("GE-Proton10-99"),
                         str(newest / "files" / "share" / "default_pfx"))

    def test_explicit_root_without_ge_uses_its_vanilla_proton_not_other_roots_ge(self):
        self.tool(self.selected, "GE-Proton10-99")
        vanilla = self.override / "steamapps" / "common" / "Proton 10" / "proton"
        vanilla.parent.mkdir(parents=True)
        vanilla.write_text("")
        with patch("config.get_ge_proton_version", return_value="GE-Proton10-99"):
            self.assertIsNone(self.ge.resolve_ge_version(steam_root=str(self.override)))
            self.assertEqual(self.wrapper.get_proton_path(str(self.override)), str(vanilla))

    def test_install_extracts_into_configured_or_explicit_root(self):
        name = "GE-Proton11-7-x86_64"
        tarpath = Path(self.tmp.name, "ge.tar.gz")
        with tarfile.open(tarpath, "w:gz") as tar:
            for entry, contents in (("proton", b"#!/bin/sh\n"),
                                    ("compatibilitytool.vdf", b"tool"),
                                    ("files/share/default_pfx/drive_c/placeholder", b"pfx")):
                data = io.BytesIO(contents)
                info = tarfile.TarInfo(f"{name}/{entry}")
                info.size = len(contents)
                tar.addfile(info, data)
        with patch.object(self.ge, "_get_latest_release", return_value=(name, "fake", None, name)), \
             patch.object(self.ge, "download", side_effect=lambda _url, path, **_kw: shutil.copyfile(tarpath, path)):
            self.assertEqual(self.ge.install_ge_proton(), name)
            self.assertEqual(self.ge.install_ge_proton(steam_root=str(self.override)), name)
        self.assertTrue((self.selected / "compatibilitytools.d" / name / "proton").is_file())
        self.assertTrue((self.override / "compatibilitytools.d" / name / "proton").is_file())
        self.assertFalse((self.default / "compatibilitytools.d" / name).exists())

    def test_detected_root_when_config_has_none(self):
        self.tool(self.selected, "GE-Proton10-2")
        with patch("config.load", return_value={"steam_root": None}), \
             patch("detect_games.find_steam_root", return_value=str(self.selected)):
            self.assertEqual(self.ge.resolve_ge_version(), "GE-Proton10-2")
            self.assertEqual(self.wrapper.get_proton_path(),
                             str(self.selected / "compatibilitytools.d" / "GE-Proton10-2" / "proton"))
            self.assertTrue(self.wrapper.set_compat_tool([10190], "GE-Proton10-2"))
        self.assertIn('"10190"', (self.selected / "config" / "config.vdf").read_text())
        self.assertNotIn("CompatToolMapping", (self.default / "config" / "config.vdf").read_text())

    def test_prefix_deps_use_explicit_root_not_configured_root(self):
        self.tool(self.selected, "GE-Proton10-2")
        override = self.tool(self.override, "GE-Proton10-2")
        default_pfx = override / "files" / "share" / "default_pfx"
        (default_pfx / "from-override").write_text("correct install")
        dest = Path(self.tmp.name, "game-prefix")
        self.assertTrue(self.ge.ensure_prefix_deps("GE-Proton10-2", str(dest),
                                                    steam_root=str(self.override)))
        self.assertEqual((dest / "pfx" / "from-override").read_text(), "correct install")


def _dll(path, builtin=True, body=b""):
    """A file Proton's builtin test recognises (marker at 0x40), or a native one."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"MZ" + b"\0" * 0x3e + (b"Wine builtin DLL" if builtin else b"Native DLL here!")
                           + b"\0" * 8 + body)


class SharedDllRefreshTests(unittest.TestCase):
    """The shared system32/syswow64 follow GE-Proton upgrades without losing native DLLs."""

    def setUp(self):
        import ge_proton
        self.ge = ge_proton
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.compat = Path(self.tmp.name, "Steam", "compatibilitytools.d")
        self.shared = Path(self.tmp.name, "shared_dlls")
        p = patch("wrapper._configured_steam_root", return_value=None)
        p.start(); self.addCleanup(p.stop)
        for target, value in (("COMPAT_DIR", str(self.compat)), ("SHARED_DLL_DIR", str(self.shared))):
            p = patch.object(ge_proton, target, value)
            p.start(); self.addCleanup(p.stop)

    def tool(self, version, body):
        win = self.compat / version / "files" / "share" / "default_pfx" / "drive_c" / "windows"
        _dll(win / "system32" / "kernel32.dll", body=body)
        _dll(win / "system32" / "drivers" / "mountmgr.sys", body=body)
        _dll(win / "syswow64" / "kernel32.dll", body=body)
        _dll(win / "system32" / "d3dx9_43.dll", body=body)
        return win

    def test_new_version_updates_builtins_and_keeps_native_dlls(self):
        self.tool("GE-Proton10-1", b"old")
        self.assertTrue(self.ge._ensure_shared_dlls("GE-Proton10-1"))
        sys32 = self.shared / "system32"
        self.assertTrue((sys32 / "drivers" / "mountmgr.sys").is_file())
        self.assertEqual((self.shared / ".ge_version").read_text().strip(), "GE-Proton10-1")
        _dll(sys32 / "d3dx9_43.dll", builtin=False)          # a redist installed the native one
        _dll(sys32 / "game_only.dll", builtin=False)         # not in default_pfx at all

        win = self.tool("GE-Proton10-2", b"new, longer")
        _dll(win / "system32" / "added.dll", body=b"new")
        self.assertTrue(self.ge._ensure_shared_dlls("GE-Proton10-2"))
        self.assertTrue((sys32 / "kernel32.dll").read_bytes().endswith(b"new, longer"))
        self.assertTrue((self.shared / "syswow64" / "kernel32.dll").read_bytes().endswith(b"new, longer"))
        self.assertTrue((sys32 / "added.dll").is_file())
        self.assertIn(b"Native DLL here!", (sys32 / "d3dx9_43.dll").read_bytes())
        self.assertTrue((sys32 / "game_only.dll").is_file())
        self.assertEqual((self.shared / ".ge_version").read_text().strip(), "GE-Proton10-2")

        with patch.object(self.ge, "_refresh_dll_dir") as refresh:   # same version: nothing to do
            self.assertTrue(self.ge._ensure_shared_dlls("GE-Proton10-2"))
        refresh.assert_not_called()

    def test_relinks_links_into_a_removed_version(self):
        self.tool("GE-Proton10-2", b"x")
        lib = self.compat / "GE-Proton10-2" / "files" / "lib" / "wine" / "x86_64-windows"
        _dll(lib / "kernel32.dll")
        sys32 = self.shared / "system32"
        sys32.mkdir(parents=True)
        old = str(self.compat / "GE-Proton9-1" / "files" / "lib" / "wine" / "x86_64-windows")
        try:
            os.symlink(old + "/kernel32.dll", str(sys32 / "kernel32.dll"))
        except OSError:
            self.skipTest("symlinks not available")
        os.symlink(old + "/gone.dll", str(sys32 / "gone.dll"))
        os.symlink("/somewhere/else/user.dll", str(sys32 / "user.dll"))   # not Proton's: left alone
        self.assertEqual(self.ge.relink_dangling_builtins("GE-Proton10-2", []), (1, 1))
        self.assertEqual(os.readlink(sys32 / "kernel32.dll"), str(lib / "kernel32.dll"))
        self.assertFalse(os.path.lexists(sys32 / "gone.dll"))
        self.assertTrue(os.path.islink(sys32 / "user.dll"))

    def test_repair_recreates_a_deleted_shared_folder(self):
        self.tool("GE-Proton10-2", b"x")
        root = Path(self.tmp.name, "Steam")
        win = root / "steamapps" / "compatdata" / "10190" / "pfx" / "drive_c" / "windows"
        win.mkdir(parents=True)
        try:
            for d in ("system32", "syswow64"):
                os.symlink(str(self.shared / d), str(win / d))
        except OSError:
            self.skipTest("symlinks not available")
        with patch.dict(os.environ, {"HOME": self.tmp.name}):
            self.assertTrue(self.ge.repair_prefixes("GE-Proton10-2", str(root)))
        self.assertTrue((win / "system32" / "kernel32.dll").is_file())
        self.assertTrue((win / "syswow64" / "kernel32.dll").is_file())


class GeProtonCleanupTests(unittest.TestCase):
    """Old GE-Protons go, but only DeckOps's own, the rollback one stays,
    and nothing that is still in use is removed."""

    def setUp(self):
        import ge_proton
        self.ge = ge_proton
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name, "home")
        self.home.mkdir()
        self.root = Path(self.tmp.name, "Steam")
        self.compat = self.root / "compatibilitytools.d"
        (self.root / "config").mkdir(parents=True)
        self.tracked = ["GE-Proton10-1", "GE-Proton10-2", "GE-Proton10-3", "GE-Proton10-4", "GE-Proton10-9"]
        self.saved = None
        for v in ["GE-Proton10-0"] + self.tracked:          # 10-0: the player's own
            tool = self.compat / v
            (tool / "files" / "share" / "default_pfx").mkdir(parents=True)
            (tool / "proton").write_text("")
            (tool / "compatibilitytool.vdf").write_text("")
        self.mapping({"10190": "GE-Proton10-2", "0": "proton_9"})
        patches = [patch.object(ge_proton, "COMPAT_DIR", str(self.compat)),
                   patch.object(ge_proton, "SHARED_DLL_DIR", str(Path(self.tmp.name, "shared"))),
                   patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}),
                   patch("config.get_ge_proton_installed", side_effect=lambda: list(self.tracked)),
                   patch("config.set_ge_proton_installed", side_effect=self.save),
                   patch("config.get_ge_proton_version", return_value="GE-Proton10-4")]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def save(self, versions):
        self.saved = versions

    def mapping(self, maps):
        body = "".join(SetupCheckTests.MAP % (a, n) for a, n in maps.items())
        (self.root / "config" / "config.vdf").write_text(SetupCheckTests.CONFIG % body)

    def left(self):
        return sorted(os.listdir(self.compat))

    def test_keeps_current_previous_newer_mapped_and_players_own(self):
        removed = self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root))
        self.assertEqual(removed, ["GE-Proton10-1"])
        self.assertEqual(self.left(), ["GE-Proton10-0", "GE-Proton10-2", "GE-Proton10-3",
                                       "GE-Proton10-4", "GE-Proton10-9"])
        self.assertNotIn("GE-Proton10-1", self.saved)
        self.assertIn("GE-Proton10-4", self.saved)

    def test_mapping_released_then_removed(self):
        self.mapping({"10190": "GE-Proton10-4"})
        self.assertEqual(sorted(self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root))),
                         ["GE-Proton10-1", "GE-Proton10-2"])

    def test_cleanup_does_not_remove_other_steam_roots_versions(self):
        other = Path(self.tmp.name, "other-Steam") / "compatibilitytools.d"
        (other / "GE-Proton10-1").mkdir(parents=True)
        self.mapping({"10190": "GE-Proton10-4"})
        self.assertIn("GE-Proton10-1", self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root)))
        self.assertTrue((other / "GE-Proton10-1").is_dir())

    def test_versions_still_used_elsewhere_are_kept(self):
        self.mapping({})
        heroic = self.home / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic" / "GamesConfig"
        heroic.mkdir(parents=True)
        (heroic / "do_x.json").write_text(json.dumps({"do_x": {"wineVersion": {
            "bin": str(Path("~/.local/share/Steam/compatibilitytools.d/GE-Proton10-1/proton")).replace("\\", "/")}}}))
        (self.home / "DeckOps").mkdir()
        (self.home / "DeckOps" / "deckops.json").write_text(json.dumps({"ge_proton_version": "GE-Proton10-2"}))
        self.assertEqual(self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root)), [])

    def test_nothing_removed_when_steam_settings_unreadable(self):
        (self.root / "config" / "config.vdf").unlink()
        self.assertEqual(self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root)), [])
        (self.root / "config" / "config.vdf").write_text('"InstallConfigStore" { "Software" { broken')
        self.assertEqual(self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root)), [])
        self.assertIn("GE-Proton10-1", self.left())

    def test_incomplete_current_version_removes_nothing(self):
        (self.compat / "GE-Proton10-4" / "proton").unlink()
        self.assertEqual(self.ge.cleanup_old_ge_versions("GE-Proton10-4", str(self.root)), [])


class GeProtonTrackingTests(unittest.TestCase):
    def test_versions_deckops_used_are_remembered(self):
        import config
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "deckops.json")
            Path(path).write_text(json.dumps({"ge_proton_version": "GE-Proton9-20"}))   # older install
            with patch.object(config, "CONFIG_PATH", path), patch.object(config, "_cache", None):
                self.assertEqual(config.get_ge_proton_installed(), ["GE-Proton9-20"])
                config.set_ge_proton_version("GE-Proton10-1")
                config.set_ge_proton_version("GE-Proton10-1")
                self.assertEqual(config.get_ge_proton_version(), "GE-Proton10-1")
                self.assertEqual(sorted(config.get_ge_proton_installed()), ["GE-Proton10-1", "GE-Proton9-20"])


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


class AppInfoWriteTests(unittest.TestCase):
    """Editing appinfo.vdf must leave the file as Steam had it, plus the edit.

    mkstemp makes the replacement 0600, so one menu edit turned appinfo.vdf
    from 0644 into owner-only; and the .bak "original" was rewritten on every
    edit with the already-edited file, so it stopped being the version Steam
    shipped exactly when someone would restore it.
    """

    APPID = 202990

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "appcache").mkdir()
        self.path = self.root / "appcache" / "appinfo.vdf"
        self.original = self._build()
        self.path.write_bytes(self.original)
        os.chmod(self.path, 0o644)

    def _build(self) -> bytes:
        """A v28 appinfo.vdf with one app whose appinfo/config/launch dict has
        a stock entry -- the shape _edit_launch looks for."""
        import steam_appinfo as a
        launch = [[a._T_DICT, "0", [
            [a._T_STR, "executable", b"t6sp.exe"],
            [a._T_DICT, "config", [[a._T_STR, "oslist", b"windows"]]]]]]
        root = [[a._T_DICT, "appinfo", [
            [a._T_DICT, "config", [[a._T_DICT, "launch", launch]]]]]]
        ai = a._AppInfo.__new__(a._AppInfo)  # encode without a real file
        ai.v29, ai.strings, ai._index, ai._added = False, [], {}, []
        body = a._HDR.pack(2, 0, 0, b"\0" * 20, 1, b"\0" * 20) + a._AppInfo.encode_kv(ai, root)
        return (struct.pack("<II", a.APPINFO_V28, 1) +
                struct.pack("<II", self.APPID, len(body)) + body +
                struct.pack("<II", 0, 0))

    def _add(self, description):
        import steam_appinfo as a
        return a.add_launch_entries(str(self.root), self.APPID,
                                    [{"executable": "deckops_x.exe",
                                      "description": description}], record=False)

    def test_the_edit_keeps_the_permissions_steam_set(self):
        self.assertTrue(self._add("first"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)

    def test_the_backup_stays_the_file_steam_shipped(self):
        self.assertTrue(self._add("first"))
        backup = Path(str(self.path) + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), self.original)

        self.assertTrue(self._add("second"))
        self.assertEqual(backup.read_bytes(), self.original,
                         "the second edit overwrote .bak with an edited file")

    def test_no_temp_files_are_left_behind(self):
        self._add("first")
        self.assertEqual([p.name for p in self.path.parent.iterdir()
                          if p.name.endswith(".tmp")], [])

    def _app_with_kv(self, kv: bytes) -> bytes:
        import steam_appinfo as a
        body = a._HDR.pack(2, 0, 0, b"\0" * 20, 1, b"\0" * 20) + kv
        return (struct.pack("<II", a.APPINFO_V28, 1) +
                struct.pack("<II", self.APPID, len(body)) + body +
                struct.pack("<II", 0, 0))

    def test_unterminated_wide_string_is_skipped_not_an_endless_loop(self):
        import threading
        # type 5 (wide string), key "k", value "ab" with no 00 00 terminator
        self.path.write_bytes(self._app_with_kv(b"\x05k\x00a\x00b\x00"))
        result = []
        t = threading.Thread(target=lambda: result.append(self._add("x")), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "parse_kv never returned on a truncated wide string")
        self.assertEqual(result, [False])

    def test_truncated_app_block_is_skipped_not_raised(self):
        # type byte promises a key and value that are not there
        self.path.write_bytes(self._app_with_kv(b"\x01"))
        self.assertFalse(self._add("x"))

    def test_a_truncated_file_is_skipped_not_raised(self):
        self.path.write_bytes(self.original[:10])
        self.assertFalse(self._add("x"))

    def test_reapply_all_skips_a_bad_record(self):
        import steam_appinfo as a
        import config as cfg
        menus = {"not-an-appid": [{"executable": "x.exe"}],
                 str(self.APPID): [{"executable": "deckops_x.exe", "description": "d"}]}
        with patch.object(cfg, "load", return_value={"launch_menus": menus}):
            a.reapply_all(str(self.root))
        self.assertIn(b"deckops_x.exe", self.path.read_bytes())


class ArchiveSafetyTests(unittest.TestCase):
    """The DepotDownloader zip is the one download with no digest to check, so
    its member names have to be validated like GE-Proton's tarball."""

    def _zip(self, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        buf.seek(0)
        return zipfile.ZipFile(buf)

    def test_normal_archive_extracts(self):
        import depot_downgrade as depot
        with tempfile.TemporaryDirectory() as tmp:
            depot._safe_extractall_zip(self._zip([("DepotDownloader", b"bin"),
                                                  ("lib/x.dll", b"dll")]), tmp)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "DepotDownloader")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "lib", "x.dll")))

    def test_paths_outside_the_directory_are_refused(self):
        import depot_downgrade as depot
        for name in ("../escape.txt", "/etc/abs.txt", "a/../../b.txt"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    depot._safe_extractall_zip(self._zip([(name, b"x")]), tmp)
                self.assertEqual(os.listdir(tmp), [])

    def test_symlinks_pointing_outside_are_refused(self):
        import depot_downgrade as depot
        for target in ("/etc/passwd", "../../../outside"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                info = zipfile.ZipInfo("evil")
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, target)
            buf.seek(0)
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    depot._safe_extractall_zip(zipfile.ZipFile(buf), tmp)


class FovWatchSpawnTests(unittest.TestCase):
    """A helper that cannot spawn the game must say so with 126/127 so
    prelaunch.sh starts the game itself. Exiting 1 meant it never started."""

    def test_unspawnable_command_returns_the_helper_sentinel(self):
        import fovwatch
        # argv: <exe> <settings...> -- <command...>, as prelaunch.sh calls it
        rc = fovwatch.main(["iw4sp.exe", "cg_fov=90", "--", "/nonexistent/deckops-game"])
        self.assertEqual(rc, 127)

    def test_prelaunch_starts_the_game_when_the_helper_reports_127(self):
        """End to end: fovwatch cannot spawn it, prelaunch.sh runs it once."""
        if not shutil.which("bash"):
            self.skipTest("bash unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / "src").mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / "prelaunch.sh", root / "prelaunch.sh")
            shutil.copyfile(Path(__file__).resolve().parents[1] / "src" / "fovwatch.py",
                            root / "src" / "fovwatch.py")
            python = root / ".venv" / "bin" / "python3"
            python.symlink_to(sys.executable)
            marker = root / "launched"
            game = root / "game.sh"
            game.write_text('#!/bin/sh\necho launched >> "$1"\n')
            game.chmod(0o755)
            result = subprocess.run(
                ["bash", str(root / "prelaunch.sh"), "iw4sp", str(game), str(marker)],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "DECKOPS_MENU": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text().count("launched"), 1)


class ConfigCacheTests(unittest.TestCase):
    """Two saves inside one timestamp tick must not leave a stale cache."""

    def setUp(self):
        import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "deckops.json"
        p = patch.object(config, "CONFIG_PATH", str(self.path))
        p.start()
        self.addCleanup(p.stop)

    def test_a_same_mtime_change_is_seen(self):
        self.path.write_text(json.dumps({"player_name": "first"}))
        self.assertEqual(self.config.load()["player_name"], "first")
        st = os.stat(self.path)
        # Same length is not required -- what matters is that mtime alone is
        # not the key: write a longer value and put the timestamp back.
        self.path.write_text(json.dumps({"player_name": "second-longer"}))
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(self.path).st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(self.config.load()["player_name"], "second-longer")


class SteamRootResolutionTests(unittest.TestCase):
    """A Flatpak or moved Steam must be found, and callers with no root must
    not fall back to a hardcoded path that does not exist."""

    def _roots(self, *names):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = []
        for name in names:
            p = Path(tmp.name) / name
            (p / "steamapps").mkdir(parents=True)
            paths.append(str(p))
        return tmp.name, paths

    def test_flatpak_steam_is_in_the_detection_list(self):
        import detect_games
        self.assertTrue(any("com.valvesoftware.Steam" in p for p in detect_games.STEAM_PATHS))

    def test_find_steam_root_picks_a_flatpak_install(self):
        """The classic path is tried first; a Flatpak-only machine must still
        get a root instead of None."""
        import detect_games
        _, paths = self._roots("flatpak")           # one install, in the list
        with patch.object(detect_games, "STEAM_PATHS", ["/nonexistent/classic"] + paths):
            self.assertEqual(detect_games.find_steam_root(), paths[0])

    def test_default_steam_root_uses_the_configured_one(self):
        import steam_common
        with patch("config.load", return_value={"steam_root": "/custom/steam"}):
            self.assertEqual(steam_common.default_steam_root(), "/custom/steam")

    def test_default_steam_root_falls_back_to_the_installed_one(self):
        import steam_common
        with patch("config.load", return_value={}), \
             patch("detect_games.find_steam_root", return_value="/found/steam"):
            self.assertEqual(steam_common.default_steam_root(), "/found/steam")

    def test_default_steam_root_last_resort_is_a_real_path(self):
        import steam_common
        with patch("config.load", return_value={}), \
             patch("detect_games.find_steam_root", return_value=None):
            self.assertEqual(steam_common.default_steam_root(), steam_common.STEAM_ROOT)

    def test_shader_cache_follows_the_resolved_root(self):
        import cache_cleanup
        with patch("steam_common.default_steam_root", return_value="/flatpak/Steam"):
            self.assertEqual(cache_cleanup._shadercache_dir(),
                             os.path.join("/flatpak/Steam", "steamapps", "shadercache"))

    def test_configset_keeps_the_players_original_and_repairs_an_empty_file(self):
        import steam_common
        with tempfile.TemporaryDirectory() as tmp, patch("steam_common.record_configset_edit"):
            path = Path(tmp) / "configset_controller_neptune.vdf"
            original = '"controller_config"\n{\n\t"7940"\n\t{\n\t\t"template"\t\t"mine.vdf"\n\t}\n}\n'
            path.write_text(original)
            steam_common.patch_configset(str(path), "10090", "controller_neptune_deckops_cod.vdf")
            steam_common.patch_configset(str(path), "42690", "controller_neptune_deckops_cod.vdf")
            kept = Path(str(path) + steam_common.ORIGINAL_SUFFIX)
            self.assertEqual(kept.read_text(), original)
            self.assertIn('"42690"', path.read_text())

            empty = Path(tmp) / "configset_empty.vdf"
            empty.write_text("")
            steam_common.patch_configset(str(empty), "10090", "controller_neptune_deckops_cod.vdf")
            self.assertIn('"10090"', empty.read_text())

    def test_steam_users_and_compatdata_follow_the_resolved_root(self):
        """find_all_steam_uids / nvme_compatdata used module constants fixed to
        ~/.local/share/Steam, so a Flatpak Steam gave no users and shortcuts,
        layouts and artwork were silently skipped."""
        import steam_common
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "userdata" / "123456789").mkdir(parents=True)
            (Path(tmp) / "userdata" / "0").mkdir()          # placeholder, not an account
            with patch("steam_common.default_steam_root", return_value=tmp):
                self.assertEqual(steam_common.find_all_steam_uids(), ["123456789"])
                self.assertEqual(steam_common.nvme_compatdata(10090),
                                 os.path.join(tmp, "steamapps", "compatdata", "10090"))
                self.assertEqual(steam_common.userdata_dir(), os.path.join(tmp, "userdata"))


class DepotMergeSafetyTests(unittest.TestCase):
    """An interrupted depot merge must never leave the game missing files.

    _merge_tree deleted every destination file in one pass and moved the new
    files in a second. A crash, a power cut or a full disk between the passes
    left the install incomplete, no receipt described it, and the legacy
    marker check read the staged 32-bit folder as "already downgraded" -- so
    nothing offered to repair it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / "stage"
        self.game = self.root / "game"
        for rel, data in (("main/iw_00.iwd", b"new-iwd"), ("zone/a.ff", b"new-ff"),
                          ("zone/b.ff", b"new-bf")):
            path = self.stage / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for rel, data in (("main/iw_00.iwd", b"old-iwd"), ("zone/a.ff", b"old-ff"),
                          ("zone/b.ff", b"old-bf")):
            path = self.game / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def _merge(self):
        return depot._merge_tree(str(self.stage), str(self.game), lambda m: None)

    def test_files_are_replaced_and_sizes_recorded(self):
        merged = self._merge()
        self.assertEqual(sorted(merged), ["main/iw_00.iwd", "zone/a.ff", "zone/b.ff"])
        self.assertEqual((self.game / "zone" / "a.ff").read_bytes(), b"new-ff")
        self.assertEqual(list(self.game.rglob(depot._PENDING_SUFFIX)), [])

    def _disk_full_on_second_write(self):
        """Make the second file write fail, whichever call the merge uses to
        write it (shutil.move or os.replace), so the interruption lands at the
        same point for old and new implementations alike."""
        real_move, real_replace = shutil.move, os.replace
        seen = []

        def move(src, dst, **kw):
            seen.append(dst)
            if len(seen) == 2:
                raise OSError(28, "No space left on device")
            return real_move(src, dst, **kw)

        def replace(src, dst):
            seen.append(dst)
            if len(seen) == 2:
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        return (patch.object(depot.shutil, "move", side_effect=move),
                patch.object(depot.os, "replace", side_effect=replace))

    def test_a_failure_mid_merge_leaves_every_file_present(self):
        """A failure part way through leaves each file as either its old or
        its new version -- never missing."""
        move_patch, replace_patch = self._disk_full_on_second_write()
        with move_patch, replace_patch:
            with self.assertRaises(OSError):
                self._merge()

        for rel in ("main/iw_00.iwd", "zone/a.ff", "zone/b.ff"):
            self.assertTrue((self.game / rel).is_file(), f"{rel} disappeared")

    def test_an_interrupted_merge_is_detected_as_needing_repair(self):
        """merge_depots writes the receipt before it touches the install, so a
        half-merged directory reads as changed (not as downgraded)."""
        move_patch, replace_patch = self._disk_full_on_second_write()
        with move_patch, replace_patch:
            with self.assertRaises(OSError):
                depot.merge_depots("iw5", str(self.stage),
                                   str(self.root / "MW3"), on_progress=lambda m: None)
        install = self.root / "MW3"
        self.assertTrue(depot._receipt_path("iw5", str(install)))
        self.assertIs(depot._receipt_status("iw5", str(install)), False)
        self.assertIs(depot.is_downgrade_needed("iw5", str(install)), True)


class DepotQrRetryTests(unittest.TestCase):
    """run_depot_download_qr must always come back.

    The retry: the loop was bounded by `if max_retries > 0`, and the only
    caller never passed the argument, so the cap was dead code. Any failure
    the keyword list did not recognise printed "QR code expired. Generating a
    new one..." forever -- a fresh QR code per pass, no way out but killing
    DeckOps.

    The child: an exception while reading DepotDownloader's output returned
    None without stopping it, leaving a download running against the staging
    directory the caller had already given up on.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.logs = []

    def _fake_dd(self, body: str) -> str:
        path = self.root / "DepotDownloader"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return str(path)

    def _run(self, binary, on_log=None, **kw):
        with patch.object(depot, "DEPOTDOWNLOADER_BIN", binary), \
             patch.object(depot, "DEPOTDOWNLOADER_DIR", str(self.root)):
            return depot.run_depot_download_qr(
                staging_dir=str(self.staging),
                depot_info={"depot": 42681, "manifest": "1", "app": 42680},
                on_qr=lambda text: None,
                on_auth_success=lambda user: None,
                on_progress=lambda msg: None,
                on_log=on_log or self.logs.append,
                **kw)

    def test_an_unrecognised_failure_stops_instead_of_looping(self):
        # Exits 0 without any of _DD_ERROR_KEYWORDS: the old code read that as
        # "the QR code expired" and started another login, endlessly.
        binary = self._fake_dd('echo "something we do not recognise"\nexit 0')
        self.assertIsNone(self._run(binary))
        self.assertLessEqual(len(self.logs), depot.MAX_QR_RETRIES * 3,
                             f"kept retrying: {self.logs}")

    def test_the_retry_cap_is_reachable(self):
        binary = self._fake_dd("exit 0")
        self.assertIsNone(self._run(binary, max_retries=1))
        self.assertTrue(any("Max QR login retries reached" in m for m in self.logs), self.logs)

    def test_max_retries_zero_no_longer_means_unlimited(self):
        """0 used to disable the cap entirely; a caller passing 0 wanted
        "no retries", not "retry forever"."""
        binary = self._fake_dd("exit 0")
        self.assertIsNone(self._run(binary, max_retries=0))
        self.assertTrue(any("Max QR login retries reached" in m for m in self.logs), self.logs)

    def test_a_failed_read_does_not_leave_depotdownloader_running(self):
        pidfile = self.root / "dd.pid"
        # Writes its pid, prints one line, then stays alive: the line reaches
        # the caller's callback, which raises mid-read like a real one can.
        binary = self._fake_dd(f'echo $$ > "{pidfile}"\necho "starting"\nsleep 120')
        # The callback raises mid-read; the function reports the failure
        # rather than propagating it (its callers only look at None).
        self.assertIsNone(self._run(binary, on_log=self._boom_after_first))
        self.assertTrue(pidfile.exists(), "fake DepotDownloader never started")
        for _ in range(50):
            if not _pid_alive(int(pidfile.read_text())):
                break
            time.sleep(0.1)
        self.assertFalse(_pid_alive(int(pidfile.read_text())),
                         "DepotDownloader was left running after the caller gave up")

    def _boom_after_first(self, message):
        if "starting" in message:
            raise RuntimeError("callback failed")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


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
        for appid in ("10190", "7940"):
            self.prefix(appid)
        for target, value in (("config.get_ge_proton_version", "GE-Proton10-1"),
                              ("config.is_lcd", False)):
            p = patch(target, return_value=value)
            p.start(); self.addCleanup(p.stop)
        self.write(tool="GE-Proton10-1",
                   option='DECKOPS_MENU=\\"iw4mp.exe=iw4x-launcher.exe\\" bash \\"%s/prelaunch.sh\\" menu %%command%%' % self.inst.as_posix())

    def prefix(self, appid):
        win = self.root / "steamapps" / "compatdata" / appid / "pfx" / "drive_c" / "windows"
        for d in ("system32", "syswow64"):
            (win / d).mkdir(parents=True, exist_ok=True)
        return win

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

    def test_missing_app_block_is_a_missing_launch_option(self):
        self.write(tool="GE-Proton10-1", option="")
        local = self.root / "userdata" / "1" / "config" / "localconfig.vdf"
        local.write_text(self.LOCAL % "")
        r = self.check()
        self.assertTrue(any("launch option is missing" in p for p in r["problems"]), r)

    def test_unrelated_launch_option_is_not_healthy(self):
        self.write(tool="GE-Proton10-1", option="echo %command%")
        r = self.check()
        self.assertTrue(any("does not run the DeckOps client" in p for p in r["problems"]), r)
        self.assertTrue(r["fixable"])

    def test_launch_option_calling_a_missing_deckops_file(self):
        (self.inst / "prelaunch.sh").unlink()
        r = self.check()
        self.assertTrue(any("no longer exists" in p and "prelaunch.sh" in p for p in r["problems"]), r)

    def test_ge_proton_mapping(self):
        opt = 'DECKOPS_MENU=\\"iw4mp.exe=iw4x-launcher.exe\\" bash \\"%s/prelaunch.sh\\" menu %%command%%' % self.inst.as_posix()
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

    def test_update_rebuilds_shared_cod4_launch_option(self):
        """Fix (Update) on CoD4R MP + IW3SP SP: install_iw3sp leaves only the
        SP exe swap on 7940. Update must rebuild the combined option, or
        Check My Setup flags both modes again after every Fix."""
        from types import SimpleNamespace
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            import ui_manage
        except ImportError:
            # Same rule as the other UI tests in this file: a host without a
            # usable PyQt skips them instead of erroring the whole run.
            self.skipTest("PyQt is not available")
        from iw3sp import _build_iw3sp_launch_option
        cod4 = Path(self.tmp.name, "games", "CoD4"); cod4.mkdir()
        for marker in ("deckops_cod4r.json", "deckops_iw3sp.json"):
            (cod4 / marker).write_text("{}")
        game = {"install_dir": str(cod4), "source": "steam"}
        self.installed = {"cod4mp": game, "cod4sp": game}
        self.setup = {"cod4mp": {"client": "cod4r", "source": "steam"},
                      "cod4sp": {"client": "iw3sp", "source": "steam"}}
        for target, value in (("wrapper.LEDGER_PATH", str(Path(self.tmp.name, "ledger.json"))),
                              ("steam_common.PROJECT_ROOT", self.inst.as_posix())):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        p = patch("config.get_setup_games", return_value=self.setup)
        p.start(); self.addCleanup(p.stop)
        # What the Update loop leaves behind: only the IW3SP swap
        self.write(tool="GE-Proton10-1", appid="7940",
                   option=_build_iw3sp_launch_option().replace('"', '\\"'))
        results = self.sc.check_setup(str(self.root), self.installed, self.setup)
        self.assertTrue(all(any("does not run the DeckOps client" in p for p in r["problems"])
                            for r in results), results)

        logs, failed = [], []
        screen = SimpleNamespace(steam_root=str(self.root), _failed_games=failed,
                                 _s=SimpleNamespace(log=SimpleNamespace(emit=logs.append)))
        succeeded = [("cod4mp", {}, game), ("cod4sp", {}, game)]
        ui_manage.UpdateScreen._apply_cod4_launch_option(screen, succeeded)

        self.assertEqual(failed, [])
        self.assertIn("✓  CoD4 launch option set", logs)
        [opt] = self.sc._launch_options(str(self.root), "7940")
        self.assertEqual(opt.count("%command%"), 1, opt)
        results = self.sc.check_setup(str(self.root), self.installed, self.setup)
        self.assertEqual([r["problems"] for r in results], [[], []], results)

    def test_other_ge_proton_than_deckops_set_up(self):
        opt = 'DECKOPS_MENU=\\"iw4mp.exe=iw4x-launcher.exe\\" bash \\"%s/prelaunch.sh\\" menu %%command%%' % self.inst.as_posix()
        (self.root / "compatibilitytools.d" / "GE-Proton10-2").mkdir()
        with patch("config.get_ge_proton_version", return_value="GE-Proton10-2"):
            r = self.check()
            self.assertTrue(any("GE-Proton10-1" in p and "DeckOps set up GE-Proton10-2" in p
                                for p in r["problems"]), r)
            self.assertTrue(r["fixable"])
        # DeckOps's version is gone from disk: the mapped one is the best there is
        with patch("config.get_ge_proton_version", return_value="GE-Proton10-9"):
            self.assertEqual(self.check()["problems"], [])
        self.write(tool="proton_9", option=opt)            # the player's own choice
        with patch("config.get_ge_proton_version", return_value="GE-Proton10-2"):
            self.assertEqual(self.check()["problems"], [])

    def test_missing_or_incomplete_prefix(self):
        import shutil
        win = self.root / "steamapps" / "compatdata" / "10190" / "pfx" / "drive_c" / "windows"
        shutil.rmtree(win / "syswow64")
        r = self.check()
        self.assertIn("Its Proton prefix is incomplete.", r["problems"])
        self.assertTrue(r["fixable"])
        shutil.rmtree(self.root / "steamapps" / "compatdata" / "10190")
        r = self.check()
        self.assertIn(self.sc.PREFIX_MISSING, r["problems"])
        self.assertTrue(r["fixable"])

    def test_lcd_plutonium_runs_in_heroic_prefix(self):
        import shutil
        shutil.rmtree(self.root / "steamapps" / "compatdata")
        bo2 = Path(self.tmp.name, "games", "BO2"); bo2.mkdir()
        (bo2 / "deckops_plutonium.json").write_text("{}")
        self.installed = {"t6mp": {"install_dir": str(bo2)}}
        self.setup = {"t6mp": {"client": "plutonium", "source": "steam"}}
        self.write(tool="GE-Proton10-1", option='bash \\"%s/prelaunch.sh\\" %%command%%'
                   % self.inst.as_posix(), appid="202990")
        with patch("config.is_lcd", return_value=True):
            self.assertNotIn(self.sc.PREFIX_MISSING, self.check()["problems"])
        self.assertIn(self.sc.PREFIX_MISSING, self.check()["problems"])

    def test_prefix_links(self):
        import shutil
        win = self.root / "steamapps" / "compatdata" / "10190" / "pfx" / "drive_c" / "windows"
        shutil.rmtree(win / "system32")
        try:
            os.symlink(str(Path(self.tmp.name, "shared_dlls", "system32")), str(win / "system32"))
        except OSError:
            self.skipTest("symlinks not available")
        self.assertIn("Its Proton prefix uses a shared DLL folder that is missing.", self.check()["problems"])
        os.remove(win / "system32")
        (win / "system32").mkdir()
        os.symlink("/x/compatibilitytools.d/GE-Proton9-1/files/lib/wine/x86_64-windows/kernel32.dll",
                   str(win / "system32" / "kernel32.dll"))
        r = self.check()
        self.assertIn("Its Proton prefix links to a GE-Proton version that was removed.", r["problems"])
        self.assertTrue(r["fixable"])

    def test_shortcuts(self):
        import shortcut
        expected = {"cod4mp": ("steam", "CoD4 MP"), "t6mp": ("heroic", "BO2 MP (HGL)")}
        maps = {"111": "GE-Proton10-1"}
        with patch.object(shortcut, "expected_shortcuts", return_value=(expected, True)), \
             patch.object(shortcut, "present_shortcut_names",
                          return_value={"CoD4 MP", "BO2 MP (HGL)", shortcut.LAUNCHER_TITLE}), \
             patch.object(shortcut, "get_shortcut_appid", return_value=111), \
             patch.object(self.sc, "_compat_tool", side_effect=lambda sr, a: maps.get(a, "")):
            r = self.sc.check_shortcuts(str(self.root), {})
            self.assertEqual((r["problems"], r["fix"]), ([], "shortcuts"))
            maps.clear()                                   # CoD4 MP lost its compat tool
            r = self.sc.check_shortcuts(str(self.root), {})
            self.assertEqual(r["problems"], ["CoD4 MP is not set to run with GE-Proton."])
            self.assertTrue(r["fixable"])
            maps["111"] = "GE-Proton9-1"                   # removed GE-Proton
            self.assertIn("not installed", self.sc.check_shortcuts(str(self.root), {})["problems"][0])
        with patch.object(shortcut, "expected_shortcuts", return_value=(expected, True)), \
             patch.object(shortcut, "present_shortcut_names", return_value={"CoD4 MP"}), \
             patch.object(shortcut, "get_shortcut_appid", return_value=111), \
             patch.object(self.sc, "_compat_tool", return_value="GE-Proton10-1"):
            r = self.sc.check_shortcuts(str(self.root), {})
            self.assertEqual(r["problems"], ["BO2 MP (HGL) is missing from Steam.",
                                             f"{shortcut.LAUNCHER_TITLE} is missing from Steam."])
        with patch.object(shortcut, "expected_shortcuts", return_value=({}, False)):
            self.assertIsNone(self.sc.check_shortcuts(str(self.root), {}))

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

    def test_shortcut_fix_runs_repair_shortcuts(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication, QPushButton
        except ImportError:
            self.skipTest("PyQt6 not installed")
        app = _qapp(QApplication)
        import ui_manage
        screen = ui_manage.ConfigureScreen(None)
        results = [{"key": "shortcuts", "name": "DeckOps shortcuts", "fix": "shortcuts",
                    "problems": ["CoD4 MP is missing from Steam."], "fixable": True}]
        with patch.object(ui_manage.QDialog, "exec", return_value=0), \
             patch.object(screen, "_repair_shortcuts") as repair, \
             patch.object(screen, "_fix_game") as fix_game:
            screen._show_setup_report(results, {})
            [fix] = [b for b in screen._setup_dialog.findChildren(QPushButton) if b.text() == "Fix"]
            fix.click()
            app.processEvents()
        repair.assert_called_once_with()
        fix_game.assert_not_called()


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


class PreflightProbeTests(unittest.TestCase):
    def setUp(self):
        import preflight
        self.pf = preflight
        for p in (patch.object(preflight, "_online_result", None),
                  patch.object(preflight, "_online_thread", None)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_hung_lookup_costs_the_wait_not_the_hang(self):
        import time
        release = __import__("threading").Event()
        self.addCleanup(release.set)

        def hang(*a, **k):
            release.wait(30)
            raise OSError("never reached")

        with patch.object(self.pf, "_ONLINE_WAIT", 0.2), \
             patch.object(self.pf.socket, "create_connection", side_effect=hang):
            t0 = time.monotonic()
            self.assertFalse(self.pf._online())
            self.assertLess(time.monotonic() - t0, 3)

    def test_a_fresh_result_is_reused_without_another_probe(self):
        conn = unittest.mock.MagicMock()
        with patch.object(self.pf.socket, "create_connection", return_value=conn) as cc:
            self.assertTrue(self.pf._online())
            self.assertTrue(self.pf._online())
        self.assertEqual(cc.call_count, 1)

    def test_unreadable_free_space_warns_instead_of_blocking(self):
        with patch.object(os, "statvfs", side_effect=OSError("no such device"), create=True):
            self.assertIsNone(self.pf.free_gb("/definitely/not/a/real/path"))
        with patch.object(self.pf, "free_gb", return_value=None), \
             patch.object(self.pf, "_on_ac", return_value=True), \
             patch.object(self.pf, "_online", return_value=True), \
             patch("depot_downgrade.estimate_space_gb", return_value=5), \
             patch("steam_common.default_steam_root", return_value="/x"):
            checks = self.pf.check([("iw5mp", "steam", tempfile.gettempdir())], downgrade_keys=["iw5mp"])
        self.assertNotIn(self.pf.BLOCK, [c.level for c in checks])
        self.assertTrue(any("Could not check disk space" in c.title for c in checks))


class ConfigConcurrentUpdateTests(unittest.TestCase):
    """Two threads each doing load -> change -> save used to lose one change."""

    def setUp(self):
        import config
        self.cfg = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(config, "CONFIG_PATH", str(Path(self.tmp.name, "deckops.json"))),
                  patch.object(config, "_cache", None), patch.object(config, "_cache_key", None)):
            p.start()
            self.addCleanup(p.stop)

    def test_parallel_setters_keep_every_change(self):
        import threading
        keys = [f"game{i}" for i in range(40)]
        start = threading.Barrier(len(keys) + 1)

        def mark(k):
            start.wait()
            self.cfg.mark_game_setup(k, "client")

        def ui_setting():
            start.wait()
            for i in range(20):
                self.cfg.set_music_volume(i / 20)

        threads = [threading.Thread(target=mark, args=(k,)) for k in keys] + \
                  [threading.Thread(target=ui_setting)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(self.cfg.get_setup_games()), sorted(keys))

    def test_transaction_is_reentrant(self):
        with self.cfg.transaction():
            self.cfg.set_os_type("steamos")
            with self.cfg.transaction():
                self.cfg.set_deck_model("oled")
        self.assertEqual(self.cfg.get_deck_model(), "oled")


class PlutoniumCopySafetyTests(unittest.TestCase):
    def setUp(self):
        import plutonium
        self.pl = plutonium
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.master = self.root / "master"
        (self.master / "bin").mkdir(parents=True)
        (self.master / "bin" / "plutonium.exe").write_text("exe")
        (self.master / "config.json").write_text("{}")
        self.dest = self.root / "prefix" / "Plutonium"
        self.dest.mkdir(parents=True)
        (self.dest / "old.txt").write_text("working copy")

    def _fresh(self, key="t4mp"):
        self.pl._copy_plut_fresh(str(self.master), str(self.dest), key, lambda m: None)

    def test_missing_master_leaves_the_working_copy_alone(self):
        import shutil
        shutil.rmtree(self.master / "bin")
        with self.assertRaises(FileNotFoundError):
            self._fresh()
        self.assertEqual((self.dest / "old.txt").read_text(), "working copy")

    def test_failed_build_keeps_the_working_copy_and_leaves_no_staging(self):
        with patch.object(self.pl, "_build_plut_copy", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._fresh()
        self.assertEqual((self.dest / "old.txt").read_text(), "working copy")
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["Plutonium"])

    @unittest.skipUnless(hasattr(os, "symlink") and sys.platform != "win32", "needs symlinks")
    def test_success_swaps_in_the_new_copy_and_drops_the_old_one(self):
        self._fresh()
        self.assertFalse((self.dest / "old.txt").exists())
        self.assertTrue((self.dest / "config.json").is_file())
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["Plutonium"])

    def test_corrupt_plutonium_config_is_rewritten_not_fatal(self):
        cfg = self.dest / "config.json"
        cfg.write_text('{"token": "abc"')   # cut off mid-write
        self.pl._write_config(str(self.dest), [], {})
        self.assertEqual(json.loads(cfg.read_text()), {})
        self.assertFalse((self.dest / "config.json.tmp").exists())

    def test_zip_download_never_leaves_a_partial_final_file(self):
        import zipfile
        zpath = self.root / "src.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("pack/z_svr_bots.iwd", b"payload")
        dst = self.root / "server" / "z_svr_bots.iwd"
        dst.parent.mkdir()

        def fake_download(url, path, digest=None):
            Path(path).write_bytes(zpath.read_bytes())

        with patch.object(self.pl, "download", side_effect=fake_download):
            self.pl._lan_download(str(dst), "http://x/pack.zip", None, lambda m: None)
            self.assertEqual(dst.read_bytes(), b"payload")
            self.assertFalse(Path(str(dst) + ".part").exists())
            dst.unlink()
            with patch("shutil.copyfileobj", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.pl._lan_download(str(dst), "http://x/pack.zip", None, lambda m: None)
        self.assertFalse(dst.exists(), "a failed copy must not leave a file setup_lan would skip")
        self.assertFalse(Path(str(dst) + ".part").exists())


class PlutoniumUpdateSafetyTests(unittest.TestCase):
    def setUp(self):
        import plutonium_update
        self.pu = plutonium_update
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plut = self.root / "plut"
        (self.plut / "bin").mkdir(parents=True)

    def _info(self, files):
        import hashlib
        return {"revision": 9, "baseUrl": "http://cdn/",
                "files": [{"name": n, "hash": hashlib.sha1(b).hexdigest()} for n, b in files.items()]}

    def test_a_failed_swap_restores_the_whole_previous_revision(self):
        files = {"bin/a.exe": b"new a", "bin/b.dll": b"new b"}
        for n in files:
            (self.plut / n).write_bytes(b"old " + n.encode())
        (self.plut / "info.json").write_text('{"revision":8}')
        by_hash = {e["hash"]: files[e["name"]] for e in self._info(files)["files"]}

        def download(url, dest, *a, **k):
            Path(dest).write_bytes(by_hash[url.rsplit("/", 1)[1]])

        original = os.replace

        def fail(source, dest):
            if os.path.normpath(dest) == os.path.normpath(self.plut / "bin" / "b.dll") \
                    and str(source).endswith(".new"):
                raise OSError("swap failed")
            return original(source, dest)

        with patch.object(self.pu, "download", side_effect=download), \
             patch.object(self.pu.os, "replace", side_effect=fail):
            with self.assertRaisesRegex(OSError, "swap failed"):
                self.pu.sync_master(str(self.plut), {}, self._info(files))
        self.assertEqual((self.plut / "bin" / "a.exe").read_bytes(), b"old bin/a.exe")
        self.assertEqual((self.plut / "bin" / "b.dll").read_bytes(), b"old bin/b.dll")
        self.assertEqual(self.pu.local_revision(str(self.plut)), 8)

    def test_info_json_is_written_whole(self):
        self.pu.sync_master(str(self.plut), {}, {"revision": 9, "baseUrl": "x", "files": []})
        self.assertEqual(self.pu.local_revision(str(self.plut)), 9)
        self.assertFalse((self.plut / "info.json.tmp").exists())

    def test_yeet_patterns_cannot_delete_outside_the_plutonium_folder(self):
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "keep.txt"
        victim.write_text("mine")
        (self.plut / "bin" / "old.dll").write_text("stale")
        logs = []
        n = self.pu._yeet(str(self.plut), ["../outside/*", str(outside) + "/*", "bin/*.dll"], set(), logs.append)
        self.assertEqual(victim.read_text(), "mine")
        self.assertFalse((self.plut / "bin" / "old.dll").exists())
        self.assertEqual(n, 1)


class IwPadStagingTests(unittest.TestCase):
    """IW-Pad is updated before every launch; it must not be half-applied."""

    def setUp(self):
        import sp_mod
        self.sp = sp_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.game = Path(self.tmp.name, "game")
        self.game.mkdir()
        (self.game / "iw-pad.exe").write_bytes(b"old exe")
        (self.game / "iw-pad.dll").write_bytes(b"old dll")

    def _zip(self, members):
        import zipfile
        path = Path(self.tmp.name, "src.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return path

    def _fetch(self, zpath, **kw):
        def fake_download(url, dest, *a, **k):
            fake_download.kw = k
            Path(dest).write_bytes(Path(zpath).read_bytes())
        with patch.object(self.sp, "download", side_effect=fake_download), \
             patch("config.set_iw_pad"):
            self.sp._fetch_iw_pad("iw4sp", str(self.game), "x64", lambda p, m: None,
                                  release=("https://h/r/download/v1/iw-pad-x64.zip", "sha256:a"), **kw)
        return fake_download.kw

    def test_both_files_are_replaced_and_nothing_is_left_behind(self):
        self._fetch(self._zip({"iw-pad.exe": b"new exe", "iw-pad.dll": b"new dll"}))
        self.assertEqual((self.game / "iw-pad.exe").read_bytes(), b"new exe")
        self.assertEqual((self.game / "iw-pad.dll").read_bytes(), b"new dll")
        self.assertEqual(sorted(p.name for p in self.game.iterdir()), ["iw-pad.dll", "iw-pad.exe"])

    def test_a_bad_archive_leaves_the_installed_pair_untouched(self):
        with self.assertRaises(ValueError):
            self._fetch(self._zip({"iw-pad.exe": b"new exe", "../escape.txt": b"x"}))
        self.assertEqual((self.game / "iw-pad.exe").read_bytes(), b"old exe")
        self.assertEqual((self.game / "iw-pad.dll").read_bytes(), b"old dll")
        self.assertEqual(sorted(p.name for p in self.game.iterdir()), ["iw-pad.dll", "iw-pad.exe"])
        self.assertFalse(Path(self.tmp.name, "escape.txt").exists())
        self.assertFalse((self.game.parent / "escape.txt").exists())

    def test_prelaunch_update_makes_one_short_attempt(self):
        import config
        seen = {}
        with patch.object(self.sp, "github_asset", return_value=("https://h/r/download/v2/iw-pad-x64.zip", "sha256:b")), \
             patch.object(config, "get_iw_pad", return_value={}), \
             patch.object(config, "get_setup_games", return_value={}), \
             patch.object(self.sp, "_fetch_iw_pad", side_effect=lambda *a, **k: seen.update(k)):
            self.sp.update_iw_pad("iw4sp", str(self.game))
        self.assertEqual((seen["tries"], seen["timeout"]), (1, 10))


class Cod4xRegistryTests(unittest.TestCase):
    def test_registry_files_are_rewritten_whole_and_keep_foreign_bytes(self):
        import cod4x
        with tempfile.TemporaryDirectory() as tmp:
            pfx = Path(tmp) / "pfx"
            pfx.mkdir()
            (pfx / "user.reg").write_bytes(b'WINE REGISTRY Version 2\n[Software\\\\Other]\n"k"="caf\xe9"\n')
            (pfx / "system.reg").write_bytes(b'WINE REGISTRY Version 2\n')
            self.assertTrue(cod4x._write_registry_keys(tmp))
            self.assertTrue(cod4x._write_install_path_registry(tmp, "/games/CoD4"))
            user = (pfx / "user.reg").read_bytes()
            self.assertIn(b'"k"="caf\xe9"', user)
            self.assertIn(b'"Installed"=dword:00000001', user)
            self.assertIn(b'"InstallPath"="Z:', (pfx / "system.reg").read_bytes())
            self.assertEqual(sorted(p.name for p in pfx.iterdir()), ["system.reg", "user.reg"])


class GeProtonInstallHousekeepingTests(unittest.TestCase):
    def test_checksum_fetch_retries_then_continues_unverified(self):
        import ge_proton
        import urllib.error
        digest = "a" * 128
        good = unittest.mock.MagicMock()
        good.__enter__.return_value.read.return_value = f"{digest}  GE-Proton10-1.tar.gz\n".encode()
        with patch.object(ge_proton.urllib.request, "urlopen",
                          side_effect=[urllib.error.URLError("blip"), good]) as op, \
             patch("time.sleep"):
            self.assertEqual(ge_proton._fetch_checksum("http://x/sum"), "sha512:" + digest)
        self.assertEqual(op.call_count, 2)
        with patch.object(ge_proton.urllib.request, "urlopen", side_effect=OSError("down")) as op, \
             patch("time.sleep"):
            self.assertIsNone(ge_proton._fetch_checksum("http://x/sum"))
        self.assertEqual(op.call_count, 3)

    def test_leftover_extraction_staging_is_cleared(self):
        import ge_proton
        with tempfile.TemporaryDirectory() as tmp:
            compat = Path(tmp)
            (compat / ".deckops_extract_old" / "GE-Proton9-1").mkdir(parents=True)
            (compat / "GE-Proton10-1").mkdir()      # an installed tool is not staging
            archive = Path(tmp).parent / f"{compat.name}-empty.tar.gz"
            import tarfile
            tarfile.open(archive, "w:gz").close()
            self.addCleanup(lambda: archive.exists() and archive.unlink())
            with patch.object(ge_proton, "_compat_dir", return_value=str(compat)), \
                 patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "x")):
                with self.assertRaises(RuntimeError):       # an empty archive: nothing complete in it
                    ge_proton._extract_atomically(str(archive), "GE-Proton10-2")
            self.assertEqual(sorted(p.name for p in compat.iterdir()), ["GE-Proton10-1"])


class FovwatchChunkTests(unittest.TestCase):
    class Mem:
        def __init__(self, base, data, holes=()):
            self.base, self.data, self.holes = base, data, holes   # holes: (start, end) unreadable

        def read(self, addr, n):
            if any(s <= addr < e for s, e in self.holes):
                return b""
            off = addr - self.base
            if off < 0 or off >= len(self.data):
                return b""
            out = self.data[off:off + n]
            for s, e in self.holes:              # a read stops at the first unreadable byte
                if addr < s < addr + len(out):
                    out = out[:s - addr]
            return out

    def test_a_needle_across_a_chunk_boundary_is_found(self):
        import fovwatch
        needle = b"\x00com_maxfps\x00"
        size = 1 << 12
        data = bytearray(b"A" * (size * 3))
        at = size - 5                           # straddles the first boundary
        data[at:at + len(needle)] = needle
        mem = self.Mem(0x1000, bytes(data))
        hits = set()
        for pos, chunk in fovwatch._chunks(mem, 0x1000, 0x1000 + len(data), size=size):
            i = chunk.find(needle)
            while i != -1:
                hits.add(pos + i)
                i = chunk.find(needle, i + 1)
        self.assertEqual(hits, {0x1000 + at})

    def test_an_unreadable_page_does_not_end_the_region(self):
        import fovwatch
        page = fovwatch._PAGE
        data = b"x" * (page * 8)
        mem = self.Mem(0, data, holes=[(page * 2, page * 3)])
        covered = set()
        for pos, chunk in fovwatch._chunks(mem, 0, len(data), size=page * 2):
            covered.update(range(pos, pos + len(chunk)))
        self.assertIn(page * 6, covered)         # past the hole
        self.assertNotIn(page * 2, covered)      # the hole itself


class InhibitAndSteamCloseTests(unittest.TestCase):
    def test_a_dead_inhibit_process_is_retaken_not_trusted(self):
        import inhibit
        dead = unittest.mock.MagicMock(); dead.poll.return_value = 1; dead.returncode = 1
        live = unittest.mock.MagicMock(); live.poll.return_value = None
        # _busy is module-global and start() sets it: restore it, or a later UI test's
        # window asks "quit while busy?" in a modal dialog and the run never ends
        with patch.object(inhibit, "_proc", dead), patch.object(inhibit, "_cookie", None), \
             patch.object(inhibit, "_busy", None), \
             patch.object(inhibit, "_ss_inhibit", return_value=None), \
             patch.object(inhibit.subprocess, "Popen", return_value=live) as popen:
            self.assertTrue(inhibit.start("test"))
            self.assertIs(inhibit._proc, live)
        popen.assert_called_once()

    def test_start_reports_false_when_the_only_lock_has_already_exited(self):
        import inhibit
        dead = unittest.mock.MagicMock(); dead.poll.return_value = 1
        with patch.object(inhibit, "_proc", None), patch.object(inhibit, "_cookie", None), \
             patch.object(inhibit, "_busy", None), \
             patch.object(inhibit, "_ss_inhibit", return_value=None), \
             patch.object(inhibit.subprocess, "Popen", return_value=dead):
            self.assertFalse(inhibit.start("test"))

    def test_steam_close_matches_the_client_not_its_game_wrappers(self):
        import re
        import wrapper
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1)      # "not running": returns at once

        with patch.object(wrapper.subprocess, "run", side_effect=fake_run), \
             patch.object(wrapper, "_reapply_launch_menus"):
            wrapper.kill_steam()
        pattern = next(c[2] for c in calls if c[0] == "pgrep")
        client = "/home/deck/.local/share/Steam/ubuntu12_32/steam -srt-logger-opened"
        self.assertRegex(client, pattern)
        self.assertRegex("/home/deck/.local/share/Steam/ubuntu12_32/steam", pattern)
        for helper in ("/x/ubuntu12_32/steam-launch-wrapper -- /x/reaper SteamLaunch AppId=1",
                       "/x/ubuntu12_32/steam-runtime-launcher-service --alongside-steam"):
            self.assertIsNone(re.search(pattern, helper), helper)


class GameConfigRenameTests(unittest.TestCase):
    def test_player_rename_changes_only_the_name(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config_mp.cfg"
            path.write_bytes(b'seta name "Old"\r\nseta sensitivity "1.5"\r\nbind x "caf\xe9"\r\n')
            with patch("config.clean_player_name", side_effect=lambda n: n):
                gc._replace_player_name(str(path), "NewName")
            self.assertEqual(path.read_bytes(),
                             b'seta name "NewName"\r\nseta sensitivity "1.5"\r\nbind x "caf\xe9"\r\n')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["config_mp.cfg"])

    def test_lan_script_rename_survives_a_damaged_setup_entry(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "t6mp-lan.sh"
            script.write_bytes(b'#!/bin/bash\nexec game +name "Old" +x\n')
            games = {"t4mp": "not a dict", "t6mp": {"lan_wrapper_path": str(script), "source": "own"}}
            with patch("config.get_setup_games", return_value=games), \
                 patch("plutonium.bash_name_arg", side_effect=lambda n: f'+name "{n}"'):
                self.assertEqual(gc._rename_in_lan_scripts("New", {}, lambda m: None), 1)
            self.assertIn(b'+name "New"', script.read_bytes())

    def test_applying_configs_does_not_add_sibling_games_to_the_callers_dict(self):
        import game_config as gc
        games = {"iw5mp": {"install_dir": "/g", "exe_path": "/g/iw5mp.exe"}}
        with patch.object(gc, "_build_config_map", return_value={}), \
             patch("config.get_player_name", return_value=None), \
             patch("config.get_model_config_dir", return_value="Other/1920x1200"):
            gc.apply_game_configs(["iw5mp"], games, "/steam", "other")
        self.assertEqual(list(games), ["iw5mp"])


class ShortcutIndexAndArtworkTests(unittest.TestCase):
    def test_next_index_reads_entries_whose_first_field_is_not_the_appid(self):
        import shortcut as sc
        # An entry written by another tool: AppName first, appid later
        entry = lambda i: (b"\x00" + str(i).encode() + b"\x00\x01AppName\x00Game\x00"
                           b"\x02appid\x00\x01\x00\x00\x00\x08")
        self.assertEqual(sc._get_next_index(entry(0) + entry(1) + entry(4)), 5)
        self.assertEqual(sc._get_next_index(b""), 0)

    def test_artwork_cleanup_only_removes_this_appids_files(self):
        import shortcut as sc
        with tempfile.TemporaryDirectory() as tmp:
            grid = Path(tmp)
            mine = ["10090.png", "10090p.jpg", "10090_hero.png", "10090_logo.png", "10090_icon.ico"]
            others = ["100900p.jpg", "100901.png", "10090abc.png", "210090.png"]
            for n in mine + others:
                (grid / n).write_bytes(b"x")
            with patch.object(sc, "ThreadPoolExecutor", create=True):
                sc._download_artwork(str(grid), 10090, {"icon_ext": "ico", "grid_ext": "jpg",
                                                         "wide_ext": "png", "hero_ext": "png",
                                                         "logo_ext": "png"}, lambda m: None,
                                     clean_stale=True)
            left = sorted(p.name for p in grid.iterdir())
            self.assertEqual(left, sorted(others))


class LauncherShortcutIdempotenceTests(unittest.TestCase):
    """create_launcher_shortcut runs on install, repair and the Offline toggle.
    A launcher shortcut that is already correct must be left exactly as it is,
    not stripped and rewritten (which reset launch options and LastPlayTime)."""

    def setUp(self):
        import shortcut
        self.sc = shortcut
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.install = root / "DeckOps"
        for rel in (shortcut._LAUNCHER_SH_REL, shortcut._LAUNCHER_EXE_REL):
            p = self.install / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
        self.userdata = root / "userdata"
        self.vdf = self.userdata / "42" / "config" / "shortcuts.vdf"
        self.vdf.parent.mkdir(parents=True)
        for p in (patch.object(shortcut, "INSTALL_DIR", str(self.install)),
                  patch.object(shortcut, "_userdata_dir", return_value=str(self.userdata)),
                  patch.object(shortcut, "_find_all_steam_uids", return_value=["42"]),
                  patch.object(shortcut, "_download_artwork"),
                  patch.object(shortcut, "_assign_controller_config"),
                  patch("wrapper.clear_compat_tool"),
                  patch.object(shortcut, "_clear_compat_tool")):
            p.start()
            self.addCleanup(p.stop)

    def test_a_current_launcher_shortcut_is_not_rewritten(self):
        self.sc.create_launcher_shortcut(lambda m: None)
        first = self.vdf.read_bytes()
        self.assertIn(self.sc.LAUNCHER_TITLE.encode(), first)
        # A user edit Steam keeps (hidden from the library); a rewrite would reset it
        hidden_off = b"\x02IsHidden\x00\x00\x00\x00\x00"
        self.assertIn(hidden_off, first)
        edited = first.replace(hidden_off, b"\x02IsHidden\x00\x01\x00\x00\x00")
        self.vdf.write_bytes(edited)
        self.sc.create_launcher_shortcut(lambda m: None)
        self.assertEqual(self.vdf.read_bytes(), edited)
        self.assertEqual(first.count(self.sc.LAUNCHER_TITLE.encode()), 1)

    def test_a_launcher_shortcut_under_an_old_appid_is_replaced(self):
        sc = self.sc
        old_exe = f'"{sc._OLD_LAUNCHER_EXE_DIRECT}"'
        old_appid = sc._calc_shortcut_appid(old_exe, sc.LAUNCHER_TITLE)
        entry = sc._make_shortcut_entry(0, sc._shortcut_entry(
            old_appid, sc.LAUNCHER_TITLE, old_exe, '"/old"', "", ""))
        sc._write_shortcuts_vdf(str(self.vdf), b"", [entry])
        sc.create_launcher_shortcut(lambda m: None)
        raw = sc._read_shortcuts_raw(str(self.vdf))
        new_exe = f'"{self.install / sc._LAUNCHER_SH_REL}"'
        self.assertEqual(sc._entry_appids(raw, sc.LAUNCHER_TITLE),
                         [sc._calc_shortcut_appid(new_exe, sc.LAUNCHER_TITLE)])


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

    def _run_update_games(self, backup=None, confirm=None, *, ge_version=None,
                          installer=None, snapshot=None, stop_after_first=False):
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
        if stop_after_first:
            second = next(g for g in ALL_GAMES if "t6sp" in g.get("keys", []))
            screen.selected.append(("t6sp", second, {"install_dir": str(Path(self.root, "bo2")),
                                                     "source": "steam"}))
        order = []

        def fake_backup(*a, failures=None, **kw):
            order.append("backup")
            return backup(failures) if backup else {}

        def fake_install(*a, **kw):
            order.append("install")
            if installer is not None:
                result = installer()
                if stop_after_first:
                    screen._stop_requested.set()
                return result
            raise RuntimeError("download failed")
        with patch.object(ui_manage, "_log_to_file"), \
             patch.object(ui_manage, "inhibit"), \
             patch.object(ui_manage.cfg, "is_lcd", return_value=False), \
             patch.object(ui_manage.cfg, "get_setup_games", return_value={
                 "iw4mp": {"client": "iw4x"}, "t6sp": {"client": "t6sp_mod"}}), \
             patch.object(screen, "_confirm", create=True, side_effect=lambda text: confirm), \
             patch("save_backup.backup_saves", side_effect=fake_backup), \
             patch("wrapper.kill_steam", side_effect=lambda **kw: order.append("close steam")), \
             patch("wrapper.get_proton_path", return_value="proton"), \
             patch("wrapper.find_compatdata", return_value=self.root), \
             patch("wrapper.snapshot_launch_options",
                   side_effect=snapshot if snapshot is not None else self.wrapper.snapshot_launch_options), \
             patch("wrapper.set_compat_tool", side_effect=lambda appids, version, **kwargs: order.append(
                 ("compat", set(appids))) or True), \
             patch("wrapper.set_steam_input_enabled"), patch.object(ui_manage.cfg, "mark_game_setup"), \
             patch("ge_proton.install_ge_proton", side_effect=OSError("offline")), \
             patch("ge_proton.resolve_ge_version", return_value=ge_version), \
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

    def test_missing_launch_option_snapshot_aborts_before_clearing(self):
        before = self.vdf.read_bytes()
        screen, order = self._run_update_games(snapshot=OSError("cannot snapshot"))
        self.assertEqual(order, ["backup", "close steam"])
        self.assertEqual(self.vdf.read_bytes(), before)
        self.assertEqual([k for k, _, _ in screen._failed_games], ["iw4mp"])

    def test_successful_single_game_sets_compat_for_its_appid_only(self):
        screen, order = self._run_update_games(ge_version="GE-Test", installer=lambda: None)
        self.assertEqual(order, ["backup", "close steam", "install", ("compat", {"10190"})])
        self.assertFalse(screen._failed_games)
        self.assertEqual(self._options("42690"), "other %command%")

    def test_stop_after_current_game_leaves_others_retryable(self):
        screen, order = self._run_update_games(
            ge_version="GE-Test", installer=lambda: None, stop_after_first=True)
        self.assertEqual([k for k, _, _ in screen._failed_games], ["t6sp"])
        self.assertTrue(screen._cancelled)
        self.assertIn(("compat", {"10190"}), order)
        self.assertEqual(order[:3], ["backup", "close steam", "install"])
        self.assertLess(screen.bar.value(), 100)
        self.assertFalse(screen.retry_btn.isHidden())

    def test_incomplete_backup_and_player_says_no_changes_nothing(self):
        before = self.vdf.read_text()
        screen, order = self._run_update_games(backup=lambda f: f.append("cod4") or {}, confirm=False)
        self.assertEqual(order, ["backup"])
        self.assertEqual(self.vdf.read_text(), before)
        self.assertEqual(screen.cur.text(), "Update cancelled before changing game files or settings.")

    def test_incomplete_backup_and_player_says_yes_continues(self):
        _, order = self._run_update_games(backup=lambda f: f.append("cod4") or {}, confirm=True)
        self.assertEqual(order, ["backup", "close steam", "install"])

    def test_backup_message_is_honest(self):
        import save_backup as sb
        self.assertIn("incomplete for: cod4", sb.describe_backup({"mw2": "x"}, ["cod4"]))
        self.assertIn("Could not create", sb.describe_backup({}, ["*"]))
        self.assertTrue(sb.describe_backup({"mw2": "x"}, []).startswith("✓"))
        self.assertEqual(sb.describe_backup({}, []), "No save data found to back up.")


class MergedMpTileTests(unittest.TestCase):
    """CoD4R MP and WaW MP (non-LCD) run from the main Steam tile, so their
    old non-Steam shortcuts are dropped, but only once the tile can launch MP."""

    def _setup(self, cod4_client="cod4r"):
        return {"cod4mp": {"client": cod4_client, "source": "steam"},
                "t4mp": {"client": "plutonium", "source": "steam"}}

    def test_decision_follows_client_and_device(self):
        import shortcut
        with patch("config.get_setup_games", return_value=self._setup("cod4r")), \
             patch("config.is_lcd", return_value=False):
            self.assertTrue(shortcut.mp_on_main_tile("cod4mp"))
            self.assertTrue(shortcut.mp_on_main_tile("t4mp"))
            self.assertFalse(shortcut.mp_on_main_tile("iw4mp"))
        with patch("config.get_setup_games", return_value=self._setup("cod4x")), \
             patch("config.is_lcd", return_value=True):
            self.assertFalse(shortcut.mp_on_main_tile("cod4mp"))   # CoD4x keeps its shortcut
            self.assertFalse(shortcut.mp_on_main_tile("t4mp"))     # LCD keeps its Heroic shortcut

    def _remove(self, files):
        import shortcut
        with tempfile.TemporaryDirectory() as d:
            for n in files:
                Path(d, n).write_text("x")
            games = {"cod4mp": {"install_dir": d}, "t4mp": {"install_dir": d}}
            with patch("config.get_setup_games", return_value=self._setup()), \
                 patch("config.is_lcd", return_value=False), \
                 patch("detect_games.parse_library_folders", return_value=[]), \
                 patch("detect_games.find_installed_games", return_value=games), \
                 patch.object(shortcut, "get_shortcut_appid", return_value=123), \
                 patch.object(shortcut, "remove_shortcut") as rm, \
                 patch.object(shortcut, "_clear_compat_tool") as clear:
                removed = shortcut.remove_merged_mp_shortcuts("steam")
        return removed, rm, clear

    def test_shortcuts_removed_when_main_tile_is_ready(self):
        removed, rm, clear = self._remove(["deckops_cod4r.json", "t4plutmp.exe"])
        self.assertEqual(sorted(removed), ["Call of Duty 4: Modern Warfare - Multiplayer",
                                           "Call of Duty: World at War - Multiplayer"])
        self.assertEqual(rm.call_count, 2)
        clear.assert_called_with("123")

    def test_shortcut_kept_while_main_tile_is_not_ready(self):
        removed, rm, _ = self._remove(["deckops_cod4r.json"])
        self.assertEqual(removed, ["Call of Duty 4: Modern Warfare - Multiplayer"])
        self.assertEqual(rm.call_count, 1)


class HostMenuTests(unittest.TestCase):
    """MW3 / BO1 / BO2: the MP and ZM Plutonium entries also go on the SP
    tile's launch menu, but only when the SP game is in the same folder."""

    def _add(self, key, files, host_dir=None, lcd=False):
        import plutonium
        with tempfile.TemporaryDirectory() as d:
            for n in files:
                Path(d, n).write_text("x")
            host_key = plutonium.HOST_MENU[key][1]
            installed = {host_key: {"install_dir": d if host_dir is None else host_dir}}
            with patch("config.is_lcd", return_value=lcd), \
                 patch("steam_appinfo.add_launch_entries", return_value=True) as add:
                ok = plutonium.add_host_menu({"install_dir": d}, key, "steam", installed=installed)
        return ok, add

    def test_entry_goes_on_the_sp_appid(self):
        for key, appid, exe in (("t5mp", 42700, "t5plutmp.exe"),
                                ("t6mp", 202970, "t6plutmp.exe"), ("t6zm", 202970, "t6plutzm.exe")):
            ok, add = self._add(key, [exe])
            self.assertTrue(ok, key)
            args = add.call_args[0]
            self.assertEqual(args[1], appid)
            self.assertEqual(args[2][0]["executable"], exe)

    def test_skipped_when_wrapper_is_missing(self):
        ok, add = self._add("t6mp", [])
        self.assertFalse(ok)
        add.assert_not_called()

    def test_skipped_when_sp_is_in_another_folder(self):
        ok, add = self._add("t6mp", ["t6plutmp.exe"], host_dir=os.path.join(tempfile.gettempdir(), "elsewhere"))
        self.assertFalse(ok)
        add.assert_not_called()

    def test_skipped_on_lcd(self):
        ok, add = self._add("t6mp", ["t6plutmp.exe"], lcd=True)
        self.assertFalse(ok)
        add.assert_not_called()

    def test_stock_sp_entry_is_relabelled_with_the_host_entry(self):
        _, add = self._add("t6zm", ["t6plutzm.exe"])
        self.assertIn({"executable": "t6sp.exe", "label": "Campaign"}, add.call_args[0][2])

    def test_mw3_has_no_host_entry_and_its_mp_tile_starts_plutonium_directly(self):
        import plutonium
        self.assertNotIn("iw5mp", plutonium.HOST_MENU)
        self.assertIn(42690, plutonium.HIDE_STOCK_APPIDS)

    def test_old_mw3_host_entry_is_removed_and_its_label_forgotten(self):
        import plutonium
        with patch("steam_appinfo.remove_launch_entries") as rm, \
             patch("steam_appinfo.forget_launch_entries") as forget:
            plutonium.remove_retired_host_menus("steam")
        rm.assert_called_once_with("steam", 42680, ["iw5plutmp.exe"])
        forget.assert_called_once_with(42680, ["iw5sp.exe"])

    def test_bo1_host_entry_does_not_relabel_stock(self):
        _, add = self._add("t5mp", ["t5plutmp.exe"])
        self.assertEqual(len(add.call_args[0][2]), 1)

    def test_picker_labels(self):
        import plutonium
        L = plutonium.STEAM_MENU_LABELS
        self.assertEqual([L[k][0] for k in ("t4sp", "t4mp", "t5sp", "t5mp", "t6mp", "t6zm", "iw5mp")],
                         ["Campaign & Zombies", "Multiplayer", "Campaign & Zombies", "Multiplayer",
                          "Multiplayer", "Zombies", "Multiplayer"])
        # the host tile uses the same words
        self.assertEqual({k: v[2] for k, v in plutonium.HOST_MENU.items()},
                         {k: L[k][0] for k in plutonium.HOST_MENU})

    def test_refresh_rebuilds_each_set_up_game_once(self):
        import plutonium
        setup = {"t5mp": {"client": "plutonium", "source": "steam"},
                 "t4sp": {"client": "plutonium", "source": "steam"},
                 "iw4mp": {"client": "iw4x", "source": "steam"}}
        installed = {"t5mp": {"install_dir": "a"}, "t4sp": {"install_dir": "b"}, "iw4mp": {"install_dir": "c"}}
        with patch("config.is_lcd", return_value=False), \
             patch("config.get_setup_games", return_value=setup), \
             patch("detect_games.parse_library_folders", return_value=[]), \
             patch("detect_games.find_installed_games", return_value=installed), \
             patch.object(plutonium, "remove_retired_host_menus") as retired, \
             patch.object(plutonium, "add_steam_menu") as menu:
            done = plutonium.refresh_steam_menus("steam")
        self.assertEqual(sorted(done), ["t4sp", "t5mp"])
        self.assertEqual(menu.call_count, 2)
        retired.assert_called_once()


class Iw4xMenuTests(unittest.TestCase):
    """MW2 MP tile (10190): IW4x only, stock entry hidden."""

    def _refresh(self, files, setup=None):
        import iw4x
        setup = setup or {"iw4mp": {"client": "iw4x", "source": "steam"}}
        with tempfile.TemporaryDirectory() as d:
            for n in files:
                Path(d, n).write_text("x")
            with patch("config.get_setup_games", return_value=setup), \
                 patch("detect_games.parse_library_folders", return_value=[]), \
                 patch("detect_games.find_installed_games", return_value={"iw4mp": {"install_dir": d}}), \
                 patch("steam_appinfo.add_launch_entries", return_value=True) as add:
                ok = iw4x.refresh_steam_menu("steam")
        return ok, add

    def test_menu_is_iw4x_plus_hidden_stock_entry(self):
        ok, add = self._refresh(["iw4x-launcher.exe"])
        self.assertTrue(ok)
        entries = add.call_args[0][2]
        self.assertEqual(add.call_args[0][1], 10190)
        self.assertEqual(entries[0]["executable"], "iw4x-launcher.exe")
        self.assertIn({"executable": "iw4mp.exe", "hide": True}, entries)

    def test_stock_entry_stays_while_the_launcher_is_missing(self):
        ok, add = self._refresh([])
        self.assertFalse(ok)
        add.assert_not_called()

    def test_other_clients_are_skipped(self):
        ok, add = self._refresh(["iw4x-launcher.exe"], setup={"iw4mp": {"client": "steam"}})
        self.assertFalse(ok)
        add.assert_not_called()


class LaunchMenuEditTests(unittest.TestCase):
    """steam_appinfo: hiding and relabelling Steam's own launch entries."""

    def _launch(self):
        import steam_appinfo as a
        S, D = a._T_STR, a._T_DICT
        return [
            [D, "0", [[S, "executable", b"BlackOps.exe"], [D, "config", [[S, "oslist", b"windows"]]]]],
            [D, "1", [[S, "executable", b"CoDWaW.exe"]]],
            [D, "2", [[S, "executable", b"t6sp.exe"], [S, "description", b"Single Player"]]],
            [D, "3", [[S, "executable", b"iw5sp.exe"]]],
        ]

    def _run(self, launch, entries):
        import steam_appinfo as a
        with patch.object(a, "_edit_launch", side_effect=lambda root, appid, edit: edit(launch)), \
             patch.object(a, "_record"):
            a.add_launch_entries("root", 1, entries)
        return launch

    def _get(self, launch, exe, key):
        import steam_appinfo as a
        for _, _, node in launch:
            if a._get(node, "executable")[2] == exe:
                return a._get(node, key)
        raise AssertionError(exe)

    def test_hide_flags_stock_entry_as_macos_whether_or_not_it_has_a_config(self):
        import steam_appinfo as a
        launch = self._run(self._launch(), [{"executable": "BlackOps.exe", "hide": True},
                                            {"executable": "CoDWaW.exe", "hide": True}])
        for exe in (b"BlackOps.exe", b"CoDWaW.exe"):
            cfg = self._get(launch, exe, "config")
            self.assertEqual(a._get(cfg[2], "oslist")[2], b"macos")
        # an entry nobody asked about is left alone
        self.assertIsNone(self._get(launch, b"iw5sp.exe", "config"))

    def test_label_sets_or_replaces_the_description(self):
        launch = self._run(self._launch(), [{"executable": "t6sp.exe", "label": "Campaign"},
                                            {"executable": "iw5sp.exe", "label": "Campaign & Spec Ops"}])
        self.assertEqual(self._get(launch, b"t6sp.exe", "description")[2], b"Campaign")
        self.assertEqual(self._get(launch, b"iw5sp.exe", "description")[2], b"Campaign & Spec Ops")

    def test_edits_are_idempotent(self):
        import steam_appinfo as a
        launch = self._launch()
        entries = [{"executable": "BlackOps.exe", "hide": True}, {"executable": "t6sp.exe", "label": "Campaign"}]
        self._run(launch, entries)
        snapshot = repr(launch)
        with patch.object(a, "_edit_launch", side_effect=lambda r, ap, edit: self.assertFalse(edit(launch)) or True), \
             patch.object(a, "_record"):
            a.add_launch_entries("root", 1, entries)
        self.assertEqual(repr(launch), snapshot)


class SpLaunchOptionRepairTests(unittest.TestCase):
    """Repair puts IW-Pad's launch option back, but never replaces a working one."""

    def _repair(self, current, setup=None):
        import sp_mod
        setup = setup or {"iw5sp": {"client": "iw-pad", "source": "steam"}}
        with patch("config.get_setup_games", return_value=setup), \
             patch("wrapper.snapshot_launch_options", return_value=current), \
             patch("wrapper.clear_launch_options") as clear, \
             patch("wrapper.set_launch_options") as set_:
            fixed = sp_mod.repair_sp_launch_options("steam")
        return fixed, clear, set_

    def test_alterware_option_is_replaced(self):
        old = {"42680": {"u": "eval $(echo \\\"%command%\\\" | sed 's/iw5sp.exe/alterware-launcher.exe/')"}}
        fixed, clear, set_ = self._repair(old)
        self.assertEqual(fixed, ["iw5sp"])
        clear.assert_called_once_with("steam", 42680)
        self.assertIn("iw-pad.exe", set_.call_args[0][2])

    def test_missing_option_is_set(self):
        fixed, _, set_ = self._repair({})
        self.assertEqual(fixed, ["iw5sp"])
        set_.assert_called_once()

    def test_working_option_is_left_alone(self):
        import sp_mod
        ok = {"42680": {"u": sp_mod.build_sp_launch_option("iw5sp").replace('"', '\\"')}}
        fixed, clear, set_ = self._repair(ok)
        self.assertEqual(fixed, [])
        clear.assert_not_called()
        set_.assert_not_called()

    def _cap(self, on):
        return patch("config.get_dxvk_frame_cap", return_value=on)

    def test_frame_cap_is_on_by_default(self):
        import config
        with patch("config.load", return_value={}):
            self.assertTrue(config.get_dxvk_frame_cap())
        with patch("config.load", return_value={"dxvk_frame_cap": False}):
            self.assertFalse(config.get_dxvk_frame_cap())

    def test_frame_cap_can_be_switched_off(self):
        import sp_mod
        with self._cap(False):
            for key in ("iw4sp", "iw5sp"):
                opt = sp_mod.build_sp_launch_option(key)
                self.assertNotIn("DXVK_CONFIG", opt)
                self.assertIn("iw-pad.exe", opt)

    def test_frame_cap_can_be_switched_on(self):
        import sp_mod
        with self._cap(True):
            for key in ("iw4sp", "iw5sp"):
                self.assertTrue(sp_mod.build_sp_launch_option(key).startswith(
                    'DXVK_CONFIG="d3d9.maxFrameRate=90" '), key)

    def test_option_with_the_cap_is_replaced_while_the_setting_is_off(self):
        import sp_mod
        with self._cap(True):
            capped = sp_mod.build_sp_launch_option("iw4sp").replace('"', '\\"')
        with self._cap(False):
            fixed, clear, set_ = self._repair({"10180": {"u": capped}},
                                              setup={"iw4sp": {"client": "iw-pad", "source": "steam"}})
        self.assertEqual(fixed, ["iw4sp"])
        clear.assert_called_once_with("steam", 10180)
        self.assertNotIn("DXVK_CONFIG", set_.call_args[0][2])

    def test_option_without_the_cap_is_replaced_while_the_setting_is_on(self):
        old = {"42680": {"u": "bash -c 'exec \\\"${@/iw5sp.exe/iw-pad.exe}\\\"' -- %command%"}}
        with self._cap(True):
            fixed, clear, set_ = self._repair(old)
        self.assertEqual(fixed, ["iw5sp"])
        self.assertIn("d3d9.maxFrameRate=90", set_.call_args[0][2])

    def test_option_matching_the_setting_is_left_alone(self):
        import sp_mod
        for on in (False, True):
            with self._cap(on):
                ok = {"10180": {"u": sp_mod.build_sp_launch_option("iw4sp").replace('"', '\\"')}}
                fixed, clear, set_ = self._repair(ok, setup={"iw4sp": {"client": "iw-pad", "source": "steam"}})
            self.assertEqual(fixed, [])
            clear.assert_not_called()
            set_.assert_not_called()

    def test_games_not_set_up_with_iw_pad_are_skipped(self):
        fixed, clear, _ = self._repair({}, setup={"iw5sp": {"client": "steam", "source": "steam"}})
        self.assertEqual(fixed, [])
        clear.assert_not_called()


class PrelaunchCampaignCleanupTests(unittest.TestCase):
    """MW2 / MW3 campaign: old DeckOps lines are taken out of config.cfg before launch."""

    def _run(self, key, folder, cfg_text):
        import prelaunch
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / folder).mkdir()
            cfg = Path(d, folder, "config.cfg")
            cfg.write_bytes(cfg_text.encode())
            with patch("sp_mod.update_iw_pad") as upd:
                prelaunch._iwpad(key, ["proton", "run", os.path.join(d, f"{key}.exe")])
            return cfg.read_bytes().decode(), upd

    def test_mw2_loses_the_cheat_unlock_and_the_f10_fov_key(self):
        old = ('seta sensitivity "5"\r\nseta thereisacow "1337"\r\n'
               'bind F10 "cg_fov 90"\r\nbind w "+forward"\r\n')
        text, upd = self._run("iw4sp", "players", old)
        self.assertEqual(text, 'seta sensitivity "5"\r\nbind w "+forward"\r\n')
        upd.assert_called_once()

    def test_a_different_f10_bind_of_your_own_is_kept(self):
        text, _ = self._run("iw4sp", "players", 'bind F10 "screenshot"\r\n')
        self.assertEqual(text, 'bind F10 "screenshot"\r\n')

    def test_mw3_also_resets_the_old_fov_scale(self):
        old = ('seta thereisacow "1337"\r\nseta cg_fovScale "1.385"\r\n'
               'bind F10 "cg_fov 90; cg_fovscale 1.385"\r\nseta sensitivity "5"\r\n')
        text, _ = self._run("iw5sp", "players2", old)
        self.assertEqual(text, 'seta cg_fovScale "1"\r\nseta sensitivity "5"\r\n')

    def test_nothing_is_written_when_there_is_nothing_to_remove(self):
        text, _ = self._run("iw4sp", "players", 'seta sensitivity "5"\r\n')
        self.assertEqual(text, 'seta sensitivity "5"\r\n')
        self.assertNotIn("cg_fov", text)

    def test_other_keys_are_not_touched(self):
        import prelaunch
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "players").mkdir()
            cfg = Path(d, "players", "config.cfg")
            cfg.write_bytes(b'seta thereisacow "1337"\r\n')
            prelaunch._clean_campaign_config("iw3sp", d)
            self.assertEqual(cfg.read_bytes(), b'seta thereisacow "1337"\r\n')


class FakeMem:
    """Stands in for ProcMem: address-space regions kept as byte buffers."""

    def __init__(self, regions):
        self.regions = regions            # {start: bytearray}

    def _find(self, addr, n):
        for start, buf in self.regions.items():
            if start <= addr and addr + n <= start + len(buf):
                return start, buf
        return None

    def read(self, addr, n):
        hit = self._find(addr, 1)
        if not hit:
            return b""
        start, buf = hit
        return bytes(buf[addr - start:addr - start + n])

    def write(self, addr, data):
        hit = self._find(addr, len(data))
        if not hit:
            return False
        start, buf = hit
        buf[addr - start:addr - start + len(data)] = data
        return True


class GuideTextTests(unittest.TestCase):
    """Setup Complete screen: what to press to start each set-up game."""

    def _text(self, setup, lcd=False):
        import guide_text
        return guide_text.how_to_start_text(setup, lcd)

    def test_steam_games_describe_the_menu_buttons(self):
        text = self._text({"cod4sp": {"client": "iw3sp"}, "cod4mp": {"client": "cod4r"},
                           "iw4mp": {"client": "iw4x"}, "iw5mp": {"client": "plutonium"},
                           "t4sp": {"client": "plutonium"}, "t5mp": {"client": "plutonium"},
                           "t6zm": {"client": "plutonium"}})
        self.assertIn("MW1: Play starts the campaign", text)
        self.assertIn("starts CoD4R", text)
        self.assertIn("MW2: Play starts the campaign. The Multiplayer button in its main menu starts IW4x.", text)
        self.assertIn("starts Plutonium", text)
        self.assertIn("WaW, BO1 and BO2: press Play and pick", text)
        self.assertNotIn("shortcut", text.lower())          # the old separate-shortcut advice is gone

    def test_cod4x_still_has_its_own_entry(self):
        text = self._text({"cod4mp": {"client": "cod4x"}})
        self.assertIn("separate DeckOps entry", text)
        self.assertNotIn("CoD4R", text)

    def test_only_set_up_games_are_mentioned(self):
        text = self._text({"t6mp": {"client": "plutonium"}})
        self.assertEqual(text, "BO2: press Play and pick Campaign, Zombies or Multiplayer from the menu.")
        self.assertEqual(self._text({}), "")

    def test_lcd_waw_and_non_steam_games_get_their_own_notes(self):
        text = self._text({"t4mp": {"client": "plutonium"}}, lcd=True)
        self.assertIn("WaW multiplayer is a separate DeckOps entry on the LCD Steam Deck.", text)
        own = self._text({"iw4mp": {"client": "iw4x", "source": "own"}})
        self.assertIn("non-Steam have a separate DeckOps entry for each mode", own)
        self.assertNotIn("MW2: Play starts", own)


class FovWatchTests(unittest.TestCase):
    """fovwatch: find dvar_t records in the game's memory and hold their values."""

    BASE = 0x140000000
    EXE = "iw5sp.exe"
    FOV_STR, FPS_STR = 0x101, 0x121       # name string offsets inside the .rdata region

    def _world(self, fov=65.0, fps=85, strings=(b"cg_fov", b"com_maxfps")):
        import struct
        rdata, data = bytearray(0x1000), bytearray(0x1000)
        for off, s in zip((0x100, 0x120), strings):
            rdata[off:off + len(s) + 2] = b"\x00" + s + b"\x00"
        # dvar_t records: name pointer at +0, current value at +0x10
        data[0x200:0x208] = struct.pack("<Q", self.BASE + 0x1000 + self.FOV_STR)
        data[0x210:0x214] = struct.pack("<f", fov)
        data[0x300:0x308] = struct.pack("<Q", self.BASE + 0x1000 + self.FPS_STR)
        data[0x310:0x314] = struct.pack("<i", fps)
        mem = FakeMem({self.BASE + 0x1000: rdata, self.BASE + 0x2000: data})
        maps = [(self.BASE + 0x1000, self.BASE + 0x2000, "r--p", f"/g/{self.EXE}"),
                (self.BASE + 0x2000, self.BASE + 0x3000, "rw-p", f"/g/{self.EXE}")]
        return mem, maps

    def _find(self, mem, maps, names=("cg_fov", "com_maxfps"), **kw):
        import fovwatch
        return fovwatch.find_dvars(mem, maps, self.EXE, names, log=lambda m: None, **kw)

    def test_finds_both_dvars(self):
        mem, maps = self._world()
        self.assertEqual(self._find(mem, maps),
                         {"cg_fov": self.BASE + 0x2000 + 0x210, "com_maxfps": self.BASE + 0x2000 + 0x310})

    def test_ignores_records_with_unreasonable_values(self):
        mem, maps = self._world(fov=0.0, fps=0)
        self.assertEqual(self._find(mem, maps), {})

    def test_one_dvar_can_be_found_without_the_other(self):
        mem, maps = self._world(strings=(b"cg_fov", b"com_other"))
        self.assertEqual(list(self._find(mem, maps)), ["cg_fov"])

    def test_settings_are_parsed_by_name_and_type(self):
        import fovwatch
        self.assertEqual(fovwatch.parse_settings(["cg_fov=90", "com_maxfps=90"]),
                         {"cg_fov": 90.0, "com_maxfps": 90})
        with self.assertRaises(ValueError):
            fovwatch.parse_settings(["g_gravity=1"])

    def test_search_backs_off_and_the_heap_scan_is_limited(self):
        import fovwatch
        waits = [fovwatch.search_plan(n) for n in range(fovwatch.FAST_TRIES + 3)]
        self.assertEqual(waits[0], (5.0, True))
        self.assertEqual(sum(1 for _, heap in waits if heap), fovwatch.HEAP_TRIES)
        self.assertEqual(waits[fovwatch.FAST_TRIES - 1][0], 5.0)
        self.assertEqual(waits[fovwatch.FAST_TRIES], (fovwatch.SLOW_EVERY, False))

    def test_heap_scan_can_be_switched_off(self):
        import struct
        mem, maps = self._world()
        # the cg_fov record only exists in an anonymous region outside the image window
        far = 0x7f0000000000
        heap = bytearray(0x1000)
        heap[0x40:0x48] = struct.pack("<Q", self.BASE + 0x1000 + self.FOV_STR)
        heap[0x50:0x54] = struct.pack("<f", 65.0)
        mem.regions[far] = heap
        mem.regions[self.BASE + 0x2000] = bytearray(0x1000)         # nothing in the image's data
        maps = maps + [(far, far + 0x1000, "rw-p", "")]
        self.assertEqual(self._find(mem, maps, heap=False), {})
        self.assertEqual(self._find(mem, maps, heap=True), {"cg_fov": far + 0x50})

    def test_hold_writes_floats_and_ints_only_when_the_value_differs(self):
        import struct
        import fovwatch
        mem, maps = self._world()
        found = self._find(mem, maps)
        fov, fps = found["cg_fov"], found["com_maxfps"]
        self.assertTrue(fovwatch.hold(mem, fov, "cg_fov", 90.0))
        self.assertEqual(struct.unpack("<f", mem.read(fov, 4))[0], 90.0)
        self.assertTrue(fovwatch.hold(mem, fps, "com_maxfps", 90))
        self.assertEqual(struct.unpack("<i", mem.read(fps, 4))[0], 90)
        self.assertFalse(fovwatch.hold(mem, fov, "cg_fov", 90.0))     # already there
        self.assertFalse(fovwatch.hold(mem, fps, "com_maxfps", 90))
        mem.write(fov, struct.pack("<f", 65.0))                        # a level load puts 65 back
        self.assertTrue(fovwatch.hold(mem, fov, "cg_fov", 90.0))
        self.assertIsNone(fovwatch.hold(mem, fov + 0x10000, "cg_fov", 90.0))  # unreadable

    def test_child_gets_steams_preload_back_and_runs_to_completion(self):
        import fovwatch
        with patch.dict(os.environ, {"DECKOPS_LD_PRELOAD": "/steam/overlay.so", "DECKOPS_LD_LIBRARY_PATH": ""}):
            env = fovwatch._child_env()
        self.assertEqual(env["LD_PRELOAD"], "/steam/overlay.so")
        self.assertNotIn("DECKOPS_LD_PRELOAD", env)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        # the game's exit code is passed on even if the watcher can't do anything (no /proc here)
        rc = fovwatch.main(["iw5sp.exe", "cg_fov=90", "com_maxfps=90", "--",
                            sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(rc, 3)

    def test_bad_settings_do_not_stop_the_game(self):
        import fovwatch
        rc = fovwatch.main(["iw5sp.exe", "bogus=1", "--", sys.executable, "-c", "import sys; sys.exit(4)"])
        self.assertEqual(rc, 4)


class FpsFovTests(unittest.TestCase):
    """game_config.set_fps_fov: only cg_fov and com_maxfps change, in place."""

    V = {"cg_fov": "90", "com_maxfps": "90"}

    def _file(self, d, name, text):
        p = Path(d, name)
        p.write_bytes(text.encode())
        return p

    def test_existing_lines_get_the_new_value_and_nothing_else_changes(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, "c.cfg", 'seta name "Me"\r\nseta cg_fov "65"\r\nseta cg_fovScale "1"\r\n'
                                       'seta com_maxfps "85"\r\nseta sensitivity "5"\r\n')
            self.assertTrue(gc._patch_dvars(str(p), self.V))
            self.assertEqual(p.read_bytes().decode(),
                             'seta name "Me"\r\nseta cg_fov "90"\r\nseta cg_fovScale "1"\r\n'
                             'seta com_maxfps "90"\r\nseta sensitivity "5"\r\n')
            self.assertFalse(gc._patch_dvars(str(p), self.V))     # second run: nothing to do

    def test_missing_lines_are_added_keeping_line_endings(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, "c.cfg", 'seta cg_fovScale "1"\r\nseta sensitivity "5"')
            gc._patch_dvars(str(p), self.V)
            self.assertEqual(p.read_bytes().decode(),
                             'seta cg_fovScale "1"\r\nseta cg_fov "90"\r\nseta sensitivity "5"\r\n'
                             'seta com_maxfps "90"\r\n')

    def test_cg_fovscale_is_not_mistaken_for_cg_fov(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, "c.cfg", 'seta cg_fovScale "1.2"\n')
            gc._patch_dvars(str(p), {"cg_fov": "90"})
            self.assertIn('seta cg_fovScale "1.2"', p.read_text())

    def test_missing_file_is_only_made_on_request(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "players", "config_sp.cfg")
            self.assertFalse(gc._patch_dvars(str(p), self.V))
            self.assertFalse(p.exists())
            self.assertTrue(gc._patch_dvars(str(p), self.V, create=True))
            self.assertEqual(p.read_text(), 'seta cg_fov "90"\nseta com_maxfps "90"\n')

    def test_binds_are_set_or_added(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, "c.cfg", 'bind F10 "something"\r\nbind w "+forward"\r\n')
            gc._patch_dvars(str(p), {}, binds={"F10": "cg_fov 90", "F11": "x"})
            self.assertEqual(p.read_bytes().decode(),
                             'bind F10 "cg_fov 90"\r\nbind w "+forward"\r\nbind F11 "x"\r\n')

    def test_apply_leaves_the_mw2_and_mw3_campaign_configs_alone(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            mw2, mw3 = Path(d, "mw2"), Path(d, "mw3")
            (mw2 / "players").mkdir(parents=True)
            (mw3 / "players2").mkdir(parents=True)
            c2 = self._file(mw2 / "players", "config.cfg", 'seta com_maxfps "85"\r\n')
            c3 = self._file(mw3 / "players2", "config.cfg", 'seta com_maxfps "85"\r\n')
            games = {"iw4sp": {"install_dir": str(mw2)}, "iw5sp": {"install_dir": str(mw3)}}
            with patch("config.get_setup_games", return_value={"iw4sp": {"client": "iw-pad"},
                                                               "iw5sp": {"client": "iw-pad"}}), \
                 patch("config.get_player_name", return_value="Me"), \
                 patch("config.is_lcd", return_value=False):
                n = gc.set_fps_fov(d, games)
            self.assertEqual(n, 0)
            self.assertEqual(c2.read_text(), 'seta com_maxfps "85"\n')
            self.assertEqual(c3.read_text(), 'seta com_maxfps "85"\n')

    def test_drop_removes_matching_lines_only(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, "c.cfg", 'seta a "1"\r\nseta thereisacow "1337"\r\nseta b "2"')
            self.assertTrue(gc._patch_dvars(str(p), {}, drop=(gc._STALE_THEREISACOW,)))
            self.assertEqual(p.read_bytes().decode(), 'seta a "1"\r\nseta b "2"')
            self.assertFalse(gc._patch_dvars(str(p), {}, drop=(gc._STALE_THEREISACOW,)))

    def test_set_fps_fov_patches_mp_configs_and_the_bo2_autoexec(self):
        import game_config as gc
        with tempfile.TemporaryDirectory() as d:
            mw2, bo2 = Path(d, "mw2"), Path(d, "bo2")
            (mw2 / "players").mkdir(parents=True)
            cfg = self._file(mw2 / "players", "iw4x_config.cfg", 'seta com_maxfps "85"\r\n')
            bo2.mkdir()
            games = {"iw4mp": {"install_dir": str(mw2)}, "t6sp": {"install_dir": str(bo2)}}
            setup = {"iw4mp": {"client": "iw4x"}, "t6sp": {"client": "t6sp_mod"}}
            with patch("config.get_setup_games", return_value=setup), \
                 patch("config.get_player_name", return_value="Me"), \
                 patch("config.is_lcd", return_value=False):
                n = gc.set_fps_fov(d, games)
            self.assertIn('seta cg_fov "90"', cfg.read_text())
            self.assertIn('seta com_maxfps "90"', cfg.read_text())
            for folder in (bo2, bo2 / "players"):
                for name in ("config_sp.cfg", "persistent_sp.cfg"):
                    self.assertIn('seta cg_fov "90"', (folder / name).read_text())
            self.assertEqual(n, 5)


if __name__ == "__main__":
    unittest.main()
