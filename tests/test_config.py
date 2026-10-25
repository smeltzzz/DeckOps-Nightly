"""config: deckops.json handling and device-to-asset-folder routing."""

import json
import os

import pytest

import config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """config pointed at an empty temp deckops.json, with a clean cache."""
    path = tmp_path / "deckops.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_mtime", 0.0)
    return path


# -- load / save -------------------------------------------------------------

def test_load_without_file_returns_defaults(cfg):
    assert config.load() == config.DEFAULTS
    assert config.is_first_run()


def test_load_returns_independent_copies(cfg):
    a = config.load()
    a["setup_games"]["waw"] = {"client": "plutonium"}
    assert config.load()["setup_games"] == {}


def test_save_then_load_roundtrip(cfg):
    config.set_gyro_mode("hold")
    config.set_player_name("Soap")
    assert config.get_gyro_mode() == "hold"
    assert config.get_player_name() == "Soap"
    assert json.loads(cfg.read_text())["gyro_mode"] == "hold"


def test_load_fills_in_keys_missing_from_an_older_file(cfg):
    cfg.write_text(json.dumps({"deck_model": "lcd"}))
    c = config.load()
    assert c["deck_model"] == "lcd"
    assert c["music_volume"] == config.DEFAULTS["music_volume"]


def test_corrupt_file_is_moved_aside_not_overwritten(cfg):
    cfg.write_text('{"deck_model": "oled", "setup_ga')  # truncated mid-write
    assert config.load() == config.DEFAULTS
    assert not cfg.exists()
    saved = [p for p in cfg.parent.iterdir() if ".corrupt-" in p.name]
    assert len(saved) == 1
    assert saved[0].read_text().startswith('{"deck_model"')


def test_non_object_root_is_treated_as_corrupt(cfg):
    cfg.write_text("[1, 2, 3]")
    assert config.load() == config.DEFAULTS
    assert any(".corrupt-" in p.name for p in cfg.parent.iterdir())


def test_cache_notices_external_edits(cfg):
    config.set_gyro_mode("on")
    assert config.get_gyro_mode() == "on"
    cfg.write_text(json.dumps({"gyro_mode": "toggle"}))
    stat = cfg.stat()
    os.utime(cfg, (stat.st_atime, stat.st_mtime + 5))  # coarse mtime clocks
    assert config.get_gyro_mode() == "toggle"


# -- game setup bookkeeping --------------------------------------------------

def test_mark_and_unmark_game_setup(cfg):
    assert not config.is_game_setup("waw")
    config.mark_game_setup("waw", "plutonium")
    assert config.is_game_setup("waw")
    assert config.get_setup_games()["waw"]["client"] == "plutonium"
    config.unmark_game_setup(["waw"])
    assert not config.is_game_setup("waw")


# -- player name -------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("Soap", "Soap"),
    ("  two   spaces  ", "two spaces"),
    ('quote"s', "quotes"),             # would end the value in a cfg file
    ("a$b`c;d", "abcd"),               # would run in the bash launchers
    ("back\\slash/and:more", "backslashandmore"),
    ("tab\there\nnewline", "tab here newline"),
    ("ctrl\x00\x07chars", "ctrlchars"),
    ("", ""),
    (None, ""),
    (123, ""),
    ("x" * 100, "x" * config.PLAYER_NAME_MAX),
])
def test_clean_player_name(raw, want):
    assert config.clean_player_name(raw) == want


# -- device -> asset folder --------------------------------------------------

def _device(cfg, **values):
    cfg.write_text(json.dumps(values))


@pytest.mark.parametrize("values, want", [
    ({}, "OLED"),                                           # nothing chosen yet
    ({"deck_model": "oled"}, "OLED"),
    ({"deck_model": "lcd"}, "LCD"),
    ({"deck_model": "other", "other_device": "1920x1080"}, "Other/1920x1080"),
    ({"deck_model": "other"}, "Other/1920x1200"),
    ({"deck_model": "steam_machine", "other_device": "1280x800"}, "Other/1280x800"),
    # Docked: the display picked in setup wins over the handheld configs.
    ({"deck_model": "oled", "play_mode": "docked", "docked_resolution": "1920x1080"}, "Other/1920x1080"),
    ({"deck_model": "lcd", "play_mode": "docked", "docked_resolution": "own"}, "Other/own"),
    # Handheld: a stale docked_resolution is ignored.
    ({"deck_model": "oled", "play_mode": "handheld", "docked_resolution": "1920x1080"}, "OLED"),
    # Older PC setups have no play_mode at all.
    ({"deck_model": "other", "docked_resolution": "1920x1080"}, "Other/1920x1080"),
])
def test_get_model_config_dir(cfg, values, want):
    _device(cfg, **values)
    assert config.get_model_config_dir() == want


def test_get_model_config_dir_falls_back_when_assets_are_missing(cfg):
    _device(cfg, deck_model="other", other_device="9999x9999")
    assert config.get_model_config_dir() == "Other/1920x1200"


def test_every_returned_config_dir_exists_in_the_repo(cfg):
    # The routing above must only ever point at real asset folders.
    for values in ({}, {"deck_model": "lcd"}, {"deck_model": "other", "other_device": "1920x1200_144hz"}):
        _device(cfg, **values)
        d = config.get_model_config_dir()
        assert os.path.isdir(os.path.join(config._CONFIGS_ROOT, *d.split("/"))), d


@pytest.mark.parametrize("model, lcd, oled_path", [
    ("lcd", True, False),
    ("oled", False, True),
    ("other", False, True),
    ("steam_machine", False, True),
    (None, False, True),
])
def test_device_predicates(cfg, model, lcd, oled_path):
    _device(cfg, deck_model=model)
    assert config.is_lcd() is lcd
    assert config.uses_oled_path() is oled_path
