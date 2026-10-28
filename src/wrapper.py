import os
import json
import re
import shutil
import subprocess

from identity import LEDGER_PATH
from log import get_logger
from steam_common import (backup_file as _backup_file, newest_proton_dir, write_json as _write_json,
                          atomic_write as _atomic_write, keep_original as _keep_original)

_log = get_logger(__name__)

# ── VDF edit ledger ──────────────────────────────────────────────────────────
# Records every VDF edit DeckOps makes so the uninstaller can reverse them
# precisely instead of regex-sweeping entire files.


def _read_ledger() -> dict:
    """Read the VDF edit ledger, returning empty dict if missing/corrupt."""
    if not os.path.exists(LEDGER_PATH):
        return {}
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        _log.debug("VDF ledger read failed, starting fresh", exc_info=True)
        return {}


def _write_ledger(data: dict):
    """Write the VDF edit ledger. Failures are logged but non-fatal."""
    try:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        _write_json(LEDGER_PATH, data)
    except OSError:
        _log.debug("VDF ledger write failed", exc_info=True)


def _record_localconfig(uid: str, appid: str, key: str, value: str):
    """Record a localconfig.vdf edit in the ledger."""
    ledger = _read_ledger()
    lc = ledger.setdefault("localconfig", {})
    uid_block = lc.setdefault(uid, {})
    app_block = uid_block.setdefault(appid, {})
    app_block[key] = value
    _write_ledger(ledger)


def _record_config_vdf(appid: str, key: str, value: str):
    """Record a config.vdf edit in the ledger."""
    ledger = _read_ledger()
    cv = ledger.setdefault("config_vdf", {})
    section = cv.setdefault(key, {})
    section[appid] = value
    _write_ledger(ledger)


def _remove_config_vdf(appid: str, key: str):
    """Remove a config.vdf entry from the ledger (for clear operations)."""
    ledger = _read_ledger()
    cv = ledger.get("config_vdf", {})
    section = cv.get(key, {})
    section.pop(appid, None)
    if not section:
        cv.pop(key, None)
    _write_ledger(ledger)


def _record_configset(configset_filename: str, key: str, template_name: str):
    """Record a configset VDF edit in the ledger."""
    ledger = _read_ledger()
    cs = ledger.setdefault("configsets", {})
    file_block = cs.setdefault(configset_filename, {})
    file_block[key] = template_name
    _write_ledger(ledger)



def _find_block_end(text, start):
    """
    Brace-depth parser that skips braces inside quoted strings.

    WARNING: Must skip braces inside quoted strings - VDF values like
    bash substitutions (e.g. ${@/iw3sp.exe/iw3sp_mod.exe}) contain
    { and } characters that must NOT be counted as block delimiters.
    Failure to do this will corrupt localconfig.vdf.
    """
    depth = 0
    for i, c, in_quote in _vdf_scan(text, start):
        if not in_quote:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _vdf_scan(text, start=0):
    """Yield (index, char, inside_quotes) for the structural characters of a
    text VDF. Inside quotes a backslash escapes the next character, so
    "C:\\\\" ends at its last quote (the old check, "quote not preceded by a
    backslash", read that as an escaped quote and lost track from there)."""
    i, n, in_quote = start, len(text), False
    while i < n:
        c = text[i]
        if in_quote:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_quote = False
            else:
                yield i, c, True
        elif c == '"':
            in_quote = True
        else:
            yield i, c, False
        i += 1


def _vdf_balanced(text) -> bool:
    """Every { outside quotes has its }, and no quote is left open (an
    unterminated quote swallows the rest of the file)."""
    depth, in_quote, i, n = 0, False, 0, len(text)
    while i < n:
        c = text[i]
        if in_quote:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_quote = False
        elif c == '"':
            in_quote = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_quote


def _write_and_validate_vdf(path: str, data: str, encoding: str = "utf-8",
                            errors: str | None = None) -> bool:
    """
    Check new VDF text, then write it in one step.

    The text is checked BEFORE the disk is touched: if its braces or quotes
    don't balance, the file is left as it is and False is returned (it used
    to be written first and the .bak copied back afterwards). The write
    itself is atomic (steam_common.atomic_write), so a crash or full disk
    can't leave a cut-off file. The first edit keeps <file>.deckops-original;
    every edit refreshes <file>.bak. Returns True if the new text is on disk.

    encoding / errors -- how the text is turned back into bytes. Text read
    with errors="surrogateescape" (localconfig.vdf) is written back the same
    way, so bytes that aren't valid UTF-8 survive unchanged.
    """
    if not _vdf_balanced(data):
        _log.error("VDF edit for %s rejected: braces or quotes don't balance; file left unchanged", path)
        return False
    _keep_original(path)
    _backup_file(path)
    _atomic_write(path, data.encode(encoding, errors or "strict"))
    return True


