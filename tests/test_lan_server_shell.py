"""Server ownership when two offline game sessions share a listening port."""
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


@unittest.skipUnless(sys.platform == "linux" and shutil.which("flock"), "requires Linux flock")
class LanServerOwnershipTests(unittest.TestCase):
    def test_second_session_does_not_stop_first_sessions_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "project"
            lan = root / "assets" / "LAN"
            lan.mkdir(parents=True)
            script = lan / "lan_server.sh"
            script.write_bytes((ROOT / "assets" / "LAN" / "lan_server.sh")
                               .read_bytes().replace(b"\r\n", b"\n"))
            steam = base / "steam"
            proton = steam / "compatibilitytools.d" / "GE" / "proton"
            proton.parent.mkdir(parents=True)
            proton.write_text("""#!/bin/bash
if [[ " $* " == *" -token "* ]] && [ -n "$WAIT" ]; then
    [ -z "$CLIENT_READY" ] || touch "$CLIENT_READY"
    while [ ! -f "$WAIT" ]; do sleep .05; done
fi
""")
            proton.chmod(0o755)
            (root / "deckops.json").write_text(json.dumps({
                "steam_root": str(steam), "ge_proton_version": "GE",
            }))
            prelaunch = root / ".venv" / "bin" / "python3"
            prelaunch.parent.mkdir(parents=True)
            prelaunch.write_text("#!/bin/sh\nprintf '\\t\\n'\n")
            prelaunch.chmod(0o755)
            home = base / "home"
            game_root = home / ".local" / "share" / "deckops" / "lan" / "t6mp"
            for folder in (game_root / "client", game_root / "server"):
                folder.mkdir(parents=True)
            bins = base / "bin"
            bins.mkdir()
            mocks = {
                "ss": """#!/bin/sh
if [ -f "$PORT_FLAG" ]; then echo 'UNCONN 0 0 127.0.0.1:28960 0.0.0.0:*'; fi
""",
                "systemd-run": """#!/bin/sh
for arg in "$@"; do case $arg in --unit=*) echo "${arg#--unit=}" >> "$UNIT_LOG";; esac; done
if [ -n "$START_RELEASE" ]; then
    while [ ! -f "$START_RELEASE" ]; do sleep .05; done
fi
touch "$PORT_FLAG"
""",
                "systemctl": """#!/bin/sh
echo "$*" >> "$STOP_LOG"
rm -f "$PORT_FLAG"
""",
                "timeout": """#!/bin/sh
if [ "$1" = 1 ]; then exit 0; fi
exec /usr/bin/timeout "$@"
""",
            }
            for name, content in mocks.items():
                path = bins / name
                path.write_text(content)
                path.chmod(0o755)
            port_flag = base / "port"
            unit_log = base / "units"
            stop_log = base / "stops"
            first_release = base / "release-first"
            second_release = base / "release-second"
            second_ready = base / "second-ready"
            start_release = base / "release-start"
            env = dict(os.environ, HOME=str(home), PATH=f"{bins}:{os.environ['PATH']}",
                       START_RELEASE="",
                       PORT_FLAG=str(port_flag), UNIT_LOG=str(unit_log), STOP_LOG=str(stop_log))
            command = ["bash", str(script), "t6mp", "Z:\\game", "+name", "Player",
                       "28960", "Z:\\server"]
            first = subprocess.Popen(command, env=dict(env, WAIT=str(first_release),
                                                       START_RELEASE=str(start_release)))
            second = None
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not unit_log.exists():
                    if first.poll() is not None:
                        self.fail((root / "logs" / "lan_t6mp.log").read_text())
                    time.sleep(.02)
                self.assertTrue(unit_log.exists(), "first server did not begin startup")
                second = subprocess.Popen(command, env=dict(env, WAIT=str(second_release),
                                                             CLIENT_READY=str(second_ready)))
                # Both saw the closed port; the second must wait for the lock.
                time.sleep(.1)
                self.assertEqual(len(unit_log.read_text().splitlines()), 1)
                self.assertIsNone(second.poll())
                start_release.touch()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not second_ready.exists():
                    time.sleep(.02)
                self.assertTrue(second_ready.exists(), "second session did not register")
                self.assertEqual(len(unit_log.read_text().splitlines()), 1)
                first_release.touch()
                self.assertEqual(first.wait(timeout=10), 0)
                self.assertFalse(stop_log.exists(), "first owner stopped the second session's server")
                self.assertTrue(port_flag.exists())
                second_release.touch()
                self.assertEqual(second.wait(timeout=10), 0)
                self.assertIn(unit_log.read_text().strip(), stop_log.read_text())
            finally:
                for proc in (first, second):
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                        proc.wait()


if __name__ == "__main__":
    unittest.main()
