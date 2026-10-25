"""deckops_uninstall.sh embeds ~2,000 lines of Python as heredocs (they edit the
user's Steam files, and src/ is already gone by the time they run, so they
can't import anything from DeckOps). Nothing used to lint or run them.

These tests pull the real blocks out of the script and run them against a fake
$HOME. What they protect is the user's own non-Steam shortcuts: the uninstaller
must remove DeckOps' entries and nothing else, whatever bytes the user's
entries contain.
"""

import os
import re
import struct
import subprocess
import sys

import pytest

import shortcut                      # the installer's own writer builds the fixtures
import steam_common

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deckops_uninstall.sh")
UID = "12345678"


# -- pulling the blocks out of the script ------------------------------------

def _blocks():
    with open(SCRIPT, encoding="utf-8") as f:
        lines = f.read().replace("\r\n", "\n").split("\n")
    found, i = [], 0
    while i < len(lines):
        if re.search(r"python3 -.*<<\s*'?PYEOF'?\s*$", lines[i]):
            j = i + 1
            while lines[j].strip() != "PYEOF":
                j += 1
            found.append((i + 1, "\n".join(lines[i + 1:j]) + "\n"))
            i = j
        i += 1
    return found


BLOCKS = _blocks()


def block_containing(*needles):
    hits = [src for _, src in BLOCKS if all(n in src for n in needles)]
    assert len(hits) == 1, f"{needles}: {len(hits)} blocks match"
    return hits[0]


def run_block(src, home, *args, env=None):
    path = os.path.join(str(home), "block.py")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    e = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run([sys.executable, path, *map(str, args)], capture_output=True,
                          env=e, timeout=60, encoding="utf-8")


# -- the embedded Python as a whole ------------------------------------------

def test_the_script_has_the_embedded_blocks_we_expect():
    assert len(BLOCKS) == 12


@pytest.mark.parametrize("line, src", BLOCKS, ids=[f"L{n}" for n, _ in BLOCKS])
def test_block_is_valid_python(line, src):
    compile(src, f"deckops_uninstall.sh:{line}", "exec")


def test_blocks_have_no_undefined_names_or_unused_imports():
    ruff = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
    if ruff.returncode != 0:
        pytest.skip("ruff not installed")
    problems = []
    for line, src in BLOCKS:
        r = subprocess.run([sys.executable, "-m", "ruff", "check", "--isolated", "--select", "F,E9",
                            "--output-format", "concise",
                            "--stdin-filename", f"deckops_uninstall.sh_L{line}.py", "-"],
                           input=src.encode("utf-8"), capture_output=True)
        if r.returncode != 0:
            problems.append(r.stdout.decode("utf-8", "replace").strip())
    assert not problems, "\n".join(problems)


# -- shortcuts.vdf fixtures and an independent reader -------------------------

def entry(name, exe="/usr/bin/true", launch="", appid=None, tags=True, **extra):
    fields = {"appid": appid if appid is not None else steam_common.calc_shortcut_appid(exe, name) - (1 << 32),
              "AppName": name, "Exe": f'"{exe}"', "StartDir": "/", "LaunchOptions": launch,
              "tags": {"0": "favorite"}}
    if not tags:                      # entries written by some other tools have no tags section
        del fields["tags"]
    fields.update(extra)
    return fields


def build(entries):
    data = b"\x00shortcuts\x00"
    for i, fields in enumerate(entries):
        data += shortcut._make_shortcut_entry(i, fields)
    return data + b"\x08\x08"


def parse(data):
    """[(index, {key: value})] by walking the typed binary format (not by searching for bytes)."""
    assert data.startswith(b"\x00shortcuts\x00"), data[:20]
    assert data.endswith(b"\x08\x08"), data[-8:]
    pos, out = len(b"\x00shortcuts\x00"), []

    def cstr():
        nonlocal pos
        end = data.index(b"\x00", pos)
        s = data[pos:end].decode("utf-8")
        pos = end + 1
        return s

    def fields():
        nonlocal pos
        d = {}
        while True:
            t = data[pos]
            pos += 1
            if t == 0x08:
                return d
            key = cstr()
            if t == 0x00:
                d[key] = fields()
            elif t == 0x01:
                d[key] = cstr()
            elif t == 0x02:
                d[key] = struct.unpack("<i", data[pos:pos + 4])[0]
                pos += 4
            else:
                raise AssertionError(f"unexpected type {t:#x} at {pos}")

    while data[pos] != 0x08:
        assert data[pos] == 0x00
        pos += 1
        out.append((int(cstr()), fields()))
    assert data[pos:] == b"\x08\x08" or data[pos:] == b"\x08" + b"\x08", data[pos:]
    return out


def names(data):
    return [f["AppName"] for _, f in parse(data)]


@pytest.fixture
def steam(tmp_path):
    """A fake $HOME with a Steam userdata folder; returns helpers to write/read shortcuts.vdf."""
    root = tmp_path / ".local" / "share" / "Steam"
    cfgdir = root / "userdata" / UID / "config"
    cfgdir.mkdir(parents=True)
    vdf = cfgdir / "shortcuts.vdf"

    class S:
        home = tmp_path
        steam_root = root
        path = vdf

        @staticmethod
        def write(entries):
            vdf.write_bytes(build(entries))

        @staticmethod
        def read():
            return vdf.read_bytes()

    return S


