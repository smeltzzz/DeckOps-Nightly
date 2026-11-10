"""Regressions for the sixth sweep of the startup and update files: the sleep
inhibitor outliving DeckOps, per-file update timeouts, the launcher holding the
update lock while it waits on the player, the /tmp identity in the first-run
bootstrap, a second DeckOps window, comparisons that touch VERSION or nothing,
the pre-Qt error message without zenity and an empty menu choice."""

import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import update_manifest  # noqa: E402

_LINUX_BASH = sys.platform == "linux" and bool(shutil.which("bash"))
_HAS_FLOCK = _LINUX_BASH and bool(shutil.which("flock")) and bool(shutil.which("setsid"))
_POSIX = os.name == "posix"

OLD = "1" * 40
NEW = "2" * 40


def _text(name):
    return (ROOT / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _write(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if mode is not None:
        path.chmod(mode)
    return path


def _blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ── CORE-6: comparisons that touch VERSION, or nothing at all ────────────────

class CompareProtectedPathTests(unittest.TestCase):
    sha = "a" * 40

    def test_version_in_a_comparison_is_skipped_not_a_failure(self):
        data = {"status": "ahead", "files": [
            {"status": "modified", "filename": "src/ui_qt.py", "sha": self.sha},
            {"status": "modified", "filename": "VERSION", "sha": self.sha},
            {"status": "added", "filename": "logs/install.log", "sha": self.sha},
            {"status": "removed", "filename": "deckops.json"},
            {"status": "renamed", "filename": "src/new.py", "previous_filename": "assets/music/background.mp3",
             "sha": self.sha},
            {"status": "renamed", "filename": "VERSION", "previous_filename": "src/old.py", "sha": self.sha},
        ]}
        self.assertEqual(list(update_manifest.compare(data)), [
            ("M", "src/ui_qt.py", self.sha), ("M", "src/new.py", self.sha), ("D", "src/old.py", "-")])

    def test_empty_comparison_is_valid(self):
        self.assertEqual(list(update_manifest.compare({"status": "ahead", "files": []})), [])
        only_version = {"status": "ahead", "files": [{"status": "modified", "filename": "VERSION", "sha": self.sha}]}
        self.assertEqual(list(update_manifest.compare(only_version)), [])

    def test_unsafe_paths_still_reject_the_comparison(self):
        for path in ("../VERSION", "/VERSION", "logs/../../x", ".update/files/VERSION", "C:/VERSION"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                list(update_manifest.compare({"status": "ahead", "files": [
                    {"status": "modified", "filename": path, "sha": self.sha}]}))
        with self.assertRaises(ValueError):            # a protected path still needs a valid entry
            list(update_manifest.compare({"status": "ahead", "files": [
                {"status": "bogus", "filename": "VERSION", "sha": self.sha}]}))

    def test_apply_still_refuses_protected_rows(self):
        with self.assertRaises(ValueError):
            update_manifest._path("VERSION")

    def _cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(SRC / "update_manifest.py"), *args], input=stdin,
                              capture_output=True, text=True, timeout=60)

    def test_cli_compare_succeeds_with_an_empty_result(self):
        r = self._cli("compare", stdin=json.dumps({"status": "ahead", "files": [
            {"status": "modified", "filename": "VERSION", "sha": self.sha}]}))
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_cli_apply_with_no_changes_only_records_the_version(self):
        for content in ("", "\n"):
            with self.subTest(content=repr(content)), tempfile.TemporaryDirectory() as d:
                install = Path(d, "install")
                _write(install / "VERSION", OLD + "\n")
                _write(install / "src" / "main.py", "print('app')\n")
                changes = _write(Path(d, "changes.tsv"), content)
                r = self._cli("apply", str(install), str(Path(d, "files")), NEW, str(changes))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual((install / "VERSION").read_text().strip(), NEW)
                self.assertEqual((install / "src" / "main.py").read_text(), "print('app')\n")
                self.assertFalse((install / ".update" / "transaction.json").exists())


# ── CORE-5: one DeckOps at a time ─────────────────────────────────────────────

class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    @unittest.skipIf(_POSIX, "Windows behaviour")
    def test_no_lock_on_windows(self):
        self.assertIsNone(update_manifest.instance_lock(self.dir))

    def _child(self, code):
        prefix = f"import os, sys, time\nsys.path.insert(0, {str(SRC)!r})\nimport update_manifest as u\nD = {self.dir!r}\n"
        return [sys.executable, "-c", prefix + code]

    @unittest.skipUnless(_POSIX, "flock")
    def test_second_instance_is_refused(self):
        held = update_manifest.instance_lock(self.dir)
        self.addCleanup(held.close)
        self.assertIsNotNone(held)
        r = subprocess.run(self._child(
            "try:\n    u.instance_lock(D, wait=0.3)\n    print('got it')\n"
            "except BlockingIOError:\n    print('refused')\n"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "refused", r.stderr)

    @unittest.skipUnless(_POSIX, "flock")
    def test_waits_briefly_for_an_instance_that_is_letting_go(self):
        child = subprocess.Popen(self._child(
            "l = u.instance_lock(D, wait=0)\nprint('held', flush=True)\ntime.sleep(0.7)\n"),
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.assertEqual(child.stdout.readline().strip(), "held")
        lock = update_manifest.instance_lock(self.dir, wait=5)
        self.assertIsNotNone(lock)
        lock.close()
        child.stdout.close()

    @unittest.skipUnless(_POSIX, "flock")
    def test_execv_relaunch_takes_the_lock_again(self):
        again = (f"import sys; sys.path.insert(0, {str(SRC)!r}); import update_manifest as u; "
                 f"print('relaunched ok' if u.instance_lock({self.dir!r}, wait=0) else 'none')")
        r = subprocess.run(self._child(
            "l = u.instance_lock(D, wait=0)\nassert l is not None\n"
            f"os.execv(sys.executable, [sys.executable, '-c', {again!r}])\n"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "relaunched ok", r.stderr)

    @unittest.skipUnless(_POSIX, "flock")
    def test_unopenable_lock_never_blocks_opening(self):
        with patch.object(update_manifest, "open", side_effect=OSError(30, "read-only"), create=True):
            self.assertIsNone(update_manifest.instance_lock(self.dir))

    @unittest.skipUnless(_POSIX and shutil.which("sh"), "flock and a POSIX shell")
    def test_main_says_already_open_and_exits_cleanly(self):
        install = Path(self.dir, "install")
        for name in ("main.py", "update_manifest.py"):
            _write(install / "src" / name, _text(f"src/{name}"))
        stubs = Path(self.dir, "bin")
        log = Path(self.dir, "zenity.log")
        _write(stubs / "zenity", f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n', 0o755)
        held = update_manifest.instance_lock(str(install))
        self.addCleanup(held.close)
        r = subprocess.run([sys.executable, str(install / "src" / "main.py")], capture_output=True, text=True,
                           timeout=60, env={**os.environ, "PATH": f"{stubs}:{os.environ.get('PATH', '')}",
                                            "HOME": self.dir})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already open", log.read_text())
        self.assertIn("--info", log.read_text())


# ── CORE-7: the pre-Qt message without zenity ─────────────────────────────────

def _tell_function():
    tree = ast.parse(_text("src/main.py"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tell")
    ns = {"sys": sys}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["_tell"]


class PreQtMessageTests(unittest.TestCase):
    def setUp(self):
        self.tell = _tell_function()

    def _run(self, available, codes=None):
        codes = codes or {}
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=codes.get(cmd[0], 0))

        err = io.StringIO()
        with patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}" if n in available else None), \
             patch("subprocess.run", side_effect=run), contextlib.redirect_stderr(err):
            self.tell("Could not restore an interrupted update.")
        return calls, err.getvalue()

    def test_zenity_first(self):
        calls, err = self._run({"zenity", "kdialog", "notify-send"})
        self.assertEqual([c[0] for c in calls], ["zenity"])
        self.assertIn("--error", calls[0])
        self.assertIn("Could not restore", err)

    def test_kdialog_without_zenity(self):
        calls, _ = self._run({"kdialog", "notify-send"})
        self.assertEqual([c[0] for c in calls], ["kdialog"])
        self.assertIn("--error", calls[0])

    def test_failed_zenity_falls_through(self):
        calls, _ = self._run({"zenity", "notify-send"}, {"zenity": 1})
        self.assertEqual([c[0] for c in calls], ["zenity", "notify-send"])

    def test_no_tool_still_prints(self):
        calls, err = self._run(set())
        self.assertEqual(calls, [])
        self.assertIn("Could not restore", err)

    def test_main_error_path_uses_the_fallback(self):
        block = _text("src/main.py").split("except Exception as ex:", 1)[1].split("sys.exit(1)", 1)[0]
        self.assertIn("_tell(", block)
        self.assertNotIn('["zenity"', block)


# ── CORE-1: the sleep inhibitor must not outlive DeckOps ──────────────────────

class InhibitTests(unittest.TestCase):
    def setUp(self):
        import inhibit
        self.inhibit = inhibit
        for p in (patch.object(inhibit, "_proc", None), patch.object(inhibit, "_cookie", None),
                  patch.object(inhibit, "_busy", None),
                  patch.object(inhibit, "_ss_inhibit", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_inhibitor_watches_this_process_instead_of_sleeping_6h(self):
        live = MagicMock()
        live.poll.return_value = None
        with patch.object(self.inhibit.subprocess, "Popen", return_value=live) as popen:
            self.assertTrue(self.inhibit.start("test"))
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "systemd-inhibit")
        self.assertNotIn("sleep", args[:6])
        tail = args[args.index("-c") + 1:]
        self.assertEqual(tail[0], self.inhibit._WATCH)
        self.assertEqual(tail[2], str(os.getpid()))
        self.assertEqual(int(tail[3]) * int(tail[4]), 6 * 3600)        # still bounded

    def test_stop_is_idempotent_and_skips_a_dead_child(self):
        dead = MagicMock()
        dead.poll.return_value = 0
        with patch.object(self.inhibit, "_proc", dead):
            self.inhibit.stop()
            self.inhibit.stop()
        dead.terminate.assert_not_called()
        self.assertIsNone(self.inhibit._proc)
        live = MagicMock()
        live.poll.return_value = None
        with patch.object(self.inhibit, "_proc", live):
            self.inhibit.stop()
            self.inhibit.stop()
        live.terminate.assert_called_once()

    @unittest.skipUnless(_POSIX and shutil.which("sh") and shutil.which("sleep"), "POSIX shell")
    def test_watcher_ends_when_deckops_is_gone(self):
        target = subprocess.Popen(["sleep", "60"])
        watcher = subprocess.Popen(["sh", "-c", self.inhibit._WATCH, "x", str(target.pid), "1000", "0.1"])
        try:
            time.sleep(0.3)
            self.assertIsNone(watcher.poll())
            target.kill()
            target.wait()
            self.assertEqual(watcher.wait(timeout=10), 0)
        finally:
            for p in (target, watcher):
                if p.poll() is None:
                    p.kill()
                    p.wait()

    @unittest.skipUnless(_POSIX and shutil.which("sh"), "POSIX shell")
    def test_watcher_is_still_bounded(self):
        start = time.monotonic()
        subprocess.run(["sh", "-c", self.inhibit._WATCH, "x", str(os.getpid()), "3", "0.1"], timeout=10)
        self.assertLess(time.monotonic() - start, 5)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("sh"), "Linux /proc")
    def test_crashed_deckops_does_not_leave_the_inhibitor(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d, "systemd-inhibit"),
                   '#!/bin/sh\nwhile [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n', 0o755)
            code = (f"import os, sys\nsys.path.insert(0, {str(SRC)!r})\nimport inhibit\n"
                    "inhibit._ss_inhibit = lambda why: None\ninhibit.POLL = 0.2\n"
                    "inhibit.start('test')\nprint(inhibit._proc.pid, flush=True)\nos._exit(0)\n")
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "PATH": f"{d}:{os.environ.get('PATH', '')}", "HOME": d})
            pid = int(r.stdout.split()[0])

            def gone():
                try:
                    return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] in ("Z", "X")
                except OSError:
                    return True
            self.assertTrue(_wait_for(gone, 10), "inhibitor kept running after DeckOps died")


# ── launcher.sh text checks (run everywhere) ─────────────────────────────────

class LauncherTextTests(unittest.TestCase):
    def test_per_file_download_uses_the_archive_stall_limits(self):
        lines = _text("launcher.sh").splitlines()
        raw = next(i for i, l in enumerate(lines) if "raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$REMOTE_SHA" in l)
        per_file = lines[raw - 1]
        archive = next(l for l in lines if "curl -fsL" in l and "--speed-limit" in l)
        for flag in ("--connect-timeout 15", "--speed-limit 1024", "--speed-time 30", "--max-time 1800"):
            self.assertIn(flag, per_file)
            self.assertIn(flag, archive)
        self.assertNotIn("--max-time 120 ", per_file)

    def test_compare_failure_is_decided_by_exit_status(self):
        text = _text("launcher.sh")
        self.assertIn("COMPARED=true", text)
        self.assertNotIn('| python3 "$INSTALL_DIR/src/update_manifest.py" compare) || PARSED=""', text)

    def test_identity_is_only_sourced_from_a_deckops_tree(self):
        for name in ("launcher.sh", "install.sh", "deckops_uninstall.sh"):
            text = _text(name)
            line = next(l for l in text.splitlines() if '[ -f "$SCRIPT_DIR/deckops_identity.sh" ]' in l)
            nxt = text.split(line, 1)[1].splitlines()[1]
            self.assertIn('[ -f "$SCRIPT_DIR/src/identity.py" ]', line + nxt, name)
            self.assertIn('[ -O "$SCRIPT_DIR/deckops_identity.sh" ]', line + nxt, name)


class DesktopBootstrapTextTests(unittest.TestCase):
    def setUp(self):
        self.exec_line = next(l for l in _text("DeckOps-Nightly.desktop").splitlines() if l.startswith("Exec="))

    def test_downloads_into_a_private_directory(self):
        self.assertIn("mktemp -d /tmp/deckops_XXXXXX", self.exec_line)
        self.assertNotIn("deckops_XXXXXX.sh", self.exec_line)
        self.assertIn("rm -rf", self.exec_line)
        # Desktop-entry rules: % starts a field code and backslashes are unescaped
        self.assertNotIn("%", self.exec_line)
        self.assertNotIn("\\", self.exec_line)

    @unittest.skipUnless(_LINUX_BASH, "requires Linux bash")
    def test_bootstrap_runs_from_a_private_dir_and_cleans_up(self):
        command = self.exec_line[self.exec_line.index("'") + 1:self.exec_line.rindex("'")]
        with tempfile.TemporaryDirectory() as d:
            home = Path(d, "home")
            home.mkdir()
            stubs = Path(d, "bin")
            report = Path(d, "report")
            fake_launcher = Path(d, "fake_launcher.sh")
            _write(fake_launcher, f'echo "$(cd "$(dirname "$0")" && pwd) $(stat -c %a "$(dirname "$0")")" > "{report}"\n')
            _write(stubs / "curl",
                   '#!/bin/bash\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                   f'cp "{fake_launcher}" "$out"\n', 0o755)
            r = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "HOME": str(home),
                                    "PATH": f"{stubs}:{os.environ.get('PATH', '')}"})
            self.assertEqual(r.returncode, 0, r.stderr)
            where, mode = report.read_text().split()
            self.assertTrue(where.startswith("/tmp/deckops_"), where)
            self.assertEqual(mode, "700")
            self.assertFalse(Path(where).exists(), "temporary bootstrap folder left behind")


