"""
sp_mod.py - IW-Pad for MW2/MW3 SP (controller support + aim assist)

Steam (x64): iw-pad-x64.zip in the game root + a launch option. No AlterWare.

Own (32-bit): iw-pad-x86.zip + the AlterWare SP client. 32-bit Steam
depot SP exes lack per-account CEG and silently exit on the DRM check.
AlterWare's clients host the original game code in their own process:

  MW2: iw4x-sp.exe + data/iw4sp.exe (the only iw4sp.exe build it accepts)
  MW3: iw5-mod.exe + iw5mp_server.exe + raw/scripts/sp/_cp.gsc

IW-Pad (iw-pad.exe + iw-pad.dll) adds controller support and aim assist.
Its launcher prefers iw4x-sp.exe / iw5-mod.exe when present and passes
-singleplayer itself, so the shortcut only swaps in iw-pad.exe.
"""

import os
import zipfile

from log import get_logger
from net import download, github_asset
from alterware import _CDN_BASE, cdn_digests

_log = get_logger(__name__)

_IW_PAD_URL = "https://github.com/GalvarinoDev/IW-Pad/releases/latest/download/iw-pad-{}.zip"

_SP_MOD_CONFIG = {
    "iw4sp": {
        "cdn_files": [
            ("iw4/iw4x-sp.exe", "iw4x-sp.exe"),
            ("iw4/data/iw4sp.exe", "data/iw4sp.exe"),
        ],
        "original_exe": "iw4sp.exe",
        "appid": 10180,
    },
    "iw5sp": {
        "cdn_files": [
            ("iw5/iw5-mod.exe", "iw5-mod.exe"),
            ("iw5/iw5mp_server.exe", "iw5mp_server.exe"),
            ("iw5/raw/scripts/sp/_cp.gsc", "raw/scripts/sp/_cp.gsc"),
        ],
        "original_exe": "iw5sp.exe",
        "appid": 42680,
    },
}


