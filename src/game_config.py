"""
game_config.py - DeckOps game config writer

Copies pre-built config files from assets/configs/ into the correct
destination paths for each game. The source folder is determined by the
user's device selection:

    OLED  -> assets/configs/OLED/
    LCD   -> assets/configs/LCD/
    Other -> assets/configs/Other/<resolution>/   (e.g. Other/1920x1200)

Overwrites whatever is currently there, except with "My Own" resolution
(Other/own): the display settings already in a live config are kept.

After copying, replaces the default player name ("Player") with the user's
chosen name from deckops.json in any config that has `seta name "Player"`.

LCD users receive MW1, MW2, MW3 SP, Ghosts, and AW configs.
OLED and Other users receive MW1, MW2, WaW, BO1, MW3, BO2, Ghosts, and AW configs.

LCD Plutonium games are additionally mirrored into the Heroic shared default
prefix (~/Games/Heroic/Prefixes/default), because LCD online play routes
through Heroic's Wine environment and never touches Steam's per-game
compatdata prefix. The compatdata write is preserved for future offline mode.
"""

import os
import shutil

from log import get_logger

_log = get_logger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────────────

from steam_common import PROJECT_ROOT
CONFIGS_DIR  = os.path.join(PROJECT_ROOT, "assets", "configs")

# Heroic shared default prefix — used by LCD online play for all Plutonium
# games. Configs must be mirrored here so LCD online launches see them.
HEROIC_DEFAULT_PFX = os.path.expanduser("~/Games/Heroic/Prefixes/default")


# ── Config map ────────────────────────────────────────────────────────────────
#
# Each entry maps a game key to a list of (asset_subpath, dest_resolver) pairs.
#
# asset_subpath  — path relative to assets/configs/<MODEL>/
# dest_resolver  — callable(install_dir, steam_root) -> absolute destination directory
#
# Files are copied with their original filename preserved.
#
# Dest convention:
#   None  → resolved at runtime from install_dir via _dest_from_install()
#   str   → fixed path (used for Plutonium games whose configs live in prefix AppData)
#
# Only Plutonium games store configs inside the Wine prefix AppData.
# All other games (CoD4x, IW4x, vanilla Steam) store configs in the
# game install directory.

def _compatdata(steam_root, appid, game_install_dir=None):
    """
    Return the compatdata path for a given appid.

    Uses wrapper.find_compatdata which searches all Steam library folders
    (internal + SD card) and picks the prefix in the same library as the
    game install directory. Falls back to steam_root if not found.
    """
    try:
        from wrapper import find_compatdata
        result = find_compatdata(steam_root, str(appid),
                                  game_install_dir=game_install_dir)
        if result:
            return result
    except Exception:
        _log.debug("config dest lookup failed", exc_info=True)

    # Fallback — prefix may not exist yet (os.makedirs will create it later)
    return os.path.join(steam_root, "steamapps", "compatdata", str(appid))


def _pfx_local(steam_root, appid, *parts, game_install_dir=None):
    """Return a path inside pfx/drive_c/users/steamuser/AppData/Local/ for an appid."""
    return os.path.join(
        _compatdata(steam_root, appid, game_install_dir=game_install_dir),
        "pfx", "drive_c", "users", "steamuser", "AppData", "Local",
        *parts
    )


def _own_compatdata(key, game):
    """
    Prefix of an own-copy (non-Steam) game, or None for Steam games. Uses the
    compatdata_path enrich_own_games stored; games from a plain scan
    (find_all_games: name change, Offline Mode switch, updates) don't have
    it, and their configs used to go into the Steam appid's prefix instead
    of the shortcut's. Worked out the same way enrich_own_games does.
    """
    if game.get("source") != "own":
        return None
    if game.get("compatdata_path"):
        return game["compatdata_path"]
    try:
        from shortcut import OWN_SHORTCUTS, own_plut_prefix, own_shortcut_appid
        from steam_common import nvme_compatdata
        if key not in OWN_SHORTCUTS or not game.get("exe_path") or not game.get("install_dir"):
            return None
        if key == "t4mp":                       # WaW MP shares WaW SP's prefix
            return own_plut_prefix(key, game)
        return nvme_compatdata(own_shortcut_appid(key, game))
    except Exception:
        _log.debug("own prefix lookup failed for %s", key, exc_info=True)
        return None


def _own_pfx_dest(fixed_dest, own_pfx):
    """fixed_dest (built from the Steam appid's prefix) moved into own_pfx."""
    pfx_parts = fixed_dest.split(os.sep + "pfx" + os.sep, 1)
    if own_pfx and len(pfx_parts) == 2:
        return os.path.join(own_pfx, "pfx", pfx_parts[1])
    return fixed_dest


def _cod4_appdata_profiles(steam_root, install_dir, own_pfx):
    """CoD4x's AppData profiles folder: in the own shortcut's prefix for an
    own copy (it was always the Steam 7940 prefix), else Steam's."""
    if own_pfx:
        return os.path.join(own_pfx, "pfx", "drive_c", "users", "steamuser", "AppData", "Local",
                            "CallofDuty4MW", "players", "profiles")
    return _pfx_local(steam_root, 7940, "CallofDuty4MW", "players", "profiles",
                      game_install_dir=install_dir)


