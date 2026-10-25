"""
updater.py - compare this install with GitHub main

The logic behind the in-app "Check for updates" and "Update DeckOps" buttons.
No Qt in here, so it can be tested without a display: the UI (ui_manage.py)
only calls these functions and shows what they return.

A file counts as out of date when its git blob hash differs from the one in
main's tree (or it is missing, or has lost its executable bit), not merely
when VERSION differs. That is what lets a download that was cut off or served
stale be repaired by the next update.
"""

import json
import os
import urllib.request

from identity import GITHUB_USER, GITHUB_REPO
from log import get_logger
from steam_common import PROJECT_ROOT

_log = get_logger(__name__)


# VERSION is per install; the repo still tracks an old copy from 2026-05
PROTECTED = ("deckops.json", "VERSION", "assets/music/background.mp3")
HEADERS = {"User-Agent": "DeckOps"}


def is_protected(path: str) -> bool:
    return path.startswith("logs/") or path in PROTECTED


def file_tree(api: str, sha: str) -> dict:
    """{path: (blob sha, mode, size)} for every file in the commit."""
    req = urllib.request.Request(f"{api}/git/trees/{sha}?recursive=1", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    if d.get("truncated"):
        raise RuntimeError("GitHub's file list came back incomplete")
    return {e["path"]: (e["sha"], e["mode"], e.get("size", 0)) for e in d.get("tree", []) if e.get("type") == "blob"}


def remote_changes(removed: bool = False):
    """
    (local_sha, remote_sha, stale, gone) for this install vs GitHub main.
    stale is {path: (blob sha, mode, size)} for every file on disk that differs
    from main or is missing, so a download that was cut off or served stale
    gets fixed on the next update even when VERSION already matches.
    gone lists files of the installed version that main no longer has
    (looked up only with removed=True). Raises if GitHub can't be reached.
    """
    from steam_common import git_blob_sha
    local_sha = "0"
    try:
        with open(os.path.join(PROJECT_ROOT, "VERSION")) as f:
            local_sha = f.read().strip() or "0"
    except OSError:
        pass
    api = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
    with urllib.request.urlopen(urllib.request.Request(f"{api}/commits/main", headers=HEADERS), timeout=10) as r:
        remote_sha = json.loads(r.read()).get("sha", "")
    if not remote_sha:
        raise RuntimeError("GitHub returned no commit")
    want = file_tree(api, remote_sha)
    stale = {}
    for path, (sha, mode, size) in want.items():
        if is_protected(path):
            continue
        f = os.path.join(PROJECT_ROOT, path)
        try:
            ok = os.path.isfile(f) and git_blob_sha(f) == sha and (mode != "100755" or os.access(f, os.X_OK))
        except OSError:
            ok = False
        if not ok:
            stale[path] = (sha, mode, size)
    gone = []
    if removed and local_sha not in ("0", remote_sha):
        try:
            gone = [p for p in file_tree(api, local_sha) if p not in want and not is_protected(p)]
        except Exception:
            # e.g. the installed commit is gone after a force push; leftovers stay
            _log.warning("could not list files of installed version %s", local_sha, exc_info=True)
    return local_sha, remote_sha, stale, gone


def reset_hint(names) -> bool:
    # Mirrors launcher.sh RESET_HINT: config.py or more than 3 src/ files changed
    return "src/config.py" in names or sum(n.startswith("src/") for n in names) > 3


def check_update_status():
    """Compare this install to GitHub main. Returns 'OK|..', 'UPDATE|..' or 'FAIL|..'."""
    try:
        local_sha, remote_sha, stale, _ = remote_changes()
        if local_sha == remote_sha:
            if not stale:
                return "OK|You're up to date!"
            return f"UPDATE|{len(stale)} DeckOps file(s) need repair."
        hint = "\nReset Config recommended after updating." if reset_hint(stale) else ""
        return f"UPDATE|Update available — {len(stale)} file(s) to download.{hint}"
    except Exception as ex:
        _log.warning("Update check failed: %s", ex)
        return "FAIL|Update check failed."
