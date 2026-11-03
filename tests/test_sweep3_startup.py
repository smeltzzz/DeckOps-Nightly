"""Regressions for the third sweep of the startup, install and launch files:
install.sh downloads (stalls, cut-off music file), the desktop-entry bootstrap and
config.reset() racing a setter."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _text(name):
    return (ROOT / name).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _music_block():
    """The 'step 4' section of install.sh, as it would run."""
    text = _text("install.sh")
    start = text.index("# ── step 4")
    end = text.index("# ── step 5")
    return text[start:end]


class InstallDownloadTests(unittest.TestCase):
    def test_archive_download_gives_up_on_a_dead_connection(self):
        text = _text("install.sh")
        line = next(i for i, l in enumerate(text.splitlines()) if "curl -fL --progress-bar" in l)
        call = " ".join(text.splitlines()[line:line + 3])
        # Without these a dead Wi-Fi link froze the installer for good
        self.assertIn("--connect-timeout", call)
        self.assertIn("--speed-limit", call)
        self.assertIn("--speed-time", call)

    def _run_music_step(self, rc):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "curl"
        fake.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$CURL_LOG"\n'
            "out=\n"
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            'printf "cut-off" > "$out"\n'
            'exit "$FAKE_CURL_RC"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        music = tmp / "music"
        script = tmp / "step4.sh"
        script.write_text(
            'info() { :; }; success() { echo "OK: $1"; }; warn() { echo "WARN: $1"; }\n'
            f'MUSIC_DIR="{music}"; MUSIC_FILE=background.mp3; MUSIC_URL=https://example.invalid/a.mp3\n'
            + _music_block())
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "CURL_LOG": str(tmp / "curl.log"), "FAKE_CURL_RC": str(rc)}
        result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return music, result.stdout, (tmp / "curl.log").read_text()

    def test_cut_off_music_download_is_not_left_as_the_music_file(self):
        if not (sys.platform == "linux" and shutil.which("bash")):
            self.skipTest("requires Linux bash")
        music, out, log = self._run_music_step(18)      # curl: transfer ended early
        self.assertIn("WARN", out)
        # "already present" would have kept a truncated file for every later install
        self.assertFalse((music / "background.mp3").exists())
        self.assertEqual(list(music.iterdir()), [])
        self.assertIn("--connect-timeout", log)
        self.assertIn("--speed-limit", log)

    def test_finished_music_download_is_moved_into_place(self):
        if not (sys.platform == "linux" and shutil.which("bash")):
            self.skipTest("requires Linux bash")
        music, out, _ = self._run_music_step(0)
        self.assertIn("OK: Background music downloaded.", out)
        self.assertEqual((music / "background.mp3").read_bytes(), b"cut-off")
        self.assertFalse((music / "background.mp3.part").exists())


class DesktopEntryTests(unittest.TestCase):
    def test_bootstrap_download_in_the_desktop_entry_has_time_limits(self):
        exec_line = next(l for l in _text("DeckOps-Nightly.desktop").splitlines() if l.startswith("Exec="))
        self.assertIn("curl -fsSL --connect-timeout", exec_line)
        self.assertIn("--max-time", exec_line)
        self.assertIn("smeltzzz/DeckOps-Nightly", exec_line)      # the fork, not upstream


class ConfigResetTests(unittest.TestCase):
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

    def test_reset_waits_for_a_setter_instead_of_being_undone_by_it(self):
        self.config.set_player_name("Old")
        done = threading.Event()

        def reset():
            self.config.reset()
            done.set()

        with self.config.transaction():
            data = self.config.load()           # a setter that has read the old setup
            worker = threading.Thread(target=reset)
            worker.start()
            self.assertFalse(done.wait(0.3), "reset ran in the middle of a read-modify-write")
            data["player_name"] = "Newer"
            self.config.save(data)
        worker.join(5)
        self.assertTrue(done.is_set())
        # reset came last: the old setup is not resurrected by the setter's save
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.config.get_player_name())

    def test_reset_of_a_config_that_is_already_gone_does_not_raise(self):
        self.assertFalse(self.path.exists())
        self.config.reset()
        self.path.write_text(json.dumps({"player_name": "x"}))
        with patch.object(self.config.os, "remove", side_effect=FileNotFoundError):
            self.config.reset()                 # another process removed it between check and delete


if __name__ == "__main__":
    unittest.main()