def _heroic_pfx_local(*parts):
    """
    Return a path inside the Heroic shared default prefix's
    drive_c/users/steamuser/AppData/Local/ tree.

    Mirrors _pfx_local but rooted at HEROIC_DEFAULT_PFX. No appid or library
    resolution — the Heroic prefix is a single shared location for all LCD
    Plutonium games.
    """
    return os.path.join(
        HEROIC_DEFAULT_PFX,
        "pfx", "drive_c", "users", "steamuser", "AppData", "Local",
        *parts
    )


def _heroic_mirror_path(compatdata_dest):
    """
    Given a destination path inside a compatdata pfx AppData/Local tree,
    return the equivalent path inside the Heroic shared default prefix.

    Returns None if the input does not contain '/AppData/Local/' (i.e. not
    a prefix-AppData path — install-dir destinations have no Heroic mirror).
    """
    marker = os.path.join("AppData", "Local") + os.sep
    idx = compatdata_dest.find(marker)
    if idx == -1:
        return None
    tail = compatdata_dest[idx + len(marker):]
    return _heroic_pfx_local(*tail.split(os.sep))


def _offline_mirror_paths(compatdata_dest, key):
    """
    The same folder inside the game's offline Plutonium copy (Offline Mode),
    so offline play gets the resolution, FOV, sensitivity and player name too.
    compatdata_dest is a folder under .../AppData/Local/Plutonium/. Returns []
    when Offline Mode is off or the copy isn't built yet (it is never created here).
    """
    marker = os.path.join("AppData", "Local", "Plutonium") + os.sep
    idx = compatdata_dest.find(marker)
    if idx == -1:
        return []
    import config as cfg
    from plutonium import LAN_ROOT, LAN_MOD_PLAYERS, LAN_MOD_PROFILE, OLED_LAN_WRAPPER_NAMES, _plut_key
    if not cfg.get_offline_mode() or key not in OLED_LAN_WRAPPER_NAMES:
        return []
    mode = _plut_key(key)
    client = os.path.join(LAN_ROOT, mode, "client")
    if not os.path.isdir(client):
        return []
    dest = os.path.join(client, *compatdata_dest[idx + len(marker):].split(os.sep))
    # Offline with a mod: the game reads its settings from players/<mod>/
    if mode in LAN_MOD_PLAYERS and os.path.basename(dest) == "players":
        return [os.path.join(dest, LAN_MOD_PLAYERS[mode])]
    if mode in LAN_MOD_PROFILE:
        return [dest, os.path.join(dest, LAN_MOD_PROFILE[mode])]
    return [dest]


# The config map is built inside apply_game_configs so steam_root is available.
# Keys that are absent for a given model are simply not included.

_LCD_KEYS  = {"cod4sp", "cod4mp", "iw4sp", "iw4mp", "iw5sp", "iw5mp", "iw5mp_ds", "t4sp", "t4mp", "t5sp", "t5mp", "t6zm", "t6mp", "t6sp", "t7", "t7x", "iw6sp", "iw6mp", "s1sp", "s1mp"}
_OLED_KEYS = {"cod4sp", "cod4mp", "iw4sp", "iw4mp", "t4sp", "t4mp", "t5sp", "t5mp", "iw5sp", "iw5mp", "iw5mp_ds", "t6zm", "t6mp", "t6sp", "t7", "t7x", "iw6sp", "iw6mp", "s1sp", "s1mp"}


