"""Install the bundled Steam Deck layout for DeckOps-managed games.

Steam Input layouts are controller-type specific. This bundle is a Neptune
(Steam Deck) layout; do not assign it to external controllers or other devices.
Run assignments while Steam is closed so its cached configsets cannot overwrite
our changes on exit. Legacy DeckOps layout removal remains in the uninstaller.
"""

import hashlib
import os
import shutil
import time

from log import get_logger
from steam_common import (
    PROJECT_ROOT, default_steam_root,
    calc_shortcut_appid, find_all_steam_uids, get_deck_serial,
    patch_configset, shortcut_title_key, keep_original, atomic_write,
)

_log = get_logger(__name__)
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets", "controllers")
# None: the templates folder of the Steam install DeckOps is set up for (templates_dir()).
# Set it only to point the module somewhere else (tests).
TEMPLATES_DIR = None
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


def templates_dir() -> str:
    """Steam's global controller templates folder. A fixed ~/.steam/steam only
    exists for the native install: with Flatpak Steam (or one that moved) the layout
    went into a folder Steam never reads, and a stray one was created in the home."""
    return TEMPLATES_DIR or os.path.join(default_steam_root(), "controller_base", "templates")


def install_layout(src: str, canonical_path: str):
    """Put DeckOps' layout at canonical_path (all at once, so a crash can't leave a cut-off
    file Steam then reads) and record its hash in <path>.deckops-owned.

    The first time the player's own file is kept as <path>.deckops-original. When the file
    is no longer what DeckOps wrote last time, the player (or Steam) changed it since: that
    version is kept as <path>.deckops-user-<time> before it is replaced."""
    with open(src, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    marker = canonical_path + ".deckops-owned"
    if os.path.exists(marker):
        try:
            with open(marker, encoding="ascii") as m:
                written = m.read().strip()
            with open(canonical_path, "rb") as cur:
                now = hashlib.sha256(cur.read()).hexdigest()
        except (OSError, UnicodeDecodeError):
            written = now = None
        if written and now and now not in (written, digest):
            try:
                shutil.copy2(canonical_path, f"{canonical_path}.deckops-user-{int(time.time())}")
                _log.info("kept the changed layout %s before replacing it", canonical_path)
            except OSError:
                _log.warning("could not keep the changed layout %s", canonical_path, exc_info=True)
    else:
        keep_original(canonical_path)
    atomic_write(canonical_path, data)
    atomic_write(marker, digest.encode("ascii"))


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
    templates = templates_dir()
    if os.path.isdir(templates) and not os.path.isdir(other_build):
        for name in LEGACY_TEMPLATES:
            path = os.path.join(templates, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as ex:
                prog(f"  ⚠ Could not remove old DeckOps template {name}: {ex}")
    if not deck:
        prog("  - Steam Deck layout skipped on this device; no external layout is bundled.")
        return
    os.makedirs(templates, exist_ok=True)
    shutil.copy2(src, os.path.join(templates, TEMPLATE))
    prog(f"  ✓ Installed {TEMPLATE}")


def _shortcut_appids(prog):
    """Return LCD WaW MP shortcut appids; CoD4R MP uses the main Steam tile."""
    try:
        from detect_games import parse_library_folders, find_installed_games
        from shortcut import SHORTCUTS, mp_on_main_tile
        # The Steam the layouts are written into (_write_layout), not the first one found:
        # with a native and a Flatpak Steam both installed they can differ
        steam_root = default_steam_root()
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
    steam_root = default_steam_root()
    userdata_dir = os.path.join(steam_root, "userdata", uid, "241100", "remote", "controller_config", appid)
    steam_cfg = os.path.join(steam_root, "steamapps", "common", "Steam Controller Configs", uid, "config")
    os.makedirs(userdata_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(userdata_dir, TEMPLATE))
    if numbered:
        target_dir = os.path.join(steam_cfg, appid)
        os.makedirs(target_dir, exist_ok=True)
        install_layout(src, os.path.join(target_dir, CANONICAL))
    configsets = [CONFIGSET]
    serial = get_deck_serial()
    if serial:
        configsets.append(f"configset_{serial}.vdf")
    for configset in configsets:
        path = os.path.join(steam_cfg, configset)
        patch_configset(path, appid, TEMPLATE)
        for name in names:
            patch_configset(path, name, TEMPLATE)


def _write_layout_safe(uid, appid, names=(), numbered=True, prog=None):
    """_write_layout, but a disk/permission error for one account or game is
    reported and skipped so the rest still get their layout. True if written."""
    try:
        _write_layout(uid, appid, names, numbered=numbered)
        return True
    except OSError as ex:
        _log.warning("controller layout for %s (uid %s) not written: %s", appid, uid, ex)
        if prog:
            prog(f"  ⚠ Layout for {appid} (account {uid}) not written: {ex}")
        return False


def assign_controller_profiles(gyro_mode=None, on_progress=None):
    """Apply the one Deck layout to supported games, own copies and shortcuts.

    Returns how many layouts were written (0: none could be, so the caller must not
    report them as applied), or None when this device has no layout to apply."""
    prog = on_progress or (lambda msg: None)
    if not _is_steam_deck():
        prog("  - Controller assignment skipped: bundled layout is Steam Deck only.")
        return None
    if not os.path.isfile(os.path.join(ASSETS_DIR, TEMPLATE)):
        raise FileNotFoundError(f"Steam Deck controller layout missing: {TEMPLATE}")
    uids = find_all_steam_uids()
    if not uids:
        prog("  ⚠ No Steam user accounts found; controller assignment skipped.")
        return 0
    written = 0
    for uid in uids:
        for appid, names in APPID_NAMED_KEYS.items():
            if _write_layout_safe(uid, appid, names, prog=prog):
                written += 1
                prog(f"  ✓ [{appid}] → {TEMPLATE}")
        for appid, title in _shortcut_appids(prog).items():
            if _write_layout_safe(uid, appid, [shortcut_title_key(title)], prog=prog):
                written += 1
                prog(f"  ✓ [shortcut {appid}] → {TEMPLATE}")
        try:
            from shortcut import LAUNCHER_TITLE, get_shortcut_appid
            appid = get_shortcut_appid(LAUNCHER_TITLE)
            if appid is not None and _write_layout_safe(
                    uid, str(appid), [shortcut_title_key(LAUNCHER_TITLE)], prog=prog):
                written += 1
        except Exception as ex:
            prog(f"  ⚠ Offline launcher layout skipped: {ex}")

    # Own-copy shortcuts use appids derived from their executable paths.
    try:
        from detect_games import find_own_installed_saved
        from shortcut import OWN_SHORTCUTS, own_shortcut_exe
        import config as cfg
        setup = cfg.get_setup_games() or {}
        # Only own copies DeckOps set up have a shortcut; any other copy the
        # scan finds (or a game set up from Steam) must not get a layout.
        own_keys = {k for k, e in setup.items()
                    if isinstance(e, dict) and e.get("source") == "own"}
        # Choose Folder picks included: an own copy outside the default
        # folders has a shortcut too
        for key, game in (find_own_installed_saved() or {}).items():
            if key not in OWN_SHORTCUTS or key not in own_keys or not game.get("exe_path"):
                continue
            if key == "cod4mp" and setup[key].get("client") == "cod4x":
                continue  # legacy CoD4x is not supported by this gamepad layout
            title = OWN_SHORTCUTS[key]["name"]
            appid = str(calc_shortcut_appid(f'"{own_shortcut_exe(key, game)}"', title))
            wrote = [_write_layout_safe(uid, appid, [shortcut_title_key(title)], numbered=False, prog=prog)
                     for uid in uids]
            if any(wrote):
                written += sum(wrote)
                prog(f"  ✓ [own {appid}] → {TEMPLATE}")
    except Exception as ex:
        prog(f"  ⚠ Could not assign own-copy layouts: {ex}")
    if written:
        prog("Steam Deck controller layout assigned.")
    else:
        prog("  ⚠ No controller layout could be written.")
    return written


def assign_external_controller_profiles(controller_type=None, gyro_mode=None, on_progress=None):
    """Compatibility shim for old callers; a Neptune layout cannot drive other pads."""
    (on_progress or (lambda msg: None))(
        "  - No external controller layout bundled; leaving its Steam Input layout unchanged.")
