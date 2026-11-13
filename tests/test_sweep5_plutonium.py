"""Regression tests for the fifth sweep: plutonium (Offline Mode add / remove),
plutonium_lcd, plutonium_update and lan_server.sh."""

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

import config
import plutonium
import plutonium_lcd
import plutonium_update


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class RemoveLanStopsServersTests(unittest.TestCase):
    def test_servers_are_stopped_by_pattern_and_recorded_unit_before_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            lan = os.path.join(tmp, "lan")
            _write(os.path.join(lan, "t6mp", "server.unit"), b"deckops-lan-t6mp-0123456789abcdef\n")
            calls = []

            def run(cmd, **kw):
                self.assertIn("timeout", kw)
                self.assertTrue(os.path.isdir(lan), "folders removed under a running server")
                calls.append(cmd)
            with patch.object(plutonium, "LAN_ROOT", lan), \
                 patch.object(plutonium.subprocess, "run", side_effect=run):
                plutonium.remove_lan()
            self.assertFalse(os.path.exists(lan))
        stopped = [c[-1] for c in calls]
        for mode in {plutonium._plut_key(k) for k in plutonium.LAN_KEYS}:
            self.assertIn(f"deckops-lan-{mode}-*", stopped)
        self.assertIn("deckops-lan-t6mp-0123456789abcdef", stopped)
        self.assertNotIn("deckops-lan-t6mp", stopped)       # the old exact name never matched

    def test_missing_systemctl_does_not_stop_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            lan = os.path.join(tmp, "lan")
            os.makedirs(os.path.join(lan, "t4mp"))
            with patch.object(plutonium, "LAN_ROOT", lan), \
                 patch.object(plutonium.subprocess, "run", side_effect=FileNotFoundError("systemctl")):
                plutonium.remove_lan()
            self.assertFalse(os.path.exists(lan))


class AddOfflineModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(config, "CONFIG_PATH", os.path.join(self.tmp.name, "deckops.json"))
        p.start()
        self.addCleanup(p.stop)
        config._cache = config._cache_key = None
        self.addCleanup(setattr, config, "_cache", None)
        self.addCleanup(setattr, config, "_cache_key", None)

    def _game(self, name):
        d = os.path.join(self.tmp.name, name)
        os.makedirs(d, exist_ok=True)
        return {"name": name, "install_dir": d, "source": "steam"}

    def test_missing_game_folder_stops_add_before_any_game_is_changed(self):
        setup = {"t4mp": {"client": "plutonium", "source": "steam"},
                 "t5mp": {"client": "plutonium", "source": "steam"}}
        config.save({**config.DEFAULTS, "setup_games": setup})
        with patch("detect_games.find_all_games", return_value={"t4mp": self._game("waw")}), \
             patch("wrapper.get_proton_path", return_value="/p"), \
             patch.object(plutonium, "_plut_compat", return_value="/prefix"), \
             patch.object(plutonium, "install_plutonium") as install, \
             patch("shortcut.create_launcher_shortcut") as create:
            with self.assertRaisesRegex(RuntimeError, "t5mp: configured steam game folder not found"):
                plutonium.apply_offline_mode(True, "/steam")
        install.assert_not_called()
        create.assert_not_called()
        self.assertFalse(config.get_offline_mode())

    def test_launcher_shortcut_not_added_is_a_failure_and_rolls_back(self):
        game = self._game("waw")
        script = os.path.join(game["install_dir"], "t4plut_lan_mp.sh")
        menu = os.path.join(game["install_dir"], "t4plutmp_lan.exe")
        config.save({**config.DEFAULTS, "setup_games": {"t4mp": {"client": "plutonium", "source": "steam"}}})

        def install(g, k, *a, **kw):
            if config.get_offline_mode():
                _write(script)
                _write(menu)
                config.mark_game_setup(k, "plutonium", source="steam", lan_wrapper_path=script)
        with patch("detect_games.find_all_games", return_value={"t4mp": game}), \
             patch("wrapper.get_proton_path", return_value="/p"), \
             patch("config.get_deck_model", return_value="oled"), \
             patch.object(plutonium, "_plut_compat", return_value="/prefix"), \
             patch.object(plutonium, "install_plutonium", side_effect=install) as inst, \
             patch("game_config.apply_game_configs"), \
             patch("shortcut.create_launcher_shortcut", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "launcher shortcut could not be added"):
                plutonium.apply_offline_mode(True, "/steam")
        self.assertFalse(config.get_offline_mode())
        self.assertNotIn("lan_wrapper_path", config.get_setup_games()["t4mp"])
        self.assertFalse(os.path.exists(script))
        self.assertFalse(os.path.exists(menu))
        self.assertEqual(inst.call_count, 2)                 # the add, then the online-only rerun

    def test_failed_game_rolls_back_the_games_already_set_up(self):
        waw, bo1 = self._game("waw"), self._game("bo1")
        waw_script = os.path.join(waw["install_dir"], "t4plut_lan_mp.sh")
        config.save({**config.DEFAULTS, "setup_games": {
            "t4mp": {"client": "plutonium", "source": "steam"},
            "t5mp": {"client": "plutonium", "source": "steam"}}})

        def install(g, k, *a, **kw):
            if not config.get_offline_mode():
                return
            if k == "t5mp":
                raise RuntimeError("download failed")
            _write(waw_script)
            config.mark_game_setup(k, "plutonium", source="steam", lan_wrapper_path=waw_script)
        with patch("detect_games.find_all_games", return_value={"t4mp": waw, "t5mp": bo1}), \
             patch("wrapper.get_proton_path", return_value="/p"), \
             patch.object(plutonium, "_plut_compat", return_value="/prefix"), \
             patch.object(plutonium, "install_plutonium", side_effect=install), \
             patch("shortcut.create_launcher_shortcut") as create:
            with self.assertRaisesRegex(RuntimeError, "t5mp: download failed"):
                plutonium.apply_offline_mode(True, "/steam")
        create.assert_not_called()
        self.assertFalse(config.get_offline_mode())
        self.assertNotIn("lan_wrapper_path", config.get_setup_games()["t4mp"])
        self.assertFalse(os.path.exists(waw_script))


class LinkPrefixTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "symlinks need POSIX")
    def test_shared_dir_the_master_gained_is_linked_and_real_dirs_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = os.path.join(tmp, "master")
            for d in plutonium_update.SHARED_DIRS:
                _write(os.path.join(master, d, "f"))
            prefix = os.path.join(tmp, "pfx", "Plutonium")
            os.makedirs(os.path.join(prefix, "games"))               # full copy install
            os.symlink(os.path.join(master, "bin"), os.path.join(prefix, "bin"))
            n = plutonium_update.link_prefix(master, prefix, {"files": []})
            self.assertEqual(n, 1)
            self.assertEqual(os.path.realpath(os.path.join(prefix, "launcher")),
                             os.path.realpath(os.path.join(master, "launcher")))
            self.assertFalse(os.path.islink(os.path.join(prefix, "games")))
            self.assertEqual(plutonium_update.link_prefix(master, prefix, {"files": []}), 0)


class HeroicMinimizeTests(unittest.TestCase):
    def test_config_that_is_not_utf8_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            _write(path, b'{"defaultSettings": {"name": "\xff\xfe"}}')
            msgs = []
            with patch.object(plutonium_lcd.os.path, "expanduser", return_value=path):
                plutonium_lcd._set_heroic_minimize_on_launch(msgs.append)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b'{"defaultSettings": {"name": "\xff\xfe"}}')
        self.assertTrue(any("Could not read" in m for m in msgs))


class LcdSteamMenuWithoutProtonTests(unittest.TestCase):
    def test_missing_proton_path_still_writes_offline_script_and_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = {"name": "WaW", "install_dir": tmp}
            with patch.object(plutonium_lcd, "get_shared_plut_dir", return_value=tmp), \
                 patch.object(plutonium_lcd, "is_plutonium_ready_lcd", return_value=True), \
                 patch.object(plutonium_lcd, "_write_plutonium_config_lcd"), \
                 patch.object(plutonium_lcd, "_install_menu_mod_lcd"), \
                 patch.object(plutonium_lcd, "setup_heroic_game"), \
                 patch.object(plutonium_lcd, "_set_heroic_steam_launch_options"), \
                 patch.object(plutonium_lcd, "_nuke_shader_cache"), \
                 patch.object(plutonium_lcd, "_write_metadata_lcd"), \
                 patch.object(plutonium_lcd, "_write_lcd_menu_files") as menu_files, \
                 patch("config.get_ge_proton_version", return_value="GE"), \
                 patch("config.get_offline_mode", return_value=True), \
                 patch("config.mark_game_setup") as mark, \
                 patch.object(plutonium, "_restore_original_exe"), \
                 patch.object(plutonium, "_write_offline_wrapper", return_value="/w/t4.sh") as offline, \
                 patch.object(plutonium, "add_steam_menu") as menu:
                plutonium_lcd.install_plutonium_lcd(game, "t4mp", {"t4mp": game}, steam_root="/steam",
                                                    proton_path=None, source="steam", lan_ok=True)
        offline.assert_called_once_with(game, "t4mp", "steam")
        menu_files.assert_called_once_with(game, "t4mp", "/w/t4.sh")
        menu.assert_called_once()
        self.assertEqual(mark.call_args.kwargs["lan_wrapper_path"], "/w/t4.sh")


