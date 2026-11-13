"""Offline launcher, LAN source, manifest and depot rollback regressions (temp roots only)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import depot_downgrade as depot
import plutonium
import plutonium_update

try:
    import launcher_plut_win as launcher
except ImportError:
    launcher = None


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


@unittest.skipIf(launcher is None, "PyQt5 unavailable")
class PrivateIntentAndGamepadTests(unittest.TestCase):
    def test_malformed_absolute_intent_cannot_replace_config_or_escape_private_dir(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launcher, "_LINUX_PROJECT_ROOT", tmp):
            root = Path(tmp)
            config = root / "deckops.json"
            config.write_text("private config")
            private = root / ".lan_intent.12345678"
            private.mkdir()
            for candidate in (config, private / "../deckops.json", root / "intent.json"):
                with self.subTest(path=str(candidate)), patch.object(launcher, "_INTENT_FILE", str(candidate)):
                    with self.assertRaisesRegex(ValueError, "private DeckOps directory"):
                        launcher._write_intent(str(root / "game.sh"))
            self.assertEqual(config.read_text(), "private config")
            with patch.object(launcher, "_INTENT_FILE", str(private / "intent.json")):
                launcher._write_intent(str(root / "game.sh"))
            self.assertEqual(json.loads((private / "intent.json").read_text())["lan_wrapper_path"],
                             str(root / "game.sh"))
            with patch.object(launcher, "_INTENT_FILE", str(private / "intent.json")):
                with self.assertRaisesRegex(ValueError, "private DeckOps directory"):
                    launcher._write_intent("another")

    def test_xinput_disconnect_error_and_controller_switch_release_held_buttons(self):
        fired = []
        poller = launcher.GamepadPoller(*([lambda: None] * 4),
                                       lambda: fired.append("A"), lambda: fired.append("B"),
                                       lambda: fired.append("Y"))
        try:
            state = lambda buttons: SimpleNamespace(Gamepad=SimpleNamespace(wButtons=buttons))
            with patch.object(launcher, "XInput", create=True) as xi:
                xi.get_connected.return_value = [True, False, False, False]
                xi.get_state.return_value = state(poller.BTN_A | poller.BTN_Y)
                poller._poll()
                poller._poll()
                self.assertEqual(fired, ["A", "Y"])
                xi.get_connected.return_value = [False] * 4
                poller._poll()
                xi.get_connected.return_value = [True, False, False, False]
                poller._poll()
                self.assertEqual(fired, ["A", "Y", "A", "Y"])
                xi.get_state.side_effect = OSError("controller lost")
                poller._poll()
                xi.get_state.side_effect = None
                poller._poll()
                self.assertEqual(fired[-2:], ["A", "Y"])
                self.assertEqual(len(fired), 6)
                xi.get_connected.return_value = [False, True, False, False]
                xi.get_state.return_value = state(poller.BTN_B)
                poller._poll()
                self.assertEqual(fired[-1], "B")
                poller.stop()
                poller._poll()
                self.assertEqual(fired[-1], "B")
                poller._poll()
                self.assertEqual(fired[-1], "B")
        finally:
            poller.stop()


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("python3"),
                     "requires Linux bash and Python")
class PrivateShellHandoffTests(unittest.TestCase):
    def test_user_supplied_intent_option_cannot_override_private_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            lan = root / "assets" / "LAN"
            lan.mkdir(parents=True)
            script = lan / "launcher_offline.sh"
            script.write_bytes((Path(__file__).resolve().parents[1] / "assets" / "LAN" / "launcher_offline.sh")
                               .read_bytes().replace(b"\r\n", b"\n"))
            (lan / "DeckOps_Offline.exe").touch()
            wrapper = root / "game.sh"
            wrapper.write_text('#!/bin/bash\necho launched\n')
            steam = Path(tmp) / "steam"
            proton = steam / "compatibilitytools.d" / "GE" / "proton"
            proton.parent.mkdir(parents=True)
            proton.write_text('''#!/bin/bash
python3 - "$@" <<'PY'
import json, os, sys
args = sys.argv[1:]
path = args[len(args) - 1]
assert args[-2] == '--intent-file' and path.startswith('Z:')
path = path[2:].replace('\\\\', '/')
with open(path, 'w') as f:
    json.dump({'lan_wrapper_path': os.environ['CHOICE']}, f)
PY
''')
            proton.chmod(0o755)
            (root / "deckops.json").write_text(json.dumps({
                "ge_proton_version": "GE", "steam_root": str(steam),
                "setup_games": {"t6zm": {"client": "plutonium", "lan_wrapper_path": str(wrapper)}}}))
            forged = root / "deckops.json"
            proc = subprocess.run(["bash", str(script), "--intent-file", str(forged)],
                                  env={**os.environ, "CHOICE": str(wrapper), "HOME": str(Path(tmp) / "home")},
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("launched", proc.stdout)
            self.assertIn('"setup_games"', forged.read_text())
            self.assertFalse(list(root.glob(".lan_intent.*")))


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash") and shutil.which("flock"),
                     "requires Linux flock and bash")
class LanServerPortTests(unittest.TestCase):
    def test_busy_port_never_stops_another_modes_live_server(self):
        import fcntl
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            lan = root / "assets" / "LAN"
            lan.mkdir(parents=True)
            script = lan / "lan_server.sh"
            script.write_bytes((Path(__file__).resolve().parents[1] / "assets" / "LAN" / "lan_server.sh")
                               .read_bytes().replace(b"\r\n", b"\n"))
            home = Path(tmp) / "home"
            state = home / ".local/share/deckops/lan"
            for part in ("client", "server"):
                (state / "t6mp" / part).mkdir(parents=True)
            steam = Path(tmp) / "steam"
            proton = steam / "compatibilitytools.d" / "GE" / "proton"
            proton.parent.mkdir(parents=True)
            proton.write_text("#!/bin/bash\nexit 0\n")
            proton.chmod(0o755)
            (root / "deckops.json").write_text(json.dumps({"steam_root": str(steam), "ge_proton_version": "GE"}))
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            for name, body in (("ss", "echo 'udp UNCONN 0 0 127.0.0.1:28960 0.0.0.0:*'"),
                               ("systemctl", f'echo "$*" >> "{Path(tmp) / "systemctl.log"}"')):
                tool = bin_dir / name
                tool.write_text("#!/bin/bash\n" + body + "\n")
                tool.chmod(0o755)
            lock = state / "server.lock"
            state.mkdir(parents=True, exist_ok=True)
            with open(lock, "w") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                proc = subprocess.Popen(["bash", str(script), "t6mp", "game", "+name", "Player",
                                         "28960", "game"],
                                        env={**os.environ, "HOME": str(home),
                                             "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]})
                try:
                    time.sleep(.25)
                    self.assertIsNone(proc.poll(), "launch bypassed the shared lock")
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                self.assertEqual(proc.wait(timeout=10), 1)
            self.assertIn("already in use", (root / "logs" / "lan_t6mp.log").read_text())
            calls = Path(tmp) / "systemctl.log"
            self.assertFalse(calls.exists() and "stop" in calls.read_text())


class LanSourceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "requires flock")
    def test_two_refreshes_of_same_mode_never_write_the_tree_at_once(self):
        import time
        with tempfile.TemporaryDirectory() as tmp, patch.object(plutonium, "LAN_ROOT", tmp), \
             patch.object(plutonium, "_lan_assets", return_value={}), \
             patch.object(plutonium, "_lan_disk_check"):
            mode = Path(tmp) / "t6zm"
            source = Path(tmp) / "online"
            source.mkdir()
            put(mode / "source.json", json.dumps({"user_plut": str(source)}).encode())
            entered = threading.Event()
            release = threading.Event()
            calls = []
            errors = []
            def mirror(*args):
                calls.append(threading.get_ident())
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("first refresh was not released")
            def worker():
                try:
                    plutonium.refresh_lan("t6zm")
                except Exception as ex:
                    errors.append(ex)
            with patch.object(plutonium, "_lan_tree", side_effect=mirror):
                first = threading.Thread(target=worker)
                second = threading.Thread(target=worker)
                first.start()
                try:
                    self.assertTrue(entered.wait(timeout=3))
                    second.start()
                    try:
                        time.sleep(.1)
                        self.assertTrue(second.is_alive())
                        self.assertEqual(len(calls), 1)
                    finally:
                        release.set()
                        second.join(timeout=5)
                finally:
                    release.set()
                    first.join(timeout=5)
            self.assertFalse(errors)
            self.assertEqual(len(calls), 2)

    def test_failed_copy_keeps_existing_offline_file_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "online", Path(tmp) / "lan"
            put(src, b"updated")
            put(dst, b"old-working")
            def failed_copy(_src, pending):
                put(Path(pending), b"partial")
                raise OSError("full disk")
            with patch.object(plutonium.os, "link", side_effect=OSError("cross drive")), \
                 patch.object(plutonium.shutil, "copy2", side_effect=failed_copy):
                with self.assertRaises(OSError):
                    plutonium._lan_link(str(src), str(dst))
            self.assertEqual(dst.read_bytes(), b"old-working")
            self.assertEqual(list(Path(tmp).glob(".deckops-lan-*")), [])

    def test_missing_online_source_fails_before_removing_offline_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(plutonium, "LAN_ROOT", tmp):
            mode = Path(tmp) / "t6zm"
            put(mode / "client" / "bin" / "game.exe", b"working")
            put(mode / "source.json", json.dumps({"user_plut": str(mode / "gone")}).encode())
            with self.assertRaisesRegex(FileNotFoundError, "source is missing"):
                plutonium.refresh_lan("t6zm")
            self.assertEqual((mode / "client" / "bin" / "game.exe").read_bytes(), b"working")

    def test_failed_setup_restores_prior_online_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(plutonium, "LAN_ROOT", tmp), \
             patch("config.get_offline_mode", return_value=True), \
             patch.object(plutonium, "_lan_prefixes"), \
             patch.object(plutonium, "refresh_lan", side_effect=OSError("cannot mirror")):
            source = Path(tmp) / "t6zm" / "source.json"
            previous = b'{"user_plut":"previous-source"}\n'
            put(source, previous)
            self.assertFalse(plutonium.setup_lan({"install_dir": str(Path(tmp) / "game")},
                                                  "t6zm", str(Path(tmp) / "new-source"), "own", lambda msg: None))
            self.assertEqual(source.read_bytes(), previous)


class DepotCommitTests(unittest.TestCase):
    def test_failed_second_commit_restores_game_and_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, game = Path(tmp) / "stage", Path(tmp) / "game"
            for name in ("one.bin", "two.bin"):
                put(stage / name, ("new-" + name).encode())
                put(game / name, ("old-" + name).encode())
            original = depot.os.replace
            calls = []
            def fail_second_pending(src, dst):
                if str(src).endswith(depot._PENDING_SUFFIX):
                    calls.append(src)
                    if len(calls) == 2:
                        raise OSError("commit failed")
                return original(src, dst)
            with patch.object(depot.os, "replace", side_effect=fail_second_pending):
                with self.assertRaisesRegex(OSError, "commit failed"):
                    depot._merge_tree(str(stage), str(game), lambda msg: None)
            for name in ("one.bin", "two.bin"):
                self.assertEqual((stage / name).read_bytes(), ("new-" + name).encode())
                self.assertEqual((game / name).read_bytes(), ("old-" + name).encode())
            self.assertFalse(list(game.rglob("*.deckops-new")))
            self.assertFalse(list(game.glob(".deckops_merge_*")))

    def test_equal_size_failed_merge_still_needs_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, game = Path(tmp) / "stage", Path(tmp) / "game"
            put(stage / "depot_42682" / "main" / "iw_00.iwd", b"new")
            put(game / "downgrade" / "main" / "iw_00.iwd", b"old")
            with patch.object(depot, "_merge_tree", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    depot.merge_depots("iw5", str(stage), str(game))
            self.assertEqual((game / "downgrade" / "main" / "iw_00.iwd").read_bytes(), b"old")
            self.assertFalse(depot._receipt_status("iw5", str(game)))
            self.assertTrue(depot.is_downgrade_needed("iw5", str(game)))


class ManifestPathTests(unittest.TestCase):
    def test_update_rejects_symlink_parent_and_traversal_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master"
            other = Path(tmp) / "other"
            master.mkdir()
            other.mkdir()
            try:
                (master / "bin").symlink_to(other, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            info = {"revision": 2, "baseUrl": "unused", "files": [
                {"name": "bin/client.exe", "hash": "a" * 40, "size": 4}]}
            with patch.object(plutonium_update, "download") as download:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    plutonium_update.sync_master(str(master), {}, info)
                download.assert_not_called()
            self.assertEqual(list(other.iterdir()), [])
            info["files"][0]["name"] = "../other/client.exe"
            with self.assertRaisesRegex(ValueError, "unsafe remote path"):
                plutonium_update.sync_master(str(master), {}, info)


if __name__ == "__main__":
    unittest.main()