def _build_config_map(steam_root, installed_games=None):
    """
    Returns a dict mapping game_key -> list of (asset_subpath, dest_dir) pairs.
    dest_dir will be created if it does not exist.

    installed_games is used to resolve the correct compatdata for SD card
    installs (pass game_install_dir so find_compatdata picks the right library).
    """
    # Helper to get install_dir for a game key
    def _game_dir(key):
        if installed_games and key in installed_games:
            return installed_games[key].get("install_dir")
        return None

    return {
        # ── MW1 SP (IW3SP-MOD) ────────────────────────────────────────────────
        "cod4sp": [
            ("MW1/iw3sp_mod_config.cfg", None),
        ],

        # ── MW1 MP (CoD4x) ────────────────────────────────────────────────────
        # Config lives in install_dir/players/profiles/Player/ (same as SP).
        # CoD4x copies this to AppData/Local/CallofDuty4MW/players/profiles/
        # on first launch. active.txt in the profiles/ dir controls the
        # displayed player name — written by _write_cod4_active_txt().
        # NOT in prefix AppData — only Plutonium uses prefix paths.
        "cod4mp": [
            ("MW1/config_mp.cfg", None),
        ],

        # ── MW2 SP ────────────────────────────────────────────────────────────
        "iw4sp": [
            ("MW2/config.cfg", None),
            ("MW2/iw4x_sp_config.cfg", None),  # read by iw4x-sp (own installs) instead of config.cfg
        ],

        # ── MW2 MP (iw4x) ─────────────────────────────────────────────────────
        "iw4mp": [
            ("MW2/iw4x_config.cfg", None),
        ],

        # ── WaW SP + MP (Plutonium t4) ────────────────────────────────────────
        "t4sp": [
            (
                "WaW/config.cfg",
                _pfx_local(
                    steam_root, 10090,
                    "Plutonium", "storage", "t4", "players", "profiles", "$$$",
                    game_install_dir=_game_dir("t4sp"),
                ),
            ),
        ],
        "t4mp": [
            (
                "WaW/config_mp.cfg",
                _pfx_local(
                    steam_root, 10090,
                    "Plutonium", "storage", "t4", "players", "profiles", "$$$",
                    game_install_dir=_game_dir("t4mp"),
                ),
            ),
        ],

        # ── BO1 SP (Plutonium t5, appid 42700) ────────────────────────────────
        "t5sp": [
            (
                "BO1/config.cfg",
                _pfx_local(
                    steam_root, 42700,
                    "Plutonium", "storage", "t5", "players",
                    game_install_dir=_game_dir("t5sp"),
                ),
            ),
        ],

        # ── BO1 MP (Plutonium t5, appid 42710) ────────────────────────────────
        "t5mp": [
            (
                "BO1/config_mp.cfg",
                _pfx_local(
                    steam_root, 42710,
                    "Plutonium", "storage", "t5", "players",
                    game_install_dir=_game_dir("t5mp"),
                ),
            ),
        ],

        # ── MW3 SP (via Steam, appid 42690) ───────────────────────────────────
        "iw5sp": [
            ("MW3/config.cfg", None),
        ],

        # ── MW3 MP (Plutonium iw5, appid 42690) ───────────────────────────────
        "iw5mp": [
            (
                "MW3/config_mp.cfg",
                _pfx_local(
                    steam_root, 42690,
                    "Plutonium", "storage", "iw5", "players",
                    game_install_dir=_game_dir("iw5mp"),
                ),
            ),
        ],

        # ── MW3 DS (Plutonium iw5 via dedicated server, appid 42750) ──────────
        "iw5mp_ds": [
            (
                "MW3/config_mp.cfg",
                _pfx_local(
                    steam_root, 42750,
                    "Plutonium", "storage", "iw5", "players",
                    game_install_dir=_game_dir("iw5mp_ds"),
                ),
            ),
        ],

        # ── BO2 ZM (Plutonium t6, appid 212910) ───────────────────────────────
        "t6zm": [
            (
                "BO2/plutonium_zm.cfg",
                _pfx_local(
                    steam_root, 212910,
                    "Plutonium", "storage", "t6", "players",
                    game_install_dir=_game_dir("t6zm"),
                ),
            ),
        ],

        # ── BO2 MP (Plutonium t6, appid 202990) ───────────────────────────────
        "t6mp": [
            (
                "BO2/plutonium_mp.cfg",
                _pfx_local(
                    steam_root, 202990,
                    "Plutonium", "storage", "t6", "players",
                    game_install_dir=_game_dir("t6mp"),
                ),
            ),
            # Without it the game auto-detects 800x600 and turns the gamepad off
            (
                "BO2/hardware_mp.chp",
                _pfx_local(
                    steam_root, 202990,
                    "Plutonium", "storage", "t6", "players",
                    game_install_dir=_game_dir("t6mp"),
                ),
            ),
        ],

        # ── BO2 SP (T6SP-MOD, uses stock BO2 install dir) ────────────────────
        # Binary settings profile — dropped into install_dir/players/.
        "t6sp": [
            ("BO2/user_sp.cgp", None),
            ("BO2/hardware.chp", None),
        ],

        # ── Ghosts SP (AlterWare iw6, appid 209160) ──────────────────────────
        "iw6sp": [
            ("Ghosts/config.cfg", None),
        ],

        # ── Ghosts MP (AlterWare iw6, appid 209170) ──────────────────────────
        "iw6mp": [
            ("Ghosts/config_mp.cfg", None),
        ],

        # ── AW SP (AlterWare s1, appid 209650) ───────────────────────────────
        "s1sp": [
            ("AW/config.cfg", None),
        ],

        # ── AW MP (AlterWare s1, appid 209660) ───────────────────────────────
        "s1mp": [
            ("AW/config_mp.cfg", None),
        ],

        # ── BO3 (CleanOps t7) ────────────────────────────────────────────────
        # CleanOps is a DLL overlay — config.ini lives in the stock BO3
        # players/ directory, same as vanilla.
        "t7": [
            ("T7X/config.ini", None),
        ],

        # ── BO3 (T7X / AlterWare) ────────────────────────────────────────────
        # T7X stores its config.ini inside the t7x/players/ subdirectory
        # of the DeckOps-T7X sibling dir.
        "t7x": [
            ("T7X/config.ini", None),
        ],
    }


# ── Install-dir based dest resolvers ──────────────────────────────────────────

def _cod4_profile_name(player_name):
    """CoD4 profile folder name as the game finds it on disk. Wine drops
    trailing dots and spaces from Windows path names, so a Steam name like
    "casimiro." makes the game use profiles/casimiro."""
    # Cleaned first: a "/" in the name made a nested path, other characters
    # aren't allowed in Windows folder names
    from config import clean_player_name
    return clean_player_name(player_name).rstrip(". ") or "Player"


def _dest_from_install(game_key, install_dir, player_name=None):
    """
    For game keys whose destination is relative to install_dir,
    return the correct absolute destination directory.
    Returns None for keys that use a fixed compatdata path instead.
    """
    if game_key in ("cod4sp", "cod4mp"):
        return os.path.join(install_dir, "players", "profiles",
                            _cod4_profile_name(player_name))
    if game_key in ("iw4sp", "iw4mp"):
        return os.path.join(install_dir, "players")
    if game_key == "iw5sp":
        return os.path.join(install_dir, "players2")
    if game_key in ("iw6sp", "iw6mp", "s1sp", "s1mp"):
        return os.path.join(install_dir, "players2")
    if game_key == "t7":
        return os.path.join(install_dir, "players")
    if game_key == "t7x":
        return os.path.join(install_dir, "t7x", "players")
    if game_key == "t6sp":
        return os.path.join(install_dir, "players")
    return None


