"""
iw3sp.py - DeckOps installer for IW3SP-MOD (Call of Duty 4: Modern Warfare Singleplayer)

Downloads the IW3SP-MOD files listed in the mod's own update manifest
(files.json, the one its in-game updater checks) into the CoD4 install
directory. Steam games use a launch option to redirect iw3sp.exe to
iw3sp_mod.exe via bash parameter substitution on %command%.

Progress is reported via a callback:
    on_progress(percent: int, status: str)
"""

import glob
import os
import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request

from net import download as _download, BROWSER_UA as _BROWSER_UA, safe_manifest_path, commit_staged_files
from steam_common import sha1_file as _sha1, write_json

# The Gitea releases lag behind this: v4.1.5's game.dll breaks right stick
# aim with m_rawinput on and ignores the config init marker, and the
# in-game updater offers the newer build on first launch anyway.
_CDN          = "https://jerryalt.codeberg.page"
METADATA_FILE = "deckops_iw3sp.json"
COD4_APPID    = "7940"


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_manifest():
    """[(relative path, size, sha1), ...] from the mod's update manifest."""
    req = urllib.request.Request(f"{_CDN}/files.json", headers=_BROWSER_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _build_iw3sp_launch_option() -> str:
    return "bash -c 'exec \"${@/iw3sp.exe/iw3sp_mod.exe}\"' -- %command%"


def cod4_launch_option(setup_games: dict) -> str:
    """
    The one 7940 launch option for what DeckOps set up from Steam: MP and
    SP share the appid, so the CoD4R pre-launch update and the IW3SP exe
    swap are built together here instead of overwriting each other.
    Returns "" when neither applies.
    """
    from steam_common import prelaunch_prefix
    steam = lambda k, c: (setup_games.get(k, {}).get("client") == c
                          and setup_games[k].get("source", "steam") == "steam")
    sp = _build_iw3sp_launch_option() if steam("cod4sp", "iw3sp") else "%command%"
    if steam("cod4mp", "cod4r"):
        return prelaunch_prefix("cod4mp") + sp
    return sp if sp != "%command%" else ""


def apply_cod4_launch_option(steam_root: str) -> bool:
    """
    Set 7940's option after the launch-option clean slate. The install
    flow runs the CoD4 installers before Steam is closed, so options they
    write there don't survive. Steam must be closed.

    Returns True only if the option is in place for at least one Steam
    account: False when none is needed, and also when set_launch_options
    wrote nothing (no account has a 7940 entry yet, a failed write).
    """
    import config as cfg
    opt = cod4_launch_option(cfg.get_setup_games())
    if not opt:
        return False
    from wrapper import set_launch_options
    return set_launch_options(steam_root, COD4_APPID, opt) > 0


# ── public API ────────────────────────────────────────────────────────────────

def install_iw3sp(game: dict, steam_root: str,
                  proton_path: str, compatdata_path: str,
                  on_progress=None, source: str = "steam"):
    """
    Install IW3SP-MOD for Call of Duty 4 singleplayer.

    Downloads every manifest file that is missing or out of date into the
    CoD4 install directory.
    For Steam games, sets a launch option to redirect iw3sp.exe to
    iw3sp_mod.exe. For own games, the shortcut points at iw3sp_mod.exe
    directly.
    """
    install_dir = game["install_dir"]
    prog = on_progress or (lambda *a: None)

    prog(2, "Checking IW3SP-MOD files...")
    try:
        files = _get_manifest()
    except Exception as ex:
        raise RuntimeError(f"Failed to fetch IW3SP-MOD file list: {ex}") from ex

    # Validate *all* paths before hashing or writing any files. The CDN's
    # manifest is remote input, including paths for files already installed.
    validated = []
    for n, sz, h in files:
        dest = safe_manifest_path(install_dir, n)
        if not isinstance(sz, int) or sz < 0 or not isinstance(h, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", h):
            raise ValueError(f"invalid IW3SP-MOD manifest entry: {n!r}")
        validated.append((n, sz, h, dest))
    todo = [(n, sz, h, dest) for n, sz, h, dest in validated
            if not (os.path.isfile(dest) and _sha1(dest) == h.lower())]
    total = sum(sz for _, sz, _, _ in todo) or 1
    done = 0
    if todo:
        # Every file is downloaded and verified before any is swapped in, so a failure
        # part-way can't leave a game.dll from one build next to the exe of another.
        # Left behind by an earlier run that was killed mid-download
        for old in glob.glob(os.path.join(glob.escape(install_dir), ".deckops_iw3sp_*")):
            shutil.rmtree(old, ignore_errors=True)
        staging = tempfile.mkdtemp(prefix=".deckops_iw3sp_", dir=install_dir)
        try:
            for n, sz, h, dest in todo:
                staged = safe_manifest_path(staging, n)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                _download(
                    f"{_CDN}/{urllib.parse.quote(n, safe='/')}", staged,
                    lambda p, m, _d=done, _s=sz: prog(5 + int((_d + _s * p / 100) / total * 55), m),
                    f"Downloading IW3SP-MOD {os.path.basename(n)}...",
                    digest=f"sha1:{h}",
                )
                done += sz
            commit_staged_files(install_dir, staging, [n for n, _, _, _ in todo])
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # Restore iw3sp.exe from legacy backup if the old exe-swap is present
    iw3sp_bak = os.path.join(install_dir, "iw3sp.exe.bak")
    if os.path.exists(iw3sp_bak):
        iw3sp = os.path.join(install_dir, "iw3sp.exe")
        os.replace(iw3sp_bak, iw3sp)  # replaces atomically; no gap with no iw3sp.exe

    # Set launch option (Steam only) — redirects iw3sp.exe to iw3sp_mod.exe.
    # Safe on shared appid 7940: substitution only matches when Steam
    # launches the SP exe, no-op for MP (iw3mp.exe).
    if source != "own":
        prog(80, "Setting launch options...")
        try:
            from wrapper import set_launch_options
            set_launch_options(steam_root, COD4_APPID, _build_iw3sp_launch_option())
        except Exception as ex:
            prog(80, f"Could not set launch options: {ex}")
    else:
        prog(80, "Own game -- skipping launch options")

    # Write metadata
    prog(95, "Saving metadata...")
    meta_path = os.path.join(install_dir, METADATA_FILE)
    write_json(meta_path, {"source": f"{_CDN}/files.json"})

    prog(100, "IW3SP-MOD installation complete!")
