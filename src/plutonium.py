"""
plutonium.py - DeckOps installer for Plutonium (every device except Steam Deck LCD)
(Call of Duty: MW3, World at War, Black Ops, Black Ops II)

LCD Decks are dispatched to plutonium_lcd.py early in
install_plutonium(). Everything below the LCD dispatch handles both
OLED and Other (non-Deck SteamOS) devices, which share the same
direct-launch architecture.

OLED / Other flow:
  1. Download plutonium.exe into a dedicated DeckOps-managed Wine prefix.
  2. Launch it through Proton so the user can log in and let Plutonium
     download its full client.
  3. Wait for user confirmation that they have logged in and closed Plutonium.
  4. Verify by checking that the storage/ folder exists in the prefix.
  5. Copy the entire Plutonium/ folder into each selected game's prefix.
  6. Write config.json in each prefix with the correct game install paths.
  7. Write a bash wrapper script that replaces the game exe, padded to the
     original exe size so Steam's file validation does not flag it.

Progress is reported via a callback:
    on_progress(percent: int, status: str)
"""

import contextlib
import os
import stat
import shutil
import json
import subprocess
import tempfile
import time

from log import get_logger
from net import DownloadError, download
from depot_downgrade import plut_game_dir

_log = get_logger(__name__)



# ── dedicated prefix ──────────────────────────────────────────────────────────
# DeckOps manages its own Wine prefix for the initial Plutonium install.
# This avoids depending on Steam to create a non-Steam game entry and gives
# us a known, stable path to copy from.

DEDICATED_PREFIX = os.path.expanduser("~/.local/share/deckops/plutonium_prefix")
PLUT_BOOTSTRAPPER_URL = "https://cdn.plutonium.pw/updater/plutonium.exe"
_PLUT_ARCHIVE_FALLBACK_URL = "https://archive.org/download/plutonium_202605/plutonium.exe"
# The archive.org item is a fixed snapshot, so its copy is pinned (checked
# 2026-10-25; the CDN served the same file then). The CDN copy changes with
# Plutonium updates and only gets the Windows-exe check below.
_PLUT_ARCHIVE_DIGEST = "sha256:85d1d1ca4d5881d9b98928c2006fb0eec9655e2705fe74088e6f974a19703f0f"
_PLUT_MIN_SIZE = 500_000


def is_windows_exe(path: str) -> bool:
    """True if path looks like a real Windows executable: MZ header, PE
    signature where the header points, and a plausible size. An HTML error
    or challenge page served with HTTP 200 used to be run as plutonium.exe."""
    import struct
    try:
        if os.path.getsize(path) < _PLUT_MIN_SIZE:
            return False
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return False
            f.seek(0x3C)
            pe = struct.unpack("<I", f.read(4))[0]
            f.seek(pe)
            return f.read(4) == b"PE\0\0"
    except (OSError, struct.error):
        return False


def fetch_bootstrapper(dest: str, prog=lambda m: None, keep_existing: bool = False):
    """
    Download plutonium.exe to dest: the Plutonium CDN first, then the pinned
    archive.org copy. Each download must be a real Windows exe, else it is
    deleted and counts as a failure. keep_existing: when both downloads fail,
    a valid exe already at dest is used instead (LCD keeps one in the Heroic
    prefix). Raises DownloadError when there is no valid exe.
    """
    # Downloads land in a staging name and replace dest only once they are a
    # real exe: an HTML error page (HTTP 200) used to overwrite a good
    # plutonium.exe, and keep_existing then had nothing left to fall back to.
    staging = dest + ".new"

    def _discard():
        for p in (staging, staging + ".part"):
            try: os.remove(p)
            except OSError: pass

    _discard()
    try:
        download(PLUT_BOOTSTRAPPER_URL, staging)
        if is_windows_exe(staging):
            os.replace(staging, dest)
            return dest
        _log.warning("Plutonium CDN sent something that isn't plutonium.exe (%d bytes)",
                     os.path.getsize(staging))
    except Exception as ex:
        _log.warning("Primary Plutonium download failed after 3 attempts: %s", ex)
    prog("Falling back to archive.org mirror...")
    # Drop the primary's file and partial or download() would resume the
    # mirror's copy on top of it.
    _discard()
    try:
        download(_PLUT_ARCHIVE_FALLBACK_URL, staging, digest=_PLUT_ARCHIVE_DIGEST)
        if not is_windows_exe(staging):
            raise IOError("archive.org copy is not a Windows executable")
        os.replace(staging, dest)
        _log.info("Plutonium bootstrapper downloaded from archive.org fallback")
        return dest
    except Exception as ex2:
        _discard()
        if keep_existing and is_windows_exe(dest):
            _log.warning("Plutonium downloads failed, using the existing plutonium.exe: %s", ex2)
            prog("Downloads failed -- using the plutonium.exe already there")
            return dest
        raise DownloadError(
            url=_PLUT_ARCHIVE_FALLBACK_URL,
            dest=dest,
            label="Plutonium bootstrapper",
            cause=ex2,
        )


# ── game metadata ─────────────────────────────────────────────────────────────
# Maps DeckOps game key → (appid, config.json path key, exe name to replace)

GAME_META = {
    "t4sp":  (10090,  "t4Path",  "CoDWaW.exe"),
    "t4mp":  (10090,  "t4Path",  "CoDWaWmp.exe"),
    "t5sp":  (42700,  "t5Path",  "BlackOps.exe"),
    "t5mp":  (42710,  "t5Path",  "BlackOpsMP.exe"),
    "t6zm":  (212910, "t6Path",  "t6zm.exe"),
    "t6mp":  (202990, "t6Path",  "t6mp.exe"),
    "iw5mp": (42690,  "iw5Path", "iw5mp.exe"),
    "iw5mp_ds": (42750, "iw5Path", "iw5mp_server.exe"),
}

METADATA_FILE = "deckops_plutonium.json"

# Standalone wrapper exe names for OLED own games.
# These are new files dropped into the game folder — the original exe is
# left untouched. shortcut.py points the non-Steam shortcut at these.
# Same filenames as LCD's LCD_OWN_WRAPPER_EXES in plutonium_lcd.py so the
# launcher treats both models identically.
OLED_OWN_WRAPPER_EXES = {
    "t4sp":  "t4plutsp.exe",
    "t4mp":  "t4plutmp.exe",
    "t5sp":  "t5plutsp.exe",
    "t5mp":  "t5plutmp.exe",
    "t6mp":  "t6plutmp.exe",
    "t6zm":  "t6plutzm.exe",
    "iw5mp": "iw5plutmp.exe",
}

# OLED Steam games: (online, offline) wrapper files written next to the
# untouched game exe. Steam launch menu entries point at these (see
# steam_appinfo.py). Offline opens the Offline launcher (write_offline_menu_file).
# Keys sharing an appid (WaW) appear in this order.
STEAM_MENU_EXES = {
    "t4sp":  ("t4plutsp.exe",  "t4plutsp_lan.exe"),
    "t4mp":  ("t4plutmp.exe",  "t4plutmp_lan.exe"),
    "t5sp":  ("t5plutsp.exe",  "t5plutsp_lan.exe"),
    "t5mp":  ("t5plutmp.exe",  "t5plutmp_lan.exe"),
    "t6mp":  ("t6plutmp.exe",  "t6plutmp_lan.exe"),
    "t6zm":  ("t6plutzm.exe",  "t6plutzm_lan.exe"),
    "iw5mp": ("iw5plutmp.exe", "iw5plutmp_lan.exe"),
    # DS shares MW3's folder, so its files need their own names
    "iw5mp_ds": ("iw5plutds.exe", "iw5plutds_lan.exe"),
}

# What the Steam picker shows for each key's (online, offline) entry; keys not
# listed get _DEFAULT_MENU_LABELS. Steam's own entries are not touched here.
_LABELS_CZ = ("Campaign & Zombies", "Campaign & Zombies (Offline)")
_LABELS_MP = ("Multiplayer", "Multiplayer (Offline)")
STEAM_MENU_LABELS = {
    "t4sp":  _LABELS_CZ,
    "t4mp":  _LABELS_MP,
    "t5sp":  _LABELS_CZ,
    "t5mp":  _LABELS_MP,
    "t6mp":  _LABELS_MP,
    "t6zm":  ("Zombies", "Zombies (Offline)"),
    "iw5mp": _LABELS_MP,
}
_DEFAULT_MENU_LABELS = ("Play Plutonium Online", "Play Plutonium Offline")

# Sidecar -lan wrapper script names for OLED offline mode.
# These are written alongside the game files (never replacing anything)
# and launched by DeckOps_Offline.exe for offline play. Shell scripts rather
# than .exe so they don't interfere with Steam's file validation.
OLED_LAN_WRAPPER_NAMES = {
    "t4sp":  "t4plut_lan_sp.sh",
    "t4mp":  "t4plut_lan_mp.sh",
    "t5sp":  "t5plut_lan_sp.sh",
    "t5mp":  "t5plut_lan_mp.sh",
    "t6mp":  "t6plut_lan_mp.sh",
    "t6zm":  "t6plut_lan_zm.sh",
    "iw5mp": "iw5plut_lan.sh",
    "iw5mp_ds": "iw5plut_lan.sh",
}

# Old shared filenames used before the unique-name fix. Used during
# install to clean up stale wrappers from previous DeckOps versions.
_OLD_OLED_LAN_WRAPPER_NAMES = {
    "t4sp":  "t4plut_lan.sh",
    "t4mp":  "t4plut_lan.sh",
    "t5sp":  "t5plut_lan.sh",
    "t5mp":  "t5plut_lan.sh",
    "t6mp":  "t6plut_lan.sh",
    "t6zm":  "t6plut_lan.sh",
}

# DeckOps client-side menu mods packaged as .iwd files (renamed .zip).
# Bundled with the repo at assets/mods/ and copied locally into Plutonium's
# storage/ directory where they are loaded automatically on game launch.
#
# T6 uses a single consolidated .iwd (deckops_bo2_menu.iwd) for both MP
# and ZM. The Lua inside contains both PopulateButtons_Multiplayer and
# PopulateButtons_Zombie, with the engine's CoD.isZombie dispatch selecting
# the right button set at runtime. Both t6mp and t6zm point at the same
# source file and same destination -- idempotent when both are installed.
_OLED_HERE = os.path.dirname(os.path.abspath(__file__))
MENU_MOD_ASSETS_DIR = os.path.join(os.path.dirname(_OLED_HERE), "assets", "mods")
MENU_MOD_FILES = {
    "t6mp":  ("t6/deckops_bo2_menu.iwd", "storage/t6/raw/deckops_bo2_menu.iwd"),
    "t6zm":  ("t6/deckops_bo2_menu.iwd", "storage/t6/raw/deckops_bo2_menu.iwd"),
}


# ── path helpers ──────────────────────────────────────────────────────────────

def _plut_dir_in_prefix(prefix_path: str) -> str:
    """Return the Plutonium/ folder path inside a given Wine prefix."""
    return os.path.join(
        prefix_path, "pfx", "drive_c", "users", "steamuser",
        "AppData", "Local", "Plutonium",
    )


