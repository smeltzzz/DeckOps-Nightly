"""Regressions for the fifth sweep of the startup, update and pre-launch files:
the pre-launch update's SIGTERM / time budget / DeckOps-update checks, unwritable
update locks, an unreadable deckops.json, and install.sh's version and messages."""

import builtins
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_HAS_BASH = sys.platform == "linux" and bool(shutil.which("bash"))
_HAS_FLOCK = _HAS_BASH and bool(shutil.which("flock"))


def _text(name):
    return (ROOT / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _between(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


# ── A1: SIGTERM from prelaunch.sh's timeout ──────────────────────────────────

class PrelaunchSigtermTests(unittest.TestCase):
    def test_sigterm_handler_raises_system_exit_not_exception(self):
        import prelaunch
        with self.assertRaises(SystemExit) as cm:
            prelaunch._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(cm.exception.code, 128 + signal.SIGTERM)
        # main() catches Exception only: SystemExit must get past it to the rollbacks
        self.assertFalse(issubclass(SystemExit, Exception))

    def test_handler_is_installed_and_shell_kills_after_a_grace_period(self):
        main_block = _text("src/prelaunch.py").split('if __name__ == "__main__":', 1)[1]
        self.assertIn("signal.signal(signal.SIGTERM, _on_sigterm)", main_block)
        self.assertIn("timeout -k 10 120 env", _text("prelaunch.sh"))

    def test_system_exit_mid_swap_puts_the_old_cod4r_files_back(self):
        import cod4r
        with tempfile.TemporaryDirectory() as d:
            install, compat = _cod4r_tree(d)
            m, files = _manifest(install, compat, {"a.dll": b"new-a", "b.dll": b"new-b"})
            for p in files:
                Path(p).write_bytes(b"old")
            real_replace = os.replace
            calls = []

            def replace(src, dst):
                calls.append(src)
                if len(calls) == 4:          # second file's .new -> place: SIGTERM lands here
                    raise SystemExit(143)
                return real_replace(src, dst)

            with patch.object(cod4r, "_fetch_manifest", return_value=m), \
                 patch.object(cod4r, "_download", side_effect=_fake_download(m)), \
                 patch.object(cod4r.os, "replace", side_effect=replace):
                with self.assertRaises(SystemExit):
                    cod4r.sync_files(install, compat)
            for p in files:
                self.assertEqual(Path(p).read_bytes(), b"old", p)

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    def test_real_sigterm_runs_finally_blocks_and_exits_143(self):
        code = (
            "import os, signal, sys, time\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "import prelaunch\n"
            "signal.signal(signal.SIGTERM, prelaunch._on_sigterm)\n"
            "try:\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "    time.sleep(10)\n"
            "finally:\n"
            "    print('rolled back', flush=True)\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": tempfile.gettempdir()})
        self.assertIn("rolled back", r.stdout)
        self.assertEqual(r.returncode, 143)

    @unittest.skipUnless(_HAS_BASH and shutil.which("timeout"), "requires Linux bash and timeout")
    def test_game_still_starts_when_the_update_is_terminated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / "prelaunch.sh").write_bytes(_text("prelaunch.sh").encode())
            (root / "src" / "prelaunch.py").write_text(
                "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
            (root / ".venv" / "bin" / "python3").symlink_to(sys.executable)
            r = subprocess.run(["bash", str(root / "prelaunch.sh"), "cod4mp", sys.executable,
                                "-c", "print('GAME STARTED')"],
                               capture_output=True, text=True, timeout=30, env={**os.environ, "DECKOPS_MENU": ""})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("GAME STARTED", r.stdout)


# ── A2: CoD4R pre-launch budget ──────────────────────────────────────────────

def _cod4r_tree(d):
    install = os.path.join(d, "game")
    compat = os.path.join(d, "compat")
    os.makedirs(install)
    os.makedirs(compat)
    return install, compat


def _manifest(install, compat, root_files):
    """A CoD4R manifest whose root assets are root_files; client and dedi already current."""
    import cod4r
    assets = [{"dest": "root", "name": n, "path": n, "size": len(b),
               "sha256": hashlib.sha256(b).hexdigest(), "_data": b} for n, b in root_files.items()]
    client, dedi = b"client", b"dedi"
    m = {"assets": assets,
         "client": {"path": "cod4r_1/cod4r_1.dll", "version": "1", "size": len(client),
                    "sha256": hashlib.sha256(client).hexdigest(), "_data": client},
         "dedi": {"path": "Ded.exe", "size": len(dedi), "sha256": hashlib.sha256(dedi).hexdigest(),
                  "_data": dedi}}
    paths = cod4r._manifest_files(m, install, compat)
    for p, e in paths[-2:]:                # client + dedi up to date: only the root files change
        os.makedirs(os.path.dirname(p), exist_ok=True)
        Path(p).write_bytes(e["_data"])
    return m, [p for p, _ in paths[:-2]]


def _fake_download(m, on_each=None):
    by_path = {e["path"]: e for e in m["assets"] + [m["client"], m["dedi"]]}

    def download(url, dest, on_progress=None, **kw):
        e = by_path[url.split("//", 1)[1].split("/", 1)[1]]     # http://host/<path>

        if on_each:
            on_each(kw)
        if on_progress:
            on_progress(50, "half")
        Path(dest).write_bytes(e["_data"])
    return download


class Cod4rBudgetTests(unittest.TestCase):
    def test_out_of_time_stops_before_any_file_is_swapped(self):
        import cod4r
        clock = [1000.0]
        with tempfile.TemporaryDirectory() as d:
            install, compat = _cod4r_tree(d)
            m, files = _manifest(install, compat, {"a.dll": b"new-a", "b.dll": b"new-b"})
            for p in files:
                Path(p).write_bytes(b"old")
            seen = []

            def slow(kw):
                seen.append(kw)
                clock[0] += 60                 # each file takes a minute

            with patch.object(cod4r, "_fetch_manifest", return_value=m), \
                 patch.object(cod4r, "_download", side_effect=_fake_download(m, slow)), \
                 patch.object(cod4r.time, "monotonic", side_effect=lambda: clock[0]):
                with self.assertRaises(cod4r.OutOfTime):
                    cod4r.sync_files(install, compat, budget=45)
            for p in files:
                self.assertEqual(Path(p).read_bytes(), b"old")
            # Quick per-file limits inside the budget, like plutonium_update
            self.assertEqual(seen[0]["tries"], 1)
            self.assertEqual(seen[0]["timeout"], 30)

    def test_without_a_budget_the_install_behaves_as_before(self):
        import cod4r
        with tempfile.TemporaryDirectory() as d:
            install, compat = _cod4r_tree(d)
            m, files = _manifest(install, compat, {"a.dll": b"new-a"})
            seen = []
            with patch.object(cod4r, "_fetch_manifest", return_value=m), \
                 patch.object(cod4r, "_download", side_effect=_fake_download(m, seen.append)):
                self.assertEqual(cod4r.sync_files(install, compat), 1)
            self.assertEqual(Path(files[0]).read_bytes(), b"new-a")
            self.assertEqual((seen[0]["tries"], seen[0]["timeout"]), (3, 120))

    def test_prelaunch_passes_the_launch_budget_and_swallows_out_of_time(self):
        import cod4r
        import plutonium_update
        import prelaunch
        with tempfile.TemporaryDirectory() as d:
            Path(d, "deckops_cod4r.json").write_text(json.dumps({"compatdata_path": d}))
            exe = os.path.join(d, "iw3mp.exe")
            with patch.object(cod4r, "sync_files", side_effect=cod4r.OutOfTime("late")) as sync:
                prelaunch._cod4mp("cod4mp", ["proton", "run", exe])
            self.assertEqual(sync.call_args.kwargs["budget"], plutonium_update.LAUNCH_BUDGET)


# ── A3: an interrupted or running DeckOps update ─────────────────────────────

class PrelaunchDeckOpsUpdateTests(unittest.TestCase):
    def setUp(self):
        import prelaunch
        self.prelaunch = prelaunch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = self.tmp.name
        self.handler = MagicMock()
        for p in (patch.object(prelaunch, "INSTALL_DIR", self.install),
                  patch.dict(prelaunch._HANDLERS, {"cod4mp": self.handler, "lan": self.handler})):
            p.start()
            self.addCleanup(p.stop)

    def _journal(self):
        os.makedirs(os.path.join(self.install, ".update"))
        Path(self.install, ".update", "transaction.json").write_text("{}")

    def test_update_runs_normally(self):
        self.prelaunch.main(["cod4mp", "x"])
        self.handler.assert_called_once_with("cod4mp", ["x"])

    def test_interrupted_update_journal_skips_the_mod_update(self):
        self._journal()
        self.assertIsNone(self.prelaunch.main(["cod4mp", "x"]))
        self.handler.assert_not_called()

    def test_running_update_skips_the_mod_update(self):
        with patch("update_manifest.update_lock", side_effect=BlockingIOError):
            self.prelaunch.main(["cod4mp", "x"])
        self.handler.assert_not_called()

    def test_unopenable_lock_does_not_block_the_update(self):
        with patch("update_manifest.update_lock", side_effect=PermissionError):
            self.prelaunch.main(["cod4mp", "x"])
        self.handler.assert_called_once()

    def test_offline_launcher_query_is_not_skipped(self):
        self._journal()
        self.prelaunch.main(["lan", "t6mp"])        # lan_server.sh needs its answer
        self.handler.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "flock")
    def test_lock_is_only_tested_and_released(self):
        import fcntl
        from update_manifest import update_lock
        self.assertIsNone(self.prelaunch._deckops_update_busy(self.install))
        with update_lock(self.install):            # prelaunch let go: the updater gets it at once
            pass
        with open(os.path.join(self.install, ".deckops_update.lock"), "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.prelaunch._deckops_update_busy(self.install), "a DeckOps update is running")


# ── A4: lock file that can't be opened for writing ───────────────────────────

class UpdateLockFallbackTests(unittest.TestCase):
    def test_python_lock_falls_back_to_a_read_only_open(self):
        import update_manifest
        real_open = builtins.open
        modes = []

        def fake_open(path, mode="r", *a, **kw):
            modes.append(mode)
            if mode == "a+":
                raise PermissionError(13, "Permission denied")
            return real_open(path, mode, *a, **kw)

        with tempfile.TemporaryDirectory() as d:
            Path(d, ".deckops_update.lock").write_text("")
            with patch.object(update_manifest, "open", fake_open, create=True):
                with update_manifest.update_lock(d):
                    pass
        self.assertEqual(modes, ["a+", "r"])

    def test_missing_unwritable_lock_still_raises_for_the_caller(self):
        import update_manifest
        with tempfile.TemporaryDirectory() as d:
            with patch.object(update_manifest, "open", side_effect=PermissionError, create=True):
                with self.assertRaises(OSError):
                    with update_manifest.update_lock(d):
                        pass

    def test_shell_scripts_fall_back_to_a_read_descriptor(self):
        launcher = _between(_text("launcher.sh"), "UPDATE_LOCKED=false", "release_update_lock()")
        self.assertNotIn("|| exit 1", launcher)
        self.assertIn('{ exec 9< "$LOCK_FILE"; } 2>/dev/null', launcher)
        install = _between(_text("install.sh"), "if command -v flock", "PREVIOUS_SHA=")
        self.assertIn('{ exec 8< "$INSTALL_DIR/.deckops_update.lock"; } 2>/dev/null', install)
        self.assertIn("|| die ", install)

    def _run_launcher_lock(self, install_dir):
        block = _between(_text("launcher.sh"), "UPDATE_LOCKED=false", "# DeckOps checks this lock itself")
        script = f'APP_TITLE=x; INSTALL_DIR="{install_dir}"\nzenity() {{ :; }}\n{block}\necho "LOCKED=$UPDATE_LOCKED"\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    @unittest.skipUnless(_HAS_FLOCK and hasattr(os, "geteuid") and os.geteuid() != 0,
                         "requires Linux bash, flock and a non-root user")
    def test_launcher_locks_a_read_only_lock_file(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d, ".deckops_update.lock")
            lock.write_text("")
            lock.chmod(0o444)
            r = self._run_launcher_lock(d)
            self.assertIn("LOCKED=true", r.stdout, r.stderr)

    @unittest.skipUnless(_HAS_FLOCK and hasattr(os, "geteuid") and os.geteuid() != 0,
                         "requires Linux bash, flock and a non-root user")
    def test_launcher_continues_unlocked_when_the_lock_cannot_be_opened(self):
        with tempfile.TemporaryDirectory() as d:
            ro = Path(d, "ro")
            ro.mkdir()
            ro.chmod(0o555)
            try:
                r = self._run_launcher_lock(ro)
            finally:
                ro.chmod(0o755)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("LOCKED=false", r.stdout)
            self.assertIn("without the update lock", r.stderr)


