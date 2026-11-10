"""
detect_hw.py - detect OS, device, and CPU architecture

Reads DMI identity and os-release at import time. Results are cached
for the process lifetime. All reads are best-effort: a missing sysfs
node or unrecognised string returns None, never raises.

This is a leaf module with no DeckOps imports.
"""

import platform


# ── DMI product_name -> DEVICES key ──────────────────────────────────────────
# Values come from /sys/class/dmi/id/product_name on each device.
# ASUS puts the model code after the marketing name and repeats it
# (e.g. "ROG Ally RC71L_RC71L", "ROG Ally X RC72LA_RC72LA_000123206"), so
# those match the code anywhere in the string.
#
# Every key returned here must exist in ui_setup.DEVICES (the setup screen
# looks it up directly).

_DMI_EXACT = {
    # Valve
    "Jupiter":  "sd_lcd",
    "Galileo":  "sd_oled",
    "Fremont":  "steam_machine",
    # Lenovo (opaque model codes)
    "83E1":     "legion_go",
    "83L3":     "legion_go_s",
    "83N6":     "legion_go_s",
    "83Q2":     "legion_go_s",
    "83Q3":     "legion_go_s",
    "83N0":     "legion_go_2",
    "83N1":     "legion_go_2",
}

# ASUS model codes, matched as substrings. Longer / newer codes first so a
# code can never be shadowed by a shorter one earlier in the list.
_DMI_CODES = [
    ("RC72LA", "rog_ally_x"),
    # RC73YA is the ROG Xbox Ally (non-X). It has the same 1920x1080 screen
    # and controls as the X, so both share one entry, labelled "ROG Xbox Ally".
    ("RC73YA", "xbox_ally_x"),
    ("RC73XA", "xbox_ally_x"),
    ("RC74XA", "xbox_ally_x"),
    ("RC71L",  "rog_ally"),
]

# MSI uses board_name (MS-xxxx) more reliably than product_name.
_DMI_BOARD = {
    "MS-1T8K": "msi_claw_8",
    "MS-1T52": "msi_claw_8",        # Claw 8 AI+ (1920x1200)
    # MS-1T41 is the original Claw A1M: a 7" 1920x1080 screen. It was mapped
    # to msi_claw_8 (1920x1200). With no 1080p Claw entry it goes to the
    # generic handheld, which asks for the screen resolution.
    "MS-1T41": "other_handheld",
}

# ---
# ARM handhelds have no DMI. Model strings are the ones Armada matches
# in /usr/libexec/armada/device-env.
_DT_MODEL = {
    "Retroid Pocket 6":           "rp6",
    "Retroid Pocket 6 TOP-DPAD":  "rp6",
}

# ── os-release ID -> os_key ──────────────────────────────────────────────────

_OS_ID_MAP = {
    "steamos":  "steamos",
    "bazzite":  "bazzite",
    "cachyos":  "cachyos",
    "armada":   "armada",
}


def _read_dmi(field: str) -> str:
    try:
        with open(f"/sys/class/dmi/id/{field}") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_os_release() -> dict:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            kv = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" not in line or line.startswith("#"):
                        continue
                    k, v = line.split("=", 1)
                    kv[k] = v.strip("\"'")      # os-release allows single quotes too
            return kv
        except OSError:
            continue
    return {}


def detect_os() -> str | None:
    """Returns 'steamos', 'bazzite', 'cachyos', 'armada', or None."""
    os_id = _read_os_release().get("ID", "").lower()
    return _OS_ID_MAP.get(os_id)


def detect_device() -> str | None:
    """Returns a DEVICES key ('sd_lcd', 'rog_ally', etc.) or None."""
    product = _read_dmi("product_name")
    hit = _DMI_EXACT.get(product)
    if hit:
        return hit
    for code, key in _DMI_CODES:
        if code in product:
            return key
    board = _read_dmi("board_name")
    hit = _DMI_BOARD.get(board)
    if hit:
        return hit
    try:
        with open("/sys/firmware/devicetree/base/model") as f:
            return _DT_MODEL.get(f.read().strip("\0\n "))
    except OSError:
        return None


def detect_arch() -> str:
    """Returns 'x86_64' or 'aarch64' (or whatever platform.machine() says)."""
    return platform.machine()


def is_arm() -> bool:
    return detect_arch() in ("aarch64", "arm64")
