"""
plutonium_update.py - keep Plutonium current from its own manifest, silently

plutonium.exe (the updater, which is also the login) only runs at install;
DeckOps launches games straight through bin/, so without this Plutonium
never updates. Format, traced from updater 1.0.221 on 2026-10-06:
  prod.json  -> manifests: [info.json URL], yeet: [paths/globs]
  info.json  -> revision, baseUrl, files: [{name, size, hash (sha1)}]
  each file is fetched from baseUrl + hash (by name is a 404)
  yeet: files under those paths that are not in the manifest get deleted
  the updater leaves <Plutonium dir>/info.json {revision, launchTarget}

OLED keeps one real copy, the master in the dedicated prefix. Game prefixes
link to it, bin/ launcher/ games/ as directory symlinks and Plutonium's own
storage/ files of every game as file symlinks, so an update reaches every
prefix at once and nothing is stored twice. Every game's files, not just the
prefix's own: since r5369 MW3 online fails (error 260, no profile) when the
other games' Plutonium files are missing (Deck + Bazzite, 2026-10-19). User data in storage/ (players/,
mods/, usermaps/) is never in the manifest and stays real per prefix.
"""

import fnmatch
import json
import os
import tempfile
import time

from log import get_logger
from net import download
from steam_common import sha1_file as _sha1

_log = get_logger(__name__)

PROD_URL = "https://cdn.plutonium.pw/updater/prod.json"
SHARED_DIRS = ("bin", "launcher", "games")


def fetch(timeout: int = 30, tries: int = 3):
    """(prod, info) manifests from Plutonium's CDN."""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "prod.json")
        download(PROD_URL, p, label="Plutonium prod.json", timeout=timeout, tries=tries)
        with open(p) as f:
            prod = json.load(f)
        i = os.path.join(tmp, "info.json")
        download(prod["manifests"][0], i, label="Plutonium info.json", timeout=timeout, tries=tries)
        with open(i) as f:
            info = json.load(f)
    for e in info["files"]:
        n = e["name"]
        if os.path.isabs(n) or ".." in n.split("/"):
            raise RuntimeError(f"Unsafe path in Plutonium manifest: {n}")
    return prod, info


def local_revision(plut_dir: str):
    try:
        with open(os.path.join(plut_dir, "info.json")) as f:
            return json.load(f).get("revision")
    except (OSError, ValueError):
        return None


def _yeet(plut_dir: str, patterns: list, keep: set, log) -> int:
    # One directory level per pattern, the way the updater was seen
    # to behave; if Plutonium starts yeeting nested files, recurse here.
    n = 0
    for pat in patterns:
        d = os.path.dirname(pat)
        full = os.path.join(plut_dir, d)
        if not os.path.isdir(full):
            continue
        for name in os.listdir(full):
            rel = f"{d}/{name}" if d else name
            p = os.path.join(full, name)
            if fnmatch.fnmatch(rel, pat) and rel not in keep and os.path.isfile(p) and not os.path.islink(p):
                os.remove(p)
                n += 1
    if n:
        log(f"Plutonium: removed {n} file(s) Plutonium no longer ships")
    return n


class OutOfTime(Exception):
    """The update's time budget ran out (see update())."""