# The user's own shortcuts. The last one is the nasty case: an int field made of
# 0x08 bytes (what the file format uses to end a section), as can happen in an
# app id or timestamp by chance.
USER = [
    entry("Hades", "/home/deck/games/hades"),
    entry("Celeste", "/home/deck/games/celeste"),
    entry("Emulator: 08080808", "/home/deck/emu", appid=0x08080808, LastPlayTime=0x08080808),
]


def user_entries_survived(after_names):
    return [n for n in after_names if n in {u["AppName"] for u in USER}] == [u["AppName"] for u in USER]


# -- block: remove DeckOps shortcuts (Steam path and own games) -----------------

@pytest.fixture(scope="module")
def remove_shortcuts_block():
    return block_containing("SHORTCUT_NAMES", "shortcuts.vdf")


DECKOPS = [
    entry("DeckOps", "/home/deck/DeckOps-Nightly/launcher.sh"),
    entry("Call of Duty: Black Ops III", "/home/deck/Games/bo3/t7x.exe"),
    entry("DeckOps: Plutonium Offline", "/home/deck/DeckOps-Nightly/assets/LAN/launcher_offline.sh"),
]


def test_removes_deckops_shortcuts_and_keeps_the_users(remove_shortcuts_block, steam):
    mixed = [USER[0], DECKOPS[0], USER[1], DECKOPS[1], USER[2], DECKOPS[2]]
    steam.write(mixed)
    original = steam.read()
    r = run_block(remove_shortcuts_block, steam.home)
    assert r.returncode == 0, r.stderr
    after = steam.read()
    assert names(after) == ["Hades", "Celeste", "Emulator: 08080808"]
    assert [i for i, _ in parse(after)] == [0, 1, 2]                      # re-indexed, no gaps
    assert parse(after)[2][1]["appid"] == 0x08080808                       # the nasty entry is intact
    assert parse(after)[2][1]["LastPlayTime"] == 0x08080808
    assert (steam.path.parent / "shortcuts.vdf.deckops_uninstall.bak").read_bytes() == original
    assert "removed shortcut \"DeckOps\"" in r.stdout


def test_running_it_twice_changes_nothing_more(remove_shortcuts_block, steam):
    steam.write([USER[0], DECKOPS[0], USER[1]])
    run_block(remove_shortcuts_block, steam.home)
    once = steam.read()
    r = run_block(remove_shortcuts_block, steam.home)
    assert steam.read() == once
    assert "no DeckOps shortcuts found" in r.stdout


def test_leaves_the_file_alone_when_there_is_nothing_to_remove(remove_shortcuts_block, steam):
    steam.write(USER)
    before = steam.read()
    run_block(remove_shortcuts_block, steam.home)
    assert steam.read() == before
    assert not (steam.path.parent / "shortcuts.vdf.deckops_uninstall.bak").exists()


def test_no_steam_folder_is_fine(remove_shortcuts_block, tmp_path):
    r = run_block(remove_shortcuts_block, tmp_path)
    assert r.returncode == 0 and "No Steam userdata found" in r.stdout


def test_the_other_branch_keeps_its_shortcut_and_launcher(remove_shortcuts_block, steam):
    # Stable and Nightly installed side by side: uninstalling one must not
    # take the other's "DeckOps" shortcut or its Offline launcher.
    other = "/home/deck/DeckOps"
    steam.write([
        entry("DeckOps", f"{other}/launcher.sh"),                                   # the other branch's
        entry("DeckOps: Plutonium Offline", f"{other}/assets/LAN/launcher_offline.sh"),   # its launcher
        entry("DeckOps: Plutonium Offline", "/home/deck/DeckOps-Nightly/assets/LAN/launcher_offline.sh"),
        USER[0],
    ])
    run_block(remove_shortcuts_block, steam.home,
              env={"DECKOPS_KEEP_OTHER": "1", "DECKOPS_OTHER_DIR": other, "DECKOPS_OTHER_TITLE": "DeckOps"})
    after = parse(steam.read())
    assert [f["AppName"] for _, f in after] == ["DeckOps", "DeckOps: Plutonium Offline", "Hades"]
    assert after[1][1]["Exe"].strip('"').startswith(other + "/")


# -- block: remove the old LCD / Heroic shortcuts --------------------------------

@pytest.fixture(scope="module")
def remove_lcd_block():
    return block_containing("removed_here", "lcd_wrappers/plut_")


LCD = [
    entry("Call of Duty: World at War", "/home/deck/Games/Heroic/lcd_wrappers/plut_t4sp.sh"),
    entry("Call of Duty: Black Ops", "flatpak", launch="run com.heroicgameslauncher.hgl heroic://launch/sideload/x"),
    entry("Call of Duty: Black Ops II", "/usr/bin/flatpak", launch="run com.heroicgameslauncher.hgl heroic://launch/sideload/y"),
]
# Looks similar but is the user's own: Flatpak apps with no Heroic launch URL.
USER_FLATPAK = entry("Firefox", "/usr/bin/flatpak", launch="run org.mozilla.firefox")


