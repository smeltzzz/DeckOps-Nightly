"""
iw4x.py - DeckOps installer for IW4x (Modern Warfare 2)

Downloads iw4x.dll, release.zip, and the IW4x launcher from GitHub.
release.zip contents are relocated to the launcher-compatible layout:
  iw4x/           → main/iw4x/x86/
  zone/patch/      → zone/iw4x/x86/patch/
  zone/zonebuilder/→ zone/iw4x/x86/zonebuilder/

The launcher (iw4x-launcher.exe) handles self-updating on each launch.
Steam games get a "Play IW4x" launch menu entry that runs the launcher.

Optionally downloads free DLC content from cdn.iw4x.io, including:
  - MW2 DLC map packs (main/iw4x/x86/*.iwd)
  - CoD4 ported maps → zone/iw4x/x86/dlc/*.ff
  - Black Ops maps   → zone/iw4x/x86/dlc/*.ff
  - MW3 maps         → zone/iw4x/x86/dlc/*.ff
  - CoD Online maps  → zone/iw4x/x86/dlc/*.ff

Progress is reported via a callback:
    on_progress(percent: int, status: str)
"""

import os
import json
import shutil
import stat
import tempfile
import urllib.parse
import zipfile
import threading

from net import (download as _download, github_asset, safe_manifest_path,
                 commit_staged_files)

from log import get_logger

_log = get_logger(__name__)


# iw4x.dll comes from the client repo, everything else from rawfiles.
# release.zip contains iw4x.exe, all iwd files, zone patches,
# and other assets. No separate downloads needed.
DLL_URL = "https://github.com/iw4x/iw4x-client/releases/latest/download/iw4x.dll"
ZIP_URL = "https://github.com/iw4x/iw4x-rawfiles/releases/latest/download/release.zip"

# CDN manifest for free DLC content (maps from CoD4, BO1, MW3, CoD Online, MW2 DLC)
# Approximate; the UI labels quote it as "~3 GB" and preflight budgets for it.
DLC_SIZE_GB      = 3
DLC_MANIFEST_URL = "https://cdn.iw4x.io/update.json"
DLC_CDN_BASE     = "https://cdn.iw4x.io/"

MW2_MP_APPID = "10190"

# Manifest path prefixes that contain .ff files, all remapped to zone/iw4x/x86/dlc/.
_FF_PREFIXES = (
    "iw3/zone/dlc/",
    "t5/zone/dlc/",
    "iw5/zone/dlc/",
    "codo/zone/dlc/",
)


# ── helpers ───────────────────────────────────────────────────────────────────

# _download imported from net.py; call sites pass timeout=120 for large files.


def _remap_dlc_path(manifest_path: str, install_dir: str) -> str:
    """
    Remap a CDN manifest path to the correct local destination.

    .ff files from any game-prefixed subdirectory (iw3/, t5/, iw5/, codo/)
    are all placed into zone/iw4x/x86/dlc/ to match the correct IW4x layout.
    .iwd files under iw4x/ go to main/iw4x/x86/ (launcher layout).
    """
    safe_manifest_path(install_dir, manifest_path)
    for prefix in _FF_PREFIXES:
        if manifest_path.startswith(prefix):
            filename = manifest_path[len(prefix):]
            return safe_manifest_path(install_dir, "zone/iw4x/x86/dlc/" + filename)
    # iw4x/*.iwd → main/iw4x/x86/ to match launcher layout
    if manifest_path.startswith("iw4x/"):
        return safe_manifest_path(install_dir, "main/iw4x/x86/" + manifest_path[5:])
    return safe_manifest_path(install_dir, manifest_path)


