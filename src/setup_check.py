"""
setup_check.py — read-only "Check My Setup" report for the games DeckOps set up.

For every set-up game: is its folder there, are DeckOps's mod files there, and
(Steam copies) does Steam still map it to an installed compatibility tool and
still have the launch option DeckOps set, with every DeckOps file it calls on
disk. Nothing is written; Steam may be running. Repairs go through the normal
per-game Update flow (ui_manage), never through this module.
"""

import os
import re

from log import get_logger

_log = get_logger(__name__)

# A file DeckOps leaves in the game folder for each client
CLIENT_MARKERS = {
    "cod4r": "deckops_cod4r.json",
    "cod4x": "deckops_cod4x.json",
    "iw3sp": "deckops_iw3sp.json",
    "iw4x": "iw4x-launcher.exe",
    "iw-pad": "iw-pad.exe",
    "plutonium": "deckops_plutonium.json",
    "t6sp_mod": "deckops_t6sp_mod.json",
}

# Clients whose Steam copy gets a launch option (CoD4's is worked out by iw3sp)
_LAUNCH_OPTION_CLIENTS = {"iw4x", "iw-pad", "plutonium", "t6sp_mod"}
_COD4_KEYS = ("cod4mp", "cod4sp")


def _appid(key: str, client: str) -> str:
    from detect_games import GAMES
    if client == "plutonium":
        from plutonium import GAME_META
        if key in GAME_META:
            return str(GAME_META[key][0])
    return str(GAMES.get(key, {}).get("appid", ""))


def _name(key: str) -> str:
    from detect_games import GAMES
    return GAMES.get(key, {}).get("name", key)


def _launch_options(steam_root: str, appid: str) -> list:
    """LaunchOptions of appid for every Steam user that has the game (unescaped)."""
    from wrapper import _localconfigs, _app_block, _flat_values
    values = []
    for _, _, content in _localconfigs(steam_root):
        block = _app_block(content, appid)
        if not block:
            continue
        try:
            found = _flat_values(content, block, "LaunchOptions")
        except ValueError:
            continue
        values.append(content[found[0][1]:found[0][2]].replace('\\"', '"') if found else "")
    return values


def _compat_tool(steam_root: str, appid: str):
    """Name of the compat tool config.vdf maps appid to; "" if none, None if unreadable."""
    from wrapper import _vdf_child_block, _flat_values
    path = os.path.join(steam_root, "config", "config.vdf")
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
        block = (-1, len(content))
        for key in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", appid):
            block = _vdf_child_block(content, block, key)
            if not block:
                return ""
        found = _flat_values(content, block, "name")
        return content[found[0][1]:found[0][2]] if found else ""
    except (OSError, ValueError):
        return None


def _tool_dirs(steam_root: str) -> list:
    return [os.path.join(steam_root, "compatibilitytools.d"),
            os.path.expanduser("~/.local/share/Steam/compatibilitytools.d")]


def _deckops_paths(option: str) -> list:
    """Absolute paths in a launch option that belong to DeckOps."""
    from identity import INSTALL_DIR
    roots = (INSTALL_DIR.rstrip("/") + "/", os.path.expanduser("~/.local/share/deckops/"))
    return [p for root in roots for p in re.findall(re.escape(root) + r'[^\s"\']+', option)]


def check_game(key: str, entry: dict, game: dict | None, steam_root: str,
               setup_games: dict) -> dict:
    """One game's result: {"key", "name", "problems": [str], "fixable": bool}."""
    client = entry.get("client", "")
    source = entry.get("source", "steam")
    result = {"key": key, "name": _name(key), "problems": [], "fixable": False}
    problems = result["problems"]

    install_dir = (game or {}).get("install_dir", "")
    if not install_dir or not os.path.isdir(install_dir):
        problems.append("Game folder not found. Install the game again in Steam, "
                        "or insert the SD card it is on.")
        return result      # nothing else can be checked or fixed from here

    fixable = False
    marker = CLIENT_MARKERS.get(client)
    if marker and not os.path.exists(os.path.join(install_dir, marker)):
        problems.append(f"DeckOps's {client} files are missing from the game folder.")
        fixable = True

    if source == "steam":
        appid = _appid(key, client)
        if appid:
            tool = _compat_tool(steam_root, appid)
            if tool == "":
                problems.append("Steam is not set to run it with GE-Proton.")
                fixable = True
            elif tool and tool.startswith("GE-Proton") and \
                    not any(os.path.isdir(os.path.join(d, tool)) for d in _tool_dirs(steam_root)):
                problems.append(f"{tool} is selected in Steam but is not installed.")
                fixable = True

            expected = None
            if key in _COD4_KEYS:
                from iw3sp import cod4_launch_option
                expected = bool(cod4_launch_option(setup_games))
            elif client in _LAUNCH_OPTION_CLIENTS:
                expected = True
            if expected:
                options = _launch_options(steam_root, appid)
                set_ones = [o for o in options if o.strip()]
                if options and not set_ones:
                    problems.append("Its Steam launch option is missing.")
                    fixable = True
                for o in set_ones:
                    gone = [p for p in _deckops_paths(o) if not os.path.exists(p)]
                    if gone:
                        problems.append("Its Steam launch option uses a file that no longer exists: "
                                        + ", ".join(sorted(set(gone))))
                        fixable = True
                        break
    result["fixable"] = fixable
    return result


def check_setup(steam_root: str, installed: dict, setup_games: dict) -> list:
    """Results for every game in setup_games, in setup order."""
    results = []
    for key, entry in (setup_games or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            results.append(check_game(key, entry, (installed or {}).get(key), steam_root, setup_games))
        except Exception as ex:
            _log.warning("setup check failed for %s", key, exc_info=True)
            results.append({"key": key, "name": _name(key),
                            "problems": [f"Could not be checked: {ex}"], "fixable": False})
    return results