# ── CORE-4: a planted /tmp identity is never sourced ─────────────────────────

@unittest.skipUnless(_LINUX_BASH, "requires Linux bash")
class TmpIdentityTests(unittest.TestCase):
    def _run(self, script, with_tree):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            stubs = root / "bin"
            _write(stubs / "curl", "#!/bin/sh\nexit 22\n", 0o755)
            _write(stubs / "zenity", "#!/bin/sh\nexit 0\n", 0o755)
            drop = root / "drop"
            _write(drop / "x.sh", _text(script))
            marker = root / "planted_ran"
            _write(drop / "deckops_identity.sh",
                   f'touch "{marker}"\nBRANCH="nightly"; INSTALL_DIR="{root}/elsewhere"; APP_TITLE=x\n')
            if with_tree:
                _write(drop / "src" / "identity.py", "BRANCH = 'nightly'\n")
            subprocess.run(["bash", str(drop / "x.sh")], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True,
                           env={**os.environ, "HOME": str(home), "PATH": f"{stubs}:{os.environ.get('PATH', '')}"})
            return marker.exists()

    def test_launcher_ignores_a_stray_identity(self):
        self.assertFalse(self._run("launcher.sh", with_tree=False))

    def test_launcher_still_uses_its_own_tree(self):
        self.assertTrue(self._run("launcher.sh", with_tree=True))

    def test_installer_ignores_a_stray_identity(self):
        self.assertFalse(self._run("install.sh", with_tree=False))


