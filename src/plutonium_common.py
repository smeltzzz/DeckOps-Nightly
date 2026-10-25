"""
plutonium_common.py - helpers shared by both Plutonium installers

plutonium.py (OLED, Other, Steam Machine: Plutonium launched directly through
Proton) and plutonium_lcd.py (Steam Deck LCD: Plutonium launched through
Heroic) are different pipelines, but they need a few of the same small
helpers. They import each other lazily, so the helpers used to be copied into
both (plutonium_lcd.py even said so: "so it doesn't need to import private
helpers from plutonium.py"), and the copies could drift apart.

This is a leaf module: it imports only the standard library, log and
steam_common, so either installer can import it without creating a cycle.
Each installer keeps its old private name for these as an alias, so existing
imports of them keep working.
"""

import json
import os
import shutil

from log import get_logger
from steam_common import PROJECT_ROOT

_log = get_logger(__name__)


# Written into a game's install folder once Plutonium is set up for it.
METADATA_FILE = "deckops_plutonium.json"

# DeckOps client-side menu mods packaged as .iwd files (renamed .zip).
# Bundled with the repo at assets/mods/ and copied into Plutonium's storage/
# directory, where they are loaded automatically on game launch.
#
# T6 uses a single consolidated .iwd (deckops_bo2_menu.iwd) for both MP
# and ZM. The Lua inside contains both PopulateButtons_Multiplayer and
# PopulateButtons_Zombie, with the engine's CoD.isZombie dispatch selecting
# the right button set at runtime. Both t6mp and t6zm point at the same
# source file and same destination -- idempotent when both are installed.
MENU_MOD_ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets", "mods")
MENU_MOD_FILES = {
    "t6mp":  ("t6/deckops_bo2_menu.iwd", "storage/t6/raw/deckops_bo2_menu.iwd"),
    "t6zm":  ("t6/deckops_bo2_menu.iwd", "storage/t6/raw/deckops_bo2_menu.iwd"),
}


def wine_path(linux_path: str) -> str:
    """Convert a Linux path to Wine Z: drive notation."""
    return "Z:" + linux_path.replace("/", "\\")


def plut_storage_ready(plut_dir: str) -> bool:
    """
    True if the user has logged in and Plutonium has downloaded its client
    files into plut_dir. Checked by looking for the storage/ folder, which
    Plutonium only creates after a successful login, with at least one game
    subfolder (t4, t5, t6, iw5, demonware) inside it.
    """
    storage = os.path.join(plut_dir, "storage")
    if not os.path.isdir(storage):
        return False
    try:
        return any(
            os.path.isdir(os.path.join(storage, d))
            for d in os.listdir(storage)
        )
    except OSError:
        _log.debug("could not read %s", storage, exc_info=True)
        return False


def install_menu_mod(plut_dir: str, game_key: str, on_progress=None):
    """
    Copy the DeckOps menu mod for game_key into a Plutonium folder.

    Copies the bundled mod file from assets/mods/ into plut_dir's storage
    path (raw/). Plutonium loads .iwd files from raw/ automatically on game
    launch, overriding the default main menu with the DeckOps community
    version (server connect, unlock all, reset stats buttons).

    Uses .iwd in raw/ (not mods/) so it:
    - Uses normal player stats (no separate mod stats)
    - Persists across disconnects
    - Loads automatically on every launch

    plut_dir is the Plutonium/ folder of the game's own prefix (OLED / Other)
    or of Heroic's shared prefix (LCD).

    Skips silently for game keys without a menu mod defined.
    """
    def prog(msg):
        if on_progress:
            on_progress(msg)

    if game_key not in MENU_MOD_FILES:
        return

    asset_rel, local_path = MENU_MOD_FILES[game_key]
    src_file = os.path.join(MENU_MOD_ASSETS_DIR, asset_rel)
    dest_file = os.path.join(plut_dir, local_path)

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


def write_metadata(install_dir: str, data: dict):
    """Write the DeckOps metadata sentinel into a game's install directory.
    Raises OSError if it can't be written."""
    path = os.path.join(install_dir, METADATA_FILE)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
