"""Install the bundled Steam Deck layout for DeckOps-managed games.

Steam Input layouts are controller-type specific. This bundle is a Neptune
(Steam Deck) layout; do not assign it to external controllers or other devices.
Run assignments while Steam is closed so its cached configsets cannot overwrite
our changes on exit. Legacy DeckOps layout removal remains in the uninstaller.
"""

import hashlib
import os
import shutil

from log import get_logger
from steam_common import (
    PROJECT_ROOT, STEAM_ROOT,
    calc_shortcut_appid, find_all_steam_uids, get_deck_serial,
    patch_configset, shortcut_title_key, keep_original,
)

_log = get_logger(__name__)
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets", "controllers")
TEMPLATES_DIR = os.path.expanduser("~/.steam/steam/controller_base/templates")
TEMPLATE = "controller_neptune_deckops_cod.vdf"
CONFIGSET = "configset_controller_neptune.vdf"
CANONICAL = "controller_neptune.vdf"

# Only names DeckOps shipped in earlier builds. Never remove a player's
# controller_neptune.vdf or an unrelated Steam Input layout.
LEGACY_TEMPLATES = {
    f"controller_{device}_deckops{suffix}.vdf"
    for device in ("neptune", "triton")
    for suffix in ("_ads", "_off", "_hold", "_toggle",
                   "_other_ads", "_other_off", "_other_hold", "_other_toggle")
} | {
    f"controller_{device}_deckops_{variant}_{mode}.vdf"
    for device, variant in (("neptune", "legion"), ("neptune", "legion_other"),
                            ("neptune", "2btn"), ("neptune", "2btn_other"),
                            ("steamos_handheld", "legion"),
                            ("steamos_handheld", "legion_other"))
    for mode in ("ads", "off")
} | {
    f"controller_{device}_deckops{suffix}.vdf"
    for device in ("ps4", "ps5", "ps5_edge")
    for suffix in ("", "_ads", "_other", "_other_ads", "_bo3", "_bo3_ads")
} | {
    f"controller_{device}_deckops{suffix}.vdf"
    for device in ("xbox360", "xboxone", "xboxelite", "generic")
    for suffix in ("", "_other")
}

# Steam resolves layouts by both numeric appid and a normalized game title.
# MW2/MW3 campaigns share a named key with MP; the numeric appid distinguishes them.
APPID_NAMED_KEYS = {
    "7940":   ["call of duty 4 modern warfare (2007)"],
    "10090":  ["call of duty world at war", "call of duty world at war - multiplayer"],
    "10180":  [],
    "10190":  ["call of duty modern warfare 2 (2009) - multiplayer"],
    "42680":  [],
    "42690":  ["call of duty modern warfare 3 - multiplayer"],
    "42750":  ["call of duty modern warfare 3 - dedicated server"],
    "42700":  ["call of duty black ops", "call of duty black ops - zombies"],
    "42710":  ["call of duty black ops - multiplayer"],
    "202970": [],
    "202990": ["call of duty black ops ii - multiplayer"],
    "212910": ["call of duty black ops ii - zombies"],
}


def _is_steam_deck():
    import config as cfg
    return cfg.get_deck_model() in ("lcd", "oled")