# ── Player name replacement ───────────────────────────────────────────────────

def _replace_player_name(filepath, player_name):
    """
    Replace 'seta name "<anything>"' with the user's chosen name in a config file.

    Uses a regex to match any current name value, not just the default
    "Player". This allows the Settings screen to rename the player after
    initial install without needing to re-deploy configs from scratch.

    Only modifies lines that match `seta name "..."` exactly — other cvars
    that happen to contain "name" are not affected.

    Does nothing if player_name is None or empty.
    """
    if not player_name:
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        import re as _re
        pattern = r'seta name ".*?"'
        from config import clean_player_name
        safe_name = clean_player_name(player_name)
        if not safe_name:
            return
        replacement = f'seta name "{safe_name}"'

        # A function, so the name is used literally: as a replacement
        # string, "\1" or "\g" in a name raised re.error (not caught here)
        new_content = _re.sub(pattern, lambda m: replacement, content)
        if new_content == content:
            return

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
    except (IOError, OSError):
        pass  # Non-fatal — config still works with current name


# ── "My Own" resolution ───────────────────────────────────────────────────────
# With "My Own" (assets Other/own) the user sets the resolution in-game, and
# the own assets have no resolution lines. A redeploy (reinstall, adding a
# game, switching Offline Mode) copied the asset over the live config, so the
# game fell back to its default resolution. The display settings already in
# the live file are carried over instead, and BO2's binary hardware/user
# profiles (which hold the resolution) are left as they are.
import re as _re

_OWN_DISPLAY_CFG = _re.compile(
    r'^\s*seta\s+(r_mode|r_customMode|r_aspectRatio\w*|r_customAspectRatio|'
    r'r_displayRefresh|r_refreshRate|r_displayMode|r_videomode|r_fullscreen\w*|'
    r'r_previousFullscreen|r_noborder|r_monitor|r_resolution|'
    r'vid_width|vid_height|vid_xpos|vid_ypos)\b', _re.IGNORECASE)
_OWN_DISPLAY_INI = _re.compile(
    r'^\s*(WindowSize|RefreshRate|FullScreenMode|Monitor|ResolutionPercent)\s*=',
    _re.IGNORECASE)
_OWN_KEEP_BINARY = (".chp", ".cgp")


def _display_lines(path):
    """{setting name (lowercase): line} of the display settings in a config."""
    pat = _OWN_DISPLAY_INI if path.lower().endswith(".ini") else _OWN_DISPLAY_CFG
    found = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f.read().splitlines():
            m = pat.match(line)
            if m:
                found[m.group(1).lower()] = line
    return found


def _deploy_config(src, dest, keep_display):
    """
    Copy a config asset to dest. keep_display (the "My Own" resolution) with
    an existing dest: the user's display settings stay -- binary BO2 profiles
    aren't replaced at all, and the display lines of a text config are put
    back after the copy. Returns "kept" when dest was left as it was.
    """
    if not (keep_display and os.path.isfile(dest)):
        shutil.copy2(src, dest)
        return "copied"
    if dest.lower().endswith(_OWN_KEEP_BINARY):
        return "kept"
    try:
        saved = _display_lines(dest)
    except (IOError, OSError):
        saved = {}
    shutil.copy2(src, dest)
    if not saved:
        return "copied"
    pat = _OWN_DISPLAY_INI if dest.lower().endswith(".ini") else _OWN_DISPLAY_CFG
    with open(dest, "r", encoding="utf-8", errors="replace", newline="") as f:
        content = f.read()
    nl = "\r\n" if "\r\n" in content else "\n"
    out, used = [], set()
    for line in content.splitlines():
        m = pat.match(line)
        key = m.group(1).lower() if m else None
        if key in saved:
            if key not in used:
                out.append(saved[key])
                used.add(key)
            continue
        out.append(line)
    out += [line for key, line in saved.items() if key not in used]
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(out) + nl)
    return "copied"


def _write_cod4_active_txt(profiles_dir, player_name):
    """
    Write (or overwrite) the active.txt file in a CoD4 profiles directory.

    CoD4 / CoD4x reads active.txt to determine the active profile name
    displayed in-game. Without this file, or if it still says "Player",
    the player name will show as "Player" regardless of the seta name
    cvar in config_mp.cfg.

    profiles_dir — the .../players/profiles/ directory (not the profile
                   subfolder itself).
    player_name  — the name to write into active.txt. Does nothing if
                   None or empty.
    """
    if not player_name:
        return

    try:
        os.makedirs(profiles_dir, exist_ok=True)
        active_path = os.path.join(profiles_dir, "active.txt")
        from config import clean_player_name
        safe_name = clean_player_name(player_name) or "Player"
        with open(active_path, "w", encoding="utf-8") as f:
            f.write(safe_name)
    except (IOError, OSError):
        pass  # Non-fatal


def _mark_iw3sp_cfg_initialized(profile_dir):
    """
    Pre-write IW3SP-MOD's first-run marker. Without it, the first SP launch
    on a profile rebuilds iw3sp_mod_config.cfg from the vanilla config.cfg
    (or defaults when there is none) and the deployed DeckOps settings are
    lost: 1024x768, 60 Hz, controller off. Text matches what the mod writes.
    """
    try:
        d = os.path.join(profile_dir, "iw3sp_mod_init_cfg")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "IW3SP_MOD_CONFIG_INIT_DONE"), "w", encoding="utf-8") as f:
            f.write("// INIT REALLOCATION THE SETTINGS FROM ORIGINAL 'config.cfg' to 'iw3sp_mod_config.cfg'")
    except (IOError, OSError):
        pass  # Non-fatal