_DLC_FF_FILENAMES = {
    # CoD4 (iw3)
    "mp_convoy_load.ff", "mp_backlot_load.ff", "mp_broadcast_load.ff",
    "mp_pipeline_load.ff", "mp_killhouse.ff", "mp_broadcast.ff",
    "mp_countdown.ff", "mp_showdown_load.ff", "mp_carentan.ff",
    "mp_citystreets_load.ff", "mp_convoy.ff", "mp_farm_load.ff",
    "mp_cargoship_load.ff", "mp_cargoship.ff", "mp_backlot.ff",
    "mp_carentan_load.ff", "mp_crash_snow.ff", "mp_cross_fire_load.ff",
    "mp_countdown_load.ff", "mp_bloc.ff", "mp_killhouse_load.ff",
    "mp_farm.ff", "mp_citystreets.ff", "mp_bloc_load.ff",
    "mp_cross_fire.ff", "mp_showdown.ff", "mp_crash_snow_load.ff",
    "mp_pipeline.ff",
    # Black Ops (t5)
    "mp_firingrange.ff", "mp_nuked_load.ff", "mp_firingrange_load.ff",
    "mp_nuked.ff",
    # MW3 (iw5)
    "mp_village.ff", "mp_bravo.ff", "mp_paris.ff", "mp_underground_load.ff",
    "mp_hardhat_load.ff", "mp_underground.ff", "mp_plaza2_load.ff",
    "mp_bravo_load.ff", "mp_paris_load.ff", "mp_hardhat.ff",
    "mp_plaza2.ff", "mp_seatown.ff", "mp_alpha_load.ff", "mp_dome.ff",
    "mp_dome_load.ff", "mp_seatown_load.ff", "mp_village_load.ff",
    "mp_alpha.ff",
    # CoD Online (codo)
    "mp_storm_spring_load.ff", "mp_fav_tropical.ff", "mp_estate_tropical.ff",
    "mp_fav_tropical_load.ff", "mp_cargoship_sh_load.ff", "mp_bloc_sh_load.ff",
    "mp_crash_tropical_load.ff", "mp_crash_tropical.ff", "mp_cargoship_sh.ff",
    "mp_estate_tropical_load.ff", "mp_shipment_load.ff", "mp_rust_long_load.ff",
    "mp_shipment_long_load.ff", "mp_rust_long.ff", "mp_storm_spring.ff",
    "mp_shipment.ff", "mp_shipment_long.ff", "mp_bog_sh_load.ff",
    "mp_bog_sh.ff", "mp_nuked_shaders.ff", "mp_bloc_sh.ff",
}


def _remove_dlc_ff(install_dir: str):
    """Remove DLC .ff files from zone/iw4x/x86/dlc/ (and legacy zone/dlc/)."""
    for dlc_dir in (
        os.path.join(install_dir, "zone", "iw4x", "x86", "dlc"),
        os.path.join(install_dir, "zone", "dlc"),
    ):
        if not os.path.isdir(dlc_dir):
            continue
        for fname in _DLC_FF_FILENAMES:
            p = os.path.join(dlc_dir, fname)
            if os.path.exists(p):
                os.remove(p)
        # Clean up empty parent directories
        d = dlc_dir
        while d != install_dir and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            d = os.path.dirname(d)


def is_iw4x_dlc_installed(install_dir: str) -> bool:
    """Returns True if DLC content appears to be present."""
    markers = [
        os.path.join(install_dir, "main", "iw4x", "x86", "iw_dlc3_00.iwd"),       # MW2 DLC iwd
        os.path.join(install_dir, "zone", "iw4x", "x86", "dlc", "mp_backlot.ff"),  # CoD4 ff
        os.path.join(install_dir, "zone", "iw4x", "x86", "dlc", "mp_nuked.ff"),    # BO1 ff
    ]
    if all(os.path.exists(m) for m in markers):
        return True
    # Check legacy paths for installs that haven't been migrated yet
    legacy = [
        os.path.join(install_dir, "iw4x", "iw_dlc3_00.iwd"),
        os.path.join(install_dir, "zone", "dlc", "mp_backlot.ff"),
        os.path.join(install_dir, "zone", "dlc", "mp_nuked.ff"),
    ]
    return all(os.path.exists(m) for m in legacy)


# ── DLC migration ────────────────────────────────────────────────────────────

def _migrate_dlc_ff(install_dir: str):
    """Move .ff files from legacy zone/dlc/ to zone/iw4x/x86/dlc/."""
    old_dir = os.path.join(install_dir, "zone", "dlc")
    if not os.path.isdir(old_dir):
        return
    new_dir = os.path.join(install_dir, "zone", "iw4x", "x86", "dlc")
    moved = 0
    for fname in _DLC_FF_FILENAMES:
        old = os.path.join(old_dir, fname)
        if os.path.isfile(old):
            os.makedirs(new_dir, exist_ok=True)
            shutil.move(old, os.path.join(new_dir, fname))
            moved += 1
    if moved:
        _log.info("Migrated %d DLC .ff files from zone/dlc/ to zone/iw4x/x86/dlc/", moved)
    # Clean up empty legacy directory
    d = old_dir
    while d != install_dir and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)


