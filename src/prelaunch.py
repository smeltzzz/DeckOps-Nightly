"""
prelaunch.py - silent mod client update run by prelaunch.sh before a game

    prelaunch.py <game key> <original launch command...>

Never blocks the launch on failure: prelaunch.sh starts the game no matter
how this exits. Output goes to install.log only, never a window.
"""

import json
import os
import sys
import time

from log import setup_logging, get_logger

_log = get_logger("prelaunch")


def _cod4mp(key, cmd):
    exe = next((a for a in cmd if a.lower().endswith("iw3mp.exe")), None)
    if not exe:
        return  # SP launch on the shared 7940 appid
    install_dir = os.path.dirname(exe)
    meta = os.path.join(install_dir, "deckops_cod4r.json")
    if not os.path.isfile(meta):
        return  # CoD4x or vanilla MP
    with open(meta) as f:
        compat = json.load(f)["compatdata_path"]
    from cod4r import sync_files
    # One quick try at the manifest so being offline costs a few seconds.
    sync_files(install_dir, compat, log=lambda m: _log.info(m.strip()), fetch_timeout=5, tries=1)


def _plutonium(key, cmd):
    # OLED only: the online wrapper and own shortcuts export the game's
    # prefix. Offline/LAN launches don't come through here.
    from plutonium import DEDICATED_PREFIX, _plut_dir_in_prefix
    import plutonium_update as pu
    master = _plut_dir_in_prefix(DEDICATED_PREFIX)
    compat = os.environ.get("STEAM_COMPAT_DATA_PATH", "")
    if not os.path.isdir(master) or not compat:
        return
    log = lambda m: _log.info(m)
    # Inside prelaunch.sh's 120 s limit, with room left for linking the prefix
    info = pu.update(master, log, budget=pu.LAUNCH_BUDGET)
    prefix = _plut_dir_in_prefix(compat)
    if os.path.isdir(prefix) and os.path.realpath(prefix) != os.path.realpath(master):
        pu.link_prefix(master, prefix, info, log)


def _clean_campaign_config(key, install_dir):
    """MW2 / MW3 campaign: take the lines out of config.cfg that older DeckOps
    versions added and that no longer do anything (thereisacow, the F10 FOV key,
    MW3's cg_fovScale). FOV is held by fovwatch.py and FPS by the DXVK launch
    option, so nothing is written to the config any more. Runs before the IW-Pad
    update, so a network failure can't skip it."""
    from game_config import _patch_dvars, _dest_from_install, _CAMPAIGN_CLEANUP
    spec = _CAMPAIGN_CLEANUP.get(key)
    if not spec:
        return
    path = os.path.join(_dest_from_install(key, install_dir), "config.cfg")
    if _patch_dvars(path, spec["dvars"], drop=spec["drop"]):
        _log.info("prelaunch %s: old FOV lines removed from config.cfg", key)


def _iwpad(key, cmd):
    exe = f"{key}.exe"
    path = next((a for a in cmd if a.lower().endswith(exe)), None)
    if path:
        try:
            _clean_campaign_config(key, os.path.dirname(path))
        except Exception as ex:
            _log.warning("prelaunch %s: config lines skipped: %s", key, ex)
        from sp_mod import update_iw_pad
        update_iw_pad(key, os.path.dirname(path), log=_log.info)


def _lan(key, args):
    # Offline launch (lan_server.sh): relink the offline copies, then hand it the
    # Steam Linux Runtime and the library mounts on stdout
    from plutonium import refresh_lan, lan_runtime
    try:
        refresh_lan(args[0])
    finally:
        print(*lan_runtime(), sep="\t")


_HANDLERS = {"cod4mp": _cod4mp, "lan": _lan, "iw4sp": _iwpad, "iw5sp": _iwpad}
_HANDLERS.update(dict.fromkeys(
    ("t4sp", "t4mp", "t5sp", "t5mp", "t6mp", "t6zm", "iw5mp", "iw5mp_ds"), _plutonium))


def main(argv):
    if not argv or argv[0] not in _HANDLERS:
        return
    t0 = time.monotonic()
    try:
        _HANDLERS[argv[0]](argv[0], argv[1:])
    except Exception as ex:
        _log.warning("prelaunch %s: update skipped: %s", argv[0], ex)
    _log.info("prelaunch %s: %.1fs", argv[0], time.monotonic() - t0)


if __name__ == "__main__":
    setup_logging()
    main(sys.argv[1:])
