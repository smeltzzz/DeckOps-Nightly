"""The campaign pre-launch helper must not make a damaged install block play."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and shutil.which("bash"), "requires Linux bash")
class PrelaunchFallbackTests(unittest.TestCase):
    def test_missing_fov_helper_dependency_still_starts_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / "prelaunch.sh").write_bytes((ROOT / "prelaunch.sh").read_bytes().replace(b"\r\n", b"\n"))
            (root / "src" / "fovwatch.py").write_text("import missing_fovwatch_dependency\n")
            (root / ".venv" / "bin" / "python3").symlink_to(sys.executable)
            result = subprocess.run(
                ["bash", str(root / "prelaunch.sh"), "iw4sp", sys.executable,
                 "-c", "print('GAME STARTED')"],
                capture_output=True, text=True, timeout=12, env={**os.environ, "DECKOPS_MENU": ""},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GAME STARTED", result.stdout)


if __name__ == "__main__":
    unittest.main()