def _plut_dir_in_compatdata(steam_root: str, appid: int) -> str:
    """Return the Plutonium/ folder path inside a Steam compatdata prefix."""
    return os.path.join(
        steam_root, "steamapps", "compatdata", str(appid),
        "pfx", "drive_c", "users", "steamuser",
        "AppData", "Local", "Plutonium",
    )


def _wine_path(linux_path: str) -> str:
    """Convert a Linux path to Wine Z: drive notation."""
    return "Z:" + linux_path.replace("/", "\\")


# ── dedicated prefix helpers ──────────────────────────────────────────────────

def get_dedicated_plut_dir() -> str:
    """Return the Plutonium/ folder inside DeckOps's dedicated prefix."""
    return _plut_dir_in_prefix(DEDICATED_PREFIX)


def is_plutonium_ready() -> bool:
    """
    Returns True if the user has logged in and Plutonium has downloaded
    its client files. We check for the storage/ folder which is only
    created after a successful login.
    """
    storage = os.path.join(get_dedicated_plut_dir(), "storage")
    if not os.path.isdir(storage):
        return False
    # Must have at least one game subfolder inside storage/
    return any(
        os.path.isdir(os.path.join(storage, d))
        for d in os.listdir(storage)
    )


# ── bootstrapper ──────────────────────────────────────────────────────────────

def launch_bootstrapper(proton_path: str, on_progress=None, steam_root: str = None):
    """
    Download plutonium.exe into the dedicated prefix and launch it through
    Proton. Blocks until the user closes the Plutonium window.

    The UI should display instructions to the user before calling this,
    and show a confirmation button after it returns.
    """
    def prog(pct, msg):
        if on_progress:
            on_progress(pct, msg)

    plut_dir = get_dedicated_plut_dir()
    os.makedirs(plut_dir, exist_ok=True)

    # Proton needs pfx/ to exist , initialise a minimal prefix structure
    os.makedirs(DEDICATED_PREFIX, exist_ok=True)

    bootstrapper = os.path.join(plut_dir, "plutonium.exe")

    prog(5, "Downloading Plutonium bootstrapper...")
    fetch_bootstrapper(bootstrapper, lambda m: prog(5, m))

    prog(15, "Launching Plutonium , please log in, then close the window when done.")

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"]           = DEDICATED_PREFIX
    if steam_root:
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = steam_root
    else:
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.dirname(
            os.path.dirname(proton_path)
        )

    proc = subprocess.Popen(
        [proton_path, "run", bootstrapper],
        env=env,
        cwd=plut_dir,
    )
    proc.wait()
    prog(30, "Plutonium closed.")


# ── shared Plutonium directories ─────────────────────────────────────────────
# bin/, launcher/, and games/ are identical across all prefixes (~129MB total).
# Each prefix symlinks them to the master copy in the dedicated prefix, so
# pre-launch updates reach every prefix. Only storage/ is per-game.
_PLUT_SHARED_SUBDIRS = ("bin", "launcher", "games")

# Old copy of the shared dirs that prefixes used to link to. Update Plutonium
# removes it, then reinstalls every Plutonium game so they link to the master.
SHARED_PLUT_DIR = os.path.expanduser("~/.local/share/deckops/plutonium_shared")


_STORAGE_SUBDIRS = {
    "t4sp": "t4", "t4mp": "t4",
    "t5sp": "t5", "t5mp": "t5",
    "t6zm": "t6", "t6mp": "t6",
    "iw5mp": "iw5", "iw5mp_ds": "iw5",
}


def _link_to_master(master: str, plut_dir: str, prog):
    """
    Swap the Plutonium files just copied into plut_dir for links to the
    master, so each file is stored once and pre-launch updates reach every
    prefix. Best effort: without the manifest the copies stay and the first
    online launch links them instead.
    """
    def say(msg):
        _log.info(msg); prog(msg)
    try:
        import plutonium_update as pu
        _, info = pu.fetch()
        pu.link_prefix(master, plut_dir, info, say)
    except Exception as ex:
        say(f"  ⚠ Plutonium links skipped, copies kept: {ex}")


def _copy_plut_to_prefix(src_plut_dir: str, dest_plut_dir: str,
                          game_key: str = "", on_progress=None):
    """
    Set up Plutonium in a game prefix using symlinks for shared dirs.

    bin/, launcher/, and games/ are symlinked to the master copy in the
    dedicated prefix. Only the relevant storage/
    subdirectory for game_key is copied as real files, so large mods
    don't leak into unrelated prefixes.
    """

    def prog(msg):
        if on_progress:
            on_progress(msg)

    prog(f"  Copying Plutonium: {src_plut_dir} -> {dest_plut_dir}")

    # The fresh copy below wipes dest_plut_dir, which also holds the user's
    # stats (players/, e.g. WaW mpdata) and mods. Move those aside first and
    # put them back after, so reinstall/update never resets progress.
    keep_root = dest_plut_dir.rstrip(os.sep) + ".deckops_keep"
    # The stash is inside the try: one part that fails to move (full or locked
    # disk) used to leave the parts already moved in keep_root, out of the
    # prefix the game still runs from, until some later install succeeded.
    try:
        _stash_user_storage(dest_plut_dir, keep_root, prog)
        _copy_plut_fresh(src_plut_dir, dest_plut_dir, game_key, prog)
    finally:
        _restore_user_storage(dest_plut_dir, keep_root, prog)


# storage/<game>/ parts that belong to the user, not to Plutonium.
_PLUT_USER_PARTS = ("players", "mods", "usermaps")

# Never copied from one prefix to another: each prefix keeps its own.
_PLUT_NO_COPY = ("mods", "usermaps")


def _skip_no_copy(root):
    return lambda d, names: [n for n in names if n in _PLUT_NO_COPY] if d == root else []


def _stash_user_storage(plut_dir: str, keep_root: str, prog):
    storage = os.path.join(plut_dir, "storage")
    if not os.path.isdir(storage):
        return
    for store in os.listdir(storage):
        for part in _PLUT_USER_PARTS:
            src = os.path.join(storage, store, part)
            dst = os.path.join(keep_root, store, part)
            # A keep dir left by an earlier failed run already holds the
            # user's data; what's in the prefix now is a fresh copy. (The failed
            # restore is the usual cause, right after the fresh copy, so the keep
            # dir is the real one -- not worth guessing the other way.)
            if not os.path.isdir(src) or os.path.lexists(dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            prog(f"  Kept storage/{store}/{part}")


def _restore_user_storage(plut_dir: str, keep_root: str, prog):
    if not os.path.isdir(keep_root):
        return
    try:
        for store in os.listdir(keep_root):
            for part in os.listdir(os.path.join(keep_root, store)):
                src = os.path.join(keep_root, store, part)
                dst = os.path.join(plut_dir, "storage", store, part)
                if os.path.lexists(dst):
                    shutil.rmtree(dst)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
        shutil.rmtree(keep_root)
    except OSError as ex:
        _log.error("Could not restore Plutonium user data from %s: %s", keep_root, ex)
        prog(f"  ⚠ Your Plutonium stats/mods are safe in {keep_root} but could not be moved back: {ex}")


def _copy_plut_fresh(src_plut_dir: str, dest_plut_dir: str, game_key: str, prog):
    """Build the prefix's Plutonium next to the old one and swap it in, so a missing
    master, a full disk or a kill mid-copy leaves the working copy as it was."""
    # The prefix links into the master's bin/: without it the new copy is empty shells
    if not os.path.isdir(os.path.join(src_plut_dir, "bin")):
        raise FileNotFoundError(f"Plutonium master copy is missing or incomplete: {src_plut_dir}")

    base = dest_plut_dir.rstrip(os.sep)
    staging, old = base + ".deckops_new", base + ".deckops_old"
    # A kill between the two swaps below leaves the working copy (with the player's
    # stats) as .deckops_old and nothing at dest: put it back before the leftovers
    # are cleared, or this run would delete the only copy.
    if os.path.lexists(old) and not os.path.lexists(dest_plut_dir):
        try:
            os.replace(old, dest_plut_dir)
            _log.warning("restored the Plutonium copy an interrupted install left in %s", old)
        except OSError:
            _log.error("Could not put the previous Plutonium back from %s", old, exc_info=True)
            raise
    for leftover in (staging, old):
        if os.path.lexists(leftover):
            shutil.rmtree(leftover, ignore_errors=True)
    try:
        _build_plut_copy(src_plut_dir, staging, game_key, prog)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    had_old = os.path.lexists(dest_plut_dir)
    if had_old:
        prog(f"  Replacing existing Plutonium at {dest_plut_dir}...")
        os.replace(dest_plut_dir, old)
    try:
        os.replace(staging, dest_plut_dir)
    except BaseException:
        if had_old:
            try:
                os.replace(old, dest_plut_dir)
            except OSError:
                _log.error("Could not put the previous Plutonium back from %s", old)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if had_old:
        shutil.rmtree(old, ignore_errors=True)


def _build_plut_copy(src_plut_dir: str, dest_plut_dir: str, game_key: str, prog):
    start = time.time()

    os.makedirs(dest_plut_dir, exist_ok=True)

    # Symlink shared directories to the master
    for subdir in _PLUT_SHARED_SUBDIRS:
        shared_src = os.path.join(src_plut_dir, subdir)
        dest_sub = os.path.join(dest_plut_dir, subdir)
        if os.path.isdir(shared_src):
            os.symlink(shared_src, dest_sub)

    # Copy only the storage subdirectory for this game
    storage_src = os.path.join(src_plut_dir, "storage")
    if os.path.isdir(storage_src):
        storage_name = _STORAGE_SUBDIRS.get(game_key)
        if storage_name:
            sub_src = os.path.join(storage_src, storage_name)
            if os.path.isdir(sub_src):
                sub_dst = os.path.join(dest_plut_dir, "storage", storage_name)
                os.makedirs(os.path.join(dest_plut_dir, "storage"), exist_ok=True)
                shutil.copytree(sub_src, sub_dst, ignore=_skip_no_copy(sub_src))
        else:
            storage_dst = os.path.join(dest_plut_dir, "storage")
            shutil.copytree(storage_src, storage_dst)

    # Copy any remaining top-level files (config, etc.)
    for item in os.listdir(src_plut_dir):
        src_item = os.path.join(src_plut_dir, item)
        dst_item = os.path.join(dest_plut_dir, item)
        if os.path.exists(dst_item):
            continue  # already handled (symlink or storage)
        if os.path.isfile(src_item):
            shutil.copy2(src_item, dst_item)

    elapsed = time.time() - start
    prog(f"  Copied Plutonium with symlinks ({elapsed:.1f}s)")


# ── config.json ───────────────────────────────────────────────────────────────

def _write_config(plut_dir: str, game_keys: list, installed_games: dict):
    """
    Write config.json inside a prefix with the correct game install paths.
    Reads the existing config from the dedicated prefix and updates path keys.
    game_keys   , list of game keys being installed into this prefix
    installed_games , dict from detect_games.find_installed_games()
    """
    config_path = os.path.join(plut_dir, "config.json")

    # Read existing config (token etc.) from this prefix if present
    data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                data = json.load(f)
        except (OSError, ValueError) as ex:
            # A cut-off or damaged file has no token left to keep; rewriting it beats
            # aborting the whole install on it (plutonium_lcd does the same).
            _log.warning("Plutonium config.json unreadable, rewriting it: %s", ex)
        if not isinstance(data, dict):
            data = {}

    # Write the path key for each game in this prefix
    for key in game_keys:
        if key not in GAME_META:
            continue
        _, path_key, _ = GAME_META[key]
        game = installed_games.get(key, {})
        install_dir = game.get("install_dir", "")
        if install_dir:
            # MW3 Steam: point at the downgrade/ subfolder where 32-bit
            # depot files live, keeping the 64-bit install untouched.
            install_dir = plut_game_dir(key, install_dir, game.get("source", "steam"))
            data[path_key] = _wine_path(install_dir)

    tmp = config_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, config_path)


