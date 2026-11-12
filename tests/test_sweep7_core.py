"""Sweep 7 regressions: launch environment, config cache, and pre-Qt cleanup."""

import ast
import builtins
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class FovWatchEnvironmentTests(unittest.TestCase):
    def test_empty_saved_values_remove_ambient_helper_libraries(self):
        import fovwatch
        with patch.dict(os.environ, {
            "LD_PRELOAD": "/helper/preload.so",
            "LD_LIBRARY_PATH": "/helper/lib",
            "DECKOPS_LD_PRELOAD": "",
            "DECKOPS_LD_LIBRARY_PATH": "",
        }):
            env = fovwatch._child_env()
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("DECKOPS_LD_PRELOAD", env)
        self.assertNotIn("DECKOPS_LD_LIBRARY_PATH", env)

    def test_nonempty_saved_values_restore_steams_environment(self):
        import fovwatch
        with patch.dict(os.environ, {
            "LD_LIBRARY_PATH": "/helper/lib",
            "DECKOPS_LD_PRELOAD": "/steam/overlay.so",
            "DECKOPS_LD_LIBRARY_PATH": "/steam/runtime/lib",
        }):
            env = fovwatch._child_env()
        self.assertEqual(env["LD_PRELOAD"], "/steam/overlay.so")
        self.assertEqual(env["LD_LIBRARY_PATH"], "/steam/runtime/lib")

    def test_without_saved_values_direct_invocation_keeps_environment(self):
        import fovwatch
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/game/lib"}, clear=True):
            env = fovwatch._child_env()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/game/lib")


class ConfigCacheTests(unittest.TestCase):
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

    def test_external_deletion_invalidates_the_cached_setup(self):
        self.config.set_player_name("Before")
        self.assertEqual(self.config.get_player_name(), "Before")
        self.path.unlink()
        self.assertIsNone(self.config.get_player_name())
        self.config.set_player_name("After")
        self.assertEqual(json.loads(self.path.read_text())["player_name"], "After")

    def test_corrupt_config_does_not_leave_a_stale_cached_setup(self):
        self.config.set_player_name("Before")
        self.path.write_text("{truncated")
        self.assertIsNone(self.config.get_player_name())
        self.assertTrue(list(Path(self.tmp.name).glob("deckops.json.corrupt-*")))
        self.config.set_player_name("After")
        self.assertEqual(json.loads(self.path.read_text())["player_name"], "After")

    def test_corrupt_config_that_cannot_be_moved_aside_is_not_overwritten(self):
        self.path.write_text("{truncated")
        with patch.object(self.config.os, "replace", side_effect=OSError("read-only directory")):
            self.assertIsNone(self.config.get_player_name())
            with self.assertRaises(self.config.ConfigUnreadable):
                self.config.set_player_name("After")
        self.assertEqual(self.path.read_text(), "{truncated")

    def test_disappearing_corrupt_file_invalidates_the_cache(self):
        self.config.set_player_name("Before")
        self.path.write_text("{truncated")
        st = self.path.stat()
        with patch.object(self.config.os, "stat", side_effect=[st, FileNotFoundError()]):
            self.assertIsNone(self.config.get_player_name())
        self.assertIsNone(self.config._cache)
        self.assertIsNone(self.config._cache_key)


class MainQtProgressCleanupTests(unittest.TestCase):
    def _function(self):
        tree = ast.parse((ROOT / "src" / "main.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_qt6")
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)
        return compile(module, str(ROOT / "src" / "main.py"), "exec")

    def test_exited_zenity_does_not_crash_a_successful_component_install(self):
        code = self._function()
        pip = "/venv/bin/pip"
        z = MagicMock()
        z.stdin.close.side_effect = BrokenPipeError("dialog already closed")
        z.terminate.side_effect = ProcessLookupError("dialog already exited")
        fake_subprocess = types.SimpleNamespace(
            Popen=MagicMock(return_value=z),
            run=MagicMock(side_effect=[types.SimpleNamespace(returncode=0),
                                       types.SimpleNamespace(returncode=0)]),
            SubprocessError=subprocess.SubprocessError,
            DEVNULL=subprocess.DEVNULL,
            PIPE=subprocess.PIPE,
        )
        fake_shutil = types.SimpleNamespace(which=MagicMock(return_value="/usr/bin/zenity"))
        fake_importlib = types.SimpleNamespace(invalidate_caches=MagicMock())
        fake_os = types.SimpleNamespace(path=types.SimpleNamespace(
            join=os.path.join,
            exists=lambda p: p == pip,
        ))
        fake_sys = types.SimpleNamespace(prefix="/venv", base_prefix="/usr", executable="/venv/bin/python")
        ns = {"sys": fake_sys, "os": fake_os, "get_logger": MagicMock()}
        exec(code, ns)
        real_import = builtins.__import__

        def no_qt(name, *args, **kwargs):
            if name.startswith("PyQt"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch.dict(sys.modules, {"shutil": fake_shutil, "subprocess": fake_subprocess,
                                      "importlib": fake_importlib}), \
             patch("builtins.__import__", side_effect=no_qt):
            ns["_ensure_qt6"]()
        z.wait.assert_called_once_with(timeout=5)
        self.assertEqual(fake_subprocess.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