# ── A5: import-time folders, dead bootstrap module ───────────────────────────

class ImportTimeFolderTests(unittest.TestCase):
    def test_unwritable_install_dir_does_not_break_importing_the_ui(self):
        try:
            import qt  # noqa: F401
        except Exception:
            self.skipTest("PyQt unavailable")
        spec = importlib.util.spec_from_file_location("_ui_constants_ro", ROOT / "src" / "ui_constants.py")
        module = importlib.util.module_from_spec(spec)
        with patch("os.makedirs", side_effect=PermissionError(13, "read-only")):
            spec.loader.exec_module(module)       # a separate copy: the real module is untouched
        self.assertTrue(module.LOG_PATH.endswith("install.log"))

    def test_unused_bootstrap_module_is_gone(self):
        self.assertFalse((ROOT / "src" / "bootstrap.py").exists())


# ── A6: unreadable deckops.json ──────────────────────────────────────────────

class ConfigUnreadableTests(unittest.TestCase):
    def setUp(self):
        import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "deckops.json"
        for p in (patch.object(config, "CONFIG_PATH", str(self.path)),
                  patch.object(config, "_cache", None),
                  patch.object(config, "_cache_key", None),
                  patch.object(config, "_unreadable", None)):
            p.start()
            self.addCleanup(p.stop)
        self.setup = {"first_run_complete": True, "player_name": "Real", "setup_games": {"cod4mp": {}}}
        self.path.write_text(json.dumps(self.setup))

    def test_unreadable_file_is_not_overwritten_by_the_next_setter(self):
        with patch.object(self.config, "open", side_effect=PermissionError(13, "denied"), create=True):
            self.assertIsNone(self.config.load()["player_name"])      # defaults, as before
            with self.assertRaises(OSError):
                self.config.set_player_name("Clobber")
        self.assertEqual(json.loads(self.path.read_text()), self.setup)

    def test_io_error_on_stat_is_not_treated_as_missing(self):
        real_stat = os.stat

        def stat(p, *a, **kw):
            if str(p) == str(self.path):
                raise OSError(5, "Input/output error")
            return real_stat(p, *a, **kw)

        with patch.object(self.config.os, "stat", side_effect=stat):
            self.config.load()
            with self.assertRaises(self.config.ConfigUnreadable):
                self.config.save(dict(self.config.DEFAULTS))
        self.assertEqual(json.loads(self.path.read_text()), self.setup)

    def test_saving_works_again_once_the_file_reads(self):
        with patch.object(self.config, "open", side_effect=PermissionError, create=True):
            self.config.load()
        self.assertEqual(self.config.get_player_name(), "Real")
        self.config.set_player_name("Next")
        self.assertEqual(json.loads(self.path.read_text())["player_name"], "Next")

    def test_missing_file_still_saves_defaults(self):
        self.path.unlink()
        self.config.set_player_name("New")
        self.assertEqual(json.loads(self.path.read_text())["player_name"], "New")

    def test_offline_mode_getter_does_not_raise(self):
        with patch.object(self.config, "open", side_effect=PermissionError, create=True):
            self.assertFalse(self.config.get_offline_mode())
        self.assertEqual(json.loads(self.path.read_text()), self.setup)

    def test_reset_allows_starting_over(self):
        with patch.object(self.config, "open", side_effect=PermissionError, create=True):
            self.config.load()
        self.config.reset()
        self.config.set_player_name("Fresh")
        self.assertEqual(json.loads(self.path.read_text())["player_name"], "Fresh")