# ── client-side menu mods ────────────────────────────────────────────────────

def _install_menu_mod(dest_plut_dir: str, game_key: str, on_progress=None):
    """
    Copy and install the DeckOps menu mod for the given game key.

    Copies the bundled mod file from assets/mods/ into the prefix's
    Plutonium storage path at raw/. Plutonium loads .iwd files from
    raw/ automatically on game launch, overriding the default main
    menu with the DeckOps community version (server connect, unlock
    all, reset stats buttons).

    Uses .iwd in raw/ (not mods/) so it:
    - Uses normal player stats (no separate mod stats)
    - Persists across disconnects
    - Loads automatically on every launch

    Skips silently for game keys without a menu mod defined.
    """
    def prog(msg):
        if on_progress:
            on_progress(msg)

    if game_key not in MENU_MOD_FILES:
        return

    asset_rel, local_path = MENU_MOD_FILES[game_key]
    src_file = os.path.join(MENU_MOD_ASSETS_DIR, asset_rel)
    dest_file = os.path.join(dest_plut_dir, local_path)

    prog(f"  Installing DeckOps menu mod for {game_key}...")

    if not os.path.isfile(src_file):
        prog(f"  ⚠ Menu mod asset missing: {src_file}")
        return  # Non-fatal -- game still works without the mod

    os.makedirs(os.path.dirname(dest_file), exist_ok=True)

    try:
        shutil.copy2(src_file, dest_file)
        prog(f"  ✓ Menu mod installed ({os.path.getsize(dest_file)} bytes)")
    except OSError as ex:
        prog(f"  ⚠ Menu mod copy failed: {ex}")


# ── launcher prefix game configs ──────────────────────────────────────────────
#
# Maps game keys to their DeckOps asset config and the relative storage
# path inside the Plutonium directory where the bootstrapper expects them.
# This is the single source of truth for placing game configs into a
# Plutonium folder (a game's prefix and its offline copy) — called every
# time a Plutonium game is installed or reinstalled, so configs always land
# regardless of which install flow triggered it.

_LAUNCHER_CONFIG_MAP = {
    # game_key: (asset_subpath, storage_relpath)
    "t4sp":     ("WaW/config.cfg",          os.path.join("storage", "t4", "players", "profiles", "$$$")),
    "t4mp":     ("WaW/config_mp.cfg",       os.path.join("storage", "t4", "players", "profiles", "$$$")),
    "t5sp":     ("BO1/config.cfg",          os.path.join("storage", "t5", "players")),
    "t5mp":     ("BO1/config_mp.cfg",       os.path.join("storage", "t5", "players")),
    "t6mp":     ("BO2/plutonium_mp.cfg",    os.path.join("storage", "t6", "players")),
    "t6zm":     ("BO2/plutonium_zm.cfg",    os.path.join("storage", "t6", "players")),
    "iw5mp":    ("MW3/config_mp.cfg",       os.path.join("storage", "iw5", "players")),
    "iw5mp_ds": ("MW3/config_mp.cfg",       os.path.join("storage", "iw5", "players")),
}


def _apply_launcher_game_configs(launcher_plut_dir: str, game_key: str,
                                  on_progress=None):
    """
    Copy DeckOps asset configs directly into the offline launcher prefix.

    This ensures the bootstrapper has correct resolution, FOV, sensitivity,
    and player name settings when launching games from the offline LAN
    launcher — regardless of install flow or timing.

    Called inside install_plutonium / install_plutonium_lcd after the
    storage/ tree and menu mods have been mirrored. Uses the deck model
    from deckops.json to pick LCD or OLED assets.
    """
    def prog(msg):
        if on_progress:
            on_progress(msg)

    if game_key not in _LAUNCHER_CONFIG_MAP:
        return

    import config as _cfg
    player_name = _cfg.get_player_name() or "Player"
    model_dir = _cfg.get_model_config_dir()

    _here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(_here)
    configs_dir = os.path.join(project_root, "assets", "configs")

    asset_subpath, storage_relpath = _LAUNCHER_CONFIG_MAP[game_key]
    src = os.path.join(configs_dir, model_dir, asset_subpath)

    if not os.path.exists(src):
        prog(f"  ⚠ Launcher config asset not found: {asset_subpath}")
        return

    dest_dir = os.path.join(launcher_plut_dir, storage_relpath)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))

    try:
        shutil.copy2(src, dest)

        # Apply player name
        from game_config import _replace_player_name
        _replace_player_name(dest, player_name)

        prog(f"  + {game_key}: launcher config -> {os.path.basename(src)}")
    except Exception as ex:
        prog(f"  ⚠ {game_key}: launcher config failed: {ex}")


# ── wrapper script ────────────────────────────────────────────────────────────

# Maps DeckOps game keys to the Plutonium protocol key used in
# `plutonium://play/<key>` URLs and bootstrapper arguments. Most keys
# are identical, but iw5mp_ds (the free dedicated server appid 42750)
# runs the same Plutonium client as iw5mp (the full game appid 42690).
_PLUT_KEY_MAP = {
    "iw5mp_ds": "iw5mp",
}

def bash_name_arg(player_name) -> str:
    """+name "<name>" for a bash script. The name sits in double quotes, where
    bash still expands $ and `, so a name like  Bob $(...)  ran a command on
    every offline launch. Cleaned by config, then escaped as well. Always
    the  +name "..."  shape game_config._rename_in_lan_scripts looks for."""
    from config import clean_player_name
    name = clean_player_name(player_name) or "Player"
    for ch in ("\\", "$", "`"):     # the characters bash expands inside "..."
        name = name.replace(ch, "\\" + ch)
    return '+name "' + name.replace('"', "") + '"'


def _plut_key(game_key: str) -> str:
    """Return the Plutonium protocol key for a DeckOps game key."""
    return _PLUT_KEY_MAP.get(game_key, game_key)


