"""
alterware.py - DeckOps installer for AlterWare (IW6-Mod & S1-Mod)

Downloads the native Linux AlterWare launcher from GitHub and runs it
to fetch mod client files for Ghosts (IW6-Mod) and Advanced Warfare
(S1-Mod). The launcher runs natively on Linux — no Proton needed for
the install step.

For Steam games:
  - Sets Steam launch options to redirect the game exe to the mod exe
    using bash parameter substitution on %command%
  - The launch-option redirect never touches the original exe — iw6-mod.exe
    maps it into memory at runtime, so it must remain a valid PE file.
    (Ghosts' iw6mp64_ship.exe IS replaced by AlterWare's CDN build, see
    _CDN_GAME_EXES; the stock copy is kept as iw6mp64_ship.exe.deckops_orig.)
  - Steam's own Proton invocation handles all env vars, cwd, prefix
  - GE-Proton compat tool mapping (set by ge_proton.py) is required

For own games:
  - Leaves mod client exe as-is (iw6-mod.exe, s1-mod.exe)
  - Non-Steam shortcut points to mod exe directly
  - CRC appid stable via (exe_path + shortcut_name)

Both flows:
  - Run native Linux launcher with --update flag to download files
  - Write metadata tracking version, paths, installation state
  - Launcher modifies .ff and .bik files in-place (patches)

Progress is reported via a callback:
    on_progress(percent: int, status: str)
"""

import os
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

from log import get_logger
from net import download as _download, github_asset
from steam_common import write_json

_log = get_logger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

# Native Linux launcher — no Proton needed for the install step.
_LAUNCHER_URL = (
    "https://github.com/alterware/alterware-launcher/releases/latest"
    "/download/alterware-launcher-x86_64-unknown-linux-gnu.tar.gz"
)

_CDN_BASE = "https://cdn.alterware.ovh"