# ── CORE-3 / 5 / 6 / 8: the launcher end to end ──────────────────────────────

_CURL = r'''#!/bin/bash
echo "$*" >> "$STUB_LOG/curl.log"
out=""; url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift ;;
        http*) url="$1" ;;
    esac
    shift
done
case "$url" in
    */commits/main) printf '{"sha": "%s"}\n' "$REMOTE_SHA" ;;
    */compare/*) [ -f "$COMPARE_JSON" ] || exit 22; cat "$COMPARE_JSON" ;;
    https://raw.githubusercontent.com/*)
        rel="${url#https://raw.githubusercontent.com/*/*/*/}"
        cp "$RAW_DIR/$rel" "$out" 2>/dev/null || exit 22 ;;
    *) exit 22 ;;
esac
'''

_ZENITY = r'''#!/bin/bash
state=free
flock -n "$UPDATE_LOCK" true 2>/dev/null || state=held
echo "$1 lock=$state :: $*" >> "$STUB_LOG/zenity.log"
case "$*" in
    *--list*) printf '%s\n' "$MENU_CHOICE"; exit 0 ;;
    *--question*) exit "${QUESTION_RC:-0}" ;;
esac
exit 0
'''


@unittest.skipUnless(_HAS_FLOCK, "requires Linux bash, flock and setsid")
class LauncherFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.install = self.home / "DeckOps-Nightly"
        self.logs = root / "logs"
        self.logs.mkdir()
        self.raw = root / "raw"
        self.stubs = root / "bin"
        _write(self.install / "launcher.sh", _text("launcher.sh"), 0o755)
        _write(self.install / "deckops_identity.sh", _text("deckops_identity.sh"))
        for name in ("identity.py", "update_manifest.py"):
            _write(self.install / "src" / name, _text(f"src/{name}"))
        _write(self.install / "src" / "foo.py", "print('old')\n")
        _write(self.install / "src" / "main.py", "print('app')\n")
        _write(self.install / "VERSION", OLD + "\n")
        _write(self.install / ".venv" / "bin" / "python3",
               f'#!/bin/sh\necho "$*" >> "{self.logs}/venv.log"\nexit 0\n', 0o755)
        _write(self.stubs / "curl", _CURL, 0o755)
        _write(self.stubs / "zenity", _ZENITY, 0o755)
        self.compare = root / "compare.json"
        self.env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.stubs}:{os.environ.get('PATH', '')}",
                    "STUB_LOG": str(self.logs), "REMOTE_SHA": NEW, "COMPARE_JSON": str(self.compare),
                    "RAW_DIR": str(self.raw), "UPDATE_LOCK": str(self.install / ".deckops_update.lock"),
                    "MENU_CHOICE": "Launch DeckOps"}

    def _compare(self, files):
        self.compare.write_text(json.dumps({"status": "ahead", "files": files}))

    def _raw(self, rel, data):
        _write(self.raw / rel, data.decode())
        return {"status": "modified", "filename": rel, "sha": _blob(data)}

    def _launch(self, *args, **env):
        r = subprocess.run(["bash", str(self.install / "launcher.sh"), *args], capture_output=True, text=True,
                           timeout=120, stdin=subprocess.DEVNULL, env={**self.env, **env})
        return r

    def _log(self, name):
        p = self.logs / name
        return p.read_text() if p.exists() else ""

    def _launched(self):
        return _wait_for(lambda: "src/main.py" in self._log("venv.log"), 10)

    def test_lock_is_free_at_the_menu_question_and_completion(self):
        self._compare([self._raw("src/foo.py", b"print('new')\n")])
        r = self._launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.install / "src" / "foo.py").read_text(), "print('new')\n")
        self.assertEqual((self.install / "VERSION").read_text().strip(), NEW)
        log = self._log("zenity.log")
        self.assertIn("--question", log)
        self.assertIn("Update complete!", log)
        for line in log.splitlines():
            self.assertIn("lock=free", line)
        self.assertTrue(self._launched())

    def test_per_file_download_has_stall_limits(self):
        self._compare([self._raw("src/foo.py", b"print('new')\n")])
        self._launch()
        raw_call = next(l for l in self._log("curl.log").splitlines() if "raw.githubusercontent.com" in l)
        self.assertIn("--speed-limit 1024", raw_call)
        self.assertIn("--max-time 1800", raw_call)

    def test_version_in_the_diff_keeps_the_partial_update(self):
        self._compare([self._raw("src/foo.py", b"print('new')\n"),
                       {"status": "modified", "filename": "VERSION", "sha": "b" * 40}])
        r = self._launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("/archive/", self._log("curl.log"))
        self.assertIn("1 file(s) changed", self._log("zenity.log"))
        self.assertEqual((self.install / "VERSION").read_text().strip(), NEW)

    def test_nothing_to_download_only_records_the_version(self):
        self._compare([{"status": "modified", "filename": "VERSION", "sha": "b" * 40}])
        r = self._launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("/archive/", self._log("curl.log"))
        self.assertNotIn("--question", self._log("zenity.log"))
        self.assertNotIn("--error", self._log("zenity.log"))
        self.assertEqual((self.install / "VERSION").read_text().strip(), NEW)
        self.assertEqual((self.install / "src" / "foo.py").read_text(), "print('old')\n")
        self.assertTrue(self._launched())

    def test_running_deckops_skips_the_update_check(self):
        held = update_manifest.instance_lock(str(self.install))
        self.addCleanup(held.close)
        self._compare([self._raw("src/foo.py", b"print('new')\n")])
        r = self._launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("commits/main", self._log("curl.log"))
        self.assertNotIn("--question", self._log("zenity.log"))
        self.assertEqual((self.install / "src" / "foo.py").read_text(), "print('old')\n")
        self.assertTrue(self._launched())            # main.py then says it is already open

    def test_empty_menu_choice_launches(self):
        self.env["REMOTE_SHA"] = OLD                   # up to date: no update question
        for choice in ("", "Launch DeckOps|Launch DeckOps"):
            with self.subTest(choice=choice):
                (self.logs / "venv.log").unlink(missing_ok=True)
                r = self._launch(MENU_CHOICE=choice)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(self._launched(), "empty selection closed the launcher")

    def test_interrupted_update_is_rolled_back_without_holding_the_lock_at_the_dialog(self):
        update = self.install / ".update"
        _write(update / "rollback" / "VERSION", OLD + "\n")
        _write(update / "transaction.json", json.dumps({"files": [], "version_existed": True}))
        (self.install / "VERSION").write_text(NEW + "\n")
        self.env["REMOTE_SHA"] = OLD
        r = self._launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.install / "VERSION").read_text().strip(), OLD)
        info = next(l for l in self._log("zenity.log").splitlines() if "rolled back" in l)
        self.assertIn("lock=free", info)

    def test_update_running_elsewhere_still_refuses_to_start(self):
        import fcntl
        with open(self.install / ".deckops_update.lock", "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = self._launch()
        self.assertEqual(r.returncode, 1)
        self.assertIn("updating in another window", self._log("zenity.log"))
        self.assertNotIn("--list", self._log("zenity.log"))


if __name__ == "__main__":
    unittest.main()