def install_controller_templates(on_progress=None):
    """Install only the bundled Steam Deck layout, leaving the player's own alone."""
    prog = on_progress or (lambda msg: None)
    deck = _is_steam_deck()
    src = os.path.join(ASSETS_DIR, TEMPLATE)
    if deck and not os.path.isfile(src):
        raise FileNotFoundError(f"Steam Deck controller layout missing: {src}")
    # Stable and Nightly share Steam's global templates directory. Keep the
    # older filenames when the other DeckOps build might still use them.
    from identity import INSTALL_DIR_NAME
    other_build = os.path.expanduser("~/DeckOps" if INSTALL_DIR_NAME == "DeckOps-Nightly"
                                     else "~/DeckOps-Nightly")
    # Leave per-account Steam layouts, including the player's source, untouched.
    if os.path.isdir(TEMPLATES_DIR) and not os.path.isdir(other_build):
        for name in LEGACY_TEMPLATES:
            path = os.path.join(TEMPLATES_DIR, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as ex:
                prog(f"  ⚠ Could not remove old DeckOps template {name}: {ex}")
    if not deck:
        prog("  - Steam Deck layout skipped on this device; no external layout is bundled.")
        return
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    shutil.copy2(src, os.path.join(TEMPLATES_DIR, TEMPLATE))
    prog(f"  ✓ Installed {TEMPLATE}")


def _shortcut_appids(prog):
    """Return LCD WaW MP shortcut appids; CoD4R MP uses the main Steam tile."""
    try:
        from detect_games import find_steam_root, parse_library_folders, find_installed_games
        from shortcut import SHORTCUTS, mp_on_main_tile
        steam_root = find_steam_root()
        if not steam_root or mp_on_main_tile("t4mp"):
            return {}
        installed = find_installed_games(parse_library_folders(steam_root), steam_root)
        game = installed.get("t4mp")
        if not game or not game.get("install_dir"):
            return {}
        shortcut = SHORTCUTS["t4mp"]
        exe_path = os.path.join(game["install_dir"], "CoDWaWmp.exe")
        return {str(calc_shortcut_appid(exe_path, shortcut["name"])): shortcut["name"]}
    except Exception as ex:
        prog(f"  ⚠ Could not locate WaW MP shortcut: {ex}")
        return {}


def _write_layout(uid, appid, names=(), numbered=True):
    """Write per-account layout and activate it in the Deck's configsets."""
    src = os.path.join(ASSETS_DIR, TEMPLATE)
    userdata_dir = os.path.join(STEAM_ROOT, "userdata", uid, "241100", "remote", "controller_config", appid)
    steam_cfg = os.path.join(STEAM_ROOT, "steamapps", "common", "Steam Controller Configs", uid, "config")
    os.makedirs(userdata_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(userdata_dir, TEMPLATE))
    if numbered:
        target_dir = os.path.join(steam_cfg, appid)
        os.makedirs(target_dir, exist_ok=True)
        canonical_path = os.path.join(target_dir, CANONICAL)
        if not os.path.exists(canonical_path + ".deckops-owned"):
            keep_original(canonical_path)
        shutil.copy2(src, canonical_path)
        with open(src, "rb") as layout, \
             open(canonical_path + ".deckops-owned", "w", encoding="ascii") as marker:
            marker.write(hashlib.sha256(layout.read()).hexdigest())
    configsets = [CONFIGSET]
    serial = get_deck_serial()
    if serial:
        configsets.append(f"configset_{serial}.vdf")
    for configset in configsets:
        path = os.path.join(steam_cfg, configset)
        patch_configset(path, appid, TEMPLATE)
        for name in names:
            patch_configset(path, name, TEMPLATE)


def assign_controller_profiles(gyro_mode=None, on_progress=None):
    """Apply the one Deck layout to supported games, own copies and shortcuts."""
    prog = on_progress or (lambda msg: None)
    if not _is_steam_deck():
        prog("  - Controller assignment skipped: bundled layout is Steam Deck only.")
        return
    if not os.path.isfile(os.path.join(ASSETS_DIR, TEMPLATE)):
        raise FileNotFoundError(f"Steam Deck controller layout missing: {TEMPLATE}")
    uids = find_all_steam_uids()
    if not uids:
        prog("  ⚠ No Steam user accounts found; controller assignment skipped.")
        return
    for uid in uids:
        for appid, names in APPID_NAMED_KEYS.items():
            _write_layout(uid, appid, names)
            prog(f"  ✓ [{appid}] → {TEMPLATE}")
        for appid, title in _shortcut_appids(prog).items():
            _write_layout(uid, appid, [shortcut_title_key(title)])
            prog(f"  ✓ [shortcut {appid}] → {TEMPLATE}")
        try:
            from shortcut import LAUNCHER_TITLE, get_shortcut_appid
            appid = get_shortcut_appid(LAUNCHER_TITLE)
            if appid is not None:
                _write_layout(uid, str(appid), [shortcut_title_key(LAUNCHER_TITLE)])
        except Exception as ex:
            prog(f"  ⚠ Offline launcher layout skipped: {ex}")

    # Own-copy shortcuts use appids derived from their executable paths.
    try:
        from detect_games import find_own_installed
        from shortcut import OWN_SHORTCUTS, own_shortcut_exe
        import config as cfg
        for key, game in (find_own_installed() or {}).items():
            if key not in OWN_SHORTCUTS or not game.get("exe_path"):
                continue
            if key == "cod4mp" and cfg.get_setup_games().get(key, {}).get("client") == "cod4x":
                continue  # legacy CoD4x is not supported by this gamepad layout
            title = OWN_SHORTCUTS[key]["name"]
            appid = str(calc_shortcut_appid(f'"{own_shortcut_exe(key, game)}"', title))
            for uid in uids:
                _write_layout(uid, appid, [shortcut_title_key(title)], numbered=False)
            prog(f"  ✓ [own {appid}] → {TEMPLATE}")
    except Exception as ex:
        prog(f"  ⚠ Could not assign own-copy layouts: {ex}")
    prog("Steam Deck controller layout assigned.")


def assign_external_controller_profiles(controller_type=None, gyro_mode=None, on_progress=None):
    """Compatibility shim for old callers; a Neptune layout cannot drive other pads."""
    (on_progress or (lambda msg: None))(
        "  - No external controller layout bundled; leaving its Steam Input layout unchanged.")
