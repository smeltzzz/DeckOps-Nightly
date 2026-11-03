"""Cross-module regressions found by the third sweep (orchestrator-owned)."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import depot_downgrade


class CopyToClipboardResultTests(unittest.TestCase):
    """copy_to_clipboard used to return None whether or not anything was copied."""

    def _proc(self, returncode):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate.return_value = (None, None)
        proc.poll.return_value = returncode
        return proc

    def test_no_helper_installed_returns_false(self):
        with patch.object(depot_downgrade.subprocess, "Popen", side_effect=FileNotFoundError("wl-copy")):
            self.assertIs(depot_downgrade.copy_to_clipboard("x"), False)

    def test_helper_that_fails_returns_false(self):
        with patch.object(depot_downgrade.subprocess, "Popen", return_value=self._proc(1)):
            self.assertIs(depot_downgrade.copy_to_clipboard("x"), False)

    def test_helper_that_succeeds_returns_true(self):
        with patch.object(depot_downgrade.subprocess, "Popen", return_value=self._proc(0)):
            self.assertIs(depot_downgrade.copy_to_clipboard("x"), True)

    def test_second_helper_is_tried_after_the_first_is_missing(self):
        calls = []

        def popen(cmd, **kwargs):
            calls.append(cmd[0])
            if cmd[0] == "wl-copy":
                raise FileNotFoundError(cmd[0])
            return self._proc(0)

        with patch.object(depot_downgrade.subprocess, "Popen", side_effect=popen):
            self.assertIs(depot_downgrade.copy_to_clipboard("x"), True)
        self.assertEqual(calls, ["wl-copy", "xclip"])

    def test_helper_that_hangs_is_killed_and_reported_as_failure(self):
        proc = self._proc(None)
        proc.communicate.side_effect = subprocess.TimeoutExpired("wl-copy", 5)
        proc.poll.return_value = None
        with patch.object(depot_downgrade.subprocess, "Popen", return_value=proc):
            self.assertIs(depot_downgrade.copy_to_clipboard("x"), False)
        proc.kill.assert_called()


if __name__ == "__main__":
    unittest.main()