def test_removes_lcd_shortcuts_and_keeps_the_users(remove_lcd_block, steam):
    steam.write([USER[0], LCD[0], USER_FLATPAK, LCD[1], USER[1], LCD[2]])
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert r.returncode == 0, r.stderr
    after = steam.read()
    assert names(after) == ["Hades", "Firefox", "Celeste"]
    assert [i for i, _ in parse(after)] == [0, 1, 2]
    assert "removed 3 LCD shortcut(s)" in r.stdout


def test_lcd_removal_does_not_corrupt_entries_that_contain_end_marker_bytes(remove_lcd_block, steam):
    # The block used to split the file on b"\x08\x08" to find entries. 0x08 is
    # also a legal byte inside an integer field, so a user's entry holding two
    # of them in a row was cut in half and the file written back damaged.
    steam.write([LCD[0], USER[2], USER[0]])
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert r.returncode == 0, r.stderr
    after = parse(steam.read())                       # raises if the file is no longer well-formed
    assert [f["AppName"] for _, f in after] == ["Emulator: 08080808", "Hades"]
    assert after[0][1]["appid"] == 0x08080808 and after[0][1]["LastPlayTime"] == 0x08080808
    assert [i for i, _ in after] == [0, 1]


def test_lcd_removal_leaves_the_file_alone_when_nothing_matches(remove_lcd_block, steam):
    steam.write(USER + [USER_FLATPAK])
    before = steam.read()
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert steam.read() == before
    assert "No DeckOps LCD shortcuts to remove" in r.stdout


def test_lcd_removal_skips_a_file_it_does_not_recognise(remove_lcd_block, steam):
    steam.path.write_bytes(b"not a shortcuts file at all")
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert r.returncode == 0
    assert steam.read() == b"not a shortcuts file at all"


def test_lcd_removal_handles_entries_without_a_tags_section(remove_lcd_block, steam):
    # Entries from some other tools have no tags section, so they don't end in
    # two 0x08 bytes: splitting on that pair merged them with their neighbour.
    # (merged: the user's entry was deleted along with the LCD one next to it,
    # or the LCD entry survived because the user's came first.)
    steam.write([
        entry("Plain 1", "/home/deck/a", tags=False),
        LCD[0],
        entry("Plain 2", "/home/deck/b", tags=False),
        entry("Old Heroic", "flatpak", launch="run x heroic://launch/sideload/z", tags=False),
        entry("Plain 3", "/home/deck/c", tags=False),
    ])
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert r.returncode == 0, r.stderr
    after = parse(steam.read())
    assert [f["AppName"] for _, f in after] == ["Plain 1", "Plain 2", "Plain 3"]
    assert [i for i, _ in after] == [0, 1, 2]


def test_lcd_removal_never_takes_a_users_entry_along_with_it(remove_lcd_block, steam):
    # The worst case of the old split: an LCD entry with no tags section sits
    # directly before the user's own, so the two were one chunk, and removing
    # the chunk deleted the user's shortcut too.
    steam.write([
        entry("Old Heroic", "flatpak", launch="run x heroic://launch/sideload/z", tags=False),
        entry("My Game", "/home/deck/games/mine", tags=False),
        entry("My Other Game", "/home/deck/games/other"),
    ])
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert r.returncode == 0, r.stderr
    after = parse(steam.read())
    assert [f["AppName"] for _, f in after] == ["My Game", "My Other Game"]
    assert [i for i, _ in after] == [0, 1]

def test_lcd_removal_changes_nothing_but_the_matched_entries(remove_lcd_block, steam):
    # Every byte of the entries that stay must survive; only their index changes.
    steam.write([LCD[0], USER[0], USER[2], entry("Plain", "/p", tags=False)])
    run_block(remove_lcd_block, steam.home, steam.steam_root)
    expected = build([USER[0], USER[2], entry("Plain", "/p", tags=False)])
    assert steam.read() == expected


@pytest.mark.parametrize("label, mangle", [
    ("truncated", lambda d: d[:-30]),
    ("no footer", lambda d: d[:-2]),
    ("trailing junk", lambda d: d + b"junk"),
    ("unknown field type", lambda d: d.replace(b"\x01StartDir", b"\x0cStartDir", 1)),
])
def test_lcd_removal_leaves_a_file_it_cannot_parse_untouched(remove_lcd_block, steam, label, mangle):
    # Better to leave a stray LCD shortcut than to rewrite a file we misread.
    steam.write([LCD[0], USER[0]])
    broken = mangle(steam.read())
    steam.path.write_bytes(broken)
    r = run_block(remove_lcd_block, steam.home, steam.steam_root)
    assert r.returncode == 0, r.stderr
    assert steam.read() == broken, label
    assert not (steam.path.parent / "shortcuts.vdf.deckops_uninstall.bak").exists()
    assert "not in the expected format" in r.stdout