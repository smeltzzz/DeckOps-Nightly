"""
config.py - DeckOps configuration manager

Handles reading and writing deckops.json which lives at the
install directory (path provided by identity.py).

The config file tracks:
    - Whether first-time setup has been completed
    - Which device the user has (oled, lcd, other, or steam_machine)
    - For 'other' and 'steam_machine' devices, which resolution profile to use
    - Which games have been set up and when
    - The Steam root path found during setup
"""

import contextlib
import copy
import functools
import os
import json
import re
import threading
from datetime import datetime

from identity import CONFIG_PATH
from log import get_logger
from steam_common import write_json, PROJECT_ROOT

_CONFIGS_ROOT = os.path.join(PROJECT_ROOT, "assets", "configs")

_log = get_logger(__name__)

DEFAULTS = {
    "first_run_complete": False,
    "os_type": None,             # "steamos", "bazzite", "cachyos", "armada", or "other_linux"
    "deck_model": None,          # "oled", "lcd", "other", or "steam_machine"
    "other_device": None,         # resolution key for non-Deck devices and Steam Machine, e.g.
                                  # "1920x1200", "1920x1200_144hz", "1920x1080", "1280x720"
    "other_device_type": None,    # device type for controller profile selection:
                                  # "legion_go", "legion_go_2", "legion_go_s", "2btn", "generic", "steam_machine"
    "gyro_mode":  None,          # "on", "off", "hold", or "toggle"
    "play_mode":  None,          # "handheld" or "docked"
    "external_controller": None, # "playstation", "xbox", "steamcontroller", or "other" -- used when docked, on Bazzite and on PC (uses_external_controller)
    "docked_resolution": None,   # "1280x720", "1280x800", "1920x1080", "1920x1200", or "own" -- picks Other/<res> configs when docked and on PC (get_model_config_dir)
                                 # NOTE: this will also be used for future Bazzite, Steam Box, and other handheld support on SteamOS
    "ge_proton_version": None,   # e.g. "GE-Proton10-32"
    "ge_proton_installed": None, # GE-Proton versions DeckOps has used; only these are ever
                                 #   removed by ge_proton.cleanup_old_ge_versions. None = older install
    "steam_root": None,
    "setup_games": {},           # key: game key, value: { "client": ..., "source": "steam"|"own", "setup_at": timestamp,
                                 #   "lan_wrapper_path": path to -lan bash script for offline launcher (all sources) }
    "depot_patched": {},         # legacy (read-only): pre-receipt downgrade flag (Ghosts/AW, removed); receipts now live in the game folder
    "game_source": None,         # "steam" or "own"
    "extra_scan_paths": [],      # Choose Folder picks: also scanned for own copies (find_all_games)
    "music_enabled": True,       # background music on/off
    "music_volume":  0.4,        # 0.0 to 1.0
    "player_name": None,         # in-game player name for configs and LCD Plutonium
    "iw_pad": {},                # sp_mod.py: { game key: { "tag", "digest" } } of the installed IW-Pad release
    "offline_mode": None,        # Offline Mode (opt-in): offline launcher, Steam menu Offline entries,
                                 #   WaW/BO1/BO2/MW3 MP bot matches. None = not chosen yet (older installs)
    "dxvk_frame_cap": True,      # sp_mod.py: cap MW2 / MW3 SP at 90 FPS with DXVK in the launch option
                                 #   (the SP builds have no com_maxfps, so there is no in-game cap)
}


# ── In-memory cache ──────────────────────────────────────────────────────────
# Avoids re-reading and re-parsing deckops.json on every getter call.
# The cache is invalidated on save() and on external file changes (mtime check).
# Thread-safe: all cache access is guarded by _lock.

_lock = threading.Lock()
_cache = None        # cached merged dict, or None if invalidated
# File identity, mtime, ctime and size when _cache was populated. Atomic
# replacements can have the same mtime and size (e.g. copied files or coarse
# timestamps); inode/ctime distinguish those from the cached version.
# prelaunch/cache_cleanup run beside the UI and read the same file.
_cache_key = None
# CONFIG_PATH as it was when load() last found the file there but could not read
# it (permissions, an I/O error), else None. load() still answers with the
# defaults, but save() refuses to write them over the player's real setup.
# Kept as the path, not a flag, so it only ever guards the file it was about.
_unreadable = None


class ConfigUnreadable(OSError):
    """deckops.json exists but could not be read, so it is not overwritten."""


