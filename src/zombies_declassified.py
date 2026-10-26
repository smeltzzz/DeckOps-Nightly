"""
zombies_declassified.py -- Zombies Declassified DLC5 installer for DeckOps

Downloads and installs the Zombies Declassified map pack (10 classic
Zombies maps ported to BO2 T6) via Plutonium. Uses Littlegods'
properly packaged files from files.littlegods.space.

All files go to <plut_dir>/storage/t6/ only. The BO2 game directory
is never touched.
"""

import hashlib
import json
import os
import shutil
import urllib.parse
import urllib.request

from log import get_logger
from net import download as _download, BROWSER_UA

_log = get_logger(__name__)

# Approximate; the UI labels quote it as "~9 GB" and preflight budgets for it.
PACK_SIZE_GB    = 9
ZD_MANIFEST_URL = "https://files.littlegods.space/manifest.txt"
ZD_CDN_BASE     = "https://files.littlegods.space/"
ZD_METADATA     = "deckops_zd.json"

# BO2 Zombies DLC ipaks required for ZD to work
_ZD_REQUIRED_DLC = [f"dlczm{i}.ipak" for i in range(5)]

# Known ZD usermap directories for clean uninstall
_ZD_USERMAPS = [
    "zm_asylum", "zm_cosmodrome", "zm_factory", "zm_moon",
    "zm_pentagon", "zm_prototype", "zm_sumpf", "zm_temple", "zm_theater",
]


def has_bo2_zm_dlc(bo2_dir: str) -> bool:
    zone = os.path.join(bo2_dir, "zone", "all")
    return all(os.path.isfile(os.path.join(zone, f)) for f in _ZD_REQUIRED_DLC)


# ZD script files dropped into storage/t6/raw/ alongside dlc5 + usermaps
_ZD_RAW_FILES = (
    "raw/maps/mp/animscripts/zm_dog_combat.gsc",
    "raw/maps/mp/animscripts/zm_dog_stop.gsc",
    "raw/scripts/zm/zzz_zm_dogfog.csc",
    "raw/scripts/zm/zzz_zm_factoryfog.csc",
    "raw/scripts/zm/zzz_zm_factorypower.csc",
    "raw/scripts/zm/zzz_zm_gglow.csc",
    "raw/scripts/zm/zzz_zm_location.gsc",
    "raw/scripts/zm/zzz_zm_moonsky.csc",
    "raw/scripts/zm/zzz_zm_sumpffog.csc",
)

# Everything ZD puts in storage/t6, relative to it
_ZD_PARTS = ("mods/dlc5", *(f"usermaps/{m}" for m in _ZD_USERMAPS), *_ZD_RAW_FILES)

_T6_APPIDS = {"t6zm": 212910, "t6mp": 202990}


def t6_storage(key, game=None):
    """storage/t6 in the prefix this BO2 key runs in. Worked out from the key itself:
    t6mp and t6zm share deckops_plutonium.json, so its plut_dir can name the wrong one."""
    import config as cfg
    from plutonium import _plut_dir_in_prefix
    game = game or {}
    if cfg.get_setup_games().get(key, {}).get("source") == "own":
        compat = game.get("compatdata_path")
        if not compat and game.get("exe_path"):
            from shortcut import own_plut_prefix
            compat = own_plut_prefix(key, game)
    else:
        from detect_games import find_steam_root
        from wrapper import find_compatdata
        root = find_steam_root()
        compat = root and find_compatdata(root, _T6_APPIDS[key], game.get("install_dir"))
    return os.path.join(_plut_dir_in_prefix(compat), "storage", "t6") if compat else None