# ── A7 / A8 / Snap root / pip timeout ────────────────────────────────────────

class ScriptTextTests(unittest.TestCase):
    def test_launcher_has_no_dead_force_branch(self):
        text = _text("launcher.sh")
        self.assertNotIn("FORCE", text)
        self.assertNotIn('"force"', text)

    def test_install_commits_unpinned_version_with_the_archive(self):
        text = _text("install.sh")
        early = _between(text, 'PREVIOUS_LIST="none"', "# ── step 3b")
        self.assertIn('INSTALLED_VERSION="0"', early)
        self.assertIn('"$INSTALLED_VERSION" full "$PREVIOUS_LIST"', early)
        self.assertNotIn('cp -r "$EXTRACTED"/. "$INSTALL_DIR"/', early)
        # launcher.sh reads anything but a 40-hex SHA as "needs a full update"
        self.assertIn('LOCAL_SHA="0"', _text("launcher.sh"))

    def test_install_success_messages_follow_the_write(self):
        text = _text("install.sh")
        self.assertIn('if echo "$BUILD_HASH ($BUILD_DATE)" > "$INSTALL_DIR/BUILD"; then', text)
        self.assertIn('if cat > "$DESKTOP_FILE" << DEOF', text)
        step8 = _between(text, "# ── step 8", "# DeckOps checks the update lock")
        self.assertIn('"$INSTALL_DIR/VERSION"', step8)
        self.assertLess(step8.index("does not match"), step8.index('success "Version recorded."'))

    @unittest.skipUnless(_HAS_BASH, "requires Linux bash")
    def test_unpinned_install_records_zero_transactionally(self):
        import update_manifest
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d, "stage")
            (stage / "src").mkdir(parents=True)
            (stage / "src" / "main.py").write_text("print('new')\n")
            (stage / "deckops_identity.sh").write_text("BRANCH=nightly\n")
            (stage / "VERSION").write_text("a" * 40 + "\n")
            install = Path(d, "install")
            install.mkdir()
            update_manifest.apply_update(str(install), str(stage), "0", full=True)
            self.assertEqual((install / "VERSION").read_text().strip(), "0")
            self.assertEqual((install / "src" / "main.py").read_text(), "print('new')\n")

    def test_install_sweep_knows_snap_steam_after_flatpak(self):
        sweep = _between(_text("install.sh"), 'STEAM_ROOT_FOR_SWEEP=""', "LAN_SWEEP_DIRS=()")
        flatpak = sweep.index("com.valvesoftware.Steam/.local/share/Steam")
        self.assertGreater(sweep.index("$HOME/snap/steam/common/.local/share/Steam"), flatpak)

    def test_pyqt6_uninstall_has_a_timeout(self):
        line = _between(_text("src/main.py"), '[pip, "uninstall"', ")\n")
        self.assertIn("timeout=", line)


if __name__ == "__main__":
    unittest.main()
