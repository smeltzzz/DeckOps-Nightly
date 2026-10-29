"""
ge_proton.py - DeckOps GE-Proton installer

Downloads and installs the latest GE-Proton release from GitHub, then
writes the CompatToolMapping entry in Steam's config.vdf so each game
uses it automatically.

Also provides ensure_prefix_deps() which copies the full dependency set
(d3dx, vcrun, xinput, partial xact) from GE-Proton's default_pfx into
any game prefix that's missing them. This eliminates the need for users
to launch each game once before mods can be installed.

Install path:
    ~/.steam/root/compatibilitytools.d/GE-ProtonX-XX/

CompatToolMapping written to:
    ~/.local/share/Steam/config/config.vdf
"""

import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request

from log import get_logger
from net import BROWSER_UA as _BROWSER_UA, download
from steam_common import nvme_compatdata as _nvme_compatdata, newest_proton_dir

_log = get_logger(__name__)


GITHUB_API   = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
COMPAT_DIR   = os.path.expanduser("~/.local/share/Steam/compatibilitytools.d")

# Steam appids that DeckOps manages — GE-Proton will be set for all of these
MANAGED_APPIDS = [
    "7940",    # CoD4
    "10090",   # WaW
    "10180",   # MW2 SP
    "10190",   # MW2 MP
    "42680",   # MW3 SP
    "42690",   # MW3 MP
    "42750",   # MW3 Dedicated Server
    "42700",   # BO1 Campaign/Zombies
    "42710",   # BO1 Multiplayer
    "202970",  # BO2 Campaign
    "202990",  # BO2 Multiplayer
    "212910",  # BO2 Zombies
]


# ── GitHub API ────────────────────────────────────────────────────────────────

