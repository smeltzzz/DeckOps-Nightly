"""Startup, install-repair and concurrent config regressions (temporary roots only)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import fovwatch  # noqa: E402
import update_manifest  # noqa: E402

LINUX_SHELL = sys.platform == "linux" and bool(shutil.which("bash") and shutil.which("flock"))


def shell_text(name):
    return (ROOT / name).read_bytes().decode().replace("\r\n", "\n")


class ConfigProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "deckops.json")
        for name, value in (("CONFIG_PATH", str(self.path)), ("_cache", None),
                            ("_cache_key", None), ("_unreadable", None)):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_same_size_and_mtime_atomic_replacement_invalidates_cache(self):
        self.path.write_text('{"player_name":"Alice"}')
        self.assertEqual(config.get_player_name(), "Alice")
        st = self.path.stat()
        replacement = self.path.with_suffix(".new")
        replacement.write_text('{"player_name":"Bobbi"}')
        os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(replacement, self.path)
        self.assertEqual(config.get_player_name(), "Bobbi")

    @unittest.skipUnless(LINUX_SHELL, "requires Linux flock")
    def test_other_process_setter_waits_for_transaction_then_preserves_both_changes(self):
        config.set_player_name("Before")
        code = ("import sys\n"
                f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
                "import config\n"
                f"config.CONFIG_PATH = {str(self.path)!r}\n"
                "print('ready', flush=True)\n"
                "config.set_music_enabled(False)\n"
                "print('done', flush=True)\n")
        child = None
        try:
            with config.transaction():
                old = config.load()
                child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                time.sleep(0.3)
                self.assertIsNone(child.poll(), "second process bypassed the transaction lock")
                old["player_name"] = "After"
                config.save(old)
            out, err = child.communicate(timeout=10)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(child.returncode, 0, err)
        self.assertIn("done", out)
        self.assertEqual(config.get_player_name(), "After")
        self.assertFalse(config.get_music_enabled())


class InstallTransactionTests(unittest.TestCase):
    def test_interrupted_full_repair_restores_old_files_and_version(self):
        with tempfile.TemporaryDirectory() as d:
            install = Path(d, "install")
            stage = Path(d, "stage")
            for root in (install, stage):
                (root / "src").mkdir(parents=True)
            (install / "src" / "main.py").write_text("print('old app')\n")
            (install / "VERSION").write_text("a" * 40 + "\n")
            (stage / "src" / "main.py").write_text("print('new app')\n")
            (stage / "src" / "extra.py").write_text("print('new extra')\n")
            (stage / "deckops_identity.sh").write_text("BRANCH=nightly\n")
            real_replace = update_manifest._replace_file
            calls = []

            def interrupted(source, target):
                calls.append(str(target))
                if len(calls) == 2:
                    raise KeyboardInterrupt("power cut during copy")
                real_replace(source, target)

            with patch.object(update_manifest, "_replace_file", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    update_manifest.apply_update(str(install), str(stage), "b" * 40, full=True)
            self.assertTrue((install / ".update" / "transaction.json").exists())
            self.assertTrue(update_manifest.recover_update(str(install)))
            self.assertEqual((install / "src" / "main.py").read_text(), "print('old app')\n")
            self.assertFalse((install / "src" / "extra.py").exists())
            self.assertEqual((install / "VERSION").read_text().strip(), "a" * 40)

    def test_unpinned_full_install_records_zero_in_the_same_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            install = Path(d, "install")
            stage = Path(d, "stage")
            install.mkdir()
            (stage / "src").mkdir(parents=True)
            (stage / "src" / "main.py").write_text("print('new app')\n")
            (stage / "deckops_identity.sh").write_text("BRANCH=nightly\n")
            (stage / "VERSION").write_text("a" * 40 + "\n")
            update_manifest.apply_update(str(install), str(stage), "0", full=True)
            self.assertEqual((install / "VERSION").read_text().strip(), "0")
            self.assertEqual((install / "src" / "main.py").read_text(), "print('new app')\n")
            self.assertFalse((install / ".update" / "transaction.json").exists())


class FovWatchLaunchEnvironmentTests(unittest.TestCase):
    def test_helper_restores_the_games_python_environment(self):
        with patch.dict(os.environ, {"DECKOPS_PYTHONHOME": "/steam/python",
                                  "DECKOPS_PYTHONPATH": "/steam/modules",
                                  "PYTHONHOME": "/helper", "PYTHONPATH": "/helper/modules"}):
            env = fovwatch._child_env()
        self.assertEqual(env["PYTHONHOME"], "/steam/python")
        self.assertEqual(env["PYTHONPATH"], "/steam/modules")
        self.assertNotIn("DECKOPS_PYTHONPATH", env)
        self.assertIn('DECKOPS_PYTHONHOME="$PYTHONHOME"', shell_text("prelaunch.sh"))

    def test_empty_saved_python_values_do_not_leak_from_helper(self):
        with patch.dict(os.environ, {"DECKOPS_PYTHONHOME": "", "DECKOPS_PYTHONPATH": "",
                                  "PYTHONHOME": "/helper", "PYTHONPATH": "/helper/modules"}):
            env = fovwatch._child_env()
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)


@unittest.skipUnless(LINUX_SHELL, "requires Linux bash and flock")
class InstallerRepairTests(unittest.TestCase):
    def test_install_shell_commits_archive_and_version_together(self):
        text = shell_text("install.sh")
        block = text[text.index('[ -f "$INSTALL_DIR/VERSION" ] && PREVIOUS_SHA'):
                     text.index("# ── step 3b")]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            install = root / "install"
            stage = root / "stage"
            (install / "src").mkdir(parents=True)
            (install / "src" / "main.py").write_text("print('old')\n")
            (install / "VERSION").write_text("old\n")
            (install / "deckops.json").write_text('{"player_name":"keep"}')
            (stage / "src").mkdir(parents=True)
            (stage / "src" / "main.py").write_text("print('new')\n")
            (stage / "src" / "update_manifest.py").write_bytes((ROOT / "src" / "update_manifest.py").read_bytes())
            (stage / "deckops_identity.sh").write_text("BRANCH=nightly\n")
            (stage / "VERSION").write_text("tracked\n")
            script = (f'die() {{ echo "$1"; exit 1; }}\nsuccess() {{ :; }}\n'
                      f'INSTALL_DIR="{install}"\n'
                      f'EXTRACTED="{stage}"\nTMPDIR_EXTRACT="{root / "unused"}"\n'
                      f'INITIAL_SHA={"b" * 40}\nPREVIOUS_SHA=""\n{block}')
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((install / "VERSION").read_text().strip(), "b" * 40)
            self.assertEqual((install / "src" / "main.py").read_text(), "print('new')\n")
            self.assertEqual((install / "deckops.json").read_text(), '{"player_name":"keep"}')
            self.assertFalse((install / ".update" / "transaction.json").exists())

    def test_installer_refuses_to_replace_files_under_a_running_app(self):
        text = shell_text("install.sh")
        start = text.index('if command -v flock >/dev/null 2>&1; then')
        block = text[start:text.index('# A repair over an existing install:', start)]
        with tempfile.TemporaryDirectory() as d:
            lock = update_manifest.instance_lock(d)
            self.assertIsNotNone(lock)
            try:
                script = f'die() {{ echo "$1"; exit 1; }}\nINSTALL_DIR="{d}"\n{block}'
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
            finally:
                lock.close()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("already open", result.stdout)

    def test_unrecoverable_journal_stops_before_copy_and_keeps_backups(self):
        text = shell_text("install.sh")
        block = text[text.index('if [ -f "$INSTALL_DIR/.update/transaction.json" ]; then'):
                     text.index('[ -f "$INSTALL_DIR/VERSION" ] && PREVIOUS_SHA',
                                text.index('if [ -f "$INSTALL_DIR/.update/transaction.json" ]; then'))]
        with tempfile.TemporaryDirectory() as d:
            install = Path(d, "installed")
            stage = Path(d, "stage")
            (install / ".update" / "rollback").mkdir(parents=True)
            (install / ".update" / "transaction.json").write_text(json.dumps({"files": [["src/main.py", True]],
                                                                                 "version_existed": True}))
            (install / ".update" / "rollback" / "VERSION").write_text("old")
            (stage / "src").mkdir(parents=True)
            (stage / "src" / "update_manifest.py").write_bytes((ROOT / "src" / "update_manifest.py").read_bytes())
            script = f'die() {{ echo "$1"; exit 1; }}\nINSTALL_DIR="{install}"\nEXTRACTED="{stage}"\n{block}\n'
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("Could not undo", result.stdout)
            self.assertTrue((install / ".update" / "transaction.json").exists())
            self.assertEqual((install / ".update" / "rollback" / "VERSION").read_text(), "old")

    def test_launcher_checks_running_instance_again_after_update_prompt(self):
        text = shell_text("launcher.sh")
        block = text[text.index('stage_and_apply_update() {'):text.index('\ncheck_for_updates() {')]
        script = ("deckops_running() { return 0; }\n"
                  "recover_interrupted_update() { echo RECOVERED; }\n"
                  f"{block}\nstage_and_apply_update\necho \"RESULT=$UPDATE_RESULT\"\n")
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("RECOVERED", result.stdout)
        self.assertIn("RESULT=skipped", result.stdout)


if __name__ == "__main__":
    unittest.main()