def _adopt_zd(dst, t6zm_game):
    """Older versions put ZD in the MP prefix or the master copy. Move it into
    the t6zm prefix once; parts already there stay and the strays are cleaned later."""
    if is_zd_installed(dst):
        return
    from plutonium import get_dedicated_plut_dir
    # Install dir only: the t6zm game's exe/prefix would point own-copy t6mp at the wrong prefix
    mp = t6_storage("t6mp", {"install_dir": (t6zm_game or {}).get("install_dir")})
    srcs = [mp, os.path.join(get_dedicated_plut_dir(), "storage", "t6")]
    meta = os.path.join((t6zm_game or {}).get("install_dir", ""), "deckops_plutonium.json")
    try:
        with open(meta) as f:
            pd = json.load(f).get("plut_dir", "")
        if pd:
            srcs.insert(0, os.path.join(pd, "storage", "t6"))
    except (OSError, ValueError):
        pass
    for src in srcs:
        if not src or os.path.realpath(src) == os.path.realpath(dst) or not is_zd_installed(src):
            continue
        _log.info("Moving Zombies Declassified from %s to %s", src, dst)
        for part in _ZD_PARTS:
            s, d = os.path.join(src, part), os.path.join(dst, part)
            if os.path.lexists(s) and not os.path.lexists(d):
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.move(s, d)
        return


def resolve_zd_storage(t6zm_game=None):
    """Plutonium storage/t6 that ZD lives in: the t6zm prefix, never MP or the master.
    One lookup for install, update and Options, so an update always finds the existing install."""
    import config as cfg
    if cfg.is_lcd():
        from plutonium_lcd import get_shared_plut_dir
        pd = get_shared_plut_dir()
        return os.path.join(pd, "storage", "t6") if pd else None
    dst = t6_storage("t6zm", t6zm_game)
    if dst:
        try:
            _adopt_zd(dst, t6zm_game)
        except OSError:
            _log.warning("Could not move Zombies Declassified into %s", dst, exc_info=True)
    return dst


def _manifest_hash(file_list: list) -> str:
    return hashlib.sha256("\n".join(file_list).encode()).hexdigest()[:16]


# Everything ZD ships lives under one of these (relative to storage/t6)
_ALLOWED_PREFIXES = ("mods/dlc5/", "usermaps/zm_", "raw/maps/", "raw/scripts/")


def _check_relpath(relpath: str) -> bool:
    """A manifest entry must be a plain relative path under one of ZD's own
    folders: no absolute path, no "..", no backslashes or empty parts.
    Entries went straight into os.path.join before, so a tampered manifest
    (plain-text, from a third-party CDN) could write anywhere the user can."""
    if not relpath or relpath.startswith("/") or "\\" in relpath or "\x00" in relpath:
        return False
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        return False
    return relpath.startswith(_ALLOWED_PREFIXES)


def _safe_dest(plut_storage_t6: str, relpath: str) -> str:
    """Destination of a checked entry, which must also resolve inside
    storage/t6 (a symlinked folder could otherwise lead outside)."""
    root = os.path.realpath(plut_storage_t6)
    dest = os.path.realpath(os.path.join(plut_storage_t6, relpath))
    if not dest.startswith(root + os.sep):
        raise RuntimeError(f"Zombies Declassified path escapes storage/t6: {relpath}")
    return os.path.join(plut_storage_t6, relpath)