def _mark_unreadable(ex):
    global _unreadable
    if _unreadable != CONFIG_PATH:
        _log.error("deckops.json could not be read (%s); using defaults and not saving over it", ex)
    _unreadable = CONFIG_PATH


def load() -> dict:
    """
    Load config from disk with in-memory caching.

    Returns a fresh copy of the cached config if the file hasn't changed
    since the last read (mtime check). Re-reads from disk if the file
    was modified externally or if save() invalidated the cache.

    Returns defaults if the file doesn't exist or is corrupt. Also if it
    can't be read (permissions, I/O error), but then save() refuses to
    replace it until a load() reads it again.
    """
    global _cache, _cache_key, _unreadable

    with _lock:
        for attempt in (0, 1):
            # Check if file exists
            try:
                st = os.stat(CONFIG_PATH)
            except FileNotFoundError:
                _unreadable = None
                _cache = None
                _cache_key = None
                return copy.deepcopy(DEFAULTS)
            except OSError as ex:
                # There may be a real setup behind this (EIO, EACCES on the folder)
                _mark_unreadable(ex)
                return copy.deepcopy(DEFAULTS)
            current_key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)

            # Return cached copy if file hasn't changed
            if _cache is not None and current_key == _cache_key:
                return copy.deepcopy(_cache)

            # Cache miss or stale — re-read from disk
            try:
                with open(CONFIG_PATH, "r") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("config root is not an object")
            except ValueError:
                # Another process (prelaunch, the UI) may have replaced the file
                # between our stat and read: the bad text we saw is gone, so look
                # at the new file instead of moving a good one aside.
                try:
                    st2 = os.stat(CONFIG_PATH)
                except OSError:
                    _cache = None
                    _cache_key = None
                    return copy.deepcopy(DEFAULTS)
                if attempt == 0 and (st2.st_dev, st2.st_ino, st2.st_mtime_ns,
                                     st2.st_ctime_ns, st2.st_size) != current_key:
                    continue
                # Unparseable (truncated by a power loss, bad hand edit). Move it
                # aside so the next save() can't overwrite the user's setup.
                bad = f"{CONFIG_PATH}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
                try: os.replace(CONFIG_PATH, bad); _log.warning("deckops.json unreadable, moved to %s", bad)
                except OSError as ex:
                    _log.warning("deckops.json unreadable and could not be moved aside: %s", ex)
                    # The damaged setup is still at CONFIG_PATH. Do not let the next
                    # setter replace it with defaults just because the move failed.
                    _mark_unreadable(ex)
                _cache = None
                _cache_key = None
                return copy.deepcopy(DEFAULTS)
            except FileNotFoundError:
                _unreadable = None          # removed between the stat and the read
                _cache = None
                _cache_key = None
                return copy.deepcopy(DEFAULTS)
            except OSError as ex:
                # Unreadable is not missing: answering with the defaults is fine, but
                # the next setter used to save them over the player's whole setup
                _mark_unreadable(ex)
                return copy.deepcopy(DEFAULTS)
            _unreadable = None
            merged = copy.deepcopy(DEFAULTS)
            merged.update(data)
            # A hand edit like {"setup_games": null} would make every setter and
            # getter that indexes into it raise: keep the default for those keys.
            for key, default in DEFAULTS.items():
                if isinstance(default, dict) and not isinstance(merged[key], dict):
                    _log.warning("deckops.json: %r is not an object, using the default", key)
                    merged[key] = copy.deepcopy(default)
            _cache = merged
            _cache_key = current_key
            return copy.deepcopy(_cache)


def unnoticed_corrupt_files() -> list:
    """deckops.json copies load() moved aside (possibly in another process,
    e.g. a game's prelaunch) that the player hasn't been told about yet."""
    import glob
    return sorted(p for p in glob.glob(glob.escape(CONFIG_PATH) + ".corrupt-*")
                  if not p.endswith(".noticed"))


def mark_corrupt_noticed(paths):
    """Rename each to <name>.noticed so the notice is shown once. Kept, never deleted."""
    for p in paths:
        try:
            os.replace(p, p + ".noticed")
        except OSError:
            _log.debug("could not mark %s as noticed", p, exc_info=True)


