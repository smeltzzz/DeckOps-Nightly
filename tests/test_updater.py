"""updater: deciding which files of this install differ from GitHub main."""

import hashlib
import json
import os
import sys
import urllib.error

import pytest

import identity
import updater
from fakes import FakeUrlopen

API = f"https://api.github.com/repos/{identity.GITHUB_USER}/{identity.GITHUB_REPO}"
REMOTE = "a" * 40
OLD = "b" * 40


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_json(files: dict, truncated: bool = False) -> bytes:
    """files: path -> (bytes, mode)"""
    entries = [{"path": p, "mode": mode, "type": "blob", "sha": blob_sha(data), "size": len(data)}
               for p, (data, mode) in files.items()]
    entries.append({"path": "src", "mode": "040000", "type": "tree", "sha": "0" * 40})   # folders are ignored
    return json.dumps({"tree": entries, "truncated": truncated}).encode()


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install dir plus a fake GitHub. Returns (root, github, files_on_main)."""
    root = tmp_path / "DeckOps"
    root.mkdir()
    gh = FakeUrlopen()
    monkeypatch.setattr(updater.urllib.request, "urlopen", gh)
    monkeypatch.setattr(updater, "PROJECT_ROOT", str(root))
    files = {
        "src/main.py": (b"print('main')\n", "100644"),
        "src/config.py": (b"# config\n", "100644"),
        "launcher.sh": (b"#!/bin/bash\n", "100755"),
        "deckops.json": (b"{}", "100644"),
        "VERSION": (b"old", "100644"),
    }

    def publish(files_on_main=files, commit=REMOTE, truncated=False):
        gh.routes[f"{API}/commits/main"] = json.dumps({"sha": commit}).encode()
        gh.routes[f"{API}/git/trees/{commit}?recursive=1"] = tree_json(files_on_main, truncated)

    def write_all(files_on_disk=files):
        for p, (data, mode) in files_on_disk.items():
            f = root / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            if mode == "100755":
                f.chmod(0o755)

    gh.publish, gh.write_all = publish, write_all
    publish()
    return root, gh, files


# -- is_protected / reset_hint -------------------------------------------------

@pytest.mark.parametrize("path, protected", [
    ("deckops.json", True),
    ("VERSION", True),
    ("assets/music/background.mp3", True),
    ("logs/install.log", True),
    ("logs/sub/x.log", True),
    ("src/main.py", False),
    ("assets/music/other.mp3", False),
    ("mylogs/x", False),
])
def test_is_protected(path, protected):
    assert updater.is_protected(path) is protected


@pytest.mark.parametrize("names, hint", [
    ([], False),
    (["README.md"], False),
    (["src/a.py", "src/b.py", "src/c.py"], False),             # three src files: not enough
    (["src/a.py", "src/b.py", "src/c.py", "src/d.py"], True),  # more than three
    (["src/config.py"], True),                                 # config changes always warn
    (["assets/configs/x.cfg"] * 10, False),
])
def test_reset_hint(names, hint):
    assert updater.reset_hint(names) is hint


# -- remote_changes ----------------------------------------------------------

def test_up_to_date_install_has_nothing_stale(install):
    root, gh, files = install
    gh.write_all()
    (root / "VERSION").write_text(REMOTE)
    local, remote, stale, gone = updater.remote_changes()
    assert (local, remote, stale, gone) == (REMOTE, REMOTE, {}, [])


def test_modified_and_missing_files_are_stale(install):
    root, gh, files = install
    gh.write_all()
    (root / "src/main.py").write_bytes(b"print('cut off')")      # same path, different bytes
    (root / "src/config.py").unlink()                             # missing
    _, _, stale, _ = updater.remote_changes()
    assert set(stale) == {"src/main.py", "src/config.py"}
    data, mode = files["src/main.py"]
    assert stale["src/main.py"] == (blob_sha(data), mode, len(data))   # what to download


def test_a_stale_file_is_found_even_when_version_matches(install):
    # The point of hashing files: VERSION says current, but a download was cut off.
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(REMOTE)
    (root / "launcher.sh").write_bytes(b"#!/bin/bash\necho cut")
    local, remote, stale, _ = updater.remote_changes()
    assert local == remote and set(stale) == {"launcher.sh"}


@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_a_script_that_lost_its_executable_bit_is_stale(install):
    root, gh, _ = install
    gh.write_all()
    (root / "launcher.sh").chmod(0o644)
    _, _, stale, _ = updater.remote_changes()
    assert set(stale) == {"launcher.sh"}


def test_protected_files_are_never_stale(install):
    root, gh, files = install
    gh.write_all()
    (root / "deckops.json").write_text('{"my": "settings"}')      # differs from main, but is the user's
    (root / "VERSION").write_text("something else")
    _, _, stale, _ = updater.remote_changes()
    assert stale == {}


def test_a_missing_protected_file_is_not_stale_either(install):
    root, gh, _ = install
    gh.write_all()
    (root / "deckops.json").unlink()
    _, _, stale, _ = updater.remote_changes()
    assert stale == {}


def test_folders_in_the_tree_are_ignored(install):
    root, gh, _ = install
    gh.write_all()
    _, _, stale, _ = updater.remote_changes()
    assert "src" not in stale


def test_without_a_version_file_local_sha_is_zero(install):
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").unlink()
    local, remote, _, gone = updater.remote_changes(removed=True)
    assert (local, remote) == ("0", REMOTE)
    assert gone == []        # nothing to compare against, and no lookup of "0"


def test_gone_lists_files_main_no_longer_has(install):
    root, gh, files = install
    gh.write_all()
    (root / "VERSION").write_text(OLD)
    old_files = dict(files)
    old_files["src/removed.py"] = (b"old", "100644")
    old_files["logs/old.log"] = (b"log", "100644")                # protected: must not be listed
    gh.routes[f"{API}/git/trees/{OLD}?recursive=1"] = tree_json(old_files)
    _, _, _, gone = updater.remote_changes(removed=True)
    assert gone == ["src/removed.py"]


def test_gone_is_only_looked_up_when_asked_for(install):
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(OLD)         # no route for OLD's tree: asking for it would KeyError
    _, _, _, gone = updater.remote_changes()
    assert gone == []


def test_gone_survives_the_installed_commit_disappearing(install):
    # e.g. after a force push: leftovers stay, but the update itself goes ahead
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(OLD)
    gh.routes[f"{API}/git/trees/{OLD}?recursive=1"] = urllib.error.HTTPError(
        "u", 404, "Not Found", {}, None)
    _, _, stale, gone = updater.remote_changes(removed=True)
    assert gone == []


def test_github_unreachable_raises(install):
    _, gh, _ = install
    gh.routes[f"{API}/commits/main"] = urllib.error.URLError("offline")
    with pytest.raises(urllib.error.URLError):
        updater.remote_changes()


def test_commit_without_a_sha_raises(install):
    _, gh, _ = install
    gh.routes[f"{API}/commits/main"] = b"{}"
    with pytest.raises(RuntimeError, match="no commit"):
        updater.remote_changes()


def test_truncated_file_list_raises_instead_of_deleting_things(install):
    root, gh, files = install
    gh.write_all()
    gh.publish(truncated=True)
    with pytest.raises(RuntimeError, match="incomplete"):
        updater.remote_changes()


def test_requests_identify_as_deckops(install):
    root, gh, _ = install
    gh.write_all()
    seen = []
    real = updater.urllib.request.urlopen

    def spy(req, timeout=None):
        seen.append(req.get_header("User-agent"))
        return real(req, timeout=timeout)

    updater.urllib.request.urlopen = spy
    try:
        updater.remote_changes()
    finally:
        updater.urllib.request.urlopen = real
    assert seen and set(seen) == {"DeckOps"}


# -- check_update_status -----------------------------------------------------

def test_status_ok(install):
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(REMOTE)
    assert updater.check_update_status() == "OK|You're up to date!"


def test_status_needs_repair_when_version_matches_but_files_differ(install):
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(REMOTE)
    (root / "src/main.py").write_bytes(b"cut off")
    assert updater.check_update_status() == "UPDATE|1 DeckOps file(s) need repair."


def test_status_update_available(install):
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(OLD)
    (root / "src/main.py").write_bytes(b"older")
    status = updater.check_update_status()
    assert status.startswith("UPDATE|Update available") and "1 file(s) to download" in status
    assert "Reset Config" not in status


def test_status_recommends_reset_config_when_config_py_changed(install):
    root, gh, _ = install
    gh.write_all()
    (root / "VERSION").write_text(OLD)
    (root / "src/config.py").write_bytes(b"# older config\n")
    assert "Reset Config recommended" in updater.check_update_status()


def test_status_fail_when_github_is_unreachable(install):
    _, gh, _ = install
    gh.routes[f"{API}/commits/main"] = urllib.error.URLError("offline")
    assert updater.check_update_status() == "FAIL|Update check failed."


def test_ui_manage_uses_the_updater_module():
    # The check and the apply step must agree on what "stale" means, so the
    # UI calls these rather than keeping its own copy.
    src = open(os.path.join(os.path.dirname(updater.__file__), "ui_manage.py"), encoding="utf-8").read()
    assert "from updater import" in src
    for old in ("def _remote_changes", "def _check_update_status", "def _tree(", "_PROTECTED ="):
        assert old not in src, old