def install_sp_mod(game_key: str, install_dir: str, on_progress=None,
                   source: str = "own", steam_root: str = ""):
    """Install IW-Pad into install_dir (plus AlterWare for own). Raises on failure."""
    cfg = _SP_MOD_CONFIG[game_key]
    own = source == "own"
    files = cfg["cdn_files"] if own else []
    steps = len(files) + 1

    def prog(pct, msg):
        _log.info(msg)
        if on_progress:
            on_progress(pct, msg)

    digests = cdn_digests() if files else {}
    for i, (cdn_path, local_path) in enumerate(files):
        dst = os.path.join(install_dir, local_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        name = os.path.basename(local_path)
        download(f"{_CDN_BASE}/{cdn_path}", dst,
                 lambda p, m, i=i: prog(int((i + p / 100) * 100 / steps), m), name,
                 digest=digests.get(cdn_path))
        _log.info("CDN: placed %s", local_path)

    _fetch_iw_pad(game_key, install_dir, "x86" if own else "x64",
                  lambda p, m: prog(int((len(files) + p / 100) * 100 / steps), m))

    if not own:
        from wrapper import set_launch_options, clear_launch_options
        clear_launch_options(steam_root, cfg["appid"])
        set_launch_options(steam_root, cfg["appid"], build_sp_launch_option(game_key))
    prog(100, "IW-Pad installed.")


def _fetch_iw_pad(game_key, install_dir, arch, prog, release=None):
    url, dg = release or github_asset("GalvarinoDev/IW-Pad", f"iw-pad-{arch}.zip",
                                      fallback=_IW_PAD_URL.format(arch))
    zip_dest = os.path.join(install_dir, f"iw-pad-{arch}.zip")
    download(url, zip_dest, prog, "IW-Pad", digest=dg)
    with zipfile.ZipFile(zip_dest) as zf:
        zf.extractall(install_dir)
    os.remove(zip_dest)
    # .../releases/download/<tag>/iw-pad-x64.zip; the unverified fallback saves
    # no version, so the next launch fetches a verified copy.
    from config import set_iw_pad
    set_iw_pad(game_key, {"tag": url.split("/")[-2], "digest": dg} if dg else {})


def update_iw_pad(game_key: str, install_dir: str, log=_log.info):
    """Pre-launch: fetch IW-Pad only if the latest release differs from the saved one."""
    from config import get_iw_pad, get_setup_games
    arch = "x86" if get_setup_games().get(game_key, {}).get("source") == "own" else "x64"
    # No fallback: an API failure or a release without its zips skips the update.
    url, dg = github_asset("GalvarinoDev/IW-Pad", f"iw-pad-{arch}.zip", timeout=5)
    tag = url.split("/")[-2]
    saved = get_iw_pad(game_key)
    if not dg:
        log(f"IW-Pad {game_key}: {tag} has no digest, update skipped")
        return
    if saved == {"tag": tag, "digest": dg}:
        log(f"IW-Pad {game_key}: {tag} is current")
        return
    log(f"IW-Pad {game_key}: {saved.get('tag', 'unknown')} -> {tag}")
    _fetch_iw_pad(game_key, install_dir, arch, lambda p, m: None, release=(url, dg))


# DXVK frame limit (setting dxvk_frame_cap, on by default): MW2 (2009) and MW3 (2011)
# render through Direct3D 9, so DXVK's limiter caps them whatever the game's own
# settings do. The 64-bit SP builds have no com_maxfps (fovwatch.py never found its
# record), so there is no in-game cap to use instead.
_DXVK_CAP = 'DXVK_CONFIG="d3d9.maxFrameRate=90" '
_CAP_GAMES = ("iw4sp", "iw5sp")


def _frame_cap(game_key: str) -> str:
    if game_key not in _CAP_GAMES:
        return ""
    from config import get_dxvk_frame_cap
    return _DXVK_CAP if get_dxvk_frame_cap() else ""


def build_sp_launch_option(game_key: str) -> str:
    """Launch option (Steam app or own shortcut) that swaps the original SP exe for iw-pad.exe,
    with the DXVK frame cap in front when the setting is on."""
    exe = _SP_MOD_CONFIG[game_key]["original_exe"]
    from steam_common import prelaunch_prefix
    return (f"{_frame_cap(game_key)}{prelaunch_prefix(game_key)}"
            f"bash -c 'exec \"${{@/{exe}/iw-pad.exe}}\"' -- %command%")


def _launch_option_ok(game_key: str, value: str) -> bool:
    """True if a tile's current launch option is what DeckOps puts there: the iw-pad.exe
    swap, and the DXVK frame cap exactly when the setting asks for it."""
    if "iw-pad.exe" not in value:
        return False
    return ("d3d9.maxFrameRate" in value) == bool(_frame_cap(game_key))


def repair_sp_launch_options(steam_root: str, log=lambda m: None) -> list:
    """Steam MW2 / MW3 SP set up with IW-Pad: put IW-Pad's launch option back
    when the tile's current one doesn't run iw-pad.exe (empty, or left over
    from something else such as an old AlterWare client), or has the DXVK
    frame cap when the setting is off (or lacks it when it is on). An option
    that matches is never touched. Steam must be closed. Returns the game
    keys fixed."""
    from config import get_setup_games
    from wrapper import snapshot_launch_options, clear_launch_options, set_launch_options
    setup = get_setup_games()
    fixed = []
    for key, cfg in _SP_MOD_CONFIG.items():
        entry = setup.get(key, {})
        if entry.get("client") != "iw-pad" or entry.get("source", "steam") != "steam":
            continue
        appid = cfg["appid"]
        current = snapshot_launch_options(steam_root, [appid]).get(str(appid), {})
        if current and all(_launch_option_ok(key, v) for v in current.values()):
            continue
        log(f"  {key}: launch option is not DeckOps' IW-Pad one, setting it again")
        clear_launch_options(steam_root, appid)
        set_launch_options(steam_root, appid, build_sp_launch_option(key))
        fixed.append(key)
    return fixed


if __name__ == "__main__":
    assert build_sp_launch_option("iw4sp").endswith(" iw4sp bash -c 'exec \"${@/iw4sp.exe/iw-pad.exe}\"' -- %command%")
    assert build_sp_launch_option("iw5sp").endswith(" iw5sp bash -c 'exec \"${@/iw5sp.exe/iw-pad.exe}\"' -- %command%")
    print("ok")