@unittest.skipUnless(sys.platform == "linux" and shutil.which("flock"), "requires Linux flock")
class LanServerOtherModeTests(unittest.TestCase):
    """All modes share one port: a server another mode left up must not be joined."""

    def _setup(self, base):
        root = base / "project"
        lan = root / "assets" / "LAN"
        lan.mkdir(parents=True)
        script = lan / "lan_server.sh"
        script.write_bytes((ROOT / "assets" / "LAN" / "lan_server.sh").read_bytes().replace(b"\r\n", b"\n"))
        steam = base / "steam"
        proton = steam / "compatibilitytools.d" / "GE" / "proton"
        proton.parent.mkdir(parents=True)
        proton.write_text("#!/bin/bash\nexit 0\n")
        proton.chmod(0o755)
        (root / "deckops.json").write_text(json.dumps({"steam_root": str(steam), "ge_proton_version": "GE"}))
        prelaunch = root / ".venv" / "bin" / "python3"
        prelaunch.parent.mkdir(parents=True)
        prelaunch.write_text("#!/bin/sh\nprintf '\\037\\n'\n")
        prelaunch.chmod(0o755)
        home = base / "home"
        state = home / ".local" / "share" / "deckops" / "lan" / "t6mp"
        for folder in (state / "client", state / "server"):
            folder.mkdir(parents=True)
        bins = base / "bin"
        bins.mkdir()
        mocks = {
            "ss": "#!/bin/sh\nif [ -f \"$PORT_FLAG\" ]; then echo 'UNCONN 0 0 127.0.0.1:28960 0.0.0.0:*'; fi\n",
            "systemd-run": """#!/bin/sh
for arg in "$@"; do case $arg in --unit=*) echo "${arg#--unit=}" >> "$UNIT_LOG";; esac; done
touch "$PORT_FLAG"
""",
            "systemctl": """#!/bin/sh
echo "$*" >> "$CTL_LOG"
case $2 in
  is-active) exit "${ACTIVE_RC:-3}" ;;
  stop) rm -f "$PORT_FLAG" ;;
esac
""",
            "timeout": "#!/bin/sh\nif [ \"$1\" = 1 ]; then exit 0; fi\nexec /usr/bin/timeout \"$@\"\n",
        }
        for name, content in mocks.items():
            path = bins / name
            path.write_text(content)
            path.chmod(0o755)
        env = dict(os.environ, HOME=str(home), PATH=f"{bins}:{os.environ['PATH']}",
                   PORT_FLAG=str(base / "port"), UNIT_LOG=str(base / "units"), CTL_LOG=str(base / "ctl"))
        command = ["bash", str(script), "t6mp", "Z:\\game", "+name", "Player", "28960", "Z:\\server"]
        return command, env, state, root

    def _run(self, command, env, root):
        start = time.monotonic()
        proc = subprocess.run(command, env=env, timeout=60)
        self.assertLess(time.monotonic() - start, 60)
        self.assertEqual(proc.returncode, 0, (root / "logs" / "lan_t6mp.log").read_text())

    def test_other_modes_server_is_not_stopped_without_proof_it_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            command, env, _state, root = self._setup(base)
            (base / "port").touch()  # Another game may be using the shared port.
            proc = subprocess.run(command, env=env, timeout=60)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("already in use", (root / "logs" / "lan_t6mp.log").read_text())
            self.assertFalse((base / "units").exists())
            self.assertFalse((base / "ctl").exists())

    def test_this_modes_active_server_is_joined(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            command, env, state, root = self._setup(base)
            (base / "port").touch()
            (state / "server.unit").write_text("deckops-lan-t6mp-0123456789abcdef\n")
            self._run(command, dict(env, ACTIVE_RC="0"), root)
            ctl = (base / "ctl").read_text().splitlines()
            self.assertIn("--user is-active --quiet deckops-lan-t6mp-0123456789abcdef", ctl)
            self.assertNotIn("--user stop deckops-lan-*", ctl)
            self.assertFalse((base / "units").exists())


if __name__ == "__main__":
    unittest.main()