# ── localconfig.vdf helpers ──────────────────────────────────────────────────

_INTERSTITIAL_RE = re.compile(
    r'"Deck_ConfiguratorInterstitialApps_AppLauncherInteractionIssues"\s*"[^"]*"\s*"apps"\s*\{',
    re.IGNORECASE
)


def _localconfigs(steam_root):
    """Yield (uid, vdf_path, content) for every user's localconfig.vdf."""
    userdata = os.path.join(steam_root, "userdata")
    if not os.path.exists(userdata):
        return
    for uid in os.listdir(userdata):
        vdf_path = os.path.join(userdata, uid, "config", "localconfig.vdf")
        if not os.path.exists(vdf_path):
            continue
        # surrogateescape: bytes that aren't UTF-8 are carried through an edit
        # unchanged (errors="replace" turned them into U+FFFD on write)
        with open(vdf_path, "r", encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
        yield uid, vdf_path, content


# ── Structural text-VDF reading ──────────────────────────────────────────────
# localconfig.vdf edits go through a small tokenizer instead of regex
# searches. WARNING: values like bash substitutions
# (e.g. ${@/iw3sp.exe/iw3sp_mod.exe}) contain { and } that must NOT be
# counted as block delimiters, and values can contain escaped quotes (\").
# Anything that doesn't parse raises ValueError and the edit is skipped
# rather than guessed at.

def _vdf_skip_ws(text, i, end):
    while i < end:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            nl = text.find("\n", i, end)
            i = end if nl == -1 else nl + 1
        else:
            break
    return i


def _vdf_quoted_end(text, i, end):
    """text[i] is an opening quote: index just past its closing quote."""
    i += 1
    while i < end:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string in VDF")


def _vdf_brace_end(text, i, end):
    """text[i] is '{': index of its matching '}' (quote-aware)."""
    depth = 0
    while i < end:
        c = text[i]
        if c == '"':
            i = _vdf_quoted_end(text, i, end)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced braces in VDF")


def _vdf_children(text, start, end):
    """
    Direct children of the block body text[start:end]. Yields
    (key, key_start, value_start, value_end, is_block): for a string value
    value_start/value_end span the text between its quotes; for a block
    they are the offsets of its '{' and '}'.
    """
    i = start
    while True:
        i = _vdf_skip_ws(text, i, end)
        if i >= end:
            return
        if text[i] != '"':
            raise ValueError(f"unexpected {text[i]!r} in VDF at offset {i}")
        k_end = _vdf_quoted_end(text, i, end)
        key = text[i + 1:k_end - 1]
        j = _vdf_skip_ws(text, k_end, end)
        if j < end and text[j] == "{":
            close = _vdf_brace_end(text, j, end)
            yield key, i, j, close, True
            i = close + 1
        elif j < end and text[j] == '"':
            v_end = _vdf_quoted_end(text, j, end)
            yield key, i, j + 1, v_end - 1, False
            i = v_end
        else:
            raise ValueError(f"VDF key {key!r} has no value")


def _vdf_child_block(text, block, key):
    """(open, close) of the direct child block named key (case-insensitive)."""
    open_, close = block
    for k, _, vs, ve, is_block in _vdf_children(text, open_ + 1, close):
        if is_block and k.lower() == key.lower():
            return vs, ve
    return None


# Where Steam keeps per-game settings (LaunchOptions, UseSteamControllerConfig).
# DeckOps also writes "<appid>" { } blocks elsewhere in localconfig.vdf (the
# Deck configurator "apps" block, see set_default_launch_option), so the
# appid must only ever be looked up inside this path.
_STEAM_APPS_PATH = ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps")


def _app_block(content, appid):
    """
    Return (open, close) brace offsets of the "<appid>" { block under
    UserLocalConfigStore/Software/Valve/Steam/apps, or None.

    This used to take the first "<appid>" { anywhere in the file, which could
    be a same-named block in another section (Steam then ignores the edit).
    """
    try:
        block = (-1, len(content))      # virtual root around the whole file
        for key in _STEAM_APPS_PATH + (str(appid),):
            block = _vdf_child_block(content, block, key)
            if not block:
                return None
        return block
    except ValueError as ex:
        _log.warning("localconfig.vdf not parsed, edit skipped: %s", ex)
        return None


def _flat_values(content, block, key):
    """
    [(key_start, value_start, value_end)] for every direct "key" "value"
    child of block -- anywhere among the app's own keys, before or after its
    sub-blocks. Keys of the same name inside sub-blocks (cloud) are never
    included. (The old _split_flat only looked before the first sub-block,
    so a LaunchOptions Steam had written after "cloud" { } was missed and a
    duplicate added.) Raises ValueError if the block doesn't parse.
    """
    open_, close = block
    return [(ks, vs, ve) for k, ks, vs, ve, is_block
            in _vdf_children(content, open_ + 1, close)
            if not is_block and k.lower() == key.lower()]


def _set_flat_value(content, found, value):
    """Set the first of found (from _flat_values) to value and remove the
    rest -- duplicates left by older DeckOps versions."""
    for ks, vs, ve in reversed(found[1:]):
        line_start = content.rfind("\n", 0, ks) + 1
        line_end = content.find("\n", ve)
        line_end = len(content) if line_end == -1 else line_end
        if not content[line_start:ks].strip() and not content[ve + 1:line_end].strip():
            content = content[:line_start] + content[line_end + 1:]   # whole line
        else:
            content = content[:ks] + content[ve + 1:]
    _, vs, ve = found[0]
    return content[:vs] + value + content[ve:]


def _insert_flat(content, block, key, value):
    """Add "key" "value" as the last direct child of block, indented like
    its siblings."""
    open_, close = block
    kids = list(_vdf_children(content, open_ + 1, close))
    if kids:
        ks = kids[0][1]
        indent = content[content.rfind("\n", 0, ks) + 1:ks]
    else:
        brace_line = content[content.rfind("\n", 0, open_) + 1:open_]
        indent = brace_line[:len(brace_line) - len(brace_line.lstrip())] + "\t"
    if indent.strip():
        indent = "\t"
    line = f'{indent}"{key}"\t\t"{value}"\n'
    line_start = content.rfind("\n", 0, close) + 1
    if not content[line_start:close].strip():      # '}' on its own line
        return content[:line_start] + line + content[line_start:]
    return content[:close] + "\n" + line + content[close:]


def _value_pattern(key):
    """Match "key" "value" with groups (prefix, value, closing quote)."""
    return re.compile(r'("' + key + r'"\s*")((?:[^"\\]|\\.)*)(")', re.IGNORECASE)


def _dlo_entry(appid, hash_key, index):
    return (
        f'\t\t\t\t"{appid}"\n'
        f'\t\t\t\t{{\n'
        f'\t\t\t\t\t"DefaultLaunchOption"\n'
        f'\t\t\t\t\t{{\n'
        f'\t\t\t\t\t\t"{hash_key}"\t\t"{index}"\n'
        f'\t\t\t\t\t}}\n'
        f'\t\t\t\t}}\n'
    )


def get_proton_path(steam_root):
    """
    Find the best available Proton binary for running Windows executables.

    Preference order:
      1. Config-pinned GE-Proton version (from config.json ge_proton_version)
      2. GE-Proton in ~/.local/share/Steam/compatibilitytools.d/
      3. GE-Proton in steam_root/compatibilitytools.d/
      4. Newest vanilla Proton in steam_root/steamapps/common/

    The config pin ensures DeckOps uses the exact GE-Proton version it
    installed, even if newer (potentially unstable) versions are present
    on disk from ProtonUp-Qt or other tools.

    Uses numeric version sorting so GE-Proton9-28 > GE-Proton9-5,
    and Proton 10 > Proton 9.
    """
    ge_search_dirs = [
        os.path.expanduser("~/.local/share/Steam/compatibilitytools.d"),
    ]
    if steam_root:
        ge_search_dirs.append(os.path.join(steam_root, "compatibilitytools.d"))

    # Try config-pinned version first
    try:
        import config as _cfg
        pinned = _cfg.get_ge_proton_version()
    except Exception:
        pinned = None

    if pinned:
        for ge_dir in ge_search_dirs:
            candidate = os.path.join(ge_dir, pinned, "proton")
            if os.path.exists(candidate):
                return candidate

    # Pinned version not found — fall back to newest GE-Proton on disk
    for ge_dir in ge_search_dirs:
        name = newest_proton_dir(ge_dir, "GE-Proton")
        if name:
            return os.path.join(ge_dir, name, "proton")

    # Fall back to vanilla Proton
    if not steam_root:
        return None
    common = os.path.join(steam_root, "steamapps", "common")
    name = newest_proton_dir(common, "Proton")
    return os.path.join(common, name, "proton") if name else None


def find_compatdata(steam_root, appid, game_install_dir=None):
    """
    Find the Wine prefix folder for a given Steam appid.

    Searches all Steam library folders (internal + SD card) so the correct
    prefix is found regardless of where the game is installed.

    When game_install_dir is provided, the prefix from the same library
    folder as the game install is preferred. This fixes the bug where
    both internal and SD card have compatdata for the same appid —
    without this hint, the internal drive's prefix is always returned
    first because _all_library_dirs lists it first.

    Returns the path or None if not found.
    """
    from detect_games import _all_library_dirs

    all_dirs = _all_library_dirs(steam_root)

    # If we know where the game is installed, prefer the prefix from
    # the same library folder. This ensures SD card games use the
    # SD card prefix, not the internal drive's.
    if game_install_dir:
        game_norm = os.path.normpath(game_install_dir)
        for steamapps_dir in all_dirs:
            sa_norm = os.path.normpath(steamapps_dir)
            # Check if the game's install_dir lives under this steamapps/
            # e.g. /run/media/deck/SD1/steamapps/common/Call of Duty 4
            #       starts with /run/media/deck/SD1/steamapps
            if game_norm.startswith(sa_norm + os.sep) or game_norm.startswith(sa_norm + "/"):
                candidate = os.path.join(steamapps_dir, "compatdata", str(appid))
                if os.path.isdir(candidate):
                    return candidate
                # Game is here but no compatdata yet — break and fall
                # through to the general scan (prefix may not exist yet)
                break

    for steamapps_dir in all_dirs:
        candidate = os.path.join(steamapps_dir, "compatdata", str(appid))
        if os.path.isdir(candidate):
            return candidate

    # Fallback to the default location (may not exist yet)
    default = os.path.join(steam_root, "steamapps", "compatdata", str(appid))
    if os.path.exists(default):
        return default
    return None


def set_launch_options(steam_root, appid, options):
    """
    Set or append launch options for a Steam game in localconfig.vdf.

    Finds all Steam user accounts under steam_root/userdata and updates
    the LaunchOptions entry for the given appid in each one. Always writes
    to the flat app block — NOT inside any cloud sub-block. Steam's UI reads
    LaunchOptions from the flat block; writing to the cloud sub-block causes
    the option to be invisible in Steam properties even if correctly written.

    steam_root  — path to the Steam root directory
    appid       — int or str Steam appid
    options     — launch option string to set, e.g. "iw4x.exe %command%"
    """
    appid = str(appid)
    # Escape double quotes so the value is valid inside a VDF quoted string
    vdf_options = options.replace('"', '\\"')

    for uid, vdf_path, content in _localconfigs(steam_root):
        block = _app_block(content, appid)
        if not block:
            continue

        # Always write LaunchOptions as a direct key of the app block.
        # Steam reads LaunchOptions from here — the cloud sub-block value
        # is NOT shown in Steam properties and should never be written to.
        try:
            found = _flat_values(content, block, "LaunchOptions")
            if found:
                existing = content[found[0][1]:found[0][2]]
                if vdf_options in existing and len(found) == 1:
                    continue
                new_options = existing if vdf_options in existing else \
                    (existing.strip() + " " + vdf_options).strip()
                content = _set_flat_value(content, found, new_options)
            else:
                content = _insert_flat(content, block, "LaunchOptions", vdf_options)
        except ValueError as ex:
            _log.warning("LaunchOptions not set for %s (uid %s): %s", appid, uid, ex)
            continue

        if _write_and_validate_vdf(vdf_path, content, errors="surrogateescape"):
            _record_localconfig(uid, appid, "LaunchOptions", vdf_options)


def clear_launch_options(steam_root, appid):
    """
    Remove any LaunchOptions for a Steam game in localconfig.vdf.

    Cleans up stale launch options left over from older DeckOps versions
    that used launch commands instead of the exe rename strategy.
    Must be called while Steam is closed.
    """
    appid = str(appid)

    for uid, vdf_path, content in _localconfigs(steam_root):
        block = _app_block(content, appid)
        if not block:
            continue

        # Only the app's own LaunchOptions (any position), never sub-blocks.
        # Duplicates from older versions are merged into one empty key.
        try:
            found = _flat_values(content, block, "LaunchOptions")
        except ValueError as ex:
            _log.warning("LaunchOptions not cleared for %s (uid %s): %s", appid, uid, ex)
            continue
        if not found or (len(found) == 1 and not content[found[0][1]:found[0][2]].strip()):
            continue

        content = _set_flat_value(content, found, "")
        if _write_and_validate_vdf(vdf_path, content, errors="surrogateescape"):
            _record_localconfig(uid, appid, "LaunchOptions", "")


def snapshot_launch_options(steam_root, appids):
    """
    {appid: {uid: raw value}} of the non-empty LaunchOptions each user has
    now, for restore_launch_options. Values are kept exactly as written in
    localconfig.vdf (already escaped). Unreadable entries are left out.
    """
    snap = {}
    for uid, _, content in _localconfigs(steam_root):
        for appid in appids:
            appid = str(appid)
            block = _app_block(content, appid)
            if not block:
                continue
            try:
                found = _flat_values(content, block, "LaunchOptions")
            except ValueError:
                continue
            if found:
                value = content[found[0][1]:found[0][2]]
                if value.strip():
                    snap.setdefault(appid, {})[uid] = value
    return snap


def restore_launch_options(steam_root, snapshot, appid):
    """
    Put back the LaunchOptions snapshot_launch_options saw for appid, but only
    for users whose entry is empty now: whatever was written since (a later
    successful install) wins. Returns the uids restored. Steam must be closed.
    """
    appid = str(appid)
    saved = (snapshot or {}).get(appid) or {}
    restored = []
    for uid, vdf_path, content in _localconfigs(steam_root):
        value = saved.get(uid)
        if not value:
            continue
        block = _app_block(content, appid)
        if not block:
            continue
        try:
            found = _flat_values(content, block, "LaunchOptions")
            if found and any(content[vs:ve].strip() for _, vs, ve in found):
                continue
            content = (_set_flat_value(content, found, value) if found
                       else _insert_flat(content, block, "LaunchOptions", value))
        except ValueError as ex:
            _log.warning("LaunchOptions not restored for %s (uid %s): %s", appid, uid, ex)
            continue
        if _write_and_validate_vdf(vdf_path, content, errors="surrogateescape"):
            _record_localconfig(uid, appid, "LaunchOptions", value)
            restored.append(uid)
    return restored


def _reapply_launch_menus():
    # Steam refreshes appinfo.vdf from Valve's servers now and then, which
    # drops our launch menu entries. Put them back whenever Steam is closed.
    try:
        import config as _cfg
        c = _cfg.load()
        if c.get("launch_menus"):
            from steam_appinfo import reapply_all
            reapply_all(c.get("steam_root") or os.path.expanduser("~/.local/share/Steam"))
    except Exception:
        _log.debug("launch menu reapply failed", exc_info=True)


def kill_steam(on_progress=None):
    """
    Gracefully close the Steam desktop client without triggering the
    SteamOS session manager (which would switch back to Game Mode).

    Sends SIGTERM to ubuntu12_32/steam and waits for Steam to shut
    itself down cleanly. Never force-kills (SIGKILL) because that can
    corrupt localconfig.vdf and trigger the SteamOS first-run wizard.

    on_progress — optional callback(str) for status messages while
                  waiting for Steam to exit.

    Raises TimeoutError after 120 seconds if Steam refuses to close.
    """
    import time

    # Check if Steam is even running
    r = subprocess.run(
        ["pgrep", "-f", "ubuntu12_32/steam"],
        capture_output=True
    )
    if r.returncode != 0:
        _reapply_launch_menus()
        return  # Steam is not running

    # SIGTERM to the main Steam process triggers graceful shutdown + config write
    subprocess.run(["pkill", "-TERM", "-f", "ubuntu12_32/steam"], capture_output=True)

    if on_progress:
        on_progress("Waiting for Steam to safely close itself...")

    # Wait for Steam to exit on its own — never force-kill
    deadline = time.time() + 120
    elapsed = 0
    while time.time() < deadline:
        r = subprocess.run(
            ["pgrep", "-f", "ubuntu12_32/steam"],
            capture_output=True
        )
        if r.returncode != 0:
            # Steam process is gone. Wait for config writes to flush to disk.
            # Steam writes config.vdf and localconfig.vdf during shutdown and
            # the kernel may still be buffering those writes after the process
            # exits. sync forces all pending I/O to complete before we touch
            # any VDF files.
            time.sleep(3)
            subprocess.run(["sync"], capture_output=True)
            _reapply_launch_menus()
            return
        time.sleep(1)
        elapsed += 1
        if on_progress and elapsed % 10 == 0:
            on_progress("Still waiting for Steam to close safely...")

    raise TimeoutError(
        "Steam did not close within 120 seconds. "
        "Please close Steam manually and try again."
    )


def launch_steam(uri=None):
    """
    Open a steam:// URI (or just Steam itself) fully detached from DeckOps.

    Uses xdg-open for URI dispatch and gtk-launch as a fallback for plain
    launch.  start_new_session=True ensures the Steam process is not a
    child of DeckOps, so it survives if DeckOps exits.

    uri — a steam:// protocol URI, e.g. "steam://open/console".
          If None, just opens Steam via its .desktop file.
    """
    try:
        if uri:
            cmd = ["xdg-open", uri]
        else:
            cmd = ["gtk-launch", "steam.desktop"]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log.info("Launched Steam%s (detached)", f" with {uri}" if uri else "")
    except FileNotFoundError:
        _log.warning("Could not find %s", cmd[0])
    except Exception as ex:
        _log.warning("Failed to launch Steam: %s", ex)


def set_steam_input_enabled(steam_root, appids=None):
    """
    Enable Steam Input for the given appids by setting
    UseSteamControllerConfig to "1" in each user's localconfig.vdf.

    Must be called while Steam is closed.

    steam_root — path to the Steam root directory
    appids     — list of int or str appids; defaults to all DeckOps-managed games
    """
    # All regular Steam appids DeckOps manages
    # (non-Steam shortcuts are handled separately via AllowDesktopConfig in shortcuts.vdf)
    DEFAULT_APPIDS = [
        "7940",    # CoD4
        "10090",   # WaW
        "10180",   # MW2 SP
        "10190",   # MW2 MP
        "42680",   # MW3 SP
        "42690",   # MW3 MP
        "42750",   # MW3 Dedicated Server
        "42700",   # BO1
        "42710",   # BO1 MP
        "202970",  # BO2 SP
        "202990",  # BO2 MP
        "212910",  # BO2 ZM
    ]

    if appids is None:
        appids = DEFAULT_APPIDS

    appids = [str(a) for a in appids]

    for uid, vdf_path, content in _localconfigs(steam_root):
        modified_appids = []
        for appid in appids:
            block = _app_block(content, appid)
            if not block:
                continue

            # Only the app's own key (any position), never inside sub-blocks
            try:
                found = _flat_values(content, block, "UseSteamControllerConfig")
                if found:
                    if len(found) == 1 and content[found[0][1]:found[0][2]] == "1":
                        continue  # already enabled
                    content = _set_flat_value(content, found, "1")
                else:
                    content = _insert_flat(content, block, "UseSteamControllerConfig", "1")
            except ValueError as ex:
                _log.warning("Steam Input not enabled for %s (uid %s): %s", appid, uid, ex)
                continue
            modified_appids.append(appid)

        if modified_appids and _write_and_validate_vdf(vdf_path, content, errors="surrogateescape"):
            for appid in modified_appids:
                _record_localconfig(uid, appid, "UseSteamControllerConfig", "1")


STEAM_CONFIG = os.path.expanduser("~/.local/share/Steam/config/config.vdf")


def set_compat_tool(appids, version):
    """
    Write CompatToolMapping entries in Steam's config.vdf for each appid.
    Single source of truth — called from both ge_proton.py and shortcut.py
    so the entries are written twice at different points in the install flow,
    making it harder for Steam to override them.

    Logic (in order, no overlap):
      1. If the appid block already exists → replace it in place
      2. Else if CompatToolMapping block exists → insert into it
      3. Else → create the entire CompatToolMapping block from scratch

    appids  — list of int or str appids, e.g. ["10190", "42690"]
    version — GE-Proton version string, e.g. "GE-Proton10-32"
    """
    if not os.path.exists(STEAM_CONFIG):
        raise FileNotFoundError(f"Steam config not found: {STEAM_CONFIG}")

    with open(STEAM_CONFIG, "r", encoding="utf-8") as f:
        data = f.read()

    def _entry(appid_str):
        return (
            f'\t\t\t\t"{appid_str}"\n'
            f'\t\t\t\t{{\n'
            f'\t\t\t\t\t"name"\t\t"{version}"\n'
            f'\t\t\t\t\t"config"\t\t""\n'
            f'\t\t\t\t\t"Priority"\t\t"250"\n'
            f'\t\t\t\t}}\n'
        )

    has_mapping = '"CompatToolMapping"' in data

    if not has_mapping:
        # Create the entire CompatToolMapping block and all entries at once
        block = '\t\t\t"CompatToolMapping"\n\t\t\t{\n'
        for appid in appids:
            block += _entry(str(appid))
        block += '\t\t\t}\n'
        data = re.sub(
            r'("Steam"\s*\{)',
            r'\1\n' + block,
            data,
            count=1,
        )
    else:
        # CompatToolMapping exists — replace or insert each appid entry
        for appid in appids:
            appid_str = str(appid)
            entry = _entry(appid_str)
            # Use re.DOTALL so [^}] correctly spans multiple lines inside the block.
            # The entry block is 4 lines, so [^}]* with DOTALL is required.
            pattern = rf'(\t+"{re.escape(appid_str)}"\n\t+\{{[^}}]*\}})'
            if re.search(pattern, data, re.MULTILINE | re.DOTALL):
                # Replace existing block
                data = re.sub(pattern, entry.rstrip('\n'), data, flags=re.MULTILINE | re.DOTALL)
            else:
                # Insert after CompatToolMapping opening brace
                data = re.sub(
                    r'("CompatToolMapping"\s*\{)',
                    r'\1\n' + entry,
                    data,
                    count=1,
                )

    if _write_and_validate_vdf(STEAM_CONFIG, data):
        for appid in appids:
            _record_config_vdf(str(appid), "CompatToolMapping", version)


def clear_compat_tool(appids):
    """
    Remove CompatToolMapping entries for the given appids from Steam's
    config.vdf. Inverse of set_compat_tool — used for games where Steam
    must NOT wrap the launch in a compat tool, e.g. LCD Plutonium games
    whose launch options invoke `flatpak run` to hand off to Heroic.

    Steam wraps any launch with a CompatToolMapping entry inside Steam
    Linux Runtime (sniper). From inside that container the host's flatpak
    binary is invisible, so the flatpak invocation fails and the launch
    flash-closes. Heroic owns the Proton invocation downstream, so the
    Steam-side compat tool is not just unnecessary — it actively breaks
    the launch.

    Silently no-ops if config.vdf doesn't exist or the entry isn't there.
    Must be called while Steam is closed so the change persists.

    appids — list of int or str appids
    """
    if not os.path.exists(STEAM_CONFIG):
        return

    with open(STEAM_CONFIG, "r", encoding="utf-8") as f:
        data = f.read()

    modified = False
    for appid in appids:
        appid_str = str(appid)
        # Same pattern as shortcut._clear_compat_tool — matches the appid
        # block including its trailing newline so the file stays clean.
        pattern = rf'\t+"{re.escape(appid_str)}"\n\t+\{{[^}}]*\}}\n?'
        if re.search(pattern, data, re.MULTILINE | re.DOTALL):
            data = re.sub(pattern, "", data, flags=re.MULTILINE | re.DOTALL)
            modified = True

    if modified and _write_and_validate_vdf(STEAM_CONFIG, data):
        for appid in appids:
            _remove_config_vdf(str(appid), "CompatToolMapping")


def set_default_launch_option(steam_root, appids_config):
    """
    Set the default launch option for games with multiple launch modes so
    Steam Deck skips the 'which mode?' dialog.

    On SteamOS the picker is controlled by the Deck configurator system, not
    the standard localconfig.vdf apps block. This function targets the correct
    Deck-specific location:

      - Writes DefaultLaunchOption into the "apps" block that sits directly
        after "Deck_ConfiguratorInterstitialApps_AppLauncherInteractionIssues"
      - Sets "Deck_ConfiguratorInterstitialsCheckbox_AppLauncherInteractionIssues"
        to "1" so the Deck configurator treats the choice as confirmed and
        stops showing the picker

    appids_config — dict mapping appid to (hash_key, index)
        e.g. {"7940": ("7a722f97", "1"), "10090": ("9aa5e05f", "0")}

    Must be called while Steam is closed.
    """
    for uid, vdf_path, content in _localconfigs(steam_root):
        modified = False

        # ── Step 1: set the checkbox to "1" so the Deck configurator treats
        # the launch choice as confirmed and stops showing the picker ──────────
        checkbox_pattern = _value_pattern("Deck_ConfiguratorInterstitialsCheckbox_AppLauncherInteractionIssues")
        if checkbox_pattern.search(content):
            content  = checkbox_pattern.sub(r'\g<1>1\g<3>', content)
            modified = True

        # ── Step 2: write DefaultLaunchOption into the Deck configurator's
        # own "apps" block — this is what the picker actually reads on SteamOS.
        # The block sits immediately after the InterstitialApps key. ──────────
        interstitial_match = _INTERSTITIAL_RE.search(content)

        if interstitial_match:
            apps_open  = interstitial_match.end() - 1
            apps_close = _find_block_end(content, apps_open)
            if apps_close != -1:
                apps_block = content[apps_open + 1:apps_close]

                for appid, (hash_key, index) in appids_config.items():
                    entry = _dlo_entry(appid, hash_key, index)
                    appid_pattern = re.compile(
                        r'"' + re.escape(appid) + r'"\s*\{',
                        re.IGNORECASE
                    )
                    appid_match = appid_pattern.search(apps_block)
                    if appid_match:
                        appid_open  = appid_match.end() - 1
                        appid_close = _find_block_end(apps_block, appid_open)
                        if appid_close != -1:
                            apps_block = (
                                apps_block[:appid_match.start()] +
                                entry.strip() +
                                apps_block[appid_close + 1:]
                            )
                    else:
                        apps_block = apps_block.rstrip() + '\n' + entry

                content = (
                    content[:apps_open + 1] +
                    apps_block +
                    content[apps_close:]
                )
                modified = True
        else:
            # Deck configurator block doesn't exist yet — build it from scratch
            # and insert before "LaunchOptionTipsShown" if present
            deck_block  = '\t\t\t"Deck_ConfiguratorInterstitialsVersionSeen_AppLauncherInteractionIssues"\t\t"1"\n'
            deck_block += '\t\t\t"Deck_ConfiguratorInterstitialsCheckbox_AppLauncherInteractionIssues"\t\t"1"\n'
            deck_block += '\t\t\t"Deck_ConfiguratorInterstitialApps_AppLauncherInteractionIssues"\t\t"[' + ','.join(appids_config.keys()) + ']"\n'
            deck_block += '\t\t\t"apps"\n\t\t\t{\n'
            for appid, (hash_key, index) in appids_config.items():
                deck_block += _dlo_entry(appid, hash_key, index)
            deck_block += '\t\t\t}\n'

            tips_pattern = re.compile(r'"LaunchOptionTipsShown"', re.IGNORECASE)
            tips_match   = tips_pattern.search(content)
            if tips_match:
                content  = content[:tips_match.start()] + deck_block + content[tips_match.start():]
                modified = True
            else:
                # LaunchOptionTipsShown absent (fresh account) — try to insert
                # before the closing brace of the Steam user block instead.
                # Find the system block that contains localconfig keys — it sits
                # inside "Software" -> "Valve" -> "Steam" in the file hierarchy.
                steam_block_pattern = re.compile(r'"Steam"\s*\{', re.IGNORECASE)
                steam_match = steam_block_pattern.search(content)
                if steam_match:
                    steam_open  = steam_match.end() - 1
                    steam_close = _find_block_end(content, steam_open)
                    if steam_close != -1:
                        # Insert deck_block just before the Steam block closes
                        content = (
                            content[:steam_close] +
                            deck_block +
                            content[steam_close:]
                        )
                        modified = True

        if modified and _write_and_validate_vdf(vdf_path, content, errors="surrogateescape"):
            for appid, (hash_key, index) in appids_config.items():
                _record_localconfig(
                    uid, appid, "DefaultLaunchOption",
                    json.dumps({"hash_key": hash_key, "index": index})
                )


def clear_default_launch_option(steam_root, appids):
    """
    Remove DeckOps' DefaultLaunchOption for these appids from the Deck
    configurator "apps" block (see set_default_launch_option), so Steam
    shows the launch menu again. Must be called while Steam is closed.
    """
    for uid, vdf_path, content in _localconfigs(steam_root):
        m = _INTERSTITIAL_RE.search(content)
        if not m:
            continue
        apps_open = m.end() - 1
        apps_close = _find_block_end(content, apps_open)
        if apps_close == -1:
            continue
        block = content[apps_open + 1:apps_close]
        changed = False
        for appid in appids:
            am = re.search(r'\s*"' + re.escape(str(appid)) + r'"\s*\{', block)
            if not am:
                continue
            end = _find_block_end(block, am.end() - 1)
            if end == -1:
                continue
            block = block[:am.start()] + block[end + 1:]
            changed = True
        if changed:
            _write_and_validate_vdf(vdf_path, content[:apps_open + 1] + block + content[apps_close:],
                                    errors="surrogateescape")