def write_script(path: str, data, mode: int = 0o755):
    """Replace path with data (str or bytes) all at once: a temp file next to it,
    flushed, then renamed over it. bash reads a script as it runs it, so rewriting
    one in place (a reinstall while its game runs) corrupted the running copy, and
    a kill or power cut mid-write left a cut-off script behind."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".deckops-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dq(value: str) -> str:
    """value escaped for inside "..." in a bash script: a path with a $, a backtick,
    a quote or a backslash (an SD card label can have any of them) must stay text."""
    for ch in ("\\", '"', "$", "`"):
        value = value.replace(ch, "\\" + ch)
    return value


def _write_oled_own_wrapper(game: dict, game_key: str, steam_root: str,
                             proton_path: str, compatdata_path: str,
                             plut_dir: str, wrapper_name: str = None) -> str | None:
    """
    Write a standalone wrapper exe for OLED own games.

    Creates a new file (e.g. t4plutmp.exe) in the game's install dir that
    launches the Plutonium launcher with a protocol URL. The original game
    exe is left untouched. shortcut.py points the non-Steam shortcut at
    this wrapper, and the launcher uses the stored wrapper_path to launch it.
    Steam games reuse it for their Online launch menu entry (wrapper_name
    from STEAM_MENU_EXES).

    Returns the full path to the written wrapper, or None if game_key
    is not in OLED_OWN_WRAPPER_EXES.
    """
    if not wrapper_name and game_key not in OLED_OWN_WRAPPER_EXES:
        return None

    install_dir  = game["install_dir"]
    wrapper_name = wrapper_name or OLED_OWN_WRAPPER_EXES[game_key]
    wrapper_path = os.path.join(install_dir, wrapper_name)

    launcher = os.path.join(plut_dir, "bin",
                            "plutonium-launcher-win32.exe")
    plut_url = f"plutonium://play/{_plut_key(game_key)}"
    from steam_common import prelaunch_prefix

    script = (
        "#!/bin/bash\n"
        f"export STEAM_COMPAT_DATA_PATH=\"{_dq(compatdata_path)}\"\n"
        f"export STEAM_COMPAT_CLIENT_INSTALL_PATH=\"{_dq(steam_root)}\"\n"
        # Silent Plutonium update; the launcher never updates on its own.
        f"{prelaunch_prefix(game_key)}true\n"
        f"\"{_dq(proton_path)}\" run \"{_dq(launcher)}\" \"{plut_url}\" &\n"
        "PROTON_PID=$!\n"
        "sleep 8\n"
        "while kill -0 $PROTON_PID 2>/dev/null || "
        f"pgrep -fa wineserver | grep -qF \"{_dq(compatdata_path)}\"; do\n"
        "  sleep 3\n"
        "done\n"
    )

    write_script(wrapper_path, script)

    return wrapper_path


def _write_offline_wrapper(game: dict, game_key: str, source: str = "steam") -> str | None:
    """
    Write the game's offline script (e.g. t6plut_lan_mp.sh) next to the game files,
    OLED and LCD alike. DeckOps_Offline.exe runs it through launcher_offline.sh (the
    Steam launch menu's Plutonium Offline entry opens that launcher). It hands over to
    assets/LAN/lan_server.sh, which starts the game from the offline Plutonium copy
    in LAN_PREFIX; WaW MP, BO1 MP, BO2 MP and MW3 MP also host a local server with bots.

    The player name stays as +name "..." so game_config's rename finds it.
    Returns the script path, or None for a key without offline play.
    """
    if game_key not in OLED_LAN_WRAPPER_NAMES:
        return None
    install_dir = game["install_dir"]
    path = os.path.join(install_dir, OLED_LAN_WRAPPER_NAMES[game_key])

    # Older versions shared one name for SP/MP pairs; drop the stale file
    old_name = _OLD_OLED_LAN_WRAPPER_NAMES.get(game_key)
    if old_name and old_name != OLED_LAN_WRAPPER_NAMES[game_key]:
        old_path = os.path.join(install_dir, old_name)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                _log.debug("old wrapper cleanup failed", exc_info=True)

    import config as _cfg
    from steam_common import wine_path_for_prefix
    name_arg = bash_name_arg(_cfg.get_player_name())
    mode = _plut_key(game_key)
    game_dir = plut_game_dir(game_key, install_dir, source)
    q = lambda v: "'" + v.replace("'", "'\\''") + "'"
    run = (f'exec {q(os.path.join(os.path.dirname(_OLED_HERE), "assets", "LAN", "lan_server.sh"))} '
           f'{mode} {q(wine_path_for_prefix(game_dir, LAN_PREFIX))} {name_arg}')
    lines = ["#!/bin/bash", f"# DeckOps offline {mode} (plutonium._write_offline_wrapper)"]
    if game_key in LAN_KEYS:
        run += f" {LAN_PORT} {q(wine_path_for_prefix(game_dir, LAN_SERVER_PREFIX))} " + _LAN_SERVER_ARGS[mode]
        if mode == "t6mp":
            # Map / Mode lists only the maps this install has
            lines += [f"G={q(game_dir)}", "M=$(cd \"$G/zone/all\" && ls mp_*.ff | sed 's/\\.ff$//' | paste -sd,)"]
            run = 'CLIENT_ARGS="+set fs_game mods/deckops_offline" ' + run
        elif mode in ("t4mp", "t5mp", "iw5mp"):
            # WaW keeps maps in zone/<language>/ next to their _load files, BO1 in zone/Common/,
            # MW3 in zone/<language>/ and zone/dlc/
            lines += [f"G={q(game_dir)}", "M=$(cd \"$G/zone\" && ls */mp_*.ff | sed 's|.*/||; s/\\.ff$//' | grep -v '_load$' | sort -u | paste -sd,)"]
            if mode == "t5mp":
                # BO1 crashes in FS_Startup switching to the server's mod mid-join: start in it
                run = 'CLIENT_ARGS="+set fs_game mods/mp_deckops_offline" ' + run
    write_script(path, "\n".join(lines + [run]) + "\n")
    return path


def _is_script(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def _restore_original_exe(game: dict, game_key: str):
    """
    Undo the old exe replacement: put <exe>.bak back over our wrapper.
    If Steam already restored the real exe (e.g. Verify), drop the stale .bak.
    """
    exe_path = os.path.join(game["install_dir"], GAME_META[game_key][2])
    bak_path = exe_path + ".bak"
    if not os.path.exists(bak_path):
        return
    if not os.path.exists(exe_path) or _is_script(exe_path):
        os.replace(bak_path, exe_path)
    else:
        os.remove(bak_path)


def _setup_steam_menu(game: dict, game_key: str, steam_root: str,
                      proton_path: str, compatdata_path: str, plut_dir: str,
                      lan_wrapper_path: str, prog) -> str | None:
    """
    OLED Steam games: keep the original exe and add Plutonium Online /
    Offline entries to the game's Steam launch menu, each pointing at its
    own wrapper file. Steam runs the chosen file through its Proton, the
    same way it ran the old replaced exe. Returns the online wrapper path.
    """
    online_name, offline_name = STEAM_MENU_EXES[game_key]
    _restore_original_exe(game, game_key)

    online_path = _write_oled_own_wrapper(
        game, game_key, steam_root, proton_path,
        compatdata_path, plut_dir, wrapper_name=online_name,
    )
    if lan_wrapper_path and os.path.exists(lan_wrapper_path):
        write_offline_menu_file(os.path.join(game["install_dir"], offline_name))
    add_steam_menu(game, game_key, steam_root, prog)

    # Fallback when Steam refreshes appinfo.vdf and our menu is gone: prelaunch.sh
    # swaps an untagged stock exe for its online file (VANILLA_TAG marks a stock pick)
    from steam_common import prelaunch_prefix
    from wrapper import set_launch_options, clear_launch_options
    appid = GAME_META[game_key][0]
    pairs = " ".join(f"{GAME_META[k][2]}={STEAM_MENU_EXES[k][0]}"
                     for k in STEAM_MENU_EXES if GAME_META[k][0] == appid)
    clear_launch_options(steam_root, appid)
    if not set_launch_options(steam_root, appid, f'DECKOPS_MENU="{pairs}" {prelaunch_prefix("menu")}%command%'):
        prog(92, "  ⚠ Launch option fallback not set: launch the game once from Steam, then set it up again")
    return online_path


def write_offline_menu_file(path: str):
    """The Steam menu's Plutonium Offline file (OLED and LCD): prelaunch.sh / cache_cleanup.py
    run it on the host, and it opens the DeckOps: Plutonium Offline launcher."""
    from shortcut import launcher_run_url
    write_script(path, f'#!/bin/bash\nexec steam "{launcher_run_url()}"\n')


def add_steam_menu(game: dict, game_key: str, steam_root: str, prog):
    """
    Rebuild our entries for this appid in STEAM_MENU_EXES order (Online
    before Offline, WaW SP before MP) whatever order keys were installed in.
    Sibling keys count once their online file is on disk. Shared with LCD,
    which writes its own files under the same names.
    """
    appid = GAME_META[game_key][0]
    keys = [k for k in STEAM_MENU_EXES if GAME_META[k][0] == appid and (
            k == game_key or os.path.exists(os.path.join(game["install_dir"], STEAM_MENU_EXES[k][0])))]
    entries = []
    for k in keys:
        on, off = STEAM_MENU_LABELS.get(k, _DEFAULT_MENU_LABELS)
        entries.append({"executable": STEAM_MENU_EXES[k][0], "description": on})
        # Offline only with Offline Mode added: its file is there exactly then
        if os.path.exists(os.path.join(game["install_dir"], STEAM_MENU_EXES[k][1])):
            entries.append({"executable": STEAM_MENU_EXES[k][1], "description": off})

    from steam_appinfo import add_launch_entries, remove_launch_entries, VANILLA_TAG
    # Stock entries: hidden on the tiles in HIDE_STOCK_APPIDS (the stock exe
    # can't run the modded client); otherwise tagged so prelaunch.sh can tell
    # a stock pick from a lost menu.
    stock = {"hide": True} if appid in HIDE_STOCK_APPIDS else {"tag": VANILLA_TAG}
    entries += [{"executable": GAME_META[k][2], **stock} for k in keys]

    # Host entries other games keep on this tile go out and back in below, so
    # the order stays Campaign first, then Multiplayer / Zombies.
    host_keys = [k for k, h in HOST_MENU.items() if h[0] == appid]
    remove_launch_entries(steam_root, appid, [n for k in keys for n in STEAM_MENU_EXES[k]]
                          + [STEAM_MENU_EXES[k][0] for k in host_keys])
    added = add_launch_entries(steam_root, appid, entries)
    prog(92, "  ✓ Launch menu: " + " / ".join(e["description"] for e in entries if "description" in e) if added
             else "  ⚠ Launch menu not added yet, will retry next time Steam is closed")
    for k in host_keys:
        try:
            add_host_menu({"install_dir": game["install_dir"]}, k, steam_root, lambda m: prog(92, m))
        except Exception as ex:
            _log.warning("host tile menu skipped for %s: %s", k, ex)
    try:
        add_host_menu(game, game_key, steam_root, lambda m: prog(92, m))
    except Exception as ex:
        _log.warning("host tile menu skipped for %s: %s", game_key, ex)


# MW3, BO1 and BO2 have their SP and MP / ZM on separate Steam appids. On OLED /
# Other the SP tile's launch menu also gets the Plutonium MP / ZM entries, so one
# tile (the "host") covers the whole game. The MP / ZM tiles are left as they are;
# hide them in Steam if you don't want them.
#   key -> (host appid, host key, menu label)
# Online only: the Offline entry's file is run through prelaunch.sh, which the
# host tile's launch option doesn't include. The online wrapper scripts export
# their own prefix, so they run from any tile.
HOST_MENU = {
    "t5mp":  (42700,  "t5sp",  STEAM_MENU_LABELS["t5mp"][0]),
    "t6mp":  (202970, "t6sp",  STEAM_MENU_LABELS["t6mp"][0]),
    "t6zm":  (202970, "t6sp",  STEAM_MENU_LABELS["t6zm"][0]),
}
# MW3 is not here: its SP tile (42680) launches the campaign straight away, and
# the main menu's Multiplayer button / the MP tile (42690) cover multiplayer.

# Host entries that used to exist and must go (appid -> executables).
RETIRED_HOST_MENUS = {
    42680: ["iw5plutmp.exe"],
}

# Tiles whose stock entries are hidden from the picker: WaW and BO1 already
# have a Plutonium entry for everything the stock exes would start, and the
# MW3 MP tile (42690) only has the Plutonium entry, so it starts that directly.
HIDE_STOCK_APPIDS = {10090, 42700, 42690}

# Steam's own SP entry on a host tile, relabelled to match: host key ->
# (stock exe, label). BO1's SP is the Plutonium "Campaign & Zombies" entry.
HOST_STOCK_LABELS = {
    "t6sp":  ("t6sp.exe",  "Campaign"),
}


def add_host_menu(game: dict, game_key: str, steam_root: str, log=lambda m: None,
                  installed: dict = None) -> bool:
    """Add game_key's online Plutonium entry to its host SP tile's launch menu
    (see HOST_MENU). OLED / Other, Steam games only. The entry is a file name
    relative to the host's install folder, so it is only added when the host
    game is in the same folder as the MP / ZM files. Returns True if added."""
    host = HOST_MENU.get(game_key)
    if not host:
        return False
    import config as _cfg
    if _cfg.is_lcd():
        return False
    appid, host_key, label = host
    online = STEAM_MENU_EXES[game_key][0]
    d = game.get("install_dir", "")
    if not d or not os.path.isfile(os.path.join(d, online)):
        return False

    if installed is None:
        from detect_games import parse_library_folders, find_installed_games
        installed = find_installed_games(parse_library_folders(steam_root), steam_root)
    host_dir = (installed.get(host_key) or {}).get("install_dir", "")
    if not host_dir or os.path.realpath(host_dir) != os.path.realpath(d):
        log(f"  ⚠ {label}: {host_key} is not in the same folder, SP tile menu skipped")
        return False

    from steam_appinfo import add_launch_entries
    entries = [{"executable": online, "description": label}]
    if host_key in HOST_STOCK_LABELS:
        stock_exe, stock_label = HOST_STOCK_LABELS[host_key]
        entries.append({"executable": stock_exe, "label": stock_label})
    if add_launch_entries(steam_root, appid, entries):
        log(f"  ✓ {label} added to the {host_key} tile ({appid})")
        return True
    log(f"  ⚠ {label} not added to {appid} yet, will retry next time Steam is closed")
    return False


def ensure_host_menus(steam_root: str, log=lambda m: None) -> list:
    """add_host_menu for every set-up Plutonium Steam game that has a host
    tile. Safe to repeat (entries are matched by executable). Steam must be
    closed. Returns the keys that got an entry."""
    import config as _cfg
    if _cfg.is_lcd():
        return []
    setup = _cfg.get_setup_games()
    keys = [k for k in HOST_MENU if setup.get(k, {}).get("client") == "plutonium"
            and setup[k].get("source", "steam") == "steam"]
    if not keys:
        return []
    from detect_games import parse_library_folders, find_installed_games
    installed = find_installed_games(parse_library_folders(steam_root), steam_root)
    return [k for k in keys if k in installed
            and add_host_menu(installed[k], k, steam_root, log, installed)]


def remove_retired_host_menus(steam_root: str, log=lambda m: None):
    """Take out host entries DeckOps no longer adds (RETIRED_HOST_MENUS), and
    forget the saved MW3 label so Steam's own entry is left as it was. Steam
    must be closed."""
    from steam_appinfo import remove_launch_entries, forget_launch_entries
    for appid, exes in RETIRED_HOST_MENUS.items():
        # Only claim a change when appinfo.vdf actually had the entries: the
        # call returns False when the app is not cached or they are already gone.
        if remove_launch_entries(steam_root, appid, exes):
            log(f"  Removed old menu entries from {appid}: {', '.join(exes)}")
    forget_launch_entries(42680, ["iw5sp.exe"])


def refresh_steam_menus(steam_root: str, log=lambda m: None) -> list:
    """Rebuild the Plutonium launch menu (entries and labels) of every set-up
    Plutonium Steam game, then each one's host tile entry (add_steam_menu does
    both). Picks up label changes without reinstalling. OLED / Other only.
    Steam must be closed. Returns the keys refreshed."""
    import config as _cfg
    if _cfg.is_lcd():
        return []
    try:
        remove_retired_host_menus(steam_root, log)
    except Exception as ex:
        _log.warning("retired host menus not removed: %s", ex)
    setup = _cfg.get_setup_games()
    keys = [k for k in STEAM_MENU_EXES if setup.get(k, {}).get("client") == "plutonium"
            and setup[k].get("source", "steam") == "steam"]
    if not keys:
        return []
    from detect_games import parse_library_folders, find_installed_games
    installed = find_installed_games(parse_library_folders(steam_root), steam_root)
    done = []
    for k in keys:
        if k not in installed:
            continue
        try:
            add_steam_menu(installed[k], k, steam_root, lambda pct, msg: log(msg))
            done.append(k)
        except Exception as ex:
            _log.warning("launch menu refresh failed for %s: %s", k, ex)
    return done


def remove_offline_files(game: dict, game_key: str):
    """Offline Mode off: the game's -lan script and its Steam menu Offline file go,
    so add_steam_menu leaves the Offline entry out (same names on OLED and LCD)."""
    d = game.get("install_dir", "")
    # MW3 MP and MW3 DS share one offline script and one server config. While the
    # other key still has its offline setup, its failing or removed setup must not
    # take the shared files away.
    own_script = OLED_LAN_WRAPPER_NAMES.get(game_key)
    shared_in_use = False
    if own_script:
        import config as _cfg
        setup = _cfg.get_setup_games()
        shared_in_use = any(
            k != game_key and name == own_script and isinstance(setup.get(k), dict)
            and setup[k].get("lan_wrapper_path")
            for k, name in OLED_LAN_WRAPPER_NAMES.items())
    for n in (own_script, STEAM_MENU_EXES.get(game_key, ("", ""))[1]):
        if n and n == own_script and shared_in_use:
            continue
        if n and d and os.path.isfile(os.path.join(d, n)):
            os.remove(os.path.join(d, n))
    # MW3's offline server config (setup_lan); the .dsr recipes may be the user's own
    if game_key in ("iw5mp", "iw5mp_ds") and d and not shared_in_use:
        for sub in ("downgrade", ""):
            cfg_path = os.path.join(d, sub, "admin", "deckops_lan.cfg")
            if os.path.isfile(cfg_path):
                os.remove(cfg_path)


# ── metadata ──────────────────────────────────────────────────────────────────

def _write_metadata(install_dir: str, data: dict):
    from steam_common import write_json
    write_json(os.path.join(install_dir, METADATA_FILE), data)


def cleanup_plut_from_non_plut_prefixes(steam_root: str, on_progress=None):
    """
    Remove leaked Plutonium directories from prefixes that aren't Plutonium games.

    Old versions ran _copy_plut_to_prefix() with a blanket storage/ copy into
    every managed prefix. Non-Plutonium appids (CoD4, MW2, MW3 SP, BO2 Campaign)
    should never have Plutonium data.

    Returns the number of prefixes cleaned.
    """
    from ge_proton import MANAGED_APPIDS

    def prog(msg):
        if on_progress:
            on_progress(msg)

    plut_appids = {str(meta[0]) for meta in GAME_META.values()}
    cleaned = 0

    for appid_str in MANAGED_APPIDS:
        if appid_str in plut_appids:
            continue
        plut_dir = _plut_dir_in_compatdata(steam_root, int(appid_str))
        if not os.path.isdir(plut_dir):
            continue
        # Same rule as the uninstaller: only a folder DeckOps linked to its
        # Plutonium copy is ours. This used to delete any Plutonium folder
        # here, storage/ (stats, mods, custom maps) included.
        if not _linked_by_deckops(plut_dir):
            _log.info("Plutonium in appid %s prefix is not DeckOps's -- left alone", appid_str)
            continue
        storage = os.path.join(plut_dir, "storage")
        if os.path.isdir(storage) and _has_real_files(storage):
            # Real files in storage/ may be the only copy of someone's data
            prog(f"  Removing leaked Plutonium data from appid {appid_str} (keeping storage/)...")
            for name in os.listdir(plut_dir):
                if name == "storage":
                    continue
                p = os.path.join(plut_dir, name)
                if os.path.islink(p) or not os.path.isdir(p):
                    os.remove(p)
                else:
                    shutil.rmtree(p, ignore_errors=True)
        else:
            prog(f"  Removing leaked Plutonium data from appid {appid_str}...")
            shutil.rmtree(plut_dir, ignore_errors=True)
        cleaned += 1
        prog(f"  ✓ Cleaned Plutonium from appid {appid_str}")

    return cleaned


def _linked_by_deckops(plut_dir: str) -> bool:
    """True if bin/, launcher/ or games/ is a link to DeckOps's Plutonium
    master (or the old shared copy it used to use)."""
    owners = [get_dedicated_plut_dir(), SHARED_PLUT_DIR]
    for sub in _PLUT_SHARED_SUBDIRS:
        p = os.path.join(plut_dir, sub)
        if os.path.islink(p) and any(
                os.path.realpath(p) == os.path.realpath(os.path.join(o, sub)) for o in owners):
            return True
    return False


def _has_real_files(path: str) -> bool:
    for dp, _, files in os.walk(path):
        if any(not os.path.islink(os.path.join(dp, f)) for f in files):
            return True
    return False


# ── public API ────────────────────────────────────────────────────────────────

# ---
# Offline: local LAN server (WaW MP, BO1 MP, BO2 MP, MW3 MP), run by assets/LAN/lan_server.sh
#
# Plain -lan play can't create a private match without a Plutonium account, so
# these games host a hidden local dedicated server and join it. Opt-in
# (config offline_mode). Server and game each run from their own Plutonium copy
# under LAN_ROOT: Plutonium's files are hard links to the online copy, settings
# and stats are real LAN files, user mods come in as one link per mod, and our
# offline-only files are real files. Every folder is real, so nothing is ever
# written through a link into an online folder: online launches never see our files.

LAN_ROOT = os.path.expanduser("~/.local/share/deckops/lan")
LAN_ASSETS = os.path.join(os.path.dirname(_OLED_HERE), "assets", "LAN", "offline")
# The offline Rejoin Match buttons connect to this port (WaW mod.ff, BO1 mod.ff Play,
# BO2 raw/deckops_bo2_menu.iwd, MW3 ui_mp/main.lua)
LAN_PORT = 4986
LAN_KEYS = ("t4mp", "t5mp", "t6mp", "iw5mp", "iw5mp_ds")
# Offline games run in their own prefix. The server runs in the Offline launcher's
# prefix: the game's runtime launch (waitforexitandrun) waits for every wineserver
# on its own prefix, so the two can't share one.
LAN_PREFIX = os.path.expanduser("~/.local/share/deckops/lan_prefix")
LAN_SERVER_PREFIX = os.path.expanduser("~/.local/share/deckops/launcher_prefix")
_LAN_SERVER_ARGS = {
    # $M: the maps in zone/all, listed by the offline script
    "t6mp": '+set fs_game mods/deckops_offline +set dcac_maps "$M" +exec deckops_lan_mp.cfg +map_rotate',
    "iw5mp": '+set dcac_maps "$M" +set sv_config deckops_lan.cfg +start_map_rotate',
    # BO1 runs +exec (storage/t5/deckops_lan_mp.cfg); Bot Warfare and the class script load as loose
    # scripts, the class menu (dcac_list) comes from the mod's mod.ff
    "t5mp": '+set fs_game mods/mp_deckops_offline +set dcac_maps "$M" +exec deckops_lan_mp.cfg +map_rotate',
    # T4 ignores +exec on a server, and Bot Warfare makes its dvars at runtime: all here.
    # WaW MP mods must be named mp_*. sv_maxclients 13: 12 bots + the player (Bot Count).
    # sv_floodprotect 0: every click in the Map / Mode and Bot Warfare menus is a menu response; fast clicks get dropped.
    "t4mp": ('+set fs_game mods/mp_deckops_offline +set dcac_maps "$M" +set sv_maxclients 13 '
             '+set sv_hostname "DeckOps Offline" +set sv_floodprotect 0 +set logfile 1 +set g_inactivity 0 +set bots_main 1 '
             '+set bots_main_firstIsHost 0 +set bots_manage_fill 10 +set bots_manage_fill_mode 0 +set bots_manage_fill_kick 1 '
             '+set bots_skill 2 +map_rotate'),
}

# Third-party server files, downloaded at install: their repos show no license,
# so they are never redistributed. Pinned to a commit, or a release checked by digest.
_T6_BW = ("https://raw.githubusercontent.com/ineedbots/t6_bot_warfare/"
          "cafee7d585160b046dc928f90798a611683e6cd1/scripts/mp/")
# Bot Warfare T5: its 2021 release predates Plutonium, so the files come from the repo, each checked
_T5_BW = ("https://raw.githubusercontent.com/ineedbots/t5_bot_warfare/"
          "93b82d7720fce4059b272d6a907747bef55e5c6a/")
_T6_GS = ("https://raw.githubusercontent.com/xerxes-at/T6ServerConfigs/"
          "6d3b313acab5b5e21d61277bd1d2f3b234dc4918/localappdata/Plutonium/storage/t6/gamesettings/")
LAN_DOWNLOADS = {
    "t6mp": [(f"storage/t6/raw/scripts/mp/{f}", _T6_BW + f, None)
             for f in ("bots.gsc", "bots_adapter_pt6.gsc")]
          + [(f"storage/t6/gamesettings/{g}.cfg", _T6_GS + g + ".cfg", None)
             for g in "conf ctf dem dm dom gun hq koth oic oneflag sas sd shrp tdm".split()],
    # Release zip; only z_svr_bots.iwd is taken from it
    "iw5mp": [("storage/iw5/z_svr_bots.iwd",
               "https://github.com/ineedbots/iw5_bot_warfare/releases/download/v2.3.0/iw5bw230.zip",
               "sha256:733692aaeda839635d86f9752803b04ecfbd0057f9475661b8cadf084543cfaa")],
    "t5mp": [(f"storage/t5/{f}", _T5_BW + f, "sha256:" + d) for f, d in (
        ("maps/mp/bots/_bot_loadout.gsc", "c2ef1cb1ff64edf92c746b88c8ad84082242025490e4d371c899e5cc8ac08424"),
        ("maps/mp/bots/_bot_script.gsc", "5430775257fde3cdce9359a1745950f280e497dd7762c8f203e1c490a784dff4"),
        ("maps/mp/bots/_bot_utility.gsc", "1c9420df6ddc5dcdd2ad8e252b756f68f39dd97a8fd97c10f5d111266afb3d06"),
        ("maps/mp/gametypes/_bot.gsc", "58447f23bda78d8c9a1e65b27bab1b97973acdc3fe4a1aa161299e946721d17c"),
        ("scripts/mp/bots_adapter_pt5.gsc", "3f0662c5386074288c7ed6f8e01e1206b7f850be474acb822d33a37de6993096"))],
    "t4mp": [("storage/t4/mods/mp_deckops_offline/z_svr_bots.iwd",
              "https://github.com/ineedbots/t4_bot_warfare/releases/download/v2.3.0/t4bw230.zip",
              "sha256:266a183f6557f58c14808ef455ba009e5d763295f47695731978b88aa45c2d21")],
}

# Plutonium's online data per title (demonware): pub is linked from the online copy,
# user (stats) stays a real LAN folder so offline stats never reach online
# 18301 is a guess for BO1: Plutonium's folder had it next to BO2's and MW3's after a
# login, with MP and SP data (Deck, 2026-10-08). WaW had none.
_LAN_DW_TITLE = {"t6": "18397", "iw5": "18409", "t5": "18301"}
# Never taken from the online folder: the LAN copy keeps its own settings, logs
# and stats, and user mods come in one link per mod
_LAN_TOP_OWN = ("console.log", "crashdumps", "storage")
_LAN_STORE_OWN = ("players", "mods", "usermaps", "main", "demo", "imgui.ini")
_LAN_USER_DIRS = ("mods", "usermaps")
# Games run offline with a mod (fs_game) read their settings from players/<mod>/, not
# players/: BO2 MP fell back to 800x600 there (Deck, 2026-10-08). game_config mirrors there.
LAN_MOD_PLAYERS = {"t6mp": os.path.join("mods", "deckops_offline"),
                   "t5mp": os.path.join("mods", "mp_deckops_offline")}
# WaW MP starts from players/profiles/$$$/ and reads profiles/$$$/<server mod>/ once it joins
# the server (1920x1200 only in one of them, PC 2026-10-09): game_config mirrors to both.
LAN_MOD_PROFILE = {"t4mp": os.path.join("mods", "mp_deckops_offline")}
# Server files older builds shipped. BO2's server needs no mod.ff since the real menus (client only, 2026-10-14).
_LAN_STALE = {"t6mp": ("storage/t6/mods/deckops_offline/mod.ff",)}
# Real (not hard-linked) files under LAN_ROOT past this mean something got copied
# by mistake (mods are only ever linked); our own files are a few MB
_LAN_DISK_WARN = 100 * 1024 * 1024


def _lan_link(src: str, dst: str):
    """dst becomes a hard link to src's real file, a copy if they're on different drives."""
    src = os.path.realpath(src)
    if os.path.lexists(dst) and not os.path.islink(dst):
        if os.path.samefile(src, dst):
            return
        # A copy from an earlier build (other drive): copy2 keeps the mtime, so a
        # file of the same size and time is current. Recopying all of Plutonium on
        # every offline launch could run past lan_server.sh's 60 s budget.
        try:
            a, b = os.stat(src), os.stat(dst)
            if a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns:
                return
        except OSError:
            pass
    # Never remove the working offline file before a potentially failing copy.
    # The temp name is private to this refresh (two launchers may overlap).
    fd, pending = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".deckops-lan-")
    os.close(fd)
    try:
        os.unlink(pending)
        try:
            os.link(src, pending)
        except OSError:
            shutil.copy2(src, pending)
        os.replace(pending, dst)
    finally:
        if os.path.lexists(pending):
            os.unlink(pending)


def _lan_mirror(src: str, dst: str, rel: str, keep, made=None):
    """Hard-link src's files (links in src followed) into real folders under dst.
    keep: paths relative to the LAN root that are never taken from src.
    made: a set that collects the paths (relative to the LAN root) that were linked."""
    os.makedirs(dst, exist_ok=True)
    for n in os.listdir(src) if os.path.isdir(src) else ():
        r = f"{rel}/{n}" if rel else n
        if r in keep:
            continue
        s, d = os.path.join(src, n), os.path.join(dst, n)
        if os.path.isdir(s):
            _lan_mirror(s, d, r, keep, made)
        elif os.path.isfile(s):
            _lan_link(s, d)
            if made is not None:
                made.add(r)


def _lan_tree(root: str, user_plut: str, master: str, store: str, files: dict, client: bool):
    """Build one LAN Plutonium copy at root from the game's online folder user_plut.
    files {rel: source} are our offline files (source None = a download, kept as is)."""
    # Links from the last build go (old layout, removed mods); real files stay
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            if os.path.islink(os.path.join(dp, n)):
                os.remove(os.path.join(dp, n))
        dns[:] = [n for n in dns if os.path.isdir(os.path.join(dp, n))]
    st = f"storage/{store}"
    keep = set(files) | set(_LAN_TOP_OWN) | {f"{st}/{n}" for n in _LAN_STORE_OWN}
    manifest = os.path.join(root, ".deckops_mirror")
    try:
        with open(manifest, encoding="utf-8") as f:
            before = {line.rstrip("\n") for line in f if line.strip()}
    except OSError:
        before = set()
    made = set()
    _lan_mirror(user_plut, root, "", keep, made)
    _lan_mirror(os.path.join(user_plut, st), os.path.join(root, st), st, keep, made)
    # Plutonium files an update dropped: their links from the last build are stale
    for rel in sorted(before - made - keep):
        if os.path.isabs(rel) or ".." in rel.split("/"):
            continue
        stale = os.path.join(root, rel)
        if os.path.isfile(stale) and not os.path.islink(stale):
            try:
                os.remove(stale)
            except OSError:
                _log.debug("could not remove stale offline file %s", stale, exc_info=True)
    try:
        from steam_common import atomic_write
        atomic_write(manifest, "".join(f"{rel}\n" for rel in sorted(made)).encode("utf-8"))
    except OSError:
        _log.debug("could not save the offline mirror list", exc_info=True)
    os.makedirs(os.path.join(root, st, "players"), exist_ok=True)
    if client:
        for d in _LAN_USER_DIRS:
            src = os.path.join(user_plut, st, d)
            ours = {r.split("/")[3] for r in files if r.startswith(f"{st}/{d}/")}
            os.makedirs(os.path.join(root, st, d), exist_ok=True)
            for n in os.listdir(src) if os.path.isdir(src) else ():
                p = os.path.join(root, st, d, n)
                if n not in ours and not os.path.lexists(p):
                    os.symlink(os.path.join(src, n), p)
        title = _LAN_DW_TITLE.get(store)
        pub = title and next((p for p in (os.path.join(b, "storage", "demonware", title, "pub")
                                          for b in (master, user_plut)) if os.path.isdir(p)), None)
        dw = os.path.join(root, "storage", "demonware", title or "")
        # The game makes an empty pub when it has none; a link makes it see the online data
        if pub:
            os.makedirs(os.path.join(dw, "user"), exist_ok=True)
            if os.path.isdir(os.path.join(dw, "pub")) and not os.path.islink(os.path.join(dw, "pub")):
                shutil.rmtree(os.path.join(dw, "pub"))
            if not os.path.lexists(os.path.join(dw, "pub")):
                os.symlink(pub, os.path.join(dw, "pub"))
    top = os.path.realpath(LAN_ROOT) + os.sep
    for rel, src in files.items():
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.realpath(os.path.dirname(dst)).startswith(top):
            raise RuntimeError(f"offline file would land outside {LAN_ROOT}: {dst}")
        if src and os.path.lexists(dst):
            os.remove(dst)
        if src:
            shutil.copyfile(src, dst)


def _lan_assets(mode: str, role: str) -> dict:
    base = os.path.join(LAN_ASSETS, mode, role)
    return {os.path.relpath(os.path.join(dp, f), base): os.path.join(dp, f)
            for dp, _, fns in os.walk(base) for f in fns}


def _lan_download(dst: str, url: str, digest, prog):
    if not url.endswith(".zip"):
        download(url, dst, digest=digest)
        return
    import tempfile, zipfile
    with tempfile.TemporaryDirectory() as tmp:
        z = os.path.join(tmp, "dl.zip")
        download(url, z, digest=digest)
        with zipfile.ZipFile(z) as zf:
            name = next((n for n in zf.namelist() if os.path.basename(n) == os.path.basename(dst)), None)
            if name is None:    # a bare StopIteration reached the user as an empty message
                raise RuntimeError(f"{os.path.basename(dst)} is not in the downloaded archive")
            # Never write dst itself: setup_lan skips a download whenever dst exists,
            # so a copy cut off mid-way would be kept as if it were complete.
            part = dst + ".part"
            try:
                with zf.open(name) as fi, open(part, "wb") as fo:
                    shutil.copyfileobj(fi, fo)
                os.replace(part, dst)
            finally:
                if os.path.exists(part):
                    os.remove(part)


def _lan_disk_check(prog):
    sizes = []
    for dp, _, fns in os.walk(LAN_ROOT):
        for f in fns:
            st = os.lstat(os.path.join(dp, f))
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                sizes.append((st.st_size, os.path.relpath(os.path.join(dp, f), LAN_ROOT)))
    total = sum(n for n, _ in sizes)
    if total > _LAN_DISK_WARN:
        big = ", ".join(f"{p} ({n >> 20} MB)" for n, p in sorted(sizes, reverse=True)[:5])
        _log.warning("offline LAN folders hold %d MB of copies: %s", total >> 20, big)
        prog(f"  ⚠ Offline folders use {total >> 20} MB of copied files: {big}")


def lan_dirs(mode: str):
    return os.path.join(LAN_ROOT, mode, "server"), os.path.join(LAN_ROOT, mode, "client")


@contextlib.contextmanager
def _lan_refresh_lock(mode: str):
    """One writer per mode: simultaneous game launches share the same offline trees."""
    os.makedirs(os.path.join(LAN_ROOT, mode), exist_ok=True)
    with open(os.path.join(LAN_ROOT, mode, ".refresh.lock"), "a+") as lock:
        try:
            import fcntl
        except ImportError:  # Windows developer tests do not have flock
            yield
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def refresh_lan(mode: str, prog=lambda m: None):
    """Rebuild the server and game copies from the online folder saved at install.
    Run at every offline launch (prelaunch.py lan <mode>) so mods added or Plutonium
    files updated since the install show up offline too."""
    with _lan_refresh_lock(mode):
        with open(os.path.join(LAN_ROOT, mode, "source.json")) as f:
            user_plut = json.load(f)["user_plut"]
        if not isinstance(user_plut, str) or not os.path.isdir(user_plut):
            raise FileNotFoundError(f"offline Plutonium source is missing: {user_plut!r}")
        store = _STORAGE_SUBDIRS[mode]
        server, client = lan_dirs(mode)
        master = get_dedicated_plut_dir()
        # Real files from older builds stay in the copies: drop the ones we stopped shipping
        for rel in _LAN_STALE.get(mode, ()):
            if os.path.isfile(os.path.join(server, rel)):
                os.remove(os.path.join(server, rel))
        if mode in LAN_KEYS:
            dl = {rel: None for rel, _, _ in LAN_DOWNLOADS.get(mode, ())}
            _lan_tree(server, user_plut, master, store, {**dl, **_lan_assets(mode, "server")}, False)
        _lan_tree(client, user_plut, master, store, _lan_assets(mode, "client"), True)
        _lan_disk_check(prog)
        prog(f"  ✓ Offline server folders ready ({mode})")


def setup_lan(game: dict, game_key: str, user_plut: str, source: str, prog):
    """Set up offline play for a Plutonium game when Offline Mode is on: the offline
    prefixes and the game's offline Plutonium copy, plus the local server for
    t4mp / t5mp / t6mp / iw5mp. user_plut is the game's online Plutonium folder (OLED: its
    compatdata prefix, LCD: Heroic's). MW3 DS shares MW3's offline folders.

    Returns True when offline play is set up, False when it failed, None when
    Offline Mode is off or the game has none. A failure used to be swallowed:
    the callers still wrote the offline script, the Plutonium Offline menu
    entry and lan_wrapper_path, so the game showed up in the Offline launcher
    and failed at launch."""
    import config as _cfg
    if game_key not in OLED_LAN_WRAPPER_NAMES or not _cfg.get_offline_mode():
        return None
    mode = _plut_key(game_key)
    source_json = os.path.join(LAN_ROOT, mode, "source.json")
    previous_source = None
    source_changed = False
    try:
        _lan_prefixes(prog)
        # Offline BO2 stats and settings landed online through the old shared players/ (2026-10-08)
        leak = os.path.join(user_plut, "storage", "t6", "players", "mods", "deckops_offline")
        if mode == "t6mp" and os.path.isdir(leak) and not os.path.islink(leak):
            shutil.rmtree(leak)
        os.makedirs(os.path.join(LAN_ROOT, mode), exist_ok=True)
        from steam_common import write_json
        # MW3 DS shares MW3 MP's folders: MP's online folder is the one they are built
        # from (its mods and maps), so DS never takes the file over from MP.
        if game_key == mode or not os.path.isfile(source_json):
            if os.path.isfile(source_json):
                with open(source_json, "rb") as f:
                    previous_source = f.read()
            write_json(source_json, {"user_plut": user_plut})
            source_changed = True
        # Folders first, so every download lands in a real folder of ours
        refresh_lan(mode, prog)
        server, _ = lan_dirs(mode)
        for rel, url, digest in LAN_DOWNLOADS.get(mode, ()) if mode in LAN_KEYS else ():
            dst = os.path.join(server, rel)
            if not os.path.isfile(dst):
                prog(f"  Downloading {os.path.basename(rel)}...")
                _lan_download(dst, url, digest, prog)
        # MW3 server config + mode recipes live in the game's admin/ (sv_config).
        # Never overwrite: the user's own dedicated server may keep files there.
        admin = os.path.join(LAN_ASSETS, mode, "admin")
        if os.path.isdir(admin):
            dst_dir = os.path.join(plut_game_dir(game_key, game["install_dir"], source), "admin")
            os.makedirs(dst_dir, exist_ok=True)
            for n in os.listdir(admin):
                # deckops_lan.cfg is ours alone, so it is kept up to date (bot count 2026-10-09)
                if n == "deckops_lan.cfg" or not os.path.exists(os.path.join(dst_dir, n)):
                    shutil.copyfile(os.path.join(admin, n), os.path.join(dst_dir, n))
    except Exception as ex:
        # Do not leave a failed game install pointing future prelaunch refreshes
        # at a different (possibly incomplete) online source.
        if source_changed:
            try:
                if previous_source is None:
                    os.remove(source_json)
                else:
                    write_script(source_json, previous_source, mode=0o644)
            except OSError:
                _log.error("could not restore offline source %s", source_json, exc_info=True)
        _log.warning("offline setup failed for %s", game_key, exc_info=True)
        prog(f"  ⚠ Offline setup failed: {ex or type(ex).__name__} -- Offline Mode is off "
             f"for this game; run Set Up again to retry")
        return False
    return True


def _lan_prefixes(prog):
    """The offline game prefix and the Offline launcher's prefix (the server's), set up
    with GE-Proton's default prefix like a first install of a game prefix."""
    import config as _cfg
    from ge_proton import ensure_prefix_deps, _prefix_ready
    from steam_common import default_steam_root
    from wrapper import get_proton_path
    # The resolved root, not the classic path: GE-Proton (and the prefix it
    # builds from) lives under the Steam that is actually installed.
    steam_root = default_steam_root()
    for p in (LAN_PREFIX, LAN_SERVER_PREFIX):
        # Wine's own files, not drive_c/windows: ensure_prefix_deps copies DLLs
        # (creating drive_c/windows/system32) before Proton finalizes, so a
        # failed or timed-out finalize used to look ready for good.
        if not _prefix_ready(os.path.join(p, "pfx")):
            os.makedirs(p, exist_ok=True)
            if not ensure_prefix_deps(_cfg.get_ge_proton_version(), p, prog,
                                      proton_path=get_proton_path(steam_root), steam_root=steam_root):
                raise RuntimeError(f"could not set up the offline prefix {os.path.basename(p)}")