def save(config: dict):
    """
    Write config to disk and invalidate the cache.
    Creates the directory if needed. Raises ConfigUnreadable (an OSError)
    instead of writing while the last load() could not read an existing file:
    `config` is then the defaults, not the player's setup.
    """
    global _cache, _cache_key

    with _lock:
        if _unreadable == CONFIG_PATH and os.path.lexists(CONFIG_PATH):
            _log.error("not saving deckops.json: the existing file could not be read")
            raise ConfigUnreadable(f"{CONFIG_PATH} could not be read; not overwriting it")
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        write_json(CONFIG_PATH, config)
        _cache = None
        _cache_key = None


# A setter holds both the in-process lock and a stable, separate flock file over
# load -> modify -> save. Prelaunch runs in another process and otherwise loses
# UI settings (or vice versa). Never lock deckops.json itself: save replaces it.
_txn_lock = threading.RLock()
_txn_depth = 0


@contextlib.contextmanager
def transaction():
    """Hold the config update lock: `with config.transaction(): c = load(); ...; save(c)`."""
    global _txn_depth
    with _txn_lock:
        if _txn_depth:
            _txn_depth += 1
            try:
                yield
            finally:
                _txn_depth -= 1
            return
        stream = None
        if os.name == "posix":
            import fcntl
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            path = CONFIG_PATH + ".lock"
            try:
                stream = open(path, "a+")
            except PermissionError:
                stream = open(path, "r")  # root-owned lock, like the update lock
            try:
                fcntl.flock(stream, fcntl.LOCK_EX)
            except BaseException:
                stream.close()
                raise
        _txn_depth = 1
        try:
            yield
        finally:
            _txn_depth = 0
            if stream is not None:
                stream.close()  # closing also releases flock


def _updates(fn):
    """Run a load-modify-save setter under the update lock."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with transaction():
            return fn(*args, **kwargs)
    return wrapper


@_updates
def _set(key: str, value):
    config = load()
    config[key] = value
    save(config)


def is_first_run() -> bool:
    """Returns True if setup has never been completed."""
    return not load().get("first_run_complete", False)


@_updates
def set_os_type(os_type: str):
    """Save the user's operating system type.

    os_type -- 'steamos', 'bazzite', 'cachyos', 'armada', or 'other_linux'
    """
    config = load()
    config["os_type"] = os_type
    save(config)


def get_deck_model() -> str | None:
    """Returns 'oled', 'lcd', 'other', 'steam_machine', or None if not yet set."""
    return load().get("deck_model")


@_updates
def set_deck_model(model: str):
    """Save the user's device model.

    model -- 'oled', 'lcd', 'other', or 'steam_machine'
    """
    config = load()
    config["deck_model"] = model
    save(config)


def is_lcd() -> bool:
    """True only for Steam Deck LCD hardware (Heroic/LCD launch path)."""
    return load().get("deck_model") == "lcd"


def is_other() -> bool:
    """True for non-Deck SteamOS devices (Legion Go, ROG Ally, etc.)."""
    return load().get("deck_model") == "other"


def is_steam_machine() -> bool:
    """True for Valve Steam Machine hardware (triton controller)."""
    return load().get("deck_model") == "steam_machine"


def uses_oled_path() -> bool:
    """True for devices that use the direct Plutonium launcher path.

    OLED, Other, and Steam Machine devices launch Plutonium directly via
    the protocol URL in Steam's compatdata prefix. LCD uses Heroic instead.
    Use this for launch pipeline decisions, NOT for config folder resolution.
    """
    return load().get("deck_model") != "lcd"


def get_other_device() -> str | None:
    """Returns the resolution key for Other/Steam Machine devices.

    e.g. '1920x1200', '1920x1080', or None.
    Steam Machine also stores its user-picked resolution here.
    """
    return load().get("other_device")


@_updates
def set_other_device(device: str):
    """Save the resolution key, e.g. '1920x1200_144hz'.

    Used by both Other devices (preset per device) and Steam Machine
    (user-picked from the resolution screen).
    """
    config = load()
    config["other_device"] = device
    save(config)


def get_other_device_type() -> str | None:
    """Returns the device type for controller profile selection.

    Values: 'legion_go', 'legion_go_2', 'legion_go_s', '2btn', 'generic',
            'steam_machine', or None.
    Used by controller_profiles.py to pick the correct profile variant.
    """
    return load().get("other_device_type")


@_updates
def set_other_device_type(device_type: str):
    """Save the device type for controller profile selection.

    device_type -- 'legion_go' (Go 1), 'legion_go_2' (Go 2),
                   'legion_go_s', '2btn' (ROG Ally,
                   MSI Claw), 'generic', or 'steam_machine'
    """
    config = load()
    config["other_device_type"] = device_type
    save(config)


def get_model_config_dir() -> str:
    """Return the asset subdirectory for the current device's configs.

    OLED          -> 'OLED'
    LCD           -> 'LCD'
    Other         -> 'Other/<resolution>'  (e.g. 'Other/1920x1200')
    Steam Machine -> 'Other/<resolution>'  (user-picked, same assets as Other)

    Docked        -> 'Other/<docked_resolution>'  (the display picked in setup)
    PC            -> 'Other/<docked_resolution>'  (older PC setups: no play_mode)

    "own" (My Own: set it in-game) maps to Other/own, which has no resolution
    lines. The resolution picked in setup used to be stored and never read:
    a PC always got 1920x1200 and a docked Deck its handheld configs.
    A folder that doesn't exist falls back (Steam Machine 1280x800 / My Own
    pointed at missing folders, so every config was skipped).

    Falls back to 'OLED' if deck_model is unset.
    """
    c = load()
    model = c.get("deck_model") or "oled"
    default = "OLED" if model == "oled" else "LCD"
    if model in ("other", "steam_machine"):
        default = f"Other/{c.get('other_device') or '1920x1200'}"
    picked = c.get("docked_resolution")
    if picked and (c.get("play_mode") == "docked"
                   or (model == "other" and not c.get("other_device") and c.get("play_mode") is None)):
        want = f"Other/{picked}"
    else:
        want = default
    for d in (want, default, "Other/1920x1200"):
        if os.path.isdir(os.path.join(_CONFIGS_ROOT, *d.split("/"))):
            if d != want:
                _log.warning("config assets for %s missing, using %s", want, d)
            return d
    return want


def get_gyro_mode() -> str | None:
    """Returns 'on', 'off', 'hold', or 'toggle', or None if not yet set."""
    return load().get("gyro_mode")


def set_gyro_mode(mode: str):
    """Save the user's gyro preference: 'on', 'off', 'hold', or 'toggle'."""
    _set("gyro_mode", mode)


