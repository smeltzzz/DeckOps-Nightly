"""log.redact: logs get pasted publicly, so personal data must be masked."""

import pytest

import log


@pytest.fixture(autouse=True)
def _no_leaked_secrets(monkeypatch):
    monkeypatch.setattr(log, "_secrets", set())


@pytest.mark.parametrize("raw, want", [
    # Linux user name in home paths (including the atomic-distro /var/home form)
    ("/home/deck/DeckOps/logs", "~/DeckOps/logs"),
    ("/var/home/ben/.local/share/Steam", "~/.local/share/Steam"),
    # ...and in Wine's view of the same
    (r"Z:\home\deck\games", r"Z:\~\games"),
    # SD card mounts
    ("/run/media/deck/SD-CARD/steamapps", "/run/media/<user>/SD-CARD/steamapps"),
    ("/media/ben/SD", "/media/<user>/SD"),
    # Steam account name and IDs
    ("DepotDownloader -username bensmith -remember-password", "DepotDownloader -username *** -remember-password"),
    ("Logging 'bensmith' into Steam3", "Logging '***' into Steam3"),
    ("Steam/userdata/123456789/config", "Steam/userdata/<uid>/config"),
    ("found uid 123456789 in vdf", "found uid <uid> in vdf"),
    # Deck serial in the configset file name, but not the controller configset
    ("configset_FV12345678.vdf", "configset_<serial>.vdf"),
    ("configset_controller_neptune.vdf", "configset_controller_neptune.vdf"),
])
def test_redact(raw, want):
    assert log.redact(raw) == want


def test_text_without_personal_data_is_unchanged():
    line = "download retry 2 for https://example.com/file.zip (4.5 MB)"
    assert log.redact(line) == line


def test_redact_also_masks_exact_words_only():
    log.redact_also("bensmith")
    assert log.redact("hello bensmith!") == "hello ***!"
    # Whole words only: don't mangle longer identifiers.
    assert log.redact("bensmithsonian") == "bensmithsonian"


def test_redact_also_ignores_short_words():
    log.redact_also("ab")
    log.redact_also("")
    assert log.redact("ab cd") == "ab cd"


def test_get_logger_namespace():
    assert log.get_logger("shortcut").name == "deckops.shortcut"
    assert log.get_logger("pkg.sub.mod").name == "deckops.mod"