def _migrate_old_layout(install_dir: str):
    """Migrate from old DeckOps layout to launcher-compatible layout."""
    # Restore iw4mp.exe from backup if the old exe-swap is present
    iw4mp_bak = os.path.join(install_dir, "iw4mp.exe.bak")
    if os.path.exists(iw4mp_bak):
        iw4mp = os.path.join(install_dir, "iw4mp.exe")
        if os.path.exists(iw4mp):
            os.remove(iw4mp)
        os.rename(iw4mp_bak, iw4mp)
        _log.info("Restored iw4mp.exe from backup")

    _moves = [
        ("iw4x", os.path.join("main", "iw4x", "x86")),
        (os.path.join("zone", "patch"), os.path.join("zone", "iw4x", "x86", "patch")),
        (os.path.join("zone", "zonebuilder"), os.path.join("zone", "iw4x", "x86", "zonebuilder")),
    ]
    for old_rel, new_rel in _moves:
        old_abs = os.path.join(install_dir, old_rel)
        new_abs = os.path.join(install_dir, new_rel)
        if not os.path.isdir(old_abs):
            continue
        os.makedirs(os.path.dirname(new_abs), exist_ok=True)
        if os.path.isdir(new_abs):
            for item in os.listdir(old_abs):
                target = os.path.join(new_abs, item)
                # A freshly installed client takes precedence over legacy
                # files; keep any conflicting old file rather than replacing it.
                if not os.path.exists(target):
                    shutil.move(os.path.join(old_abs, item), target)
            if not os.listdir(old_abs):
                os.rmdir(old_abs)
        else:
            shutil.move(old_abs, new_abs)
        _log.info("Migrated %s → %s", old_rel, new_rel)


def _relocate_extracted(install_dir: str):
    """Move release.zip dirs from flat layout to launcher-compatible layout."""
    _moves = [
        ("iw4x", os.path.join("main", "iw4x", "x86")),
        (os.path.join("zone", "patch"), os.path.join("zone", "iw4x", "x86", "patch")),
        (os.path.join("zone", "zonebuilder"), os.path.join("zone", "iw4x", "x86", "zonebuilder")),
    ]
    for old_rel, new_rel in _moves:
        old_abs = os.path.join(install_dir, old_rel)
        new_abs = os.path.join(install_dir, new_rel)
        if not os.path.isdir(old_abs):
            continue
        os.makedirs(os.path.dirname(new_abs), exist_ok=True)
        if os.path.isdir(new_abs):
            for item in os.listdir(old_abs):
                src = os.path.join(old_abs, item)
                dst = os.path.join(new_abs, item)
                if os.path.exists(dst):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    else:
                        os.remove(dst)
                shutil.move(src, dst)
            shutil.rmtree(old_abs)
        else:
            shutil.move(old_abs, new_abs)