def lan_runtime() -> tuple:
    """(Steam Linux Runtime dir the GE-Proton asks for, Steam libraries outside home).
    The game runs inside that runtime like Steam's own launches; the container only
    sees home unless the other libraries are mounted. ("", ...) if not found."""
    import re
    import config as _cfg
    from detect_games import parse_library_folders, find_steam_root
    sr = _cfg.load().get("steam_root") or find_steam_root()
    libs = [os.path.join(l, "steamapps") for l in parse_library_folders(sr)]
    home = os.path.realpath(os.path.expanduser("~")) + os.sep
    mounts = ":".join(l for l in libs if not os.path.realpath(l).startswith(home))
    try:
        with open(os.path.join(sr, "compatibilitytools.d", _cfg.get_ge_proton_version(), "toolmanifest.vdf")) as f:
            tool = re.search(r'"require_tool_appid"\s*"(\d+)"', f.read()).group(1)
    except (OSError, AttributeError, TypeError):
        return "", mounts
    for lib in libs:
        try:
            with open(os.path.join(lib, f"appmanifest_{tool}.acf")) as f:
                d = os.path.join(lib, "common", re.search(r'"installdir"\s*"([^"]+)"', f.read()).group(1))
        except (OSError, AttributeError):
            continue
        if os.path.isfile(os.path.join(d, "_v2-entry-point")):
            return d, mounts
    return "", mounts


