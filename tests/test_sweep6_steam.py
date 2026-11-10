"""Regression tests for the sixth sweep: Steam file edits (wrapper launch
options, shortcuts.vdf entries)."""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import shortcut  # noqa: E402
import wrapper  # noqa: E402

_POSIX_ONLY = unittest.skipUnless(os.name == "posix", "needs POSIX file modes")

_LOCALCONFIG = '''"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"7940"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"old %command%"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
'''


# ── STEAM-1: one unreadable localconfig.vdf ──────────────────────────────────

class UnreadableLocalconfigTests(unittest.TestCase):
    """One user's localconfig.vdf that can't be read must not stop the
    launch-option edits for every other user."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # user 11111: "localconfig.vdf" is a directory, so open() fails with
        # an OSError on every platform (EISDIR / EACCES)
        os.makedirs(os.path.join(self.root, "userdata", "11111", "config", "localconfig.vdf"))
        self.good = Path(self.root, "userdata", "22222", "config", "localconfig.vdf")
        self.good.parent.mkdir(parents=True)
        self.good.write_text(_LOCALCONFIG)
        p = patch.object(wrapper, "LEDGER_PATH", str(Path(self.root, "ledger.json")))
        p.start()
        self.addCleanup(p.stop)

    def test_generator_skips_the_unreadable_user_and_warns(self):
        with self.assertLogs("deckops.wrapper", level="WARNING") as logs:
            found = [uid for uid, _, _ in wrapper._localconfigs(self.root)]
        self.assertEqual(found, ["22222"])
        self.assertTrue(any("11111" in line for line in logs.output))

    def test_set_launch_options_still_edits_the_other_user(self):
        with self.assertLogs("deckops.wrapper", level="WARNING"):
            self.assertEqual(wrapper.set_launch_options(self.root, "7940", "new %command%"), 1)
        self.assertIn("new %command%", self.good.read_text())

    def test_snapshot_still_reads_the_other_user(self):
        with self.assertLogs("deckops.wrapper", level="WARNING"):
            snap = wrapper.snapshot_launch_options(self.root, ["7940"])
        self.assertEqual(snap, {"7940": {"22222": "old %command%"}})

    def test_clear_launch_options_does_not_raise(self):
        with self.assertLogs("deckops.wrapper", level="WARNING"):
            wrapper.clear_launch_options(self.root, "7940")
        self.assertNotIn("old %command%", self.good.read_text())

    @_POSIX_ONLY
    def test_permission_denied_file_is_skipped(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        bad_dir = Path(self.root, "userdata", "33333", "config")
        bad_dir.mkdir(parents=True)
        bad = bad_dir / "localconfig.vdf"
        bad.write_text(_LOCALCONFIG)
        os.chmod(bad, 0)
        self.addCleanup(os.chmod, bad, 0o644)
        with self.assertLogs("deckops.wrapper", level="WARNING"):
            found = sorted(uid for uid, _, _ in wrapper._localconfigs(self.root))
        self.assertEqual(found, ["22222"])


# ── STEAM-2 / STEAM-3: shortcuts.vdf ─────────────────────────────────────────

def _entry(idx: int, key: bytes, name: str, exe: str = '"/x"', appid: int = -5) -> bytes:
    return (b"\x00" + str(idx).encode() + b"\x00"
            + b"\x02appid\x00" + struct.pack("<i", appid)
            + b"\x01" + key + b"\x00" + name.encode() + b"\x00"
            + b"\x01Exe\x00" + exe.encode() + b"\x00" + b"\x08")


class _ShortcutsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.userdata = root / "userdata"
        self.vdf = self.userdata / "12345" / "config" / "shortcuts.vdf"
        self.vdf.parent.mkdir(parents=True)
        self.root = root
        for p in (patch.object(shortcut, "_userdata_dir", return_value=str(self.userdata)),
                  patch.object(shortcut, "_find_all_steam_uids", return_value=["12345"]),
                  patch.object(shortcut, "_download_artwork"),
                  patch.object(shortcut, "_assign_controller_config"),
                  patch("wrapper.clear_compat_tool"),
                  patch.object(shortcut, "_clear_compat_tool")):
            p.start()
            self.addCleanup(p.stop)


class AppNameKeyCaseTests(_ShortcutsTestBase):
    """Steam versions and other tools write the AppName key in different
    cases. Stripping only knew "AppName"/"appname", so a re-run appended a
    duplicate shortcut next to an "Appname" entry."""

    def test_strip_matches_any_key_case(self):
        for key in (b"AppName", b"appname", b"Appname", b"APPNAME", b"appName"):
            with self.subTest(key=key):
                body = _entry(0, key, "My Game") + _entry(1, b"AppName", "Other")
                new, stripped = shortcut._strip_entries_by_name(body, {"My Game"})
                self.assertEqual(stripped, {"My Game"})
                self.assertNotIn(b"My Game", new)
                self.assertIn(b"Other", new)

    def test_strip_still_needs_the_whole_title(self):
        body = _entry(0, b"Appname", "My Game Plus")
        new, stripped = shortcut._strip_entries_by_name(body, {"My Game"})
        self.assertEqual(stripped, set())
        self.assertEqual(new, body)

    def test_add_shortcut_replaces_an_appname_entry_instead_of_duplicating(self):
        self.vdf.write_bytes(b"\x00shortcuts\x00" + _entry(0, b"Appname", "My Game") + b"\x08\x08")
        shortcut.add_shortcut("My Game", '"/x"', '"/"', "", {}, "standard", "on")
        body = shortcut._read_shortcuts_raw(str(self.vdf))
        self.assertEqual(len(shortcut._split_entries(body)), 1)
        self.assertEqual(shortcut._read_existing_shortcuts(str(self.vdf)), ["My Game"])


class LauncherShortcutUnparsedEntriesTests(_ShortcutsTestBase):
    """create_launcher_shortcut skipped the _entries_parse guard every other
    writer has: entries it can't split can't be stripped, so it appended a
    second launcher shortcut to a file it couldn't read."""

    def setUp(self):
        super().setUp()
        install = self.root / "DeckOps"
        for rel in (shortcut._LAUNCHER_SH_REL, shortcut._LAUNCHER_EXE_REL):
            p = install / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
        p = patch.object(shortcut, "INSTALL_DIR", str(install))
        p.start()
        self.addCleanup(p.stop)

    def test_unparseable_entries_leave_the_file_untouched(self):
        # Valid header/footer, but the entry has an unknown field type (0x09)
        data = (b"\x00shortcuts\x00" + b"\x00" + b"0\x00" + b"\x09bogus\x00"
                + shortcut.LAUNCHER_TITLE.encode() + b"\x00\x08" + b"\x08\x08")
        self.vdf.write_bytes(data)
        self.assertEqual(shortcut._split_entries(shortcut._read_shortcuts_raw(str(self.vdf))), [])
        msgs = []
        with self.assertLogs("deckops.shortcut", level="WARNING"):
            shortcut.create_launcher_shortcut(msgs.append)
        self.assertEqual(self.vdf.read_bytes(), data)
        self.assertTrue(any("Skipped Steam user 12345" in m for m in msgs))

    def test_parseable_file_still_gets_the_launcher(self):
        self.vdf.write_bytes(b"\x00shortcuts\x00" + _entry(0, b"AppName", "Other") + b"\x08\x08")
        shortcut.create_launcher_shortcut(lambda m: None)
        names = shortcut._read_existing_shortcuts(str(self.vdf))
        self.assertEqual(sorted(names), sorted(["Other", shortcut.LAUNCHER_TITLE]))


if __name__ == "__main__":
    unittest.main()
