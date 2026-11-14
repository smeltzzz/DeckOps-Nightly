"""Sweep 9: full-pass QA fixes -- tar symlink-chain escape, atomic small
writes, Python 3.10 / identity drift guards, and controller UX (offscreen Qt)."""

import ast
import io
import os
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ge_proton  # noqa: E402
import game_config  # noqa: E402
import identity  # noqa: E402


def _tar(members):
    """members: (name, kind, linkname) with kind 'dir'|'sym'|'file'."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, kind, link in members:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            elif kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, link
                t.addfile(ti)
            else:
                data = b"x"
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


class ManualTarFilterTests(unittest.TestCase):
    """The pre-3.11.4 fallback must not be fooled by symlinks that don't exist yet."""

    def setUp(self):
        self.dest = tempfile.mkdtemp()
        saved = tarfile.__dict__.get("data_filter")
        if saved is not None:
            delattr(tarfile, "data_filter")
            self.addCleanup(setattr, tarfile, "data_filter", saved)

    def test_symlink_chain_is_rejected(self):
        tar = _tar([("d", "sym", "."), ("s", "sym", "d/d/d/../../.."), ("s/evil", "file", None)])
        with patch.object(tar, "extractall") as ex:
            with self.assertRaises(RuntimeError):
                ge_proton._safe_extractall(tar, self.dest)
        ex.assert_not_called()

    def test_member_through_link_is_rejected(self):
        tar = _tar([("lib", "sym", "files"), ("lib/x", "file", None)])
        with patch.object(tar, "extractall") as ex:
            with self.assertRaises(RuntimeError):
                ge_proton._safe_extractall(tar, self.dest)
        ex.assert_not_called()

    def test_normal_relative_links_still_allowed(self):
        tar = _tar([("files", "dir", None), ("files/lib", "dir", None), ("files/lib/a.so", "file", None),
                    ("files/lib64", "sym", "lib"), ("top", "sym", "files/lib/a.so")])
        with patch.object(tar, "extractall") as ex:
            ge_proton._safe_extractall(tar, self.dest)
        ex.assert_called_once()


class AtomicSmallWriteTests(unittest.TestCase):
    def test_active_txt_written_atomically(self):
        d = tempfile.mkdtemp()
        with patch("steam_common.atomic_write") as aw:
            game_config._write_cod4_active_txt(d, "Ben")
        aw.assert_called_once_with(os.path.join(d, "active.txt"), b"Ben")

    def test_active_txt_round_trip(self):
        d = tempfile.mkdtemp()
        game_config._write_cod4_active_txt(d, "Ben")
        self.assertEqual(Path(d, "active.txt").read_text(encoding="utf-8"), "Ben")


class GuardTests(unittest.TestCase):
    def test_update_manifest_parses_as_python_310(self):
        src = (ROOT / "src" / "update_manifest.py").read_text(encoding="utf-8")
        ast.parse(src, feature_version=(3, 10))

    def test_shell_identity_fallbacks_match_identity_py(self):
        want = {"EXPECTED_BRANCH": identity.BRANCH, "GITHUB_USER": identity.GITHUB_USER,
                "GITHUB_REPO": identity.GITHUB_REPO}
        for script in ("launcher.sh", "install.sh", "deckops_uninstall.sh"):
            text = (ROOT / script).read_text(encoding="utf-8")
            for var, value in want.items():
                m = re.search(rf'^\s*{var}="([^"]*)"', text, re.M)
                self.assertIsNotNone(m, f"{script}: {var} missing")
                self.assertEqual(m.group(1), value, f"{script}: {var}")


try:
    from qt import QApplication
    import ui_constants as uc
    import ui_install
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class ControllerUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_setup_screen_blocks_escape_while_scanning(self):
        scr = ui_install.SetupScreen.__new__(ui_install.SetupScreen)
        scr._scanning = True
        self.assertFalse(ui_install.SetupScreen.escape_allowed(scr))
        scr._scanning = False
        self.assertTrue(ui_install.SetupScreen.escape_allowed(scr))

    def test_music_button_meets_minimum_height(self):
        src = (ROOT / "src" / "ui_qt.py").read_text(encoding="utf-8")
        self.assertIn("setFixedSize(124, BTN_MIN_H)", src)
        self.assertGreaterEqual(uc.BTN_MIN_H, 44)


if __name__ == "__main__":
    unittest.main()