def _plut_compat(game_key: str, game: dict, source: str, steam_root: str) -> str:
    """The game's Proton prefix, picked like the install and update flows do."""
    import config as _cfg
    from wrapper import find_compatdata
    appid = str(GAME_META[game_key][0])
    compat = ""
    if source == "own":
        compat = game.get("compatdata_path", "")
        if not compat and not _cfg.is_lcd():
            from shortcut import own_plut_prefix
            compat = own_plut_prefix(game_key, game)
    if not compat:
        compat = find_compatdata(steam_root, appid, game_install_dir=game.get("install_dir"))
    if not compat and game.get("install_dir"):
        compat = os.path.join(os.path.dirname(os.path.dirname(game["install_dir"])), "compatdata", appid)
    return compat


def apply_offline_mode(on: bool, steam_root: str, prog=lambda pct, msg: None):
    """Add or remove Offline Mode for every set-up Plutonium game: rerun the
    Plutonium setup (the offline pieces follow config offline_mode), then the
    offline launcher. No Plutonium bootstrapper run. Steam must be closed:
    launch menu entries and shortcuts.vdf get rewritten."""
    import config as _cfg
    from detect_games import find_all_games
    from wrapper import get_proton_path
    import shortcut
    _cfg.set_offline_mode(on)  # setup_lan reads the setting during each game install
    launcher_removed = True
    touched = {}  # Add: games whose offline pieces may already be written
    try:
        if not on:
            remove_lan(lambda m: prog(5, m))
            launcher_removed = shortcut.remove_launcher_shortcut(lambda m: prog(8, m))
            for p in (LAN_SERVER_PREFIX, LAN_PREFIX):
                if os.path.isdir(p):
                    shutil.rmtree(p)
        setup = _cfg.get_setup_games()
        found = find_all_games(steam_root, preferred_sources=setup)
        configured = {k: e for k, e in setup.items()
                      if e.get("client") == "plutonium" and k in GAME_META}
        games = {k: found[k] for k, entry in configured.items()
                 if k in found and found[k].get("source", "steam") == entry.get("source", "steam")}
        if on and not configured:
            raise RuntimeError("No configured Plutonium games were found. Set up a game before adding Offline Mode.")
        if not on:
            # The offline folders and prefixes are gone already, so their scripts, menu
            # files and config entries go now, not only inside each game's reinstall below:
            # a reinstall that fails (master copy missing, disk full) used to leave an
            # offline script and an Offline menu entry that fail with "folders missing".
            with _cfg.transaction():
                cfg = _cfg.load()
                for entry in cfg.get("setup_games", {}).values():
                    if isinstance(entry, dict):
                        entry.pop("lan_wrapper_path", None)
                _cfg.save(cfg)
            for k, g in games.items():
                try:
                    remove_offline_files(g, k)
                except OSError:
                    _log.warning("could not remove the offline files of %s", k, exc_info=True)
        errors = [f"{k}: configured {entry.get('source', 'steam')} game folder not found"
                  for k, entry in configured.items() if k not in games]
        if on and errors:
            # Before any game is touched: a missing folder found after earlier games got
            # their offline scripts and menu entries left "(Offline)" entries with no launcher.
            raise RuntimeError("Offline Mode was not set up for all games: " + "; ".join(errors))
        if launcher_removed is False:
            errors.append("the Offline launcher shortcut could not be removed from Steam")
        proton = get_proton_path(steam_root)
        step = 80 // max(len(games), 1)
        for i, (k, g) in enumerate(games.items()):
            src = configured[k].get("source", "steam")
            base = 10 + i * step
            prog(base, f"{g.get('name', k)}...")
            if on:
                touched[k] = g
            try:
                install_plutonium(g, k, steam_root, proton, _plut_compat(k, g, src, steam_root),
                                  on_progress=lambda pct, msg, b=base: prog(b + pct * step // 100, msg),
                                  installed_games=games, source=src)
            except Exception as ex:
                _log.warning("offline mode: %s failed", k, exc_info=True)
                prog(base, f"  ⚠ {k}: {ex}")
                errors.append(f"{k}: {ex}")
        if errors:
            raise RuntimeError("Offline Mode was not set up for all games: " + "; ".join(errors))
        if on:
            # Configs last: game_config also fills the offline copies now that they exist.
            from game_config import apply_game_configs
            apply_game_configs(selected_keys=list(games), installed_games=games, steam_root=steam_root,
                               deck_model=_cfg.get_deck_model() or "oled", on_progress=lambda m: prog(93, m))
            if shortcut.create_launcher_shortcut(on_progress=lambda m: prog(95, m)) is False:
                raise RuntimeError("the Offline launcher shortcut could not be added to Steam")
    except Exception:
        # A partial add or removal must never leave the library claiming the
        # offline launcher is Ready. A later Add can repair leftover files.
        _cfg.set_offline_mode(False)
        if touched:
            _rollback_offline_games(touched, steam_root, prog)
        raise


def _rollback_offline_games(games: dict, steam_root: str, prog):
    """A failed Add: take the offline scripts, menu files and lan_wrapper_path of the
    games already set up back out, and rerun their setup with Offline Mode off so the
    Steam launch menus lose their Offline entries again. Best effort: the original
    error is what gets reported."""
    import config as _cfg
    from wrapper import get_proton_path
    setup = _cfg.get_setup_games()
    try:
        if any(isinstance(setup.get(k), dict) and setup[k].get("lan_wrapper_path") for k in games):
            with _cfg.transaction():
                cfg = _cfg.load()
                for k in games:
                    entry = cfg.get("setup_games", {}).get(k)
                    if isinstance(entry, dict):
                        entry.pop("lan_wrapper_path", None)
                _cfg.save(cfg)
    except Exception:
        _log.warning("offline mode rollback: config entries not removed", exc_info=True)
    for k, g in games.items():
        try:
            remove_offline_files(g, k)
        except OSError:
            _log.warning("offline mode rollback: could not remove the offline files of %s", k, exc_info=True)
    try:
        proton = get_proton_path(steam_root)
    except Exception:
        _log.warning("offline mode rollback: no Proton path", exc_info=True)
        return
    for k, g in games.items():
        src = (setup.get(k) or {}).get("source", "steam")
        try:
            install_plutonium(g, k, steam_root, proton, _plut_compat(k, g, src, steam_root),
                              installed_games=games, source=src)
        except Exception:
            _log.warning("offline mode rollback: %s setup not rerun", k, exc_info=True)
            prog(95, f"  ⚠ {k}: Offline entries may remain, remove Offline Mode to clean up")


def remove_lan(prog=lambda m: None):
    """Offline Mode removed. rmtree never follows the links into online folders."""
    for mode in sorted({_plut_key(k) for k in LAN_KEYS}):
        # lan_server.sh names each server unit deckops-lan-<mode>-<random>, so the
        # stop goes by pattern (matches loaded units) and by the unit it recorded.
        units = [f"deckops-lan-{mode}-*"]
        try:
            with open(os.path.join(LAN_ROOT, mode, "server.unit")) as f:
                recorded = f.read().strip()
            if recorded:
                units.append(recorded)
        except (OSError, ValueError):
            pass
        for unit in units:
            # No systemctl (non-systemd distro) or a hung user bus must not stop removal
            try:
                subprocess.run(["systemctl", "--user", "stop", unit],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            except (OSError, subprocess.SubprocessError) as ex:
                _log.warning("could not stop %s: %s", unit, ex)
    if os.path.isdir(LAN_ROOT):
        shutil.rmtree(LAN_ROOT)
    prog("  ✓ Offline server folders removed")


def install_plutonium(game: dict, game_key: str, steam_root: str,
                      proton_path: str, compatdata_path: str,
                      on_progress=None, installed_games: dict = None,
                      source: str = "steam"):
    """
    Full install flow for a single Plutonium game.

    LCD Decks are dispatched to plutonium_lcd.py early
    in this function. Everything after the dispatch handles
    OLED and Other (non-Deck SteamOS) devices.

    Assumes the user has already logged in and closed Plutonium via
    launch_bootstrapper(), and is_plutonium_ready() has returned True.

    game               , entry from detect_games.find_installed_games()
    game_key           , one of: t4sp, t4mp, t5sp, t5mp, t6zm, t6mp, iw5mp
    steam_root         , path to Steam root
    proton_path        , path to the proton executable
    compatdata_path    , path to this game's compatdata prefix
    on_progress        , optional callback(percent: int, status: str)
    """
    def prog(pct, msg):
        if on_progress:
            on_progress(pct, msg)

    # Fall back to single-game dict if caller doesn't supply full installed_games.
    # Supplying the full dict is preferred so sibling keys get correct paths.
    if installed_games is None:
        installed_games = {game_key: game}

    # ── LCD dispatch ──────────────────────────────────────────────────────
    # LCD Decks use Heroic Games Launcher with a single shared Wine prefix
    # for all Plutonium games. Everything from downloading the bootstrapper
    # to writing per-game shortcuts lives in plutonium_lcd.py.
    # OLED and Other devices continue below with the direct-launch path.
    import config as _cfg_lcd
    if _cfg_lcd.is_lcd():
        from plutonium_lcd import install_plutonium_lcd, get_shared_plut_dir
        # Offline runs from its own prefix and Plutonium copy, not through Heroic
        # (Heroic is for online). First, so the offline script finds its prefix.
        lan_ok = setup_lan(game, game_key, get_shared_plut_dir(), source, lambda msg: prog(5, msg))
        return install_plutonium_lcd(
            game, game_key, installed_games,
            on_progress=on_progress,
            steam_root=steam_root,
            proton_path=proton_path,
            compatdata_path=compatdata_path,
            source=source,
            lan_ok=lan_ok,
        )

    src_plut_dir  = get_dedicated_plut_dir()

    # The caller's find_compatdata() can come back empty (None, or "" for an own copy
    # with no prefix yet); a relative path would put Plutonium in the working folder
    if not compatdata_path:
        compatdata_path = _plut_compat(game_key, game, source, steam_root)
    if not compatdata_path:
        raise RuntimeError(f"No Proton prefix found for {game.get('name', game_key)}; "
                           "start the game once from Steam, then set it up again.")

    # OLED / Other past this point (LCD returns early above).
    # Plutonium files go into Steam's compatdata prefix for the game,
    # because OLED/Other launches the game through a bash wrapper that
    # Steam runs inside that same prefix. Use the compatdata_path passed
    # by the caller for correct SD card handling via find_compatdata().
    dest_plut_dir = os.path.join(
        compatdata_path, "pfx", "drive_c", "users", "steamuser",
        "AppData", "Local", "Plutonium",
    )

    prog(10, f"Copying Plutonium into prefix for {game['name']}...")
    _copy_plut_to_prefix(
        src_plut_dir, dest_plut_dir, game_key=game_key,
        on_progress=lambda msg: prog(40, msg),
    )
    _link_to_master(src_plut_dir, dest_plut_dir, lambda msg: prog(45, msg))

    prog(60, "Writing game path to config.json...")
    # Pass all keys that share this appid so config has all paths for this prefix.
    # installed_games must contain ALL installed games, not just the current one,
    # so sibling keys (e.g. t4sp + t4mp share appid 10090) get their paths written too.
    appid = GAME_META[game_key][0]
    keys_for_appid = [k for k, v in GAME_META.items() if v[0] == appid]
    _write_config(dest_plut_dir, keys_for_appid, installed_games)

    # Apply DeckOps game configs (resolution, FOV, sensitivity, name)
    # into the per-game prefix BEFORE copying to the launcher prefix.
    # This way the tuned configs propagate automatically to every prefix
    # that receives a copy of storage/ — no separate mirror step needed.
    _apply_launcher_game_configs(dest_plut_dir, game_key,
                                  on_progress=lambda msg: prog(62, msg))

    # Install DeckOps client-side menu mod (e.g. mainlobby.lua for T6 MP).
    # OLED / Other only -- LCD handles its own setup in plutonium_lcd.py.
    prog(65, "Installing menu mod...")
    _install_menu_mod(dest_plut_dir, game_key,
                      on_progress=lambda msg: prog(67, msg))
    lan_ok = setup_lan(game, game_key, dest_plut_dir, source, lambda msg: prog(68, msg))
    import config as _cfg_off
    # A failed offline setup counts as no offline for this game: no offline
    # script, menu entry or lan_wrapper_path pointing at a broken setup
    offline = _cfg_off.get_offline_mode() and lan_ok is not False
    if not offline:
        remove_offline_files(game, game_key)

    # ── OLED / Other only past this point ──────────────────────────────
    # LCD dispatched to plutonium_lcd.py at the top of this
    # function. OLED games get a Steam-side bash wrapper + metadata.
    wrapper_path = None
    if source == "own":
        # OLED own games: standalone wrapper
        prog(80, "Writing launcher wrapper...")
        wrapper_path = _write_oled_own_wrapper(
            game, game_key, steam_root, proton_path,
            compatdata_path, dest_plut_dir,
        )

        prog(85, "Writing offline LAN wrapper...")
        # Sidecar -lan script alongside the game files. Does not touch the
        # online wrapper above. DeckOps_Offline.exe reads lan_wrapper_path
        # from config and bash-runs this script for offline play.
        lan_wrapper_path = _write_offline_wrapper(game, game_key, "own") if offline else None

        prog(95, "Saving metadata...")
        _write_metadata(game["install_dir"], {
            "game_key":    game_key,
            "plut_dir":    dest_plut_dir,
        })
        import config as _cfg_own
        _cfg_own.mark_game_setup(
            game_key, "plutonium", source="own",
            wrapper_path=wrapper_path,
            lan_wrapper_path=lan_wrapper_path,
        )
    else:
        wrapper_exe = os.path.join(game["install_dir"], GAME_META[game_key][2])
        prog(85, "Writing offline LAN wrapper...")
        # Sidecar -lan script alongside the game files. DeckOps_Offline.exe
        # reads lan_wrapper_path from config and bash-runs this script for
        # offline play.
        lan_wrapper_path = _write_offline_wrapper(game, game_key, "steam") if offline else None

        if game_key in STEAM_MENU_EXES:
            prog(88, "Adding Plutonium to the Steam launch menu...")
            wrapper_exe = _setup_steam_menu(
                game, game_key, steam_root, proton_path, compatdata_path,
                dest_plut_dir, lan_wrapper_path, prog,
            ) or wrapper_exe

        prog(95, "Saving metadata...")
        _write_metadata(game["install_dir"], {
            "game_key":    game_key,
            "plut_dir":    dest_plut_dir,
            "wrapper_exe": wrapper_exe,
        })
        import config as _cfg_steam
        _cfg_steam.mark_game_setup(
            game_key, "plutonium", source="steam",
            lan_wrapper_path=lan_wrapper_path,
        )

    prog(100, f"Plutonium ready for {game['name']}!")
    return wrapper_path