def get_offline_mode() -> bool:
    """Offline Mode: the offline launcher, Steam menu Offline entries and the
    WaW/BO1/BO2/MW3 MP bot matches. Older installs set up offline for every Plutonium
    game, so they keep it."""
    c = load()
    if c.get("offline_mode") is None:
        on = any(isinstance(e, dict) and e.get("lan_wrapper_path")
                 for e in c.get("setup_games", {}).values())
        try:
            _set("offline_mode", on)
        except OSError:
            pass                    # a getter must not fail; recorded once the file/lock reads again
        return on
    return bool(c["offline_mode"])


def set_offline_mode(on: bool):
    _set("offline_mode", bool(on))


def set_play_mode(mode: str):
    """Save the user's play mode. mode should be 'handheld' or 'docked'."""
    _set("play_mode", mode)


def is_docked() -> bool:
    return load().get("play_mode") == "docked"


def uses_external_controller() -> bool:
    """
    Whether the controller picked in setup (external_controller) gets its
    profiles assigned. Docked, and also Bazzite (Neptune templates don't
    work there, setup asks for the controller in every mode) and PC (setup
    always asks; older PC setups left play_mode unset, which is recognised
    by a controller chosen with no play mode). Only checking is_docked()
    left the chosen controller unused on Bazzite handheld and on PC.
    """
    c = load()
    if c.get("play_mode") == "docked" or c.get("os_type") == "bazzite":
        return True
    return (c.get("deck_model") == "other" and c.get("play_mode") is None
            and bool(c.get("external_controller")))


def get_external_controller() -> str | None:
    """Returns 'playstation', 'xbox', 'steamcontroller', 'other', or None if not yet set."""
    return load().get("external_controller")


def set_external_controller(controller_type: str):
    """Save the user's external controller type. Should be 'playstation', 'xbox', 'steamcontroller', or 'other'."""
    _set("external_controller", controller_type)


def set_docked_resolution(resolution: str):
    """Save the user's docked display resolution. 'own' means user sets it in-game."""
    _set("docked_resolution", resolution)


def set_game_source(source: str):
    """Save game source preference."""
    _set("game_source", source)


def get_extra_scan_paths() -> list:
    """Folders the player picked with Choose Folder, scanned for own copies
    alongside the default locations (detect_games.find_all_games)."""
    paths = load().get("extra_scan_paths")
    if not isinstance(paths, list):
        return []
    return [p for p in paths if isinstance(p, str) and p]