def _get_latest_release():
    """
    Query the GitHub API for the latest GE-Proton release.
    Returns (version, tarball_url, checksum_url).
    """
    req = urllib.request.Request(GITHUB_API, headers=_BROWSER_UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    version = data["tag_name"]  # e.g. "GE-Proton11-7"
    tarball_url  = None
    checksum_url = None
    tarball_asset = None

    from detect_hw import detect_arch
    arch = detect_arch()

    for asset in data.get("assets", []):
        name = asset["name"]
        if name.endswith(f"-{arch}.tar.gz"):
            tarball_url = asset["browser_download_url"]
            tarball_asset = name
        elif name.endswith(f"-{arch}.sha512sum"):
            checksum_url = asset["browser_download_url"]

    if not tarball_url:
        raise RuntimeError(f"No {arch} .tar.gz asset found for {version}")

    # GE-Proton 11+ tarballs extract to a directory matching the asset
    # name minus .tar.gz (e.g. "GE-Proton11-7-x86_64"), not the tag.
    # Steam registers compat tools by directory name, so this must match.
    dir_name = tarball_asset.removesuffix(".tar.gz")

    return version, tarball_url, checksum_url, dir_name


def _is_complete(tool_dir: str) -> bool:
    """True if tool_dir holds a usable GE-Proton: the proton script, the
    compatibilitytool.vdf Steam registers it by, and the default prefix
    DeckOps clones. A folder left by an interrupted extraction fails this."""
    return (os.path.isfile(os.path.join(tool_dir, "proton"))
            and os.path.isfile(os.path.join(tool_dir, "compatibilitytool.vdf"))
            and os.path.isdir(os.path.join(tool_dir, "files", "share", "default_pfx")))


def _is_installed(version):
    """Returns True if this GE-Proton version is fully extracted. Checking
    only that the folder exists treated a half-extracted one (disk full,
    killed mid-install) as up to date forever."""
    return _is_complete(os.path.join(COMPAT_DIR, version))


def _get_local_version() -> str | None:
    """
    Scan compatibilitytools.d for the newest installed GE-Proton version.
    Returns the version string (e.g. 'GE-Proton10-32') or None if not found.
    Works regardless of how GE-Proton was installed (ProtonUp-Qt, manual, etc.)
    """
    return newest_proton_dir(COMPAT_DIR, "GE-Proton")


def resolve_ge_version(fresh: str | None = None) -> str | None:
    """GE-Proton version to map onto the games.

    The one just installed wins. If the download failed (offline, GitHub rate
    limit), use the version DeckOps pinned or the newest complete one already
    on disk, so games keep a compat tool. None means there is no usable
    GE-Proton at all; callers must then leave Steam's existing mappings alone
    rather than clearing entries they cannot put back.
    """
    if fresh:
        return fresh
    try:
        import config as _cfg
        pinned = _cfg.get_ge_proton_version()
    except Exception:
        pinned = None
    for candidate in (pinned, _get_local_version()):
        if candidate and _is_installed(candidate):
            return candidate
    return None


# ── default_pfx resolution ───────────────────────────────────────────────────

def _find_default_pfx(ge_version: str | None) -> str | None:
    """
    Locate GE-Proton's default_pfx directory.

    Tries the exact version first, then falls back to scanning for the
    newest available GE-Proton install. Returns the path or None.
    """
    # Try exact version first
    if ge_version:
        candidate = os.path.join(COMPAT_DIR, ge_version, "files", "share", "default_pfx")
        if os.path.isdir(candidate):
            return candidate

    # Fallback: newest GE-Proton that has a default_pfx
    if os.path.isdir(COMPAT_DIR):
        for entry in sorted(os.listdir(COMPAT_DIR), reverse=True):
            if not entry.startswith("GE-Proton"):
                continue
            candidate = os.path.join(COMPAT_DIR, entry, "files", "share", "default_pfx")
            if os.path.isdir(candidate):
                return candidate

    return None


# ── Install ───────────────────────────────────────────────────────────────────

def install_ge_proton(on_progress=None):
    """
    Download and install the latest GE-Proton to compatibilitytools.d.
    Returns the version string (e.g. 'GE-Proton10-34') so it can be
    passed to set_compat_tool().

    on_progress — optional callback(percent: int, msg: str)
    """
    prog = on_progress or (lambda *a: None)

    local_version = _get_local_version()
    prog(0, "Checking for GE-Proton updates...")

    version, tarball_url, checksum_url, dir_name = _get_latest_release()

    if _is_installed(dir_name):
        prog(100, f"GE-Proton {dir_name} is up to date.")
        return dir_name

    if local_version == dir_name or os.path.isdir(os.path.join(COMPAT_DIR, dir_name)):
        prog(5, f"GE-Proton {dir_name} is incomplete (interrupted install?) -- reinstalling")
    else:
        prog(5, f"New version available: {dir_name}")
    os.makedirs(COMPAT_DIR, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="deckops_ge_") as tmp:
        tarball_path = os.path.join(tmp, f"{dir_name}.tar.gz")

        digest = None
        if checksum_url:
            # Format: "<hash>  <filename>". net.download verifies it and
            # retries a mismatch instead of failing the install outright.
            req = urllib.request.Request(checksum_url, headers=_BROWSER_UA)
            with urllib.request.urlopen(req, timeout=15) as resp:
                digest = "sha512:" + resp.read().decode().split()[0]
        else:
            _log.warning("%s has no checksum file, downloading unverified", dir_name)

        prog(10, f"Downloading {dir_name}...")
        download(tarball_url, tarball_path, on_progress=on_progress, label=dir_name,
                 digest=digest)

        prog(87, f"Extracting {dir_name}...")
        _extract_atomically(tarball_path, dir_name)
        prog(100, f"GE-Proton {dir_name} installed.")

    return dir_name


def _safe_extractall(tar: tarfile.TarFile, dest: str):
    """extractall that can't write outside dest. Uses tarfile's "data"
    filter where available (Python 3.12, backported to 3.11.4+), otherwise
    the same checks by hand: no absolute or ../ member names, no links
    pointing outside dest, no device files."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
        return
    root = os.path.realpath(dest)
    def inside(path):
        p = os.path.realpath(path)
        return p == root or p.startswith(root + os.sep)
    for m in tar.getmembers():
        target = os.path.join(dest, m.name)
        if os.path.isabs(m.name) or not inside(target):
            raise RuntimeError(f"unsafe path in GE-Proton archive: {m.name!r}")
        if m.issym():      # relative to the link's own folder
            link_target = os.path.join(os.path.dirname(target), m.linkname)
        elif m.islnk():    # hard links name another member, relative to dest
            link_target = os.path.join(dest, m.linkname)
        else:
            link_target = None
        if link_target is not None and (os.path.isabs(m.linkname) or not inside(link_target)):
            raise RuntimeError(f"unsafe link in GE-Proton archive: {m.name!r} -> {m.linkname!r}")
        if m.isdev():
            raise RuntimeError(f"device file in GE-Proton archive: {m.name!r}")
    tar.extractall(dest)


def _extract_atomically(tarball_path: str, dir_name: str):
    """
    Extract a GE-Proton tarball so compatibilitytools.d/<dir_name> only ever
    appears complete: extract into a hidden staging folder on the same
    filesystem, check the result, then rename it into place. Any failure
    removes the staging folder and raises, leaving no half-extracted tool
    behind (which _is_installed used to accept as installed for good).
    """
    import subprocess

    staging = tempfile.mkdtemp(prefix=".deckops_extract_", dir=COMPAT_DIR)
    try:
        # System tar first: much faster and lighter than tarfile on a
        # multi-GB archive. Any failure falls back to the safe tarfile path.
        ok = False
        try:
            r = subprocess.run(["tar", "-xzf", tarball_path, "-C", staging],
                               capture_output=True, text=True)
            ok = r.returncode == 0
            if not ok:
                _log.warning("tar failed (%s): %s", r.returncode, r.stderr.strip()[-500:])
        except OSError as ex:          # tar not installed
            _log.warning("tar unavailable: %s", ex)
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging)
            with tarfile.open(tarball_path, "r:gz") as tar:
                _safe_extractall(tar, staging)

        extracted = os.path.join(staging, dir_name)
        if not _is_complete(extracted):
            found = ", ".join(sorted(os.listdir(staging))) or "nothing"
            raise RuntimeError(
                f"GE-Proton archive did not contain a complete {dir_name}/ (found: {found})")

        final = os.path.join(COMPAT_DIR, dir_name)
        if os.path.lexists(final):
            # Only reached when it failed _is_installed: a broken leftover
            if os.path.isdir(final) and not os.path.islink(final):
                shutil.rmtree(final)
            else:
                os.remove(final)
        os.rename(extracted, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ── CompatToolMapping ─────────────────────────────────────────────────────────
# Canonical implementation lives in wrapper.set_compat_tool.
# Imported here so callers can use ge_proton.set_compat_tool as before.

from wrapper import set_compat_tool  # noqa: F401


# ── Prefix dependency management ─────────────────────────────────────────────
# GE-Proton's default_pfx ships with the full d3dx, vcrun, xinput, and
# xact dependency set that these CoD games need. Instead of running
# winetricks verbs or relying on Steam to install them on first launch,
# we copy the DLLs directly from default_pfx into each game's prefix.
# This works for both Steam and own games.

def _copy_dlls(src_dir: str, dest_dir: str) -> tuple[int, int]:
    """
    Copy .dll files from src_dir into dest_dir, skipping files that
    already exist with a matching file size.

    Returns (copied, skipped) counts.
    """
    if not os.path.isdir(src_dir):
        return (0, 0)
    os.makedirs(dest_dir, exist_ok=True)
    copied = 0
    skipped = 0
    for fname in os.listdir(src_dir):
        if fname.lower().endswith(".dll"):
            src_path = os.path.join(src_dir, fname)
            dest_path = os.path.join(dest_dir, fname)
            # Skip if dest exists and matches source file size
            if os.path.isfile(dest_path):
                try:
                    if os.path.getsize(dest_path) == os.path.getsize(src_path):
                        skipped += 1
                        continue
                except OSError:
                    pass  # can't stat -- copy it
            # Unlock read-only files (Proton sometimes creates prefix
            # files with 0o444 permissions)
            if os.path.isfile(dest_path):
                try:
                    os.chmod(dest_path, 0o644)
                except OSError:
                    pass
            shutil.copy2(src_path, dest_path)
            copied += 1
    return (copied, skipped)


def ensure_prefix_deps(ge_version: str | None, prefix_path: str,
                       on_progress=None, proton_path: str | None = None,
                       steam_root: str | None = None) -> bool:
    """
    Make sure a game's compatdata prefix is fully initialized and has
    the complete dependency set from GE-Proton's default_pfx.

    Logic:
      1. If no proton_path, fall back to copytree from default_pfx and return.
      2. If pfx/drive_c already exists (prefix previously initialized):
         - Copy any missing DLLs from default_pfx (fast, no Proton run)
         - Skip the slow `proton run cmd /c exit` step entirely
      3. If pfx/drive_c does NOT exist (fresh prefix):
         - Create prefix_path so Proton has a directory to work with
         - Copy DLLs from default_pfx into system32 + syswow64
         - Run `proton run cmd /c exit` to finalize the prefix

    ge_version  -- GE-Proton version string (e.g. "GE-Proton10-33")
    prefix_path -- compatdata root (e.g. ~/.../compatdata/10090)
    on_progress -- optional callback(msg: str) for log messages
    proton_path -- path to the proton binary. When provided, the prefix is
                   finalized by Proton after DLL copy. Falls back to copytree
                   if not provided.
    steam_root  -- path to Steam root. Used for STEAM_COMPAT_CLIENT_INSTALL_PATH.
                   Falls back to deriving from proton_path if not provided.

    Returns True if deps are now in place, False if we couldn't do it.
    """
    prog = on_progress or (lambda *a: None)

    default_pfx = _find_default_pfx(ge_version)
    if not default_pfx:
        prog("⚠ No GE-Proton default_pfx found — cannot install dependencies")
        return False

    pfx_dir      = os.path.join(prefix_path, "pfx")
    sys32_target = os.path.join(pfx_dir, "drive_c", "windows", "system32")
    wow64_target = os.path.join(pfx_dir, "drive_c", "windows", "syswow64")
    version_file = os.path.join(prefix_path, "version")

    # Check if the prefix has already been initialized by Proton.
    # If drive_c exists, the prefix structure is in place — we only need
    # to copy any missing DLLs and can skip the slow Proton run.
    prefix_exists = os.path.isdir(os.path.join(pfx_dir, "drive_c"))

    # ── Fallback: no proton_path ───────────────────────────────────────
    # Can't run Proton to finalize — copy entire default_pfx as a best effort.
    if not proton_path:
        try:
            os.makedirs(prefix_path, exist_ok=True)
            if not os.path.isdir(pfx_dir):
                shutil.copytree(default_pfx, pfx_dir, symlinks=True)
            if ge_version and not os.path.isfile(version_file):
                with open(version_file, "w") as f:
                    f.write(ge_version + "\n")
            prog("  ✓ Prefix created from default_pfx (no Proton path — fallback)")
            return True
        except Exception as ex:
            prog(f"  ⚠ Prefix creation failed: {ex}")
            return False

    # STEAM_COMPAT_CLIENT_INSTALL_PATH should be the Steam root so Proton
    # can find steamclient.so. Fall back to dirname trick if not provided.
    _compat_install = steam_root or os.path.dirname(os.path.dirname(proton_path))

    # ── Step 1: Ensure the prefix directory exists ─────────────────────
    # Proton needs the compatdata directory to exist before it can initialize.
    # Don't create pfx/ itself — Proton does that in the finalize step.
    if not prefix_exists:
        os.makedirs(prefix_path, exist_ok=True)

    # ── Step 2: Always copy DLLs from default_pfx ─────────────────────
    # Copy before Proton finalizes so Proton sees the full dependency set
    # when it writes its management files.
    sys32_src = os.path.join(default_pfx, "drive_c", "windows", "system32")
    wow64_src = os.path.join(default_pfx, "drive_c", "windows", "syswow64")

    try:
        c32, s32 = _copy_dlls(sys32_src, sys32_target)
        c64, s64 = _copy_dlls(wow64_src, wow64_target)
        total_copied = c32 + c64
        total_skipped = s32 + s64
        if total_copied > 0:
            prog(f"  ✓ Copied {total_copied} DLLs, skipped {total_skipped} (already present)")
        else:
            prog(f"  ✓ All {total_skipped} DLLs already present")
    except Exception as ex:
        prog(f"  ⚠ DLL copy failed: {ex}")
        return False

    # ── Step 3: Finalize via Proton (only if prefix is new) ───────────
    # Only run `proton run cmd /c exit` when the prefix hasn't been
    # initialized yet. This is the slow step (~60s per prefix) that
    # creates the Wine prefix structure, wineserver, registry, etc.
    # For existing prefixes, the DLL copy above is sufficient.
    if prefix_exists:
        prog("  ✓ Prefix already initialized — skipped Proton run")
        return True

    import subprocess
    try:
        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"]           = prefix_path
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = _compat_install
        subprocess.run(
            [proton_path, "run", "cmd", "/c", "exit"],
            env=env, capture_output=True, timeout=180,
        )
        prog("  ✓ Prefix finalized by Proton")
        return True
    except Exception as ex:
        prog(f"  ⚠ Proton prefix finalize failed: {ex}")
        return False


def _finish_clone(source_pfx_dir: str, dest_prefix_path: str, ge_version: str | None):
    # Write version file so Proton knows which version initialized this prefix
    if ge_version:
        with open(os.path.join(dest_prefix_path, "version"), "w") as f:
            f.write(ge_version + "\n")
    # Copy tracked_files from the donor's parent directory.
    # Proton requires this file to exist -- without it, prefix setup
    # crashes with FileNotFoundError on update_builtin_libs().
    donor_tracked = os.path.join(os.path.dirname(source_pfx_dir), "tracked_files")
    if os.path.isfile(donor_tracked):
        shutil.copy2(donor_tracked, os.path.join(dest_prefix_path, "tracked_files"))


def _clone_prefix(source_pfx_dir: str, dest_prefix_path: str,
                  ge_version: str | None, on_progress=None) -> bool:
    """
    Clone an initialized prefix to a new appid's compatdata directory.

    Copies the pfx/ directory tree from a fully initialized source prefix,
    writes the version file, and copies tracked_files. This produces an
    identical prefix in ~2s instead of running `proton run cmd /c exit`
    (~60-180s).

    The donor prefix already contains all DLLs from default_pfx, so
    clones do NOT need a DLL top-up — skipping it saves ~45s per prefix
    on SD card.

    source_pfx_dir   -- the pfx/ directory inside the donor prefix
    dest_prefix_path -- the compatdata root for the target appid
    ge_version       -- GE-Proton version string for the version file
    on_progress      -- optional callback(msg: str)

    Returns True on success, False on failure.
    """
    prog = on_progress or (lambda *a: None)

    dest_pfx_dir = os.path.join(dest_prefix_path, "pfx")
    try:
        import time
        os.makedirs(dest_prefix_path, exist_ok=True)
        prog(f"  Cloning: {source_pfx_dir} → {dest_pfx_dir}")
        start = time.time()
        shutil.copytree(source_pfx_dir, dest_pfx_dir, symlinks=True)
        elapsed = time.time() - start
        _finish_clone(source_pfx_dir, dest_prefix_path, ge_version)
        prog(f"  ✓ Prefix cloned from donor ({elapsed:.1f}s)")
        return True
    except Exception as ex:
        prog(f"  ⚠ Prefix clone failed: {ex}")
        return False


def _overlay_prefix(source_pfx_dir: str, dest_prefix_path: str,
                    ge_version: str | None, on_progress=None) -> bool:
    """
    Overlay a finalized donor prefix onto an existing prefix, copying
    only files that are missing or differ in size.

    For new prefixes (no pfx/ dir yet), this is equivalent to a full clone.
    For existing prefixes, this fills in any missing files without
    re-copying the ~1200 DLLs that are already there.

    Also copies tracked_files and writes the version file.

    source_pfx_dir   -- the pfx/ directory inside the donor prefix
    dest_prefix_path -- the compatdata root for the target appid
    on_progress      -- optional callback(msg: str)

    Returns True on success, False on failure.
    """
    prog = on_progress or (lambda *a: None)

    import time

    dest_pfx_dir = os.path.join(dest_prefix_path, "pfx")
    is_new = not os.path.isdir(dest_pfx_dir)

    try:
        os.makedirs(dest_prefix_path, exist_ok=True)

        if is_new:
            # Fresh prefix -- full clone (fastest path)
            prog(f"  Cloning: {source_pfx_dir} -> {dest_pfx_dir}")
            start = time.time()
            shutil.copytree(source_pfx_dir, dest_pfx_dir, symlinks=True)
            elapsed = time.time() - start
            prog(f"  ✓ Prefix cloned from donor ({elapsed:.1f}s)")
        else:
            # Existing prefix -- overlay missing files only
            start = time.time()
            copied = 0
            skipped = 0
            for src_dirpath, dirnames, filenames in os.walk(source_pfx_dir):
                rel = os.path.relpath(src_dirpath, source_pfx_dir)
                dst_dirpath = os.path.join(dest_pfx_dir, rel)
                os.makedirs(dst_dirpath, exist_ok=True)

                # Handle symlinked directories (e.g. dosdevices/c: -> ../drive_c,
                # d: -> /run/media/deck/...). Recreate as symlinks in the
                # destination instead of descending into the target.
                real_dirnames = []
                for dname in dirnames:
                    src_dir = os.path.join(src_dirpath, dname)
                    if os.path.islink(src_dir):
                        dst_dir = os.path.join(dst_dirpath, dname)
                        link_target = os.readlink(src_dir)
                        if os.path.islink(dst_dir) or os.path.exists(dst_dir):
                            skipped += 1
                        else:
                            os.symlink(link_target, dst_dir)
                            copied += 1
                    else:
                        real_dirnames.append(dname)
                dirnames[:] = real_dirnames

                for fname in filenames:
                    src_file = os.path.join(src_dirpath, fname)
                    dst_file = os.path.join(dst_dirpath, fname)

                    # Handle symlinked files (e.g. dosdevices/h:: -> /dev/sda2).
                    # Proton maps drive letters and block devices as symlinks
                    # in dosdevices/. These vary by hardware (nvme, sda, mmcblk)
                    # and distro (SteamOS, Bazzite, CachyOS). Copy them as
                    # symlinks rather than trying to read the target.
                    if os.path.islink(src_file):
                        link_target = os.readlink(src_file)
                        if os.path.islink(dst_file) or os.path.exists(dst_file):
                            skipped += 1
                        else:
                            os.symlink(link_target, dst_file)
                            copied += 1
                        continue

                    if os.path.exists(dst_file):
                        try:
                            if os.path.getsize(dst_file) == os.path.getsize(src_file):
                                skipped += 1
                                continue
                        except OSError:
                            pass
                    # Skip existing read-only files -- these are Proton
                    # prefix scaffolding (wordpad.exe, odbccp32.dll, etc.)
                    # that differ in size between Proton versions but are
                    # not needed by any game.  Game-critical DLLs are
                    # handled separately by _copy_dlls.
                    if os.path.exists(dst_file) and not os.access(dst_file, os.W_OK):
                        skipped += 1
                        continue
                    shutil.copy2(src_file, dst_file)
                    copied += 1

            elapsed = time.time() - start
            if copied > 0:
                prog(f"  ✓ Overlaid {copied} files, skipped {skipped} ({elapsed:.1f}s)")
            else:
                prog(f"  ✓ All {skipped} files already present ({elapsed:.1f}s)")

        _finish_clone(source_pfx_dir, dest_prefix_path, ge_version)
        return True
    except Exception as ex:
        prog(f"  ⚠ Prefix overlay failed: {ex}")
        return False


# ── Shared DLL directory for symlinked prefixes ───────────────────────────────
# Instead of copying 600MB+ of identical DLLs into every prefix, we keep
# one real copy and symlink each prefix's system32/syswow64 to it.
# This lives under DeckOps's data dir so Steam can't touch it.

SHARED_DLL_DIR = os.path.expanduser("~/.local/share/deckops/shared_dlls")
# GE-Proton version the shared DLLs were last refreshed from
SHARED_DLL_STAMP = ".ge_version"

# A symlink Proton made to one of its own Wine builtin libraries, e.g.
# .../compatibilitytools.d/GE-Proton10-34/files/lib/wine/x86_64-windows/kernel32.dll
_WINE_LIB_LINK = re.compile(r"/files/(lib(?:64)?/wine/.+)$")


def _is_wine_builtin(path: str) -> bool:
    """True for a Wine builtin/placeholder DLL, including a link into some
    Proton's lib/wine that may be broken. Same test as Proton's
    file_is_wine_builtin_dll: these are the files Proton itself replaces
    when it upgrades a prefix. Anything else (a game's or redist's native
    DLL) is never touched."""
    if os.path.islink(path):
        target = os.readlink(path).replace("\\", "/")
        if re.search(r"/lib(?:64)?/wine(?:/[^/]+)?$", os.path.dirname(target)):
            return True
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            f.seek(0x40)
            return f.read(20).startswith((b"Wine placeholder DLL", b"Wine builtin DLL"))
    except OSError:
        return False


def _same_file(a: str, b: str) -> bool:
    import filecmp
    try:
        return not os.path.islink(b) and filecmp.cmp(a, b, shallow=True)
    except OSError:
        return False


def _refresh_dll_dir(src: str, dst: str) -> tuple[int, int]:
    """Bring dst up to date with src (a default_pfx system32/syswow64)
    without deleting anything: add missing files and replace Wine builtins
    that differ. Native DLLs a game or redist installed are kept.
    Returns (added, replaced)."""
    added = replaced = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = os.path.normpath(os.path.join(dst, rel))
        os.makedirs(out, exist_ok=True)
        for name in files:
            s, d = os.path.join(root, name), os.path.join(out, name)
            if not os.path.exists(s):          # broken link in default_pfx
                continue
            if os.path.lexists(d):
                if not _is_wine_builtin(d) or _same_file(s, d):
                    continue
                os.remove(d)
                replaced += 1
            else:
                added += 1
            shutil.copy2(s, d)   # follows default_pfx's links: a real copy, as before
    return added, replaced


def _read_stamp() -> str | None:
    try:
        with open(os.path.join(SHARED_DLL_DIR, SHARED_DLL_STAMP)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _ensure_shared_dlls(ge_version: str | None, on_progress=None) -> bool:
    """
    Ensure the shared DLL directory holds GE-Proton's default_pfx
    system32 and syswow64.

    No-op if it was last refreshed from this GE-Proton version. Otherwise
    missing files are added and Wine builtins replaced, never deleted: every
    symlinked prefix uses these folders, and they can hold native DLLs a
    game installed. (This used to rmtree and re-copy both folders whenever
    the new GE-Proton had more DLLs, and skip a new version otherwise.)

    Returns True if shared DLLs are ready, False on failure.
    """
    prog = on_progress or (lambda *a: None)

    default_pfx = _find_default_pfx(ge_version)
    if not default_pfx:
        prog("⚠ No GE-Proton default_pfx found -- cannot set up shared DLLs")
        return False

    shared_sys32 = os.path.join(SHARED_DLL_DIR, "system32")
    shared_wow64 = os.path.join(SHARED_DLL_DIR, "syswow64")
    src_sys32 = os.path.join(default_pfx, "drive_c", "windows", "system32")
    src_wow64 = os.path.join(default_pfx, "drive_c", "windows", "syswow64")
    # The version default_pfx really came from (it falls back to the newest)
    source_version = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.normpath(default_pfx)))))

    if (os.path.isdir(shared_sys32) and os.path.isdir(shared_wow64)
            and _read_stamp() == source_version):
        prog(f"  ✓ Shared DLLs up to date ({source_version})")
        return True

    import time
    prog(f"  Updating shared DLLs from {source_version}...")
    start = time.time()

    try:
        a32, r32 = _refresh_dll_dir(src_sys32, shared_sys32)
        a64, r64 = _refresh_dll_dir(src_wow64, shared_wow64)
        with open(os.path.join(SHARED_DLL_DIR, SHARED_DLL_STAMP), "w") as f:
            f.write(source_version + "\n")
        elapsed = time.time() - start
        prog(f"  ✓ Shared DLLs ready: {a32 + a64} added, {r32 + r64} updated ({elapsed:.1f}s)")
        return True
    except Exception as ex:
        prog(f"  ⚠ Shared DLL setup failed: {ex}")
        return False


def _clone_with_symlinks(source_pfx_dir: str, dest_prefix_path: str,
                         ge_version: str | None, on_progress=None) -> bool:
    """
    Clone a donor prefix but symlink system32 and syswow64 to the shared
    DLL directory instead of copying them. This reduces each clone from
    ~725MB to ~120MB.

    For the donor prefix itself, system32/syswow64 are real directories.
    All other prefixes get symlinks.

    source_pfx_dir   -- the pfx/ directory inside the donor prefix
    dest_prefix_path -- the compatdata root for the target appid
    ge_version       -- GE-Proton version string for the version file
    on_progress      -- optional callback(msg: str)

    Returns True on success, False on failure.
    """
    prog = on_progress or (lambda *a: None)

    import time

    dest_pfx_dir = os.path.join(dest_prefix_path, "pfx")

    try:
        os.makedirs(dest_prefix_path, exist_ok=True)
        start = time.time()

        # Copy the prefix skeleton -- everything except the heavy DLL dirs
        def _ignore_dll_dirs(directory, contents):
            # Only ignore at the windows/ level
            if os.path.basename(directory) == "windows":
                return [c for c in contents if c in ("system32", "syswow64")]
            return []

        prog(f"  Cloning skeleton: {source_pfx_dir} -> {dest_pfx_dir}")
        shutil.copytree(source_pfx_dir, dest_pfx_dir, symlinks=True,
                        ignore=_ignore_dll_dirs)

        # Create symlinks for system32 and syswow64 pointing to shared DLLs
        windows_dir = os.path.join(dest_pfx_dir, "drive_c", "windows")
        shared_sys32 = os.path.join(SHARED_DLL_DIR, "system32")
        shared_wow64 = os.path.join(SHARED_DLL_DIR, "syswow64")

        os.symlink(shared_sys32, os.path.join(windows_dir, "system32"))
        os.symlink(shared_wow64, os.path.join(windows_dir, "syswow64"))

        elapsed = time.time() - start
        prog(f"  ✓ Prefix cloned with symlinked DLLs ({elapsed:.1f}s)")

        _finish_clone(source_pfx_dir, dest_prefix_path, ge_version)
        return True
    except Exception as ex:
        prog(f"  ⚠ Symlinked clone failed: {ex}")
        return False


SD_PREFIX_ASIDE_SUFFIX = ".deckops_old"


def _cleanup_sd_card_prefixes(appids: list[str], on_progress=None):
    """
    Move SD card compatdata prefixes for the given appids out of the way.
    Prefixes now live on NVMe for performance, so the SD card copies are no
    longer used.

    They are renamed to <appid>.deckops_old (an instant same-card rename),
    never deleted: a prefix can hold the only copy of a game's saves and
    settings (drive_c/users/steamuser, Plutonium's AppData). This used to
    rmtree the SD prefix of all 17 managed games on every install, including
    games that weren't part of the run, with no backup or prompt.
    Returns the list of set-aside paths.
    """
    import glob

    prog = on_progress or (lambda *a: None)

    sd_patterns = [
        "/run/media/deck/*/steamapps/compatdata",
        "/run/media/deck/*/SteamLibrary/steamapps/compatdata",
        "/run/media/mmcblk0p1/steamapps/compatdata",
        "/run/media/mmcblk0p1/SteamLibrary/steamapps/compatdata",
    ]

    set_aside = []
    seen = set()
    for pattern in sd_patterns:
        for compat_dir in glob.glob(pattern):
            for appid in appids:
                prefix_dir = os.path.join(compat_dir, str(appid))
                real = os.path.realpath(prefix_dir)
                # Symlinks are left alone: someone pointed it somewhere on purpose
                if real in seen or os.path.islink(prefix_dir) or not os.path.isdir(prefix_dir):
                    continue
                seen.add(real)
                dest = prefix_dir + SD_PREFIX_ASIDE_SUFFIX
                n = 1
                while os.path.lexists(dest):
                    n += 1
                    dest = f"{prefix_dir}{SD_PREFIX_ASIDE_SUFFIX}{n}"
                try:
                    os.rename(prefix_dir, dest)
                    set_aside.append(dest)
                    prog(f"  ✓ SD card prefix for {appid} moved aside (not deleted): {dest}")
                except OSError as ex:
                    prog(f"  ⚠ Could not move SD card prefix {appid} aside, left in place: {ex}")

    if set_aside:
        prog(f"  ℹ {len(set_aside)} old SD card prefix(es) kept as *{SD_PREFIX_ASIDE_SUFFIX} -- "
             f"copy any saves you need out of them, then delete them to free space")
    else:
        prog("  No SD card prefixes to clean up")
    return set_aside


def ensure_all_prefix_deps(ge_version: str | None, prefix_paths: list[tuple[str, str]],
                           on_progress=None, proton_path: str | None = None,
                           steam_root: str | None = None) -> int:
    """
    Initialize and install dependencies for a list of game prefixes.

    All prefixes are created on NVMe for performance. SD card prefixes
    of the managed games in this run are renamed to <appid>.deckops_old
    (kept, not deleted). Each prefix uses symlinked system32
    and syswow64 directories pointing to a shared DLL copy, reducing
    disk usage from ~8GB to ~1.9GB.

    Flow:
      1. Set up shared DLL directory from GE-Proton's default_pfx (one-time)
      2. Move this run's SD card prefixes aside (<appid>.deckops_old)
      3. Override all prefix paths to NVMe
      4. Pick first prefix as donor -- initialize via Proton if new
      5. Clone donor to remaining prefixes using symlinked DLL dirs

    prefix_paths -- list of (label, compatdata_path) tuples
    on_progress  -- optional callback(msg: str)
    proton_path  -- path to the Proton binary for prefix initialization
    steam_root   -- path to Steam root for STEAM_COMPAT_CLIENT_INSTALL_PATH

    Returns the number of prefixes that now have deps installed.
    """
    import subprocess
    import time

    prog = on_progress or (lambda *a: None)

    default_pfx = _find_default_pfx(ge_version)
    if not default_pfx:
        prog("⚠ No GE-Proton default_pfx found -- cannot install dependencies")
        return 0

    # ── Step 1: Ensure shared DLLs exist ──────────────────────────────────
    if not _ensure_shared_dlls(ge_version, on_progress=on_progress):
        prog("⚠ Shared DLL setup failed -- falling back to full copies")
        # Fall back to the old approach if shared DLLs can't be set up
        shared_available = False
    else:
        shared_available = True

    # ── Step 2: Move this run's SD card prefixes aside ────────────────────
    # Only the managed games being set up now -- never every managed appid.
    managed = {str(a) for a in MANAGED_APPIDS}
    run_appids = sorted({os.path.basename(os.path.normpath(p))
                         for _, p in prefix_paths if p} & managed)
    if run_appids:
        prog("  Checking for old SD card prefixes...")
        _cleanup_sd_card_prefixes(run_appids, on_progress=on_progress)

    # ── Step 3: Deduplicate and override paths to NVMe ────────────────────
    seen_paths = {}
    unique_paths = []
    for label, compat_path in prefix_paths:
        if not compat_path:
            prog(f"  {label}: no compatdata path -- skipped")
            continue
        # Extract appid from the path and force NVMe location
        appid = os.path.basename(os.path.normpath(compat_path))
        nvme_path = _nvme_compatdata(appid)
        norm = os.path.normpath(nvme_path)
        if norm in seen_paths:
            prog(f"  {label}: shares prefix with {seen_paths[norm]} -- skipped")
            continue
        seen_paths[norm] = label
        unique_paths.append((label, nvme_path))

    if not unique_paths:
        return 0

    success = 0

    # ── Step 4: Prepare donor prefix ──────────────────────────────────────
    donor_label, donor_path = unique_paths[0]
    donor_pfx_dir = os.path.join(donor_path, "pfx")
    donor_exists = os.path.isdir(os.path.join(donor_pfx_dir, "drive_c"))

    if donor_exists:
        # Donor already on NVMe -- verify DLLs if it has real dirs (not symlinks)
        sys32_dir = os.path.join(donor_pfx_dir, "drive_c", "windows", "system32")
        if os.path.islink(sys32_dir):
            prog(f"  {donor_label}: donor already has symlinked DLLs -- ready")
        else:
            prog(f"  {donor_label}: verifying donor DLLs...")
            sys32_src = os.path.join(default_pfx, "drive_c", "windows", "system32")
            wow64_src = os.path.join(default_pfx, "drive_c", "windows", "syswow64")
            wow64_dir = os.path.join(donor_pfx_dir, "drive_c", "windows", "syswow64")
            try:
                c32, s32 = _copy_dlls(sys32_src, sys32_dir)
                c64, s64 = _copy_dlls(wow64_src, wow64_dir)
                total_copied = c32 + c64
                total_skipped = s32 + s64
                if total_copied > 0:
                    prog(f"  ✓ Donor: copied {total_copied} missing DLLs, {total_skipped} already present")
                else:
                    prog(f"  ✓ Donor: all {total_skipped} DLLs verified")
            except Exception as ex:
                prog(f"  ⚠ {donor_label}: donor DLL check failed: {ex}")
        success += 1
    else:
        # Donor needs Proton initialization
        if not proton_path:
            for label, compat_path in unique_paths:
                prog(f"  {label}: checking dependencies...")
                ok = ensure_prefix_deps(ge_version, compat_path,
                                        on_progress=on_progress,
                                        proton_path=None,
                                        steam_root=steam_root)
                if ok:
                    success += 1
            return success

        _compat_install = steam_root or os.path.dirname(os.path.dirname(proton_path))
        prog(f"  {donor_label}: initializing donor prefix on NVMe...")
        os.makedirs(donor_path, exist_ok=True)

        try:
            env = os.environ.copy()
            env["STEAM_COMPAT_DATA_PATH"]           = donor_path
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = _compat_install
            start = time.time()
            subprocess.run(
                [proton_path, "run", "cmd", "/c", "exit"],
                env=env, capture_output=True, timeout=600,
            )
            elapsed = time.time() - start
            prog(f"  ✓ Donor prefix finalized by Proton ({elapsed:.1f}s)")
            success += 1
        except Exception as ex:
            prog(f"  ⚠ Donor prefix finalize failed: {ex}")
            for label, compat_path in unique_paths[1:]:
                ok = ensure_prefix_deps(ge_version, compat_path,
                                        on_progress=on_progress,
                                        proton_path=proton_path,
                                        steam_root=steam_root)
                if ok:
                    success += 1
            return success

    # ── Step 5: Clone donor to remaining prefixes ─────────────────────────
    for label, compat_path in unique_paths[1:]:
        dest_pfx = os.path.join(compat_path, "pfx")
        dest_exists = os.path.isdir(os.path.join(dest_pfx, "drive_c"))

        if dest_exists:
            # Existing NVMe prefix -- check if already symlinked
            sys32_dir = os.path.join(dest_pfx, "drive_c", "windows", "system32")
            if os.path.islink(sys32_dir):
                prog(f"  {label}: already symlinked -- skipping")
                success += 1
                continue
            # Existing with real dirs -- overlay to fill in any gaps
            prog(f"  {label}: overlaying from donor...")
            ok = _overlay_prefix(donor_pfx_dir, compat_path, ge_version,
                                 on_progress=on_progress)
        elif shared_available:
            # New prefix -- clone with symlinked DLLs (fast)
            prog(f"  {label}: cloning with symlinked DLLs...")
            ok = _clone_with_symlinks(donor_pfx_dir, compat_path, ge_version,
                                      on_progress=on_progress)
        else:
            # Fallback -- full clone if shared DLLs aren't available
            prog(f"  {label}: cloning from donor (full copy)...")
            ok = _clone_prefix(donor_pfx_dir, compat_path, ge_version,
                               on_progress=on_progress)

        if not ok:
            prog(f"  {label}: clone failed, falling back to individual init...")
            ok = ensure_prefix_deps(ge_version, compat_path,
                                    on_progress=on_progress,
                                    proton_path=proton_path,
                                    steam_root=steam_root)
        if ok:
            success += 1

    return success


# ── Old GE-Proton cleanup and prefix link repair ─────────────────────────────
#
# Proton fills a prefix's system32/syswow64 with links into its own
# compatibilitytools.d/<version>/files/lib/wine. Prefixes DeckOps cloned
# share one system32/syswow64 (SHARED_DLL_DIR), and Proton writes those links
# through the shared folder, so a prefix can hold links into a GE-Proton other
# than the one it runs with. Proton only refreshes them when the prefix's own
# version changes, so deleting an old GE-Proton can leave a prefix whose
# version did not change with broken links. Every removal is followed by
# relink_dangling_builtins, which points them at the current GE-Proton.

_PROTON_FILES_LINK = re.compile(r"/compatibilitytools\.d/(GE-Proton[^/]*)/files/(.+)$")


def _version_key(name: str) -> tuple:
    # Same order as steam_common.newest_proton_dir
    return tuple(int(p) for p in re.findall(r"\d+", name))


def _links_to_shared(prefix: str) -> bool:
    shared = os.path.realpath(SHARED_DLL_DIR)
    for d in ("system32", "syswow64"):
        p = os.path.join(prefix, "pfx", "drive_c", "windows", d)
        if os.path.islink(p) and os.path.realpath(p).startswith(shared + os.sep):
            return True
    return False


def _deckops_prefixes(steam_root: str | None = None) -> list:
    """compatdata roots DeckOps set up: the managed appids' prefixes, every
    prefix linked to the shared DLLs (own-copy shortcuts included) and the
    offline prefixes."""
    from steam_common import STEAM_ROOT
    sr = steam_root or STEAM_ROOT
    out, seen = [], set()

    def add(p):
        if p and os.path.isdir(os.path.join(p, "pfx")):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append(p)

    try:
        from wrapper import find_compatdata
        for appid in MANAGED_APPIDS:
            add(find_compatdata(sr, appid))
    except Exception:
        _log.debug("managed prefixes not resolved", exc_info=True)
    root = os.path.join(sr, "steamapps", "compatdata")
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if _links_to_shared(p):
                add(p)
    try:
        from plutonium import LAN_PREFIX, LAN_SERVER_PREFIX
        add(LAN_PREFIX)
        add(LAN_SERVER_PREFIX)
    except Exception:
        _log.debug("offline prefixes not resolved", exc_info=True)
    return out


def relink_dangling_builtins(ge_version: str, prefixes: list) -> tuple[int, int]:
    """Point broken links into a removed GE-Proton at the same file in
    ge_version. A broken Wine builtin link with no counterpart is removed;
    Proton recreates it on the next launch. Looks in the shared DLLs and in
    each prefix's drive_c/windows. Returns (relinked, removed)."""
    tool = os.path.join(COMPAT_DIR, ge_version) if ge_version else None
    relinked = removed = 0
    roots = [SHARED_DLL_DIR] + [os.path.join(p, "pfx", "drive_c", "windows") for p in prefixes]
    for top in roots:
        if not os.path.isdir(top):
            continue
        for root, _dirs, files in os.walk(top):      # does not follow linked folders
            for name in files:
                path = os.path.join(root, name)
                if not os.path.islink(path) or os.path.exists(path):
                    continue
                target = os.path.normpath(os.path.join(root, os.readlink(path))).replace("\\", "/")
                m = _PROTON_FILES_LINK.search(target)
                if not m:
                    continue
                new = os.path.join(tool, "files", m.group(2)) if tool else None
                try:
                    if new and os.path.exists(new):
                        os.remove(path)
                        os.symlink(new, path)
                        relinked += 1
                    elif _is_wine_builtin(path):
                        os.remove(path)
                        removed += 1
                except OSError:
                    _log.warning("could not repair link %s", path, exc_info=True)
    return relinked, removed


def _mapped_ge_versions(steam_root: str) -> set | None:
    """Every compat tool name in config.vdf's CompatToolMapping (all appids,
    the global default "0" included). None if config.vdf can't be read."""
    from wrapper import _vdf_child_block, _flat_values, _vdf_children
    path = os.path.join(steam_root, "config", "config.vdf")
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
        block = (-1, len(content))
        for key in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
            block = _vdf_child_block(content, block, key)
            if not block:
                return set()
        names = set()
        for _k, _ks, vs, ve, is_block in _vdf_children(content, block[0] + 1, block[1]):
            if is_block:
                for _ks2, nvs, nve in _flat_values(content, (vs, ve), "name"):
                    names.add(content[nvs:nve])
        return names
    except (OSError, ValueError):
        _log.warning("config.vdf not readable, old GE-Proton kept", exc_info=True)
        return None


def _ge_versions_in_use(steam_root: str) -> set | None:
    """GE-Proton names something still points at: Steam's compat tool
    mappings, this and the other DeckOps build's pinned version, and Heroic's
    game configs (LCD Plutonium). None if Steam's mappings can't be read."""
    used = _mapped_ge_versions(steam_root)
    if used is None:
        return None
    for name in ("DeckOps", "DeckOps-Nightly"):         # Stable and Nightly side by side
        try:
            with open(os.path.expanduser(f"~/{name}/deckops.json")) as f:
                v = json.load(f).get("ge_proton_version")
            if isinstance(v, str) and v:
                used.add(v)
        except (OSError, ValueError, AttributeError):
            continue
    try:
        import config as _cfg
        if _cfg.get_ge_proton_version():
            used.add(_cfg.get_ge_proton_version())
    except Exception:
        _log.debug("pinned GE-Proton not read", exc_info=True)
    for d in (os.path.expanduser("~/.var/app/com.heroicgameslauncher.hgl/config/heroic/GamesConfig"),
              os.path.expanduser("~/.config/heroic/GamesConfig")):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            try:
                with open(os.path.join(d, name), errors="replace") as f:
                    used.update(re.findall(r"compatibilitytools\.d/([^/\"\\]+)", f.read()))
            except OSError:
                pass
    return used


def cleanup_old_ge_versions(current: str, steam_root: str | None = None,
                            on_progress=None) -> list:
    """
    Remove GE-Proton versions DeckOps installed that are older than current,
    keeping current and the newest one before it (a rollback). Only versions
    DeckOps recorded (config ge_proton_installed) are candidates, so
    GE-Protons the player installed are never touched; neither is any version
    a game, shortcut, Heroic config or the other DeckOps build still uses.
    Nothing is removed if config.vdf can't be read. Returns removed names.
    """
    import config as _cfg
    from steam_common import STEAM_ROOT
    prog = on_progress or (lambda *a: None)
    sr = steam_root or STEAM_ROOT

    if not current or not _is_installed(current):
        return []
    tracked = set(_cfg.get_ge_proton_installed()) | {current}
    on_disk = {v for v in tracked
               if os.path.isdir(os.path.join(COMPAT_DIR, v))
               and not os.path.islink(os.path.join(COMPAT_DIR, v))}
    older = sorted((v for v in on_disk if _version_key(v) < _version_key(current)),
                   key=_version_key, reverse=True)
    if len(older) < 2:
        return []

    in_use = _ge_versions_in_use(sr)
    if in_use is None:
        prog("  Old GE-Proton versions kept: Steam's settings could not be read")
        return []

    removed = []
    for v in older[1:]:                      # older[0] is the rollback version
        if v in in_use:
            prog(f"  {v} kept: still in use")
            continue
        try:
            shutil.rmtree(os.path.join(COMPAT_DIR, v))
            removed.append(v)
            prog(f"  ✓ Removed old {v}")
        except OSError as ex:
            prog(f"  ⚠ Could not remove {v}: {ex}")

    _cfg.set_ge_proton_installed(sorted(
        (v for v in tracked if v == current or (v in on_disk and v not in removed)),
        key=_version_key))
    if removed:
        relinked, gone = relink_dangling_builtins(current, _deckops_prefixes(sr))
        if relinked or gone:
            prog(f"  ✓ {relinked} prefix link(s) moved to {current}")
    return removed


def repair_prefixes(ge_version: str, steam_root: str | None = None,
                    on_progress=None) -> bool:
    """Bring DeckOps's prefixes in line with ge_version: refresh (or
    recreate) the shared DLLs if any prefix uses them and repoint links into
    a removed GE-Proton. Returns False if the shared DLLs could not be set up."""
    prog = on_progress or (lambda *a: None)
    if not ge_version:
        return True
    prefixes = _deckops_prefixes(steam_root)
    ok = True
    if any(_links_to_shared(p) for p in prefixes):
        ok = _ensure_shared_dlls(ge_version, on_progress=prog)
    relinked, removed = relink_dangling_builtins(ge_version, prefixes)
    if relinked or removed:
        prog(f"  ✓ Repaired {relinked + removed} prefix link(s) to a removed GE-Proton")
    return ok