def sync_master(plut_dir: str, prod: dict, info: dict, log=None, deadline: float = None) -> int:
    """
    Bring the master Plutonium folder in line with the manifest and return
    how many files were replaced. Changed files download to <file>.new and
    are only renamed into place once every download succeeded, so an
    interrupted update leaves the previous version runnable.

    deadline -- time.monotonic() value; past it the downloads stop (even mid
    file, checked after each 1 MB read, so it can run over by one read) and
    OutOfTime is raised with nothing renamed into place. Finished
    .new files and the .part of the stopped one are kept, so the next run
    picks up where this one stopped.
    """
    log = log or (lambda *a: None)
    files = info["files"]
    todo = [e for e in files if not (
        os.path.isfile(p := os.path.join(plut_dir, e["name"])) and _sha1(p) == e["hash"])]
    log(f"Plutonium r{info['revision']}: {len(files) - len(todo)} of {len(files)} files up to date")

    def _check_time(*_):
        if deadline is not None and time.monotonic() > deadline:
            raise OutOfTime()

    for e in todo:
        p = os.path.join(plut_dir, e["name"])
        new = p + ".new"
        # Left by an earlier interrupted update and already complete.
        if os.path.isfile(new) and _sha1(new) == e["hash"]:
            continue
        _check_time()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        download(info["baseUrl"] + e["hash"], new, label=os.path.basename(p),
                 timeout=120 if deadline is None else 30,
                 tries=3 if deadline is None else 1,
                 digest=f"sha1:{e['hash']}", on_progress=_check_time)
    _check_time()
    for e in todo:
        p = os.path.join(plut_dir, e["name"])
        os.replace(p + ".new", p)
    _yeet(plut_dir, prod.get("yeet", []), {e["name"] for e in files}, log)
    with open(os.path.join(plut_dir, "info.json"), "w") as f:
        json.dump({"revision": info["revision"],
                   "launchTarget": prod.get("launchTarget", "bin/plutonium-launcher-win32.exe")},
                  f, separators=(",", ":"))
    if todo:
        log(f"Plutonium: updated {len(todo)} file(s) to r{info['revision']}")
    return len(todo)


# Budget for the pre-launch update: it runs between Play and the game starting.
# It had no total limit -- after a big Plutonium release on slow Wi-Fi, Play
# showed nothing for minutes (LCD: no outer limit at all; OLED: prelaunch.sh
# kills it at 120 s, so every launch waited 2 minutes and finished nothing).
LAUNCH_BUDGET = 45


def update(plut_dir: str, log=None, budget: float = None) -> dict:
    """Fetch once, sync plut_dir if its revision differs, return info.
    budget -- seconds; when the update can't finish in time it stops, the
    game launches on the current revision and the rest is fetched (resumed)
    on the next launch."""
    log = log or (lambda *a: None)
    deadline = time.monotonic() + budget if budget else None
    # One quick try so being offline costs a few seconds at most.
    prod, info = fetch(timeout=5, tries=1)
    local = local_revision(plut_dir)
    if local != info["revision"]:
        log(f"Plutonium: r{local}, server r{info['revision']}, updating")
        try:
            sync_master(plut_dir, prod, info, log, deadline=deadline)
        except OutOfTime:
            log(f"Plutonium: update didn't finish in {budget:.0f}s, launching r{local}; "
                "the rest downloads on the next launch")
    else:
        log(f"Plutonium r{local}: up to date")
    return info


def link_prefix(master: str, prefix_plut: str, info: dict, log=None) -> int:
    """
    Point a game prefix's Plutonium folder at the master: the shared dir
    symlinks, and every manifest file under storage/. Real copies
    are swapped for links only when the master's file is complete, user
    files are never touched, and real bin/ launcher/ games/ folders (full
    copy installs) are left alone. Returns how many links were (re)made.
    """
    log = log or (lambda *a: None)
    m = os.path.realpath(master)
    n = 0
    for d in SHARED_DIRS:
        src, dst = os.path.join(m, d), os.path.join(prefix_plut, d)
        if os.path.isdir(src) and os.path.islink(dst) and os.path.realpath(dst) != src:
            os.unlink(dst)
            os.symlink(src, dst)
            n += 1
    for e in info["files"]:
        if not e["name"].startswith("storage/"):
            continue
        tgt, dst = os.path.join(m, e["name"]), os.path.join(prefix_plut, e["name"])
        if not os.path.isfile(tgt) or os.path.getsize(tgt) != e["size"]:
            continue  # master incomplete, keep whatever the prefix has
        if os.path.islink(dst) and os.readlink(dst) == tgt:
            continue
        if os.path.isdir(dst) and not os.path.islink(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".deckops_link"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(tgt, tmp)
        os.replace(tmp, dst)
        n += 1
    # Links to files Plutonium dropped from the manifest.
    root = os.path.join(prefix_plut, "storage")
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            if os.path.islink(p) and os.readlink(p).startswith(m + os.sep) and not os.path.exists(p):
                os.remove(p)
    if n:
        log(f"Plutonium: linked {n} item(s) in {prefix_plut} to the master copy")
    return n
