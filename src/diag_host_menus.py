"""
diag_host_menus.py - why is (or isn't) a Plutonium MP/ZM entry on an SP tile?

    .venv/bin/python3 src/diag_host_menus.py           read-only report
    .venv/bin/python3 src/diag_host_menus.py --apply   also run ensure_host_menus
                                                       (close Steam first)

The report is printed and saved to ~/deckops-diag.txt.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as c
import steam_appinfo as a
from detect_games import parse_library_folders, find_installed_games, find_steam_root

OUT = os.path.expanduser("~/deckops-diag.txt")
LINES = []


def say(*parts):
    line = " ".join(str(p) for p in parts)
    print(line)
    LINES.append(line)


def plain(node):
    """A parsed binary-KV node as plain Python (dicts, str, int)."""
    import struct
    out = {}
    for t, key, val in node:
        if t == a._T_DICT:
            out[key] = plain(val)
        elif t == a._T_STR:
            out[key] = val.decode("utf-8", "replace")
        elif t == a._T_WSTR:
            out[key] = val.decode("utf-16-le", "replace")
        elif t == a._T_INT32:
            out[key] = struct.unpack("<I", val)[0]
        else:
            out[key] = repr(bytes(val))
    return out


def full_launch(ai, appid):
    span = ai.find_app(appid)
    if not span:
        return "NOT in appinfo.vdf"
    st, en = span
    node, _ = ai.parse_kv(bytes(ai.data[st + 8 + a._HDR.size:en]))
    launch = a._dict(a._dict(a._dict(node, "appinfo") or [], "config") or [], "launch")
    return plain(launch) if launch else "no launch entries"


def launch_entries(ai, appid):
    span = ai.find_app(appid)
    if not span:
        return "NOT in appinfo.vdf"
    st, en = span
    node, _ = ai.parse_kv(bytes(ai.data[st + 8 + a._HDR.size:en]))
    launch = a._dict(a._dict(a._dict(node, "appinfo") or [], "config") or [], "launch")
    if not launch:
        return "no launch entries"
    return [(i[1], a._get(i[2], "executable")[2].decode(),
             (a._get(i[2], "description") or [0, 0, b""])[2].decode()) for i in launch]


def main():
    cfg = c.load()
    sr = cfg.get("steam_root") or find_steam_root()
    say("steam_root:", sr)
    say("deck_model:", cfg.get("deck_model"), "| is_lcd:", c.is_lcd())
    say("VERSION:", open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")).read().strip())

    setup = c.get_setup_games()
    for k in ("iw5mp", "t5mp", "t6mp", "t6zm"):
        e = setup.get(k)
        say("setup", k, "->", {x: e.get(x) for x in ("client", "source")} if e else None)

    inst = find_installed_games(parse_library_folders(sr), sr)
    for k in ("iw5sp", "iw5mp", "t5sp", "t5mp", "t6sp", "t6mp", "t6zm"):
        d = (inst.get(k) or {}).get("install_dir")
        say("dir", k, "->", d)

    from plutonium import HOST_MENU, STEAM_MENU_EXES
    for k, (appid, host_key, label) in HOST_MENU.items():
        d = (inst.get(k) or {}).get("install_dir", "")
        wrapper = os.path.join(d, STEAM_MENU_EXES[k][0]) if d else ""
        say("wrapper", k, "->", STEAM_MENU_EXES[k][0], "exists:", bool(wrapper and os.path.isfile(wrapper)),
            "| same folder as", host_key + ":",
            bool(d and (inst.get(host_key) or {}).get("install_dir")
                 and os.path.realpath(d) == os.path.realpath(inst[host_key]["install_dir"])))

    try:
        ai = a._AppInfo(open(a.appinfo_path(sr), "rb").read())
        for appid in (42680, 42700, 202970):
            say("appinfo", appid, "->", launch_entries(ai, appid))
        # Every field of every launch entry (stock ones too): tells whether a
        # stock entry's label can be changed and what the extra WaW options are
        import json
        for appid in (10090, 42680, 42700, 202970):
            say("FULL launch", appid)
            say(json.dumps(full_launch(ai, appid), indent=1, ensure_ascii=False))
    except Exception as ex:
        say("appinfo unreadable:", ex)

    # The launch option each tile really has (this is what swaps in IW-Pad etc.)
    try:
        import wrapper as w
        for uid, _path, content in w._localconfigs(sr):
            for appid in ("10180", "42680", "42690", "42700", "202970", "10090", "7940"):
                block = w._app_block(content, appid)
                if block:
                    found = w._flat_values(content, block, "LaunchOptions")
                    say("LaunchOptions", uid, appid, "->", [content[vs:ve] for _, vs, ve in found])
    except Exception as ex:
        say("LaunchOptions not read:", ex)

    # MW3 folder: which IW-Pad / AlterWare files are there
    d = (inst.get("iw5sp") or {}).get("install_dir", "")
    if d and os.path.isdir(d):
        names = sorted(n for n in os.listdir(d)
                       if n.lower().startswith(("iw-pad", "iw_pad", "alterware", "awcache", "iw5", "deckops"))
                       or n.lower().endswith(".json"))
        say("MW3 folder:", [(n, os.path.getsize(os.path.join(d, n))) for n in names if os.path.isfile(os.path.join(d, n))])
        say("MW3 subfolders:", sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))))
    try:
        from config import get_iw_pad
        say("IW-Pad iw5sp saved:", get_iw_pad("iw5sp"), "| setup iw5sp:", setup.get("iw5sp"))
    except Exception as ex:
        say("IW-Pad config not read:", ex)

    # MW2 campaign: do the FOV lines survive, and what does IW-Pad say?
    import re, time
    d2 = (inst.get("iw4sp") or {}).get("install_dir", "")
    if d2:
        cfg_path = os.path.join(d2, "players", "config.cfg")
        if os.path.isfile(cfg_path):
            st = os.stat(cfg_path)
            say("MW2 config.cfg:", st.st_size, "bytes, modified", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "| writable:", os.access(cfg_path, os.W_OK))
            pat = re.compile(r'thereisacow|bind\s+F10|cg_fov|com_maxfps|developer|con_|ui_console|monkeytoy', re.I)
            with open(cfg_path, errors="replace") as f:
                for line in f:
                    if pat.search(line):
                        say("   ", line.rstrip())
        else:
            say("MW2 config.cfg: not found at", cfg_path)
        log_path = os.path.join(d2, "iw-pad.log")
        if os.path.isfile(log_path):
            with open(log_path, errors="replace") as f:
                tail = f.read().splitlines()[-25:]
            say("--- MW2 iw-pad.log (last 25 lines) ---")
            for l in tail:
                say(l)
        else:
            say("MW2 iw-pad.log: not found")

    say("recorded launch_menus:",
        {k: [e["executable"] for e in v] for k, v in cfg.get("launch_menus", {}).items()})

    if "--apply" in sys.argv:
        import plutonium
        say("--- ensure_host_menus ---")
        say("added:", plutonium.ensure_host_menus(sr, say))
        try:
            ai = a._AppInfo(open(a.appinfo_path(sr), "rb").read())
            for appid in (42680, 42700, 202970):
                say("appinfo after", appid, "->", launch_entries(ai, appid))
        except Exception as ex:
            say("appinfo unreadable:", ex)

    try:
        from identity import LOG_DIR
        log = os.path.join(LOG_DIR, "install.log")
        with open(log, errors="replace") as f:
            lines = f.readlines()
        hits = [l.rstrip() for l in lines if any(w in l.lower() for w in ("launch menu", "appinfo", "host tile"))]
        say("--- install.log (last 15 menu lines) ---")
        for l in hits[-15:]:
            say(l)
        iwpad = [l.rstrip() for l in lines if any(w in l.lower() for w in ("iw-pad", "iw5sp", "prelaunch"))]
        say("--- install.log (last 15 IW-Pad / prelaunch lines) ---")
        for l in iwpad[-15:]:
            say(l)
        fov = [l.rstrip() for l in lines if "fovwatch" in l.lower() or "cg_fov" in l.lower()]
        say("--- install.log (last 25 fovwatch lines) ---")
        for l in fov[-25:]:
            say(l)
        try:
            say("ptrace_scope:", open("/proc/sys/kernel/yama/ptrace_scope").read().strip())
        except OSError as ex:
            say("ptrace_scope not readable:", ex)
    except Exception as ex:
        say("install.log not read:", ex)

    with open(OUT, "w") as f:
        f.write("\n".join(LINES) + "\n")
    print("\nSaved to", OUT)


if __name__ == "__main__":
    main()