def _stage_archive(archive: str, staged_files: str, relocate: bool = False):
    """Extract regular archive files to staging, validating names before writing."""
    with zipfile.ZipFile(archive) as zf:
        entries = []
        for info in zf.infolist():
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            safe_manifest_path(staged_files, name)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink in IW4x archive: {info.filename!r}")
            if not info.is_dir():
                rel = name
                if relocate:
                    for old, new in (("iw4x/", "main/iw4x/x86/"),
                                     ("zone/patch/", "zone/iw4x/x86/patch/"),
                                     ("zone/zonebuilder/", "zone/iw4x/x86/zonebuilder/")):
                        if name.startswith(old):
                            rel = new + name[len(old):]
                            break
                safe_manifest_path(staged_files, rel)
                entries.append((info, rel))
        for info, rel in entries:
            dest = safe_manifest_path(staged_files, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as source, open(dest, "wb") as output:
                shutil.copyfileobj(source, output)


def _old_client_files(install_dir: str, remove_dlc: bool):
    """Files the former installer removed under main/iw4x/x86."""
    root = safe_manifest_path(install_dir, "main/iw4x/x86")
    if not os.path.isdir(root):
        return []
    old = []
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise ValueError(f"symlink in existing IW4x files: {path}")
        for name in files:
            path = os.path.join(directory, name)
            if remove_dlc or directory != root or not name.endswith(".iwd"):
                old.append(os.path.relpath(path, install_dir).replace(os.sep, "/"))
    return old


# Steam copy launch menu entry. Launcher ships x86 and x64 (mm) clients
# since v1.1.8-b.20; without --arch it may show a terminal picker nobody
# can answer in Game Mode.
IW4X_MENU = [
    {"executable": "iw4x-launcher.exe", "arguments": "--arch x86", "description": "Play IW4x"},
]

# The stock MW2 MP entry is hidden from the picker (flagged macOS-only, nothing is
# deleted): the campaign's Multiplayer button starts this tile, and with only the
# IW4x entry left it goes straight into IW4x.
IW4X_STOCK_ENTRY = {"executable": "iw4mp.exe", "hide": True}


def refresh_steam_menu(steam_root: str, log=lambda m: None) -> bool:
    """Put the IW4x launch menu on the Steam MW2 MP tile (10190) and hide the stock
    entry, for a copy set up earlier with the old menu. Only when iw4x-launcher.exe
    is in the game folder. Steam must be closed. Returns True if the menu was applied."""
    import config as cfg
    entry = cfg.get_setup_games().get("iw4mp", {})
    if entry.get("client") != "iw4x" or entry.get("source", "steam") != "steam":
        return False
    from detect_games import parse_library_folders, find_installed_games
    game = find_installed_games(parse_library_folders(steam_root), steam_root).get("iw4mp") or {}
    install_dir = game.get("install_dir", "")
    if not install_dir or not os.path.isfile(os.path.join(install_dir, "iw4x-launcher.exe")):
        return False
    from steam_appinfo import add_launch_entries
    ok = add_launch_entries(steam_root, int(MW2_MP_APPID), IW4X_MENU + [IW4X_STOCK_ENTRY])
    log("  ✓ MW2 Multiplayer: IW4x only" if ok else
        "  ⚠ MW2 Multiplayer menu not applied yet, will retry next time Steam is closed")
    return ok


# ── DLC install ──────────────────────────────────────────────────────────────

def install_iw4x_dlc(install_dir: str, on_progress=None):
    """
    Download and install free DLC content from cdn.iw4x.io.

    Fetches the manifest (update.json), then downloads every file listed
    in it to the correct relative path under install_dir. All .ff files
    are remapped into zone/iw4x/x86/dlc/ regardless of their manifest prefix.
    Files are downloaded with up to 4 concurrent workers.

    on_progress — optional callback(percent: int, status: str)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    prog = on_progress or (lambda *a: None)

    # ── Fetch manifest ────────────────────────────────────────────────────
    prog(0, "Fetching DLC manifest...")
    # Fetch into a temporary folder: a failed or unsafe manifest must not
    # overwrite an existing update.json in the player's game directory.
    with tempfile.TemporaryDirectory(prefix=".deckops_iw4x_manifest_", dir=install_dir) as temp:
        manifest_path = os.path.join(temp, "update.json")
        _download(DLC_MANIFEST_URL, manifest_path, None, "DLC manifest", timeout=120)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    files = manifest.get("files", [])
    # Reject the entire manifest before legacy migrations or directory writes.
    dirs_needed = {os.path.dirname(_remap_dlc_path(entry["path"], install_dir))
                   for entry in files}
    _migrate_dlc_ff(install_dir)
    _migrate_old_layout(install_dir)
    if not files:
        prog(100, "No DLC files found in manifest.")
        return

    # ── Create destination directories ────────────────────────────────────
    prog(2, "Creating DLC directories...")
    for d in dirs_needed:
        os.makedirs(d, exist_ok=True)

    # ── Download all files concurrently ───────────────────────────────────
    total_files = len(files)
    dl_done     = [0]
    dl_errors   = []
    dl_lock     = threading.Lock()

    def _dl_one(entry):
        rel_path = entry["path"]
        dest     = _remap_dlc_path(rel_path, install_dir)
        name     = entry.get("asset_name", os.path.basename(rel_path))
        url      = DLC_CDN_BASE + urllib.parse.quote(rel_path, safe="/")

        # This CDN manifest provides sizes but no trustworthy hashes. A
        # size match is not an integrity check; downloaded DLC cannot be
        # cryptographically verified until the upstream publishes digests.
        expected_size = entry.get("size", 0)
        if os.path.exists(dest) and expected_size:
            try:
                actual = os.path.getsize(dest)
                if actual == expected_size:
                    with dl_lock:
                        dl_done[0] += 1
                        prog(2 + int(dl_done[0] / total_files * 96),
                             f"Skipped {name} (already exists)")
                    return
            except OSError:
                _log.debug("size check failed", exc_info=True)

        _download(url, dest, None, name, timeout=120)
        with dl_lock:
            dl_done[0] += 1
            prog(2 + int(dl_done[0] / total_files * 96),
                 f"Downloaded {name} ({dl_done[0]}/{total_files})")

    prog(3, f"Downloading {total_files} DLC files (~3 GB)...")

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_dl_one, entry): entry for entry in files}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                name = futs[fut].get("asset_name", "unknown")
                dl_errors.append(f"{name}: {e}")

    if dl_errors:
        raise RuntimeError("DLC download failed:\n" + "\n".join(dl_errors))

    prog(100, "DLC installation complete!")


# ── public API ────────────────────────────────────────────────────────────────

def install_iw4x(game: dict, steam_root: str,
                 proton_path: str, compatdata_path: str,
                 on_progress=None, source: str = "steam",
                 install_dlc: bool = False,
                 remove_dlc: bool = False):
    """
    Install or reinstall IW4x for Modern Warfare 2.

    Downloads iw4x.dll, release.zip, and the IW4x launcher concurrently.
    release.zip contents are relocated to the launcher-compatible layout.
    For Steam games, adds a "Play IW4x" launch menu entry (iw4x-launcher.exe --arch x86).
    """
    install_dir = game["install_dir"]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    base_end = 50 if install_dlc else 100

    def prog(pct, msg):
        if on_progress:
            scaled = int(pct / 100 * base_end)
            on_progress(scaled, msg)

    # Order matters: resolve and download everything (into a staging folder)
    # and check it BEFORE touching the existing install. The old client files
    # used to be deleted first, so a GitHub rate limit (60 requests/hour
    # unauthenticated), an outage or a failed download left MW2 with IW4x
    # half removed.

    # ── Resolve URLs + sha256 digests ────────────────────────────────────
    # The launcher zip name is versioned, so its lookup has no fallback URL.
    # The launcher updates itself on every launch, so an existing one is kept
    # when the lookup fails; only a first install needs it.
    prog(2, "Checking latest launcher version...")
    existing_launcher = os.path.isfile(os.path.join(install_dir, "iw4x-launcher.exe"))
    try:
        launcher_url, launcher_dg = github_asset("iw4x/launcher", "x86_64-windows.zip")
    except Exception as ex:
        if not existing_launcher:
            raise RuntimeError(f"Could not look up the IW4x launcher download: {ex}") from ex
        _log.warning("IW4x launcher lookup failed, keeping the installed launcher: %s", ex)
        prog(3, "Launcher lookup failed -- keeping the installed launcher (it self-updates)")
        launcher_url = launcher_dg = None
    dll_url, dll_dg = github_asset("iw4x/iw4x-client", "iw4x.dll", fallback=DLL_URL)
    zip_url, zip_dg = github_asset("iw4x/iw4x-rawfiles", "release.zip", fallback=ZIP_URL)

    with tempfile.TemporaryDirectory(prefix=".deckops_iw4x_", dir=install_dir) as staging:
        # ── Download iw4x.dll, release.zip, and launcher concurrently ────
        prog(5, "Downloading iw4x files...")

        staged_dll = os.path.join(staging, "iw4x.dll")
        staged_zip = os.path.join(staging, "release.zip")
        launcher_zip = os.path.join(staging, "launcher.zip")
        dl_tasks = [
            (dll_url, staged_dll, "iw4x.dll", dll_dg),
            (zip_url, staged_zip, "release.zip", zip_dg),
        ]
        if launcher_url:
            dl_tasks.append((launcher_url, launcher_zip, "iw4x-launcher", launcher_dg))
        dl_errors = []
        dl_done   = [0]
        dl_lock   = threading.Lock()

        def _dl(url, dest, label, digest):
            _download(url, dest, None, f"Downloading {label}...", timeout=120, digest=digest)
            with dl_lock:
                dl_done[0] += 1
                prog(5 + int(dl_done[0] / len(dl_tasks) * 40), f"Downloaded {label}")

        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(_dl, *t): t[2] for t in dl_tasks}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    dl_errors.append(f"{futs[fut]}: {e}")

        if dl_errors:
            raise RuntimeError("Download failed (existing IW4x left untouched):\n"
                               + "\n".join(dl_errors))

        # Corrupt archives are caught here too, while nothing is changed yet
        for z in [staged_zip] + ([launcher_zip] if launcher_url else []):
            try:
                with zipfile.ZipFile(z) as zf:
                    bad = zf.testzip()
            except zipfile.BadZipFile as e:
                raise RuntimeError(f"{os.path.basename(z)} is not a valid zip "
                                   f"(existing IW4x left untouched): {e}") from e
            if bad:
                raise RuntimeError(f"{os.path.basename(z)} is corrupt at {bad} "
                                   f"(existing IW4x left untouched)")

        # Extract to a separate tree. No playable file is touched until all
        # archives have passed validation and extraction.
        staged_files = os.path.join(staging, "files")
        os.makedirs(staged_files)
        prog(50, "Extracting release.zip...")
        _stage_archive(staged_zip, staged_files, relocate=True)
        shutil.copy2(staged_dll, os.path.join(staged_files, "iw4x.dll"))
        if launcher_url:
            prog(70, "Extracting iw4x-launcher...")
            _stage_archive(launcher_zip, staged_files)

        staged_paths = []
        for directory, _, names in os.walk(staged_files):
            for name in names:
                staged_paths.append(os.path.relpath(os.path.join(directory, name), staged_files)
                                    .replace(os.sep, "/"))
        remove = _old_client_files(install_dir, remove_dlc)
        # Old DLC files are intentionally removed only when the caller asked
        # for it; include them in the same rollback as the base update.
        if remove_dlc:
            for dlc_dir in ("zone/iw4x/x86/dlc", "zone/dlc"):
                for name in _DLC_FF_FILENAMES:
                    rel = dlc_dir + "/" + name
                    if os.path.isfile(safe_manifest_path(install_dir, rel)):
                        remove.append(rel)
            legacy = safe_manifest_path(install_dir, "iw4x")
            if os.path.isdir(legacy):
                for name in os.listdir(legacy):
                    if name.endswith(".iwd"):
                        rel = "iw4x/" + name
                        if os.path.isfile(safe_manifest_path(install_dir, rel)):
                            remove.append(rel)
        prog(75, "Installing IW4x files...")
        commit_staged_files(install_dir, staged_files, staged_paths, remove)

    # Legacy layout migrations are deferred until after the base commit. A
    # failed download or replacement must not move an existing installation.
    try:
        if not remove_dlc:
            _migrate_dlc_ff(install_dir)
        _migrate_old_layout(install_dir)
    except OSError as ex:
        # Legacy migration is best-effort; the newly committed client remains
        # playable even when an old-layout file cannot be moved.
        _log.warning("IW4x legacy layout migration incomplete: %s", ex)
        prog(77, f"Could not migrate all legacy files: {ex}")

    # ── Launch menu (Steam only) ─────────────────────────────────────────
    # "Play IW4x" runs the launcher directly, which updates and then starts the
    # x86 client; the stock entry is hidden (IW4X_STOCK_ENTRY). If Steam drops
    # the menu, the launch option's prelaunch.sh turns plain Play into IW4x.
    if source != "own":
        prog(80, "Adding IW4x to the Steam launch menu...")
        try:
            from wrapper import clear_launch_options, set_launch_options
            from steam_appinfo import add_launch_entries
            from steam_common import prelaunch_prefix
            clear_launch_options(steam_root, MW2_MP_APPID)
            set_launch_options(steam_root, MW2_MP_APPID,
                               'DECKOPS_MENU="iw4mp.exe=iw4x-launcher.exe" DECKOPS_MENU_ARGS="--arch x86" '
                               f'{prelaunch_prefix("menu")}%command%')
            if add_launch_entries(steam_root, MW2_MP_APPID, IW4X_MENU + [IW4X_STOCK_ENTRY]):
                prog(80, "  ✓ Launch menu: Play IW4x")
            else:
                prog(80, "  ⚠ Launch menu not added yet, will retry next time Steam is closed")
        except Exception as ex:
            prog(80, f"Could not add launch menu: {ex}")

    prog(100, "IW4x base installation complete!")

    # ── Optional DLC download ─────────────────────────────────────────────
    if install_dlc:
        def dlc_prog(pct, msg):
            if on_progress:
                on_progress(50 + int(pct / 100 * 50), msg)
        install_iw4x_dlc(install_dir, on_progress=dlc_prog)
