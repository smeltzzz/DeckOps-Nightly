"""
preflight.py - checks worth running before a long install starts

Disk space was only ever checked inside the depot downgrade, after
Steam had already been killed and the install was underway. These run
first, while backing out is still free.

Only sizes the codebase already knows are checked: the depot downgrade
headroom and the IW4x DLC pack. There is
deliberately no estimate for the clients themselves. Guessing a number
and then refusing to install based on it is worse than not checking.
"""

import os
import socket
import threading
import time
from collections import namedtuple

from log import get_logger

_log = get_logger(__name__)

Check = namedtuple("Check", "level title detail")
OK, WARN, BLOCK = "ok", "warn", "block"

# Mirrors _DG_KEY_MAP in ui_install._run_inner. Kept here so preflight
# stays a leaf module; update both if the depot game list changes.
DOWNGRADE_KEYS = {"iw5mp", "iw5mp_ds", "iw5sp"}
DOWNGRADE_GAME = {"iw5mp": "iw5", "iw5mp_ds": "iw5", "iw5sp": "iw5"}


def free_gb(path: str):
    """Free space in GB, or None when it can't be read. Zero would mean "full", and
    a failed statvfs said "Not enough disk space" and blocked the install."""
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except OSError:
        return None


def _probe(path: str):
    """Filesystem id and a usable path, walking up to the nearest one that exists."""
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return os.stat(p).st_dev, p
    except OSError:
        return None, p


def _on_ac() -> bool:
    """True when any mains or USB supply reports online. No sysfs reads as plugged in."""
    base = "/sys/class/power_supply"
    seen = False
    try:
        names = os.listdir(base)
    except OSError:
        return True
    for n in names:
        d = os.path.join(base, n)
        try:
            with open(os.path.join(d, "type")) as f:
                if f.read().strip() not in ("Mains", "USB"):
                    continue
            with open(os.path.join(d, "online")) as f:
                seen = True
                if f.read().strip() == "1":
                    return True
        except OSError:
            continue
    return not seen


# The check runs when a button is clicked, on the UI thread. create_connection's
# timeout does not cover the DNS lookup, so an offline click could freeze the window
# for far longer than 5 s. The probe runs in a worker instead; the caller waits a
# short while at most (so the worst case is a couple of seconds, not a frozen window),
# and a recent result is reused.
_ONLINE_WAIT = 2.0          # longest a check waits for the probe
_ONLINE_FRESH = 60.0        # how long a probe result is reused
_online_lock = threading.Lock()
_online_result = None       # (time.monotonic(), bool) of the last finished probe
_online_thread = None


def _probe_online():
    global _online_result
    try:
        socket.create_connection(("api.github.com", 443), timeout=5).close()
        ok = True
    except OSError:
        ok = False
    with _online_lock:
        _online_result = (time.monotonic(), ok)


def start_online_probe():
    """Begin the connectivity probe in the background if none is running or fresh."""
    global _online_thread
    with _online_lock:
        if _online_thread is not None and _online_thread.is_alive():
            return _online_thread
        if _online_result and time.monotonic() - _online_result[0] < _ONLINE_FRESH:
            return None
        _online_thread = threading.Thread(target=_probe_online, daemon=True)
        _online_thread.start()
        return _online_thread


def _online() -> bool:
    with _online_lock:
        cached = _online_result
    if cached and time.monotonic() - cached[0] < _ONLINE_FRESH:
        return cached[1]
    t = start_online_probe()
    if t is not None:
        t.join(_ONLINE_WAIT)
    with _online_lock:
        cached = _online_result
    # Still unanswered after the wait (a hung lookup): treat as offline, which only warns
    return bool(cached and time.monotonic() - cached[0] < _ONLINE_FRESH and cached[1])


def check(selected, iw4x_dlc=False, downgrade_keys=()):
    """
    Run the pre-install checks.

    selected       -- list of (key, client, install_dir)
    downgrade_keys -- selected keys that will need a depot downgrade
    Returns a list of Check(level, title, detail); empty means nothing to say.
    """
    from depot_downgrade import estimate_space_gb
    from steam_common import default_steam_root
    # Resolved here rather than taken from the classic-path constant: on a
    # Flatpak or moved install the old value measured space for a Steam that
    # is not where the depots will actually be written.
    steam_root = default_steam_root()
    from iw4x import DLC_SIZE_GB

    need = {}   # device -> [gb wanted, representative path]

    def add(path, gb):
        if not path:
            return
        dev, probe = _probe(path)
        entry = need.setdefault(dev, [0.0, probe])
        entry[0] += gb

    dg = {k for k in downgrade_keys if k in DOWNGRADE_KEYS}
    dg_games = set()
    for key, client, install_dir in selected:
        # The downgrade stages a second copy beside the game it belongs to,
        # sized from that game's depots (far more than the
        # old flat 15 GB), once per game and added up per drive.
        if key in dg and install_dir:
            game_id = DOWNGRADE_GAME.get(key)
            if game_id not in dg_games:
                dg_games.add(game_id)
                add(install_dir, estimate_space_gb(game_id, steam_root))
        if client == "iw4x" and iw4x_dlc:
            add(install_dir, DLC_SIZE_GB)

    out = []
    for want, path in sorted(need.values(), key=lambda e: e[1]):
        have = free_gb(path)
        if have is None:
            out.append(Check(WARN, "Could not check disk space",
                             f"{path}: free space unreadable, {want:.0f} GB needed"))
            continue
        detail = f"{path}: {have:.1f} GB free, {want:.0f} GB needed"
        if have < want:
            out.append(Check(BLOCK, "Not enough disk space", detail))
        else:
            out.append(Check(OK, "Disk space", detail))

    if dg:
        out.append(Check(WARN, "This will take a long time",
                         "Downgrading game files can run for hours. Keep the "
                         "device plugged in and somewhere it can stay cool."))

    if not _on_ac():
        out.append(Check(WARN, "On battery",
                         "Plug the device in. Installs can run for hours."))
    if not _online():
        out.append(Check(WARN, "No internet connection",
                         "Downloads will fail. Connect to Wi-Fi first."))
    return out


def worst(checks):
    for level in (BLOCK, WARN):
        if any(c.level == level for c in checks):
            return level
    return OK