# ── Public API ────────────────────────────────────────────────────────────────

def apply_game_configs(selected_keys, installed_games, steam_root,
                       deck_model, on_progress=None):
    """
    Copy pre-built config files into the correct destination for each
    selected game key, based on the user's deck model.

    After copying, replaces `seta name "Player"` with the user's chosen
    player name from deckops.json (if set).

    For LCD installs, Plutonium configs (those with a fixed prefix-AppData
    destination) are additionally mirrored into the Heroic shared default
    prefix at ~/Games/Heroic/Prefixes/default, since LCD online play routes
    through Heroic's Wine environment instead of Steam's per-game prefix.
    The Heroic mirror copy is best-effort and does not affect applied/failed
    counts — the compatdata write is the source of truth for those.

    selected_keys   — list of game keys the user selected to install
    installed_games — dict from detect_games.find_installed_games()
    steam_root      — path to Steam root
    deck_model      — 'oled', 'lcd', or 'other'
    on_progress     — optional callback(msg: str)

    Returns (applied, skipped, failed) counts.
    """
    prog = on_progress or (lambda *a: None)

    applied  = 0
    skipped  = 0
    failed   = 0

    # Read the player name once for the whole batch
    import config as cfg
    player_name = cfg.get_player_name()

    # ── Sibling key expansion ─────────────────────────────────────────────
    # MW2 SP (iw4sp) and MW3 SP (iw5sp) have config entries but aren't
    # tracked by detect_games / ALL_GAMES — they run through vanilla Steam.
    # When their MP sibling is selected, auto-include the SP key so its
    # display config also gets written.  The SP shares the same install_dir.
    _SIBLING_MAP = {
        "iw4mp": "iw4sp",      # MW2 MP  → MW2 SP
        "iw5mp": "iw5sp",      # MW3 MP  → MW3 SP
        "iw5mp_ds": "iw5sp",   # MW3 DS  → MW3 SP
        "iw6mp": "iw6sp",      # Ghosts MP → Ghosts SP
        "s1mp":  "s1sp",       # AW MP    → AW SP
    }
    expanded_keys = list(selected_keys)
    for mp_key, sp_key in _SIBLING_MAP.items():
        if mp_key in selected_keys and sp_key not in expanded_keys:
            expanded_keys.append(sp_key)
            # Re-use the MP game entry so the SP key has a valid install_dir
            if mp_key in installed_games and sp_key not in installed_games:
                installed_games[sp_key] = installed_games[mp_key]

    model_dir    = cfg.get_model_config_dir()
    keep_display = model_dir == "Other/own"     # "My Own": resolution set in-game
    allowed_keys = _LCD_KEYS if deck_model == "lcd" else _OLED_KEYS
    config_map  = _build_config_map(steam_root, installed_games)

    for key in expanded_keys:
        if key not in allowed_keys:
            continue
        if key not in config_map:
            prog(f"  - {key}: no config available yet, skipping")
            skipped += 1
            continue

        game       = installed_games.get(key, {})
        install_dir = game.get("install_dir", "")
        own_pfx     = _own_compatdata(key, game)

        for asset_subpath, fixed_dest in config_map[key]:
            src = os.path.join(CONFIGS_DIR, model_dir, asset_subpath)

            if not os.path.exists(src):
                prog(f"  - {key}: asset not found ({asset_subpath}), skipping")
                skipped += 1
                continue

            # Resolve destination directory
            if fixed_dest:
                # For "own" games, fixed_dest was built using the Steam appid-based
                # compatdata path. Swap the base out for the game's shortcut prefix
                # so configs land in the right prefix.
                fixed_dest = _own_pfx_dest(fixed_dest, own_pfx)
                dest_dir = fixed_dest
            else:
                if not install_dir:
                    prog(f"  - {key}: install_dir unknown, skipping")
                    skipped += 1
                    continue
                dest_dir = _dest_from_install(key, install_dir,
                                             player_name=player_name)
                if not dest_dir:
                    prog(f"  - {key}: could not resolve destination, skipping")
                    skipped += 1
                    continue

            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))

            try:
                if _deploy_config(src, dest, keep_display) == "kept":
                    prog(f"  = {key}: kept your {os.path.basename(src)} (My Own resolution)")
                else:
                    prog(f"  + {key}: {os.path.basename(src)} -> {dest_dir}")
                # Replace default player name with user's chosen name
                _replace_player_name(dest, player_name)
                applied += 1
                if os.path.basename(dest) == "iw3sp_mod_config.cfg":
                    _mark_iw3sp_cfg_initialized(dest_dir)

                # CoD4 / CoD4x uses active.txt in the profiles directory
                # to determine the displayed player name. Write it after
                # every config deploy so the profile name stays in sync.
                # Write to both the install dir AND the AppData prefix —
                # CoD4x creates its own active.txt on first launch from
                # the folder name, so pre-seeding the prefix ensures the
                # correct name is used even on the very first launch.
                if key in ("cod4sp", "cod4mp") and player_name:
                    _write_cod4_active_txt(os.path.dirname(dest_dir), player_name)
                    _write_cod4_active_txt(_cod4_appdata_profiles(steam_root, install_dir, own_pfx),
                                           player_name)

            except Exception as ex:
                prog(f"  - {key}: failed to copy {os.path.basename(src)}: {ex}")
                failed += 1
                continue

            # ── Heroic shared prefix mirror (LCD only) ────────────────────
            # LCD online play routes through Heroic's default prefix, which
            # never touches the per-game compatdata above. Mirror the same
            # config there so online launches see it. Best-effort: failures
            # here are logged but don't increment applied/failed.
            # OLED and Other devices skip this — they launch directly.
            if deck_model == "lcd" and fixed_dest:
                heroic_dest_dir = _heroic_mirror_path(dest_dir)
                if heroic_dest_dir:
                    try:
                        os.makedirs(heroic_dest_dir, exist_ok=True)
                        heroic_dest = os.path.join(
                            heroic_dest_dir, os.path.basename(src)
                        )
                        _deploy_config(src, heroic_dest, keep_display)
                        _replace_player_name(heroic_dest, player_name)
                        prog(f"  + {key}: also mirrored to HGL prefix "
                             f"-> {heroic_dest_dir}")
                    except Exception as ex:
                        prog(f"  ! {key}: HGL mirror failed "
                             f"({os.path.basename(src)}): {ex}")

            # ── Offline copy mirror (Offline Mode) ─────────────────────────
            # Offline play runs from DeckOps' own Plutonium copy (OLED and LCD),
            # so it gets the same configs.
            # Best-effort — does not affect applied/failed counts.
            if fixed_dest:
                for offline_dir in _offline_mirror_paths(dest_dir, key):
                    try:
                        os.makedirs(offline_dir, exist_ok=True)
                        offline_dest = os.path.join(
                            offline_dir, os.path.basename(src)
                        )
                        _deploy_config(src, offline_dest, keep_display)
                        _replace_player_name(offline_dest, player_name)
                        prog(f"  + {key}: also mirrored to the offline copy "
                             f"-> {offline_dir}")
                    except Exception as ex:
                        prog(f"  ! {key}: offline copy mirror failed "
                             f"({os.path.basename(src)}): {ex}")

    prog(f"Game configs: {applied} applied, {skipped} skipped, {failed} failed.")
    return applied, skipped, failed


