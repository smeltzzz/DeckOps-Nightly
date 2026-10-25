"""plutonium_common, and the parts of plutonium.py / plutonium_lcd.py that must agree."""

import json
import os

import pytest

import plutonium
import plutonium_common as common
import plutonium_lcd


# -- wine_path ---------------------------------------------------------------

def test_wine_path():
    assert common.wine_path("/home/deck/Games/x") == r"Z:\home\deck\Games\x"
    assert common.wine_path("/") == "Z:\\"


# -- plut_storage_ready ------------------------------------------------------

def test_storage_ready_needs_storage_with_a_game_folder(tmp_path):
    assert not common.plut_storage_ready(str(tmp_path))              # no storage/ at all
    (tmp_path / "storage").mkdir()
    assert not common.plut_storage_ready(str(tmp_path))              # empty
    (tmp_path / "storage" / "stray.txt").write_text("x")
    assert not common.plut_storage_ready(str(tmp_path))              # files don't count
    (tmp_path / "storage" / "t6").mkdir()
    assert common.plut_storage_ready(str(tmp_path))


def test_storage_ready_survives_an_unreadable_folder(tmp_path, monkeypatch):
    (tmp_path / "storage" / "t6").mkdir(parents=True)

    def boom(path):
        raise PermissionError(path)

    monkeypatch.setattr(common.os, "listdir", boom)
    assert common.plut_storage_ready(str(tmp_path)) is False


def test_both_installers_use_the_shared_check(tmp_path, monkeypatch):
    dedicated, shared = tmp_path / "dedicated", tmp_path / "shared"
    for d in (dedicated, shared):
        (d / "storage" / "t5").mkdir(parents=True)
    monkeypatch.setattr(plutonium, "get_dedicated_plut_dir", lambda: str(dedicated))
    monkeypatch.setattr(plutonium_lcd, "get_shared_plut_dir", lambda: str(shared))
    assert plutonium.is_plutonium_ready() and plutonium_lcd.is_plutonium_ready_lcd()
    (shared / "storage" / "t5").rmdir()
    assert plutonium.is_plutonium_ready() and not plutonium_lcd.is_plutonium_ready_lcd()


# -- install_menu_mod --------------------------------------------------------

@pytest.fixture
def mod_assets(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "t6").mkdir(parents=True)
    (assets / "t6" / "deckops_bo2_menu.iwd").write_bytes(b"PK menu mod")
    monkeypatch.setattr(common, "MENU_MOD_ASSETS_DIR", str(assets))
    return assets


def test_install_menu_mod_copies_into_storage_raw(mod_assets, tmp_path):
    plut = tmp_path / "Plutonium"
    msgs = []
    common.install_menu_mod(str(plut), "t6mp", msgs.append)
    assert (plut / "storage/t6/raw/deckops_bo2_menu.iwd").read_bytes() == b"PK menu mod"
    assert "Installing DeckOps menu mod for t6mp" in msgs[0]
    assert "Menu mod installed (11 bytes)" in msgs[-1]


def test_install_menu_mod_is_idempotent_for_mp_and_zm(mod_assets, tmp_path):
    plut = tmp_path / "Plutonium"
    common.install_menu_mod(str(plut), "t6mp")
    common.install_menu_mod(str(plut), "t6zm")      # same file, same destination
    assert (plut / "storage/t6/raw/deckops_bo2_menu.iwd").read_bytes() == b"PK menu mod"


def test_install_menu_mod_ignores_games_without_one(mod_assets, tmp_path):
    msgs = []
    common.install_menu_mod(str(tmp_path / "p"), "t4mp", msgs.append)
    assert msgs == [] and not (tmp_path / "p").exists()


def test_install_menu_mod_missing_asset_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "MENU_MOD_ASSETS_DIR", str(tmp_path / "nowhere"))
    msgs = []
    common.install_menu_mod(str(tmp_path / "p"), "t6mp", msgs.append)
    assert any("Menu mod asset missing" in m for m in msgs)
    assert not (tmp_path / "p").exists()


def test_install_menu_mod_copy_failure_is_not_fatal(mod_assets, tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(common.shutil, "copy2", boom)
    msgs = []
    common.install_menu_mod(str(tmp_path / "p"), "t6mp", msgs.append)
    assert any("Menu mod copy failed: disk full" in m for m in msgs)


def test_the_bundled_menu_mods_exist_in_the_repo():
    for key, (asset_rel, _dest) in common.MENU_MOD_FILES.items():
        assert os.path.isfile(os.path.join(common.MENU_MOD_ASSETS_DIR, asset_rel)), key


# -- write_metadata ----------------------------------------------------------

def test_write_metadata_roundtrip(tmp_path):
    common.write_metadata(str(tmp_path), {"client": "plutonium", "n": 1})
    assert json.loads((tmp_path / "deckops_plutonium.json").read_text()) == {"client": "plutonium", "n": 1}


def test_write_metadata_raises_when_the_folder_is_missing(tmp_path):
    with pytest.raises(OSError):
        common.write_metadata(str(tmp_path / "gone"), {})


def test_lcd_metadata_is_best_effort(tmp_path):
    plutonium_lcd._write_metadata_lcd("", {"a": 1})                      # nothing to write into
    plutonium_lcd._write_metadata_lcd(str(tmp_path / "gone"), {"a": 1})  # logged, not raised
    plutonium_lcd._write_metadata_lcd(str(tmp_path), {"a": 1})
    assert json.loads((tmp_path / common.METADATA_FILE).read_text()) == {"a": 1}


# -- the installers share these, they don't each keep a copy -------------------

def test_installers_use_the_shared_implementations():
    assert plutonium._wine_path is common.wine_path
    assert plutonium_lcd._wine_path_lcd is common.wine_path
    assert plutonium._install_menu_mod is common.install_menu_mod
    assert plutonium_lcd._install_menu_mod_lcd is common.install_menu_mod
    assert plutonium._write_metadata is common.write_metadata
    assert plutonium.MENU_MOD_FILES is plutonium_lcd.MENU_MOD_FILES is common.MENU_MOD_FILES
    assert plutonium.METADATA_FILE == plutonium_lcd.METADATA_FILE == common.METADATA_FILE


def test_plutonium_common_stays_a_leaf_module():
    # It exists so the two installers (which import each other) can share
    # code without a cycle; importing either of them would defeat that.
    import ast
    tree = ast.parse(open(common.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"json", "os", "shutil", "log", "steam_common"}, imported


# -- tables the two installers each keep, which must not drift apart ----------

def test_lcd_game_tables_match_plutonium_game_meta():
    meta = plutonium.GAME_META                     # key -> (appid, config path key, exe)
    assert set(plutonium_lcd.PLUT_CONFIG_KEYS) == set(meta)
    assert set(plutonium_lcd.PLUT_GAME_EXES) == set(meta)
    assert set(plutonium_lcd.HEROIC_PLUT_GAMES) == set(meta)
    for key, (appid, path_key, exe) in meta.items():
        assert plutonium_lcd.PLUT_CONFIG_KEYS[key] == path_key, key
        assert plutonium_lcd.PLUT_GAME_EXES[key] == (appid, exe), key