@_updates
def add_extra_scan_path(path: str):
    """Remember a Choose Folder folder, so the Library finds the games in it
    after setup. Kept as given; duplicates are ignored."""
    if not isinstance(path, str) or not path:
        return
    path = os.path.normpath(path)
    current = get_extra_scan_paths()
    if path in current:
        return
    config = load()
    config["extra_scan_paths"] = current + [path]
    save(config)


def get_music_enabled() -> bool:
    """Returns True if background music is enabled."""
    value = load().get("music_enabled", True)
    return value if isinstance(value, bool) else True       # a hand edit like "no" is no switch


def set_music_enabled(enabled: bool):
    """Save background music on/off preference."""
    _set("music_enabled", enabled)


def get_music_volume() -> float:
    """Returns music volume as a float between 0.0 and 1.0."""
    value = load().get("music_volume", 0.4)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return DEFAULTS["music_volume"]         # a text or null value would reach the audio output
    return max(0.0, min(1.0, float(value)))


def set_music_volume(volume: float):
    """Save music volume. Clamped to 0.0 - 1.0."""
    _set("music_volume", max(0.0, min(1.0, volume)))


def get_ge_proton_version() -> str | None:
    """Returns the installed GE-Proton version string, e.g. 'GE-Proton10-32', or None."""
    return load().get("ge_proton_version")


@_updates
def set_ge_proton_version(version: str):
    """Save the installed GE-Proton version after CompatToolMapping is applied.
    Also remembers it (and the version it replaces) as one DeckOps uses, so
    cleanup_old_ge_versions can tell DeckOps's GE-Protons from ones the player
    installed with ProtonUp-Qt or by hand."""
    config = load()
    used = _ge_used(config)
    if version and version not in used:
        used.append(version)
    config["ge_proton_version"] = version
    config["ge_proton_installed"] = used
    save(config)


def _ge_used(config: dict) -> list:
    used = config.get("ge_proton_installed")
    used = [v for v in used if isinstance(v, str) and v] if isinstance(used, list) else []
    # Installs from before this list existed: the pinned version is DeckOps's own
    pinned = config.get("ge_proton_version")
    if pinned and pinned not in used:
        used.append(pinned)
    return used


def get_ge_proton_installed() -> list:
    """GE-Proton versions DeckOps installed or used, in no particular order."""
    return _ge_used(load())


def set_ge_proton_installed(versions: list):
    _set("ge_proton_installed", [v for v in versions if isinstance(v, str) and v])


PLAYER_NAME_MAX = 32

# The name ends up inside "..." in game cfgs (seta name), in bash scripts
# (+name "..."), in CoD4's profile folder name and in active.txt. These
# characters break one of those: quotes end the value, $ and ` run commands
# in the bash scripts, \ escapes, ; splits cfg commands, / and the rest are
# not allowed in a (Windows) folder name.
_NAME_BAD = set('"\\/$`;<>:|?*')


def clean_player_name(name) -> str:
    """The name with unsafe and control characters removed and whitespace
    collapsed, at most PLAYER_NAME_MAX long. "" if nothing usable is left."""
    if not isinstance(name, str):
        return ""
    kept = "".join(" " if c.isspace() else c for c in name
                   if c.isspace() or (c.isprintable() and c not in _NAME_BAD))
    return " ".join(kept.split())[:PLAYER_NAME_MAX].strip()


def get_player_name() -> str | None:
    """Returns the player's chosen in-game name, or None if not yet set.
    Cleaned on read too, for names saved before clean_player_name existed."""
    return clean_player_name(load().get("player_name")) or None


def set_player_name(name: str):
    """Save the player's chosen in-game name (cleaned; unusable -> None)."""
    _set("player_name", clean_player_name(name) or None)


def get_steam_display_name(steam_root: str | None = None) -> str | None:
    """
    Read the active Steam user's display name from loginusers.vdf.

    Looks for the account with MostRecent=1 and returns its PersonaName.
    Falls back to the first account if MostRecent is not set.
    Returns None if the file can't be read or parsed.

    steam_root -- path to Steam root. Defaults to the root setup recorded,
                 else the first installation found on disk.
    """
    if not steam_root:
        from steam_common import default_steam_root
        steam_root = default_steam_root()
    vdf_path = os.path.join(steam_root, "config", "loginusers.vdf")
    if not os.path.exists(vdf_path):
        return None
    try:
        with open(vdf_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError):
        return None

    # Parse all accounts: find PersonaName and MostRecent per block
    # VDF is simple enough here to use regex instead of a full parser
    first_name = None
    most_recent_name = None
    # Split into per-account blocks by finding Steam ID headers
    blocks = re.split(r'"\d{17}"\s*\{', content)
    for block in blocks[1:]:  # skip the "users" { header
        persona = re.search(r'"PersonaName"\s+"([^"]*)"', block)
        recent = re.search(r'"MostRecent"\s+"1"', block)
        if persona:
            name = persona.group(1)
            if first_name is None:
                first_name = name
            if recent:
                most_recent_name = name

    return most_recent_name or first_name