def fetch_manifest(on_progress=None):
    """Fetch manifest.txt from Littlegods CDN. Returns list of relative paths.
    Refuses the whole manifest if any entry isn't a safe ZD path."""
    if on_progress:
        on_progress(0, "Checking for Zombies Declassified files...")

    req = urllib.request.Request(ZD_MANIFEST_URL, headers=BROWSER_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8-sig")

    files = [line.strip() for line in raw.splitlines() if line.strip()]

    if not files:
        raise RuntimeError("Empty manifest from Littlegods CDN")

    bad = [f for f in files if not _check_relpath(f)]
    if bad:
        raise RuntimeError(f"Zombies Declassified manifest has unexpected paths, refusing it: {bad[:5]}")

    return files


def _file_url(relpath: str) -> str:
    return ZD_CDN_BASE + urllib.parse.quote(relpath)


def _remote_info(url: str):
    """(size, etag) from a HEAD request, (None, None) if unavailable."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers=BROWSER_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            size = r.headers.get("Content-Length")
            return (int(size) if size and size.isdigit() else None,
                    (r.headers.get("ETag") or "").strip())
    except Exception:
        _log.debug("HEAD failed for %s", url, exc_info=True)
        return None, None


def _etag_matches(path: str, etag: str):
    """
    Check a file against the CDN's ETag. True/False, or None if the ETag
    isn't a checksum we can reproduce. The CDN is S3-style: a plain ETag is
    the file's MD5; a multipart one ("<md5>-<N>") is the MD5 of the N parts'
    MD5 digests (8 MiB parts on this CDN; other common sizes tried too).
    """
    tag = etag.strip()
    if tag.startswith("W/"):
        return None
    tag = tag.strip('"').lower()
    digest, _, parts = tag.partition("-")
    if len(digest) != 32 or any(c not in "0123456789abcdef" for c in digest):
        return None
    if not parts:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == digest
    if not parts.isdigit():
        return None
    n, size = int(parts), os.path.getsize(path)
    checked = False
    for part_size in (8 << 20, 16 << 20, 5 << 20, 15 << 20, 64 << 20, 100 << 20):
        if -(-size // part_size) != n:
            continue
        checked = True
        md5s = []
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(part_size), b""):
                md5s.append(hashlib.md5(chunk).digest())
        if hashlib.md5(b"".join(md5s)).hexdigest() == digest:
            return True
    # An unfamiliar multipart chunk size cannot prove the file is bad.
    return False if checked else None


def _metadata_path(plut_storage_t6: str) -> str:
    return os.path.join(plut_storage_t6, "mods", "dlc5", ZD_METADATA)


def is_zd_installed(plut_storage_t6: str) -> bool:
    return os.path.isfile(_metadata_path(plut_storage_t6))


def get_zd_info(plut_storage_t6: str) -> dict:
    mp = _metadata_path(plut_storage_t6)
    if not os.path.isfile(mp):
        return {}
    try:
        with open(mp) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_metadata(plut_storage_t6: str, manifest_files: list, failed=()):
    from datetime import datetime
    mp = _metadata_path(plut_storage_t6)
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    # A partial install must not look complete: update_zd skips when the
    # stored hash equals the manifest's, so failed files were never retried.
    # "incomplete-..." never equals it, so the next update fetches them.
    h = _manifest_hash(manifest_files)
    meta = {
        "manifest_hash": h if not failed else f"incomplete-{h}",
        "installed_at": datetime.now().isoformat(),
        "file_count": len(manifest_files),
        "source": "littlegods",
    }
    if failed:
        meta["failed_files"] = list(failed)
    tmp = mp + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, mp)


def install_zd(plut_storage_t6: str, on_progress=None, manifest_files=None, results=None):
    """
    Download and install Zombies Declassified.

    plut_storage_t6 -- path to <plut_dir>/storage/t6/
    on_progress     -- callback(percent: int, message: str)
    """
    prog = on_progress or (lambda *a: None)

    files = manifest_files if manifest_files is not None else fetch_manifest(on_progress)
    total = len(files)
    if results is not None:
        results["downloaded"] = 0
        results["unverified"] = 0

    prog(2, f"Installing Zombies Declassified ({total} files)...")

    errors = []
    for idx, relpath in enumerate(files):
        dest = _safe_dest(plut_storage_t6, relpath)
        url = _file_url(relpath)
        size, etag = _remote_info(url)

        # Check existing files too: an unchanged list of filenames does not
        # mean the installed maps are still present or uncorrupted. When the
        # CDN exposes a reproducible ETag, a same-sized damaged file is repaired.
        prog(2 + int(idx / total * 96), f"Checking {idx + 1}/{total}: {relpath}")
        try:
            present = os.path.isfile(dest) and os.path.getsize(dest) > 0
            same_size = size is None or (present and os.path.getsize(dest) == size)
            checksum = _etag_matches(dest, etag) if present and same_size and etag else None
        except OSError:
            present, same_size, checksum = False, False, None
        if present and same_size and checksum is not False:
            pct = 2 + int((idx + 1) / total * 96)
            if results is not None and size is None and checksum is not True:
                results["unverified"] += 1
                prog(pct, f"Could not verify {idx + 1}/{total}: {relpath} (kept existing file)")
            else:
                prog(pct, f"Checked {idx + 1}/{total}: {relpath}")
            continue

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        pct = 2 + int(idx / total * 96)
        prog(pct, f"Downloading {idx + 1}/{total}: {relpath}")

        _span = 96.0 / total
        try:
            _download(
                url, dest,
                on_progress=lambda p, m, _i=idx: prog(
                    2 + int(_i * _span + p * _span / 100), m),
                label=f"{idx + 1}/{total}: {relpath}", timeout=300)
        except Exception as ex:
            # net.download re-raises the network error itself, not DownloadError:
            # catching only DownloadError let the first failure abort the whole pack
            _log.warning("ZD download failed for %s: %s", relpath, ex)
            errors.append(relpath)
            continue

        problem = None
        if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
            problem = "empty or missing after download"
        elif size is not None and os.path.getsize(dest) != size:
            problem = f"size {os.path.getsize(dest)} != {size}"
        elif etag and _etag_matches(dest, etag) is False:
            problem = "checksum (ETag) mismatch"
        if problem:
            _log.warning("ZD file rejected, %s: %s", problem, relpath)
            errors.append(relpath)
            try:
                os.remove(dest)
            except OSError:
                pass
        elif results is not None:
            results["downloaded"] += 1

    _write_metadata(plut_storage_t6, files, errors)

    if errors:
        prog(100, f"Installed with {len(errors)} error(s)")
        _log.warning("ZD install errors: %s", errors)
    else:
        prog(100, "Zombies Declassified installed!")

    return errors


def update_zd(plut_storage_t6: str, on_progress=None):
    """Check and repair installed files. Returns True if anything changed."""
    prog = on_progress or (lambda *a: None)

    current = get_zd_info(plut_storage_t6)
    current_hash = current.get("manifest_hash", "")
    if not current_hash:
        # No install at this path: never turn an update into a fresh ~9 GB download
        prog(100, f"Zombies Declassified not found in {plut_storage_t6}, skipped.")
        _log.warning("update_zd: no ZD metadata at %s", plut_storage_t6)
        return False

    files = fetch_manifest(on_progress)
    results = {}
    errors = install_zd(plut_storage_t6, on_progress, manifest_files=files, results=results)
    if errors:
        raise RuntimeError(f"{len(errors)} Zombies Declassified file(s) could not be repaired; try again")
    if results["unverified"]:
        raise RuntimeError(f"Could not verify {results['unverified']} Zombies Declassified file(s); existing files were kept. Try again when the CDN is available")
    changed = current_hash != _manifest_hash(files) or results["downloaded"] > 0
    if not changed:
        prog(100, "Zombies Declassified files checked; no changes needed.")
    return changed


def uninstall_zd(plut_storage_t6: str, on_progress=None):
    """Remove Zombies Declassified files from storage/t6/."""
    prog = on_progress or (lambda *a: None)

    prog(0, "Removing Zombies Declassified...")

    # Remove mods/dlc5/
    dlc5_dir = os.path.join(plut_storage_t6, "mods", "dlc5")
    if os.path.isdir(dlc5_dir):
        shutil.rmtree(dlc5_dir, ignore_errors=True)
    prog(20, "Removed mods/dlc5/")

    # Remove usermaps/zm_*/
    um_dir = os.path.join(plut_storage_t6, "usermaps")
    for mapname in _ZD_USERMAPS:
        mp = os.path.join(um_dir, mapname)
        if os.path.isdir(mp):
            shutil.rmtree(mp, ignore_errors=True)
    prog(70, "Removed usermaps/")

    # Remove raw/ scripts
    for subpath in _ZD_RAW_FILES:
        fp = os.path.join(plut_storage_t6, subpath)
        if os.path.isfile(fp):
            os.remove(fp)
    prog(90, "Removed raw scripts")

    prog(100, "Zombies Declassified removed.")