def rename_player(player_name, steam_root, installed_games=None,
                  on_progress=None):
    """
    Update the player name in all deployed config files without re-copying
    from assets. Scans all setup game keys, resolves their config
    destinations, and applies _replace_player_name to each file found.

    Also updates the Heroic shared prefix mirror for LCD Plutonium configs.

    Returns the number of files updated.
    """
    import config as cfg

    prog = on_progress or (lambda *a: None)

    if not player_name:
        prog("No player name provided.")
        return 0

    setup_keys = list(cfg.get_setup_games().keys())
    if not setup_keys:
        prog("No games set up yet.")
        return 0

    config_map = _build_config_map(steam_root, installed_games)
    updated = 0

    for key in setup_keys:
        if key not in config_map:
            continue

        game = (installed_games or {}).get(key, {})
        install_dir = game.get("install_dir", "")
        own_pfx = _own_compatdata(key, game)

        for asset_subpath, fixed_dest in config_map[key]:
            # Resolve destination
            if fixed_dest:
                fixed_dest = _own_pfx_dest(fixed_dest, own_pfx)
                dest_dir = fixed_dest
            else:
                if not install_dir:
                    continue
                # For CoD4, we need to find the OLD profile folder first
                # (could be "Player" or a previous custom name) and rename it.
                if key in ("cod4sp", "cod4mp"):
                    profiles_dir = os.path.join(install_dir, "players", "profiles")
                    new_profile = _cod4_profile_name(player_name)
                    new_profile_dir = os.path.join(profiles_dir, new_profile)
                    # Find the existing profile folder — check active.txt first,
                    # then fall back to any single subfolder.
                    old_profile_dir = None
                    active_path = os.path.join(profiles_dir, "active.txt")
                    if os.path.exists(active_path):
                        try:
                            with open(active_path, "r", encoding="utf-8") as f:
                                old_name = f.read().strip()
                            candidate = os.path.join(profiles_dir, _cod4_profile_name(old_name))
                            if os.path.isdir(candidate):
                                old_profile_dir = candidate
                        except (IOError, OSError):
                            pass
                    if not old_profile_dir:
                        # Fall back: look for any existing profile subfolder
                        try:
                            subdirs = [
                                d for d in os.listdir(profiles_dir)
                                if os.path.isdir(os.path.join(profiles_dir, d))
                            ]
                            if len(subdirs) == 1:
                                old_profile_dir = os.path.join(profiles_dir, subdirs[0])
                        except (IOError, OSError):
                            pass

                    if old_profile_dir and old_profile_dir != new_profile_dir:
                        try:
                            os.rename(old_profile_dir, new_profile_dir)
                            prog(f"  + {key}: renamed profile folder "
                                 f"{os.path.basename(old_profile_dir)} -> {new_profile}")
                        except (IOError, OSError) as ex:
                            prog(f"  ⚠ {key}: could not rename profile folder: {ex}")

                    dest_dir = new_profile_dir
                else:
                    dest_dir = _dest_from_install(key, install_dir,
                                                 player_name=player_name)
                if not dest_dir:
                    continue

            dest = os.path.join(dest_dir, os.path.basename(asset_subpath))
            if os.path.exists(dest):
                _replace_player_name(dest, player_name)
                updated += 1
                prog(f"  + {key}: renamed in {os.path.basename(dest)}")

                # Update active.txt for CoD4 / CoD4x in the install dir
                if key in ("cod4sp", "cod4mp"):
                    _write_cod4_active_txt(os.path.dirname(dest_dir), player_name)

            # CoD4x also reads from the AppData prefix — update active.txt
            # there too so the profile name is correct after first launch.
            if key in ("cod4sp", "cod4mp") and install_dir:
                _write_cod4_active_txt(_cod4_appdata_profiles(steam_root, install_dir, own_pfx),
                                       player_name)
                prog(f"  + {key}: updated active.txt in AppData prefix")

            # Heroic mirror
            if fixed_dest:
                heroic_dest_dir = _heroic_mirror_path(dest_dir)
                if heroic_dest_dir:
                    heroic_dest = os.path.join(
                        heroic_dest_dir, os.path.basename(asset_subpath)
                    )
                    if os.path.exists(heroic_dest):
                        _replace_player_name(heroic_dest, player_name)
                        updated += 1
                        prog(f"  + {key}: renamed in HGL mirror")

            # Offline copy mirror
            if fixed_dest:
                for offline_dir in _offline_mirror_paths(dest_dir, key):
                    offline_dest = os.path.join(
                        offline_dir, os.path.basename(asset_subpath)
                    )
                    if os.path.exists(offline_dest):
                        _replace_player_name(offline_dest, player_name)
                        updated += 1
                        prog(f"  + {key}: renamed in the offline copy")

    # ── T7X special case ─────────────────────────────────────────────────
    # T7X stores its player name in t7x/players/properties.json as JSON,
    # not as a seta cvar. Handled separately from the config map.
    if "t7x" in setup_keys:
        try:
            from t7x import set_player_name as _t7x_set_name, _get_sibling_dir
            game = (installed_games or {}).get("t7x", {})
            # t7x install_dir IS the sibling dir (set during install)
            sibling = game.get("install_dir", "")
            if not sibling:
                # Fall back: look up from the t7 (BO3) install dir
                t7_game = (installed_games or {}).get("t7", {})
                t7_dir = t7_game.get("install_dir", "")
                if t7_dir:
                    sibling = _get_sibling_dir(t7_dir)
            if sibling and os.path.isdir(sibling):
                _t7x_set_name(sibling, player_name)
                updated += 1
                prog("  + t7x: renamed in properties.json")
        except Exception as ex:
            prog(f"  ⚠ t7x: rename failed: {ex}")

    updated += _rename_in_lan_scripts(player_name, installed_games, prog)

    prog(f"Player name updated in {updated} file(s).")
    return updated