@_updates
def mark_game_setup(game_key: str, client: str, source: str = "steam",
                    wrapper_path: str = None,
                    lan_wrapper_path: str = None):
    """
    Record that a game has been set up successfully.
    game_key         -- e.g. 'cod4mp', 'iw4mp', 't5sp'
    client           -- e.g. 'cod4x', 'iw4x', 'plutonium'
    source           -- 'steam' or 'own' (which install path was used)
    wrapper_path     -- optional path to the online launcher wrapper script
                        (OLED own games, used by legacy callers)
    lan_wrapper_path -- optional path to the -lan bash script for offline
                        mode. Used by the offline launcher for all sources
                        and both hardware models.
    """
    config = load()
    entry = {
        "client": client,
        "source": source,
        "setup_at": datetime.now().isoformat(),
    }
    if wrapper_path:
        entry["wrapper_path"] = wrapper_path
    if lan_wrapper_path:
        entry["lan_wrapper_path"] = lan_wrapper_path
    config["setup_games"][game_key] = entry
    save(config)


def is_game_setup(game_key: str) -> bool:
    """Returns True if this game key has been set up (any source)."""
    return game_key in load().get("setup_games", {})


@_updates
def unmark_game_setup(game_keys):
    """
    Remove one or more game keys from setup_games so they appear as
    'not set up' in ManagementScreen. Accepts a single string or a list.
    Used when the user wants to do a clean reinstall of a game.
    """
    if isinstance(game_keys, str):
        game_keys = [game_keys]
    config = load()
    changed = False
    for key in game_keys:
        if key in config.get("setup_games", {}):
            del config["setup_games"][key]
            changed = True
    if changed:
        save(config)


def is_game_setup_for_source(game_key: str, source: str) -> bool:
    """Returns True if this game key has been set up from the given source.
    Entries without a 'source' field are treated as 'steam' for backward compat."""
    entry = load().get("setup_games", {}).get(game_key)
    if entry is None:
        return False
    if not isinstance(entry, dict):         # damaged by a hand edit: no source recorded
        return source == "steam"
    return entry.get("source", "steam") == source


def get_setup_games() -> dict:
    """Returns the full setup_games dict."""
    return load().get("setup_games", {})


def is_depot_patched(game_id: str) -> bool:
    return game_id in load().get("depot_patched", {})


@_updates
def clear_depot_patched(game_id: str):
    config = load()
    dp = config.get("depot_patched", {})
    if game_id in dp:
        del dp[game_id]
        save(config)


def get_dxvk_frame_cap() -> bool:
    """True if MW2 / MW3 SP launch options carry a DXVK 90 FPS cap (on by default)."""
    return bool(load().get("dxvk_frame_cap", True))


def get_iw_pad(game_key: str) -> dict:
    return load().get("iw_pad", {}).get(game_key) or {}


@_updates
def set_iw_pad(game_key: str, info: dict):
    config = load()
    config.setdefault("iw_pad", {})[game_key] = info
    save(config)


@_updates
def complete_first_run(steam_root: str):
    """
    Call this at the end of the setup wizard to mark first run as done.
    """
    config = load()
    config["first_run_complete"] = True
    config["steam_root"] = steam_root
    save(config)


def reset():
    """
    Wipe the config and start fresh. Useful for testing or reinstalling.
    """
    global _cache, _cache_key, _unreadable

    # Same order as the setters (update lock, then cache lock). Without the update
    # lock a setter that had already loaded the old config saved it back right after
    # the delete, and "reset" brought the whole old setup back.
    with transaction(), _lock:
        try:
            os.remove(CONFIG_PATH)
        except FileNotFoundError:
            pass            # already gone (another process reset it): nothing to wipe
        _cache = None
        _cache_key = None
        _unreadable = None  # the player chose to start over: saving is allowed again
