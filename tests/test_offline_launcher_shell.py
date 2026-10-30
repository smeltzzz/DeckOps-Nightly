"""Linux integration checks for the Steam shell-to-Wine intent handoff."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("python3"),
                     "requires Linux bash and Python")
class OfflineShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        lan = self.root / "assets" / "LAN"
        lan.mkdir(parents=True)
        self.script = lan / "launcher_offline.sh"
        # Windows checkouts may have CRLF; a Linux checkout of the script is LF.
        self.script.write_bytes((ROOT / "assets" / "LAN" / "launcher_offline.sh")
                                .read_bytes().replace(b"\r\n", b"\n"))
        (lan / "DeckOps_Offline.exe").touch()
        self.steam = Path(self.tmp.name) / "steam"
        proton = self.steam / "compatibilitytools.d" / "GE" / "proton"
        proton.parent.mkdir(parents=True)
        proton.write_text("""#!/bin/bash
python3 - "$@" <<'PY'
import json, os, sys
args = sys.argv[1:]
path = args[args.index('--intent-file') + 1]
assert path.startswith('Z:')
path = path[2:].replace('\\\\', '/')
with open(os.environ['PATH_LOG'], 'a') as f:
    f.write(path + '\\n')
if os.environ.get('CHOICE'):
    with open(path, 'w') as f:
        json.dump({'lan_wrapper_path': os.environ['CHOICE']}, f)
PY
if [ -n "$WAIT" ]; then
  while [ ! -f "$WAIT" ]; do sleep .05; done
fi
""")
        proton.chmod(0o755)
        self.one = self.root / "one.sh"
        self.two = self.root / "two.sh"
        self.rogue = self.root / "rogue.sh"
        for path in (self.one, self.two, self.rogue):
            path.write_text(f'#!/bin/bash\necho "wrapper:{path.stem}"\n')
        (self.root / "deckops.json").write_text(json.dumps({
            "steam_root": str(self.steam), "ge_proton_version": "GE",
            "setup_games": {
                "t4mp": {"client": "plutonium", "lan_wrapper_path": str(self.one)},
                "t5mp": {"client": "plutonium", "lan_wrapper_path": str(self.two)},
                "steam": {"client": "steam", "lan_wrapper_path": str(self.rogue)},
            },
        }))
        self.path_log = Path(self.tmp.name) / "paths"
        self.env = dict(os.environ, HOME=str(Path(self.tmp.name) / "home"),
                        PATH_LOG=str(self.path_log), WAIT="")

    def run_launcher(self, choice, wait=""):
        return subprocess.Popen(["bash", str(self.script)], env=dict(self.env, CHOICE=choice, WAIT=wait),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_only_configured_plutonium_wrappers_execute(self):
        for choice, expected in ((str(self.one), "wrapper:one"),
                                 (str(self.rogue), None), ("/not/configured.sh", None),
                                 ("one.sh", None), ("", None)):
            with self.subTest(choice=choice):
                proc = self.run_launcher(choice)
                out, err = proc.communicate(timeout=10)
                if expected:
                    self.assertEqual(proc.returncode, 0, err)
                    self.assertIn(expected, out)
                elif choice:
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertNotIn("wrapper:", out)
                else:
                    self.assertEqual(proc.returncode, 0, err)
                    self.assertEqual(out, "")
        self.assertFalse(list(self.root.glob(".lan_intent.*")))

    def test_null_config_values_are_reported_as_missing(self):
        """`null` used to print as the string "None", which is not empty, so
        the config check passed and the launcher died with "Proton not found
        at None/compatibilitytools.d/..." instead of naming what was missing."""
        (self.root / "deckops.json").write_text(json.dumps(
            {"steam_root": None, "ge_proton_version": None}))
        proc = subprocess.run(["bash", str(self.script)], env=self.env,
                              capture_output=True, text=True, timeout=20)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Could not read ge_proton_version or steam_root", proc.stderr)
        self.assertNotIn("None/compatibilitytools.d", proc.stderr)

    def test_overlapping_windows_keep_their_own_intents(self):
        wait_one = str(Path(self.tmp.name) / "release-one")
        wait_two = str(Path(self.tmp.name) / "release-two")
        first = self.run_launcher(str(self.one), wait_one)
        second = self.run_launcher(str(self.two), wait_two)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.path_log.exists() and len(self.path_log.read_text().splitlines()) == 2:
                    break
                time.sleep(.02)
            else:
                self.fail("Both launchers did not reach their UI")
            paths = self.path_log.read_text().splitlines()
            self.assertEqual(len(set(paths)), 2)
            Path(wait_two).touch()
            out, err = second.communicate(timeout=10)
            self.assertEqual(second.returncode, 0, err)
            self.assertIn("wrapper:two", out)
            self.assertIsNone(first.poll())
            Path(wait_one).touch()
            out, err = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, err)
            self.assertIn("wrapper:one", out)
            self.assertFalse(list(self.root.glob(".lan_intent.*")))
        finally:
            for proc in (first, second):
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()


if __name__ == "__main__":
    unittest.main()