def _rename_in_lan_scripts(player_name, installed_games, prog):
    """
    Plutonium offline scripts bake the name in as +name "...": the -lan .sh
    (used by the LAN launcher) and the LCD Steam replaced exe fallback.
    Rewrite it in each.
    """
    import re
    import config as cfg
    from plutonium_lcd import PLUT_GAME_EXES

    from plutonium import bash_name_arg
    new = bash_name_arg(player_name)
    updated = 0
    for key, entry in cfg.get_setup_games().items():
        lan = entry.get("lan_wrapper_path")
        if not lan:
            continue
        d = os.path.dirname(lan)
        paths = {lan}
        if key in PLUT_GAME_EXES and entry.get("source") != "own":
            paths.add(os.path.join(d, PLUT_GAME_EXES[key][1]))

        for p in paths:
            try:
                with open(p, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if not raw.startswith(b"#!"):
                continue
            body = raw.rstrip(b"\x00")
            text = re.sub(r'\+name "[^"]*"', lambda m: new, body.decode("utf-8", "replace"), count=1)
            out = text.encode("utf-8")
            if out == body:
                continue
            # Replaced game exes are padded to the original size for Steam validation
            if len(body) < len(raw) and len(out) < len(raw):
                out += b"\x00" * (len(raw) - len(out))
            with open(p, "wb") as f:
                f.write(out)
            updated += 1
            prog(f"  + {key}: renamed in {os.path.basename(p)}")
    return updated


# ── FPS cap and FOV ───────────────────────────────────────────────────────────
# Patches com_maxfps / cg_fov in the configs already on disk, so nothing else
# in them (sensitivity, binds, resolution) is touched. The templates in
# assets/configs carry the same values for fresh installs.

DEFAULT_FPS = 90
DEFAULT_FOV = 90

# MW1, MW2, MW3, WaW, BO1 and BO2: every mode
FPS_FOV_KEYS = ("cod4sp", "cod4mp", "iw4sp", "iw4mp", "iw5sp", "iw5mp",
                "t4sp", "t4mp", "t5sp", "t5mp", "t6sp", "t6mp", "t6zm")

# MW2 (2009) campaign, stock build: cg_fov is cheat-protected and the game puts it
# back to 65 when a level loads. thereisacow 1337 unlocks it; F10 re-applies it
# (map a button to F10 in Steam Input). Only the stock config.cfg gets these.
_EXTRA_PATCHES = {
    ("iw4sp", "config.cfg"): {"extra_dvars": {"thereisacow": "1337"},
                              "binds": {"F10": "cg_fov 90"}},
}

# BO2 SP (T6SP-Mod) keeps its settings in binary profiles. The mod runs
# config_sp.cfg when the game starts and persistent_sp.cfg on each level change,
# so those are written next to the game and in its players folder.
_T6SP_AUTOEXEC = ("config_sp.cfg", "persistent_sp.cfg")


def _patch_dvars(path, values, create=False, binds=None):
    """
    Set `seta <name> "<value>"` for each name in values, in place. An existing
    line gets the new value; a missing one is added (cg_fov after cg_fovScale,
    others at the end). binds ({key: command}) does the same for
    `bind <key> "<command>"` lines. Line endings and every other line stay as
    they are. create=True makes the file if it isn't there. Returns True if
    it changed.
    """
    import re as _re

    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        if not create:
            return False
        text = ""
    nl = "\r\n" if "\r\n" in text else "\n"
    new = text
    for name, value in values.items():
        pat = _re.compile(r'^([ \t]*seta[ \t]+' + _re.escape(name) + r'[ \t]+")[^"\r\n]*(")',
                          _re.IGNORECASE | _re.MULTILINE)
        if pat.search(new):
            new = pat.sub(lambda m: m.group(1) + value + m.group(2), new)
            continue
        line = f'seta {name} "{value}"'
        anchor = None
        if name.lower() == "cg_fov":
            anchor = _re.search(r'^[ \t]*seta[ \t]+cg_fovScale[ \t]+"[^"\r\n]*"[^\r\n]*\r?\n',
                                new, _re.IGNORECASE | _re.MULTILINE)
        if anchor:
            new = new[:anchor.end()] + line + nl + new[anchor.end():]
        else:
            if new and not new.endswith("\n"):
                new += nl
            new += line + nl
    for key, command in (binds or {}).items():
        pat = _re.compile(r'^([ \t]*bind[ \t]+' + _re.escape(key) + r'[ \t]+")[^"\r\n]*(")',
                          _re.IGNORECASE | _re.MULTILINE)
        if pat.search(new):
            new = pat.sub(lambda m: m.group(1) + command + m.group(2), new)
        else:
            if new and not new.endswith("\n"):
                new += nl
            new += f'bind {key} "{command}"' + nl
    if new == text:
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".deckops-tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(new)
    os.replace(tmp, path)
    return True


def set_fps_fov(steam_root, installed_games, fps=DEFAULT_FPS, fov=DEFAULT_FOV,
                on_progress=None):
    """
    Set com_maxfps and cg_fov in the deployed configs of every set-up game in
    FPS_FOV_KEYS (SP, MP and ZM), including the Plutonium offline copies and,
    on LCD, the Heroic mirror. Games that haven't been set up are skipped, and
    so are config files that don't exist yet (a fresh install writes them from
    the templates). Returns the number of files changed.
    """
    import config as cfg

    prog = on_progress or (lambda *a: None)
    values = {"cg_fov": str(fov), "com_maxfps": str(fps)}
    setup_keys = set(cfg.get_setup_games())
    keys = [k for k in FPS_FOV_KEYS if k in setup_keys]
    if not keys:
        prog("No supported games set up yet.")
        return 0

    installed_games = installed_games or {}
    config_map = _build_config_map(steam_root, installed_games)
    player_name = cfg.get_player_name()
    changed = 0

    def patch(path, label, create=False, extra_dvars=None, binds=None):
        nonlocal changed
        try:
            if _patch_dvars(path, {**values, **(extra_dvars or {})}, create=create, binds=binds):
                changed += 1
                prog(f"  + {label}: {os.path.basename(path)}")
        except OSError as ex:
            prog(f"  ! {label}: could not update {path}: {ex}")

    for key in keys:
        game = installed_games.get(key, {})
        install_dir = game.get("install_dir", "")

        if key == "t6sp":
            if install_dir:
                for folder in (install_dir, os.path.join(install_dir, "players")):
                    for name in _T6SP_AUTOEXEC:
                        patch(os.path.join(folder, name), key, create=True)
            continue

        if key not in config_map:
            continue
        own_pfx = _own_compatdata(key, game)

        for asset_subpath, fixed_dest in config_map[key]:
            if asset_subpath.lower().endswith((".chp", ".cgp")):
                continue                       # binary profile, no dvar lines
            name = os.path.basename(asset_subpath)
            if fixed_dest:
                dest_dir = _own_pfx_dest(fixed_dest, own_pfx)
            elif install_dir:
                dest_dir = _dest_from_install(key, install_dir, player_name=player_name)
            else:
                continue
            if not dest_dir:
                continue

            extra = _EXTRA_PATCHES.get((key, name.lower()), {})
            patch(os.path.join(dest_dir, name), key, **extra)

            if fixed_dest:
                if cfg.is_lcd():
                    heroic = _heroic_mirror_path(dest_dir)
                    if heroic:
                        patch(os.path.join(heroic, name), key)
                try:
                    for offline_dir in _offline_mirror_paths(dest_dir, key):
                        patch(os.path.join(offline_dir, name), key)
                except Exception:
                    _log.debug("offline mirror lookup failed for %s", key, exc_info=True)

            # CoD4R / CoD4x also keep a copy of the profile under AppData
            if key in ("cod4sp", "cod4mp") and install_dir:
                try:
                    profiles = _cod4_appdata_profiles(steam_root, install_dir, own_pfx)
                    if os.path.isdir(profiles):
                        for sub in os.listdir(profiles):
                            patch(os.path.join(profiles, sub, name), key)
                except OSError:
                    _log.debug("cod4 appdata profiles not read", exc_info=True)

    prog(f"{fps} FPS / {fov} FOV: {changed} config file(s) updated.")
    return changed