def cdn_digests() -> dict:
    """{cdn_path: "sha1:<hex>"} from the CDN's files.json. Empty on failure
    so a CDN hiccup skips verification instead of blocking the install."""
    try:
        import urllib.request
        from net import BROWSER_UA
        req = urllib.request.Request(f"{_CDN_BASE}/files.json", headers=BROWSER_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return {f["name"]: f"sha1:{f['hash']}" for f in json.loads(r.read()) if f.get("hash")}
    except Exception as ex:
        _log.warning("AlterWare files.json lookup failed, downloading unverified: %s", ex)
        return {}

# Game exe replacements the launcher doesn't download on its own.
# (cdn_path, local_filename) — downloaded after the launcher finishes.
# These overwrite a stock game file, so the original is kept first as
# <file>.deckops_orig (see _backup_stock_file) for the uninstaller.
_CDN_GAME_EXES = {
    "iw6mp": [("iw6/iw6mp64_ship.exe", "iw6mp64_ship.exe")],
    "iw6sp": [("iw6/iw6mp64_ship.exe", "iw6mp64_ship.exe")],
}

# Not ".bak": _migrate_old_wrapper treats a .bak as a legacy wrapper backup
# and restores it over the exe.
ORIG_SUFFIX = ".deckops_orig"

METADATA_FILE = "deckops_alterware.json"

# Maps game_key → (launcher_arg, client_exe, original_exe)
#
# launcher_arg:  passed to the alterware-launcher binary to select the client
# client_exe:    the mod exe that the launcher downloads into the game dir
# original_exe:  the vanilla Steam exe that the mod maps into memory at runtime
#
# Both iw6mp and iw6sp use the same client (iw6-mod), just launched with
# different flags (-multiplayer / -singleplayer) at runtime. Same for s1.
_GAME_CONFIG = {
    "iw6mp": ("iw6-mod", "iw6-mod.exe", "iw6mp64_ship.exe"),
    "iw6sp": ("iw6-mod", "iw6-mod.exe", "iw6sp64_ship.exe"),
    "s1mp":  ("s1-mod",  "s1-mod.exe",  "s1_mp64_ship.exe"),
    "s1sp":  ("s1-mod",  "s1-mod.exe",  "s1_sp64_ship.exe"),
}

# The built-in launcher UI in iw6-mod and s1-mod crashes under Wine/Proton.
# These flags bypass it and launch directly into the correct game mode.
# Must be passed via launch options (Steam installs) or shortcut
# launch options (own installs).
_MODE_FLAGS = {
    "iw6mp": "-multiplayer",
    "iw6sp": "-singleplayer",
    "s1mp":  "-multiplayer",
    "s1sp":  "-singleplayer",
}

# Maps game_key to its Steam appid for launch option writing.
_APPIDS = {
    "iw6mp": "209170",
    "iw6sp": "209160",
    "s1mp":  "209660",
    "s1sp":  "209650",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_launch_option(original_exe: str, client_exe: str,
                         mode_flag: str) -> str:
    """
    Build the Steam launch option string that redirects the original exe
    to the mod exe using bash parameter substitution on %command%.

    Steam expands %command% to the full Proton launch chain:
      SLR/_v2-entry-point --verb=waitforexitandrun -- GE-Proton/proton
      waitforexitandrun /path/to/game/iw6mp64_ship.exe

    The ${@/old/new} substitution replaces the original exe name with
    the mod exe in whichever positional parameter contains it. The mode
    flag (-multiplayer/-singleplayer) is appended to bypass the mod's
    built-in launcher UI which crashes under Wine/Proton.

    The original exe is never touched on disk — the mod exe maps it
    into memory at runtime, so it must remain a valid PE binary.
    """
    return (
        f"bash -c "
        f"'exec \"${{@/{original_exe}/{client_exe}}}\" {mode_flag}' "
        f"-- %command%"
    )


def _migrate_old_wrapper(install_dir: str, original_exe: str):
    """
    Migrate from the old wrapper-over-exe flow to the new launch options
    flow. If a .bak file exists, the old flow replaced the original exe
    with a bash wrapper — restore the original PE binary so the mod exe
    can map it at runtime.

    Safe to call even if no .bak exists (no-op).
    """
    original_path = os.path.join(install_dir, original_exe)
    backup_path = original_path + ".bak"

    if os.path.exists(backup_path):
        # Restore the original PE binary
        if os.path.exists(original_path):
            os.remove(original_path)
        os.rename(backup_path, original_path)


# The launcher used to run with a fixed 10 minute timeout and its output
# captured until the end: with bonus content on a slow connection it timed out
# while still downloading, and the bar sat at 25% the whole time. Now its
# output is shown as it comes, and it is only stopped when it makes no
# progress at all (no output and no file written in the game folder).
LAUNCHER_STALL_TIMEOUT = 10 * 60
LAUNCHER_STALL_CHECK = 30
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _newest_mtime(path: str) -> float:
    newest = 0.0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                pass
    return newest


def _run_launcher(cmd, install_dir: str, prog, timeout: float = None, check: float = None):
    """Run the AlterWare launcher, logging and showing its output as it
    comes. Returns (exit code, stalled, last output lines)."""
    timeout = LAUNCHER_STALL_TIMEOUT if timeout is None else timeout
    check = LAUNCHER_STALL_CHECK if check is None else check
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=install_dir)
    last = [time.monotonic()]
    stalled = [False]
    stop = threading.Event()

    def _watch():
        seen = _newest_mtime(install_dir)
        while not stop.wait(check):
            newest = _newest_mtime(install_dir)
            if newest > seen:
                seen = newest
                last[0] = time.monotonic()
            if time.monotonic() - last[0] > timeout:
                stalled[0] = True
                _log.warning("alterware-launcher stalled for %ds, stopping it", int(timeout))
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    threading.Thread(target=_watch, daemon=True).start()
    tail = []
    shown = 0.0
    try:
        for raw in iter(proc.stdout.readline, b""):
            last[0] = time.monotonic()
            for part in raw.decode("utf-8", errors="replace").split("\r"):
                line = _ANSI.sub("", part).strip()
                if not line:
                    continue
                _log.info("alterware-launcher: %s", line)
                tail = (tail + [line])[-5:]
                if time.monotonic() - shown > 0.5:
                    shown = time.monotonic()
                    prog(40, f"AlterWare: {line[:100]}")
        proc.wait()
    finally:
        stop.set()
    return proc.returncode, stalled[0], " | ".join(tail[-3:])


def _sha1(path: str) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_stock_file(path: str, digest: str | None) -> bool:
    """
    Keep the stock copy of a game file the CDN download is about to replace,
    as <path>.deckops_orig, so uninstall can put it back (a Steam "verify"
    was the only way before). Only the first, real original is kept: nothing
    happens if a backup already exists, the file is missing, or the file
    already is the CDN build (sha1 matches). Without a CDN hash the file is
    backed up anyway -- at worst that keeps a copy of a CDN build that is
    already on disk.
    Returns True if a backup exists afterwards.
    """
    backup = path + ORIG_SUFFIX
    if os.path.exists(backup):
        return True
    if not os.path.isfile(path):
        return False
    if digest and digest.startswith("sha1:") and _sha1(path) == digest[5:].lower():
        return False    # already the CDN build, the original is gone
    tmp = backup + ".tmp"
    shutil.copy2(path, tmp)
    os.replace(tmp, backup)
    _log.info("AlterWare: kept stock %s as %s", os.path.basename(path), os.path.basename(backup))
    return True


# ── public API ────────────────────────────────────────────────────────────────

def install_alterware(game: dict, game_key: str,
                      steam_root: str = "", proton_path: str = "",
                      compatdata_path: str = "",
                      on_progress=None, source: str = "steam",
                      download_bonus: bool = True):
    """
    Install AlterWare mod client (IW6-Mod or S1-Mod).

    Downloads the native Linux AlterWare launcher, runs it to fetch the
    mod client files, then sets Steam launch options to redirect the
    original exe to the mod exe. The launcher runs natively — no Proton
    needed for the install step.

    Parameters:
      game            — dict from detect_games with install_dir
      game_key        — one of: iw6mp, iw6sp, s1mp, s1sp
      steam_root      — path to Steam root (for launch option writing)
      proton_path     — path to Proton binary (unused, kept for API compat)
      compatdata_path — path to compatdata prefix (unused, kept for API compat)
      on_progress     — optional callback(percent: int, status: str)
      source          — "steam" or "own"
      download_bonus  — if True, download bonus content (extra maps/DLC)
    """
    install_dir = game["install_dir"]

    cfg = _GAME_CONFIG.get(game_key)
    if not cfg:
        raise ValueError(f"Unknown game key: {game_key}")
    launcher_arg, client_exe, original_exe = cfg

    prog = on_progress or (lambda *a: None)

    # ── Step 1: Download the native Linux launcher ────────────────────────
    prog(5, "Downloading AlterWare launcher...")
    tmp_dir = tempfile.mkdtemp(prefix="deckops_alterware_")
    tar_path = os.path.join(tmp_dir, "alterware-launcher.tar.gz")
    launcher_bin = os.path.join(tmp_dir, "alterware-launcher")

    try:
        url, dg = github_asset("alterware/alterware-launcher",
                               "x86_64-unknown-linux-gnu.tar.gz", fallback=_LAUNCHER_URL)
        _download(
            url, tar_path, digest=dg,
            on_progress=lambda pct, lbl: prog(5 + int(pct * 0.15), lbl),
            label="AlterWare launcher",
            timeout=120,
        )
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to download AlterWare launcher: {e}")

    # ── Step 2: Extract launcher binary ───────────────────────────────────
    prog(22, "Extracting launcher...")
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            # The tarball contains a single binary: alterware-launcher
            for member in tf.getmembers():
                if member.name.endswith("alterware-launcher") and member.isfile():
                    member.name = "alterware-launcher"
                    tf.extract(member, tmp_dir)
                    break
            else:
                raise RuntimeError("alterware-launcher binary not found in tarball")
        os.chmod(launcher_bin, 0o755)
    except tarfile.TarError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to extract AlterWare launcher: {e}")

    # ── Step 3: Run launcher to download mod client files ─────────────────
    prog(25, f"Downloading {launcher_arg} client files...")
    bonus_flag = "--bonus" if download_bonus else "--skip-bonus"
    cmd = [
        launcher_bin, launcher_arg,
        "--update",
        "--skip-launcher-update",
        "--path", install_dir,
        bonus_flag,
    ]

    try:
        rc, stalled, tail = _run_launcher(cmd, install_dir, prog)
    except OSError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to run AlterWare launcher: {e}")
    if stalled or rc != 0:
        # A failed update used to count as success whenever an older client
        # exe was still there, so "Update AlterWare" reported "complete" on
        # the old files. The old files stay; the install is reported failed.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        kept = (" Your previous client files were left in place."
                if os.path.exists(os.path.join(install_dir, client_exe)) else "")
        if stalled:
            raise RuntimeError(f"AlterWare launcher made no progress for "
                               f"{LAUNCHER_STALL_TIMEOUT // 60} minutes and was stopped.{kept}")
        raise RuntimeError(f"AlterWare launcher failed (exit {rc}): "
                           f"{tail or 'no error output'}.{kept}")

    # ── Step 3a: Undo the old wrapper-over-exe flow (Steam installs) ─────
    # Before the CDN step: it puts a legacy .bak back over the exe, which
    # used to undo the CDN replacement downloaded just before it.
    appid = _APPIDS.get(game_key)
    if source != "own" and appid and steam_root:
        prog(74, "Checking for old wrapper migration...")
        _migrate_old_wrapper(install_dir, original_exe)

    # ── Step 3b: Download CDN game exe replacements ─────────────────────
    cdn_files = _CDN_GAME_EXES.get(game_key, [])
    digests = cdn_digests() if cdn_files else {}
    for cdn_path, local_name in cdn_files:
        dst = os.path.join(install_dir, local_name)
        try:
            _backup_stock_file(dst, digests.get(cdn_path))
        except OSError as e:
            # No backup, no overwrite: leave the stock file alone
            _log.warning("Could not back up %s, CDN replacement skipped: %s", local_name, e)
            prog(76, f"Warning: could not back up {local_name}, left it unchanged: {e}")
            continue
        prog(75, f"Downloading {local_name} from CDN...")
        try:
            _download(
                f"{_CDN_BASE}/{cdn_path}", dst,
                on_progress=lambda p, m: prog(75 + int(p * 0.04), m),
                label=local_name, timeout=120, digest=digests.get(cdn_path),
            )
            _log.info("CDN: placed %s", local_name)
        except Exception as e:
            _log.warning("CDN download failed for %s: %s", local_name, e)
            prog(76, f"Warning: could not download {local_name}: {e}")

    prog(80, "Verifying client files...")

    # ── Step 4: Verify mod client exe was downloaded ──────────────────────
    client_exe_path = os.path.join(install_dir, client_exe)
    if not os.path.exists(client_exe_path):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(
            f"AlterWare launcher completed but {client_exe} not found "
            f"in {install_dir}"
        )

    # ── Step 5: Set launch options (Steam) or skip (own) ─────────────────
    # The mod exe (iw6-mod.exe / s1-mod.exe) is a Windows PE binary that
    # must run through Proton. It maps the original game exe into memory
    # at runtime — the original exe MUST remain a valid PE file on disk.
    #
    # For Steam installs we set launch options that use bash parameter
    # substitution to swap the exe name in Steam's Proton launch chain.
    # Steam's compat tool mapping (set by ge_proton.py) provides the
    # Proton environment — no manual env var setup needed.
    #
    # The built-in launcher UI crashes under Wine/Proton, so we append
    # -multiplayer / -singleplayer to bypass it.
    mode_flag = _MODE_FLAGS.get(game_key, "")
    if source != "own":
        if appid and steam_root:
            prog(85, f"Setting launch options for {original_exe}...")
            launch_opt = _build_launch_option(original_exe, client_exe,
                                              mode_flag)

            from wrapper import set_launch_options, clear_launch_options
            # Clear first to avoid appending duplicates on reinstall
            clear_launch_options(steam_root, appid)
            set_launch_options(steam_root, appid, launch_opt)
        else:
            prog(85, f"⚠ Missing appid or steam_root — skipping launch options")
    else:
        prog(85, "Own game — skipping launch options")

    # ── Step 6: Write metadata ────────────────────────────────────────────
    prog(90, "Saving metadata...")
    write_json(os.path.join(install_dir, METADATA_FILE), {
        "client": launcher_arg,
        "client_exe": client_exe,
        "original_exe": original_exe,
        "source": source,
        "game_key": game_key,
        "download_bonus": download_bonus,
        "mode_flag": mode_flag,
        # Stock files replaced by CDN builds -> their backups (for uninstall).
        # Listed from disk so iw6sp/iw6mp, which share this file, both keep it.
        "replaced_files": {
            n: n + ORIG_SUFFIX
            for n in sorted({ln for k in _CDN_GAME_EXES for _, ln in _CDN_GAME_EXES[k]})
            if os.path.exists(os.path.join(install_dir, n + ORIG_SUFFIX))
        },
    })

    # ── Step 7: Clean up temp dir ─────────────────────────────────────────
    shutil.rmtree(tmp_dir, ignore_errors=True)

    prog(100, f"{launcher_arg} installation complete!")
