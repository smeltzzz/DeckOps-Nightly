"""steam_common: the helpers whose output other systems (Steam, Heroic, GitHub) depend on."""

import json
import os

import pytest

import steam_common as sc


# -- shortcut appid ----------------------------------------------------------

def test_shortcut_appid_golden():
    # Must match Steam's own algorithm. If this value changes, every existing
    # shortcut's artwork and controller config points at the wrong appid.
    assert sc.calc_shortcut_appid('"/home/deck/DeckOps/launcher.sh"', "DeckOps: Test") == 2205139136


def test_shortcut_appid_always_has_high_bit_and_fits_32_bits():
    for name in ("", "a", "DeckOps: Plutonium Offline", "é☃"):
        appid = sc.calc_shortcut_appid("/x/y", name)
        assert appid & 0x80000000
        assert 0 <= appid <= 0xFFFFFFFF


def test_shortcut_appid_depends_on_exe_and_name():
    base = sc.calc_shortcut_appid("/a", "n")
    assert base == sc.calc_shortcut_appid("/a", "n")
    assert base != sc.calc_shortcut_appid("/b", "n")
    assert base != sc.calc_shortcut_appid("/a", "m")


# -- hashing -----------------------------------------------------------------

def test_git_blob_sha_matches_git(tmp_path):
    # Values from `git hash-object`.
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello\n")
    assert sc.git_blob_sha(str(f)) == "ce013625030ba8dba906f756967f9e9ca394464a"
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert sc.git_blob_sha(str(empty)) == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_git_blob_sha_handles_files_larger_than_one_chunk(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * ((1 << 20) * 2 + 5))
    # Same result as hashing the whole thing in one go.
    import hashlib
    data = f.read_bytes()
    want = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    assert sc.git_blob_sha(str(f)) == want


def test_sha1_file(tmp_path):
    f = tmp_path / "a"
    f.write_bytes(b"abc")
    assert sc.sha1_file(str(f)) == "a9993e364706816aba3e25717850c26c9cd0d89d"


def test_heroic_app_name_is_stable():
    # Existing LCD installs and their Steam shortcuts are keyed by these.
    assert sc.heroic_app_name("waw") == "do_Epw-0vg389gO_mZebOb"
    assert sc.heroic_app_name("bo1") == "do_BSOSQJei2CRyAToKcC6"


def test_heroic_app_name_shape():
    name = sc.heroic_app_name("anything")
    assert name.startswith("do_") and len(name) == 22


# -- newest_proton_dir -------------------------------------------------------

def _proton(parent, name, with_script=True):
    d = parent / name
    d.mkdir()
    if with_script:
        (d / "proton").write_text("")


def test_newest_proton_dir_sorts_numerically(tmp_path):
    for n in ("GE-Proton9-5", "GE-Proton9-28", "GE-Proton10-1"):
        _proton(tmp_path, n)
    assert sc.newest_proton_dir(str(tmp_path), "GE-Proton") == "GE-Proton10-1"
    (tmp_path / "GE-Proton10-1").rename(tmp_path / "x")
    assert sc.newest_proton_dir(str(tmp_path), "GE-Proton") == "GE-Proton9-28"


def test_newest_proton_dir_ignores_dirs_without_proton_script(tmp_path):
    _proton(tmp_path, "GE-Proton9-1")
    _proton(tmp_path, "GE-Proton9-9", with_script=False)
    assert sc.newest_proton_dir(str(tmp_path), "GE-Proton") == "GE-Proton9-1"


def test_newest_proton_dir_missing_parent_or_no_match(tmp_path):
    assert sc.newest_proton_dir(str(tmp_path / "nope"), "GE-Proton") is None
    _proton(tmp_path, "Proton 9.0")
    assert sc.newest_proton_dir(str(tmp_path), "GE-Proton") is None


# -- Steam accounts ----------------------------------------------------------

def test_find_all_steam_uids(tmp_path, monkeypatch):
    for name in ("12345678", "9999", "notanumber", "87654321"):
        (tmp_path / name).mkdir()
    (tmp_path / "55555555").write_text("a file, still counted by name")
    monkeypatch.setattr(sc, "USERDATA_DIR", str(tmp_path))
    # 9999 is below MIN_UID and "notanumber" isn't an id.
    assert sorted(sc.find_all_steam_uids()) == ["12345678", "55555555", "87654321"]


def test_find_all_steam_uids_without_userdata(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "USERDATA_DIR", str(tmp_path / "missing"))
    assert sc.find_all_steam_uids() == []


# -- file helpers ------------------------------------------------------------

def test_write_json_roundtrip_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "out.json"
    sc.write_json(str(p), {"a": [1, 2], "b": "é"})
    assert json.loads(p.read_text()) == {"a": [1, 2], "b": "é"}
    assert [f.name for f in tmp_path.iterdir()] == ["out.json"]


def test_write_json_failure_keeps_old_file(tmp_path):
    p = tmp_path / "out.json"
    sc.write_json(str(p), {"ok": True})
    with pytest.raises(TypeError):
        sc.write_json(str(p), {"bad": object()})
    assert json.loads(p.read_text()) == {"ok": True}
    assert [f.name for f in tmp_path.iterdir()] == ["out.json"]


def test_backup_file(tmp_path):
    p = tmp_path / "config.vdf"
    p.write_text("original")
    sc.backup_file(str(p))
    assert (tmp_path / "config.vdf.bak").read_text() == "original"
    # A missing file is not an error.
    sc.backup_file(str(tmp_path / "missing"))
    assert not os.path.exists(tmp_path / "missing.bak")
