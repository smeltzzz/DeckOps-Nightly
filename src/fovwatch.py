#!/usr/bin/env python3
"""
fovwatch.py - run a game and hold some of its dvars at fixed values

    fovwatch.py <game exe name> <dvar>=<value> [...] -- <the game's launch command...>
    e.g.  fovwatch.py iw5sp.exe cg_fov=90 com_maxfps=90 -- bash -c '...' -- proton ...

For MW2 / MW3 SP (64-bit Steam builds), where cg_fov can't be changed from the
config or the console, and where the in-game FPS cap is simplest to hold the same
way. This is what FOV changers do: find the dvar's record in the game's memory and
write the value into it. Runs on the host (Linux), not inside Wine:

  1. start the game as a child, so it is a descendant of this process and
     /proc/<pid>/mem is readable even with ptrace_scope=1;
  2. find the game's process (comm == the exe name);
  3. find each dvar's name string in the exe's image, then the dvar_t whose name
     pointer points at it (dvar_t: name at +0, current value at +0x10, as in
     IW-Pad's table for these builds);
  4. write the value, and again whenever the game puts its own back (level loads).

The launch never depends on any of this: the game is started first, and every
failure is only logged (install.log).
"""

import os
import signal
import struct
import subprocess
import sys
import threading
import time

from log import setup_logging, get_logger

_log = get_logger("fovwatch")

VALUE_OFFSET = 0x10                 # dvar_t current value (x64 builds, IW-Pad's dvarValue)
IMAGE_WINDOW = 0x6000000            # how far past the image base to look (image + .bss)
HEAP_SCAN_LIMIT = 512 * 1024 * 1024  # fallback: anonymous memory to search at most

# What each dvar holds: "f" = float32, "i" = int32, and the range a real record's
# current value is in (a record outside it is not the dvar, and is ignored).
DVAR_TYPES = {
    "cg_fov":     ("f", 20.0, 200.0),
    "com_maxfps": ("i", 10, 1000),
}


def _decode(kind, raw):
    return struct.unpack("<f" if kind == "f" else "<i", raw)[0]


def _encode(kind, value):
    return struct.pack("<f", float(value)) if kind == "f" else struct.pack("<i", int(value))


def parse_settings(args):
    """['cg_fov=90', ...] -> {name: value}. Unknown names are an error."""
    out = {}
    for a in args:
        name, _, value = a.partition("=")
        if name not in DVAR_TYPES or not value:
            raise ValueError(f"unknown setting {a!r}")
        out[name] = float(value) if DVAR_TYPES[name][0] == "f" else int(value)
    return out


# ── /proc helpers ────────────────────────────────────────────────────────────

def read_maps(pid):
    """[(start, end, perms, path)] from /proc/<pid>/maps."""
    out = []
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            parts = line.split(None, 5)
            if len(parts) < 5:
                continue
            a, b = parts[0].split("-")
            out.append((int(a, 16), int(b, 16), parts[1], parts[5].strip() if len(parts) > 5 else ""))
    return out


def find_game_pid(comm, root_pid):
    """PID of the process whose name is comm, preferring descendants of root_pid."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/comm") as f:
                if f.read().strip() != comm[:15]:
                    continue
        except OSError:
            continue
        found.append(int(name))
    for pid in found:
        if _is_descendant(pid, root_pid):
            return pid
    return found[0] if found else None


def _is_descendant(pid, root_pid):
    seen = 0
    while pid > 1 and seen < 64:
        if pid == root_pid:
            return True
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        seen += 1
    return False


class ProcMem:
    """Read and write another process's memory through /proc/<pid>/mem."""

    def __init__(self, pid):
        self.fd = os.open(f"/proc/{pid}/mem", os.O_RDWR)

    def read(self, addr, n):
        try:
            return os.pread(self.fd, n, addr)
        except OSError:
            return b""

    def write(self, addr, data):
        try:
            return os.pwrite(self.fd, data, addr) == len(data)
        except OSError:
            return False

    def close(self):
        os.close(self.fd)


# ── finding the dvars ────────────────────────────────────────────────────────

def _chunks(mem, start, end, size=16 * 1024 * 1024):
    pos = start
    while pos < end:
        n = min(size, end - pos)
        data = mem.read(pos, n)
        if not data:
            break
        yield pos, data
        pos += len(data)


def find_dvars(mem, maps, exe_name, names, log=_log.info, heap=True):
    """{name: address of the dvar_t's current value} for the names found. heap=False
    skips the (large) fallback search of anonymous memory."""
    img = [m for m in maps if m[3].lower().endswith(exe_name.lower())]
    base = min((m[0] for m in img), default=0x140000000)
    window = [m for m in maps if base <= m[0] < base + IMAGE_WINDOW and m[2][0] == "r"]

    # name string address -> dvar name
    strings = {}
    for name in names:
        needle = b"\x00" + name.encode() + b"\x00"
        for s, e, perms, path in window:
            for pos, data in _chunks(mem, s, e):
                i = data.find(needle)
                while i != -1:
                    strings[pos + i + 1] = name
                    i = data.find(needle, i + 1)
    log(f"dvars {sorted(names)}: base {base:#x}, {len(window)} readable image regions, "
        f"{len(strings)} name string(s)")
    if not strings:
        return {}

    pointers = {struct.pack("<Q", a): n for a, n in strings.items()}
    found = {}

    def scan(regions, label):
        for s, e, perms, path in regions:
            if perms[1] != "w":
                continue
            for pos, data in _chunks(mem, s, e):
                for needle_bytes, name in pointers.items():
                    if name in found:
                        continue
                    i = data.find(needle_bytes)
                    while i != -1:
                        if (pos + i) % 8 == 0:
                            kind, lo, hi = DVAR_TYPES[name]
                            raw = mem.read(pos + i + VALUE_OFFSET, 4)
                            if len(raw) == 4 and lo <= _decode(kind, raw) <= hi:
                                found[name] = pos + i + VALUE_OFFSET
                                log(f"{name}: {label} record {pos + i:#x}, value {_decode(kind, raw)}")
                                break
                        i = data.find(needle_bytes, i + 1)

    scan(window, "image")
    if heap and len(found) < len(set(strings.values())):
        budget, anon = HEAP_SCAN_LIMIT, []
        for m in maps:
            size = m[1] - m[0]
            if m[2][0] == "r" and m[2][1] == "w" and not m[3] and size <= budget:
                anon.append(m)
                budget -= size
        scan(anon, "heap")
    return found


# ── the watch loop ───────────────────────────────────────────────────────────

def hold(mem, addr, name, value):
    """Write value if the dvar has another value. Returns True if it wrote, False if it
    was already right, None if unreadable."""
    kind = DVAR_TYPES[name][0]
    raw = mem.read(addr, 4)
    if len(raw) != 4:
        return None
    cur = _decode(kind, raw)
    if (abs(cur - value) < 0.01) if kind == "f" else (cur == value):
        return False
    return mem.write(addr, _encode(kind, value))


FAST_TRIES = 24      # searches every 5 s while the game is loading (about 2 minutes)
HEAP_TRIES = 4       # the big heap search runs on at most this many of them
SLOW_EVERY = 60.0    # after that, only the cheap image search, once a minute


def search_plan(tries):
    """(seconds to wait before search number `tries`, whether it may scan the heap)."""
    return (5.0 if tries < FAST_TRIES else SLOW_EVERY), tries < HEAP_TRIES


def watch(root_pid, settings, exe, stop, interval=0.2):
    pid = mem = None
    addrs = {}                       # name -> address, for the dvars found
    last_find = tries = 0
    told = set()

    def once(key, msg):
        if key not in told:
            told.add(key)
            _log.info(msg)

    while not stop.is_set():
        try:
            if pid is None:
                pid = find_game_pid(exe, root_pid)
                if pid is None:
                    stop.wait(1.0)
                    continue
                _log.info("fovwatch: game process %s found", pid)
                mem = ProcMem(pid)
            if not os.path.isdir(f"/proc/{pid}"):
                if mem:
                    mem.close()
                pid = mem = None
                addrs = {}
                last_find = tries = 0
                continue
            missing = [n for n in settings if n not in addrs]
            if missing:
                wait, heap = search_plan(tries)
                if time.monotonic() - last_find >= wait:
                    last_find = time.monotonic()
                    tries += 1
                    got = find_dvars(mem, read_maps(pid), exe, missing, heap=heap)
                    for name, addr in got.items():
                        addrs[name] = addr
                        _log.info("fovwatch: %s at %#x, holding %s", name, addr, settings[name])
                    if tries == FAST_TRIES and len(addrs) < len(settings):
                        _log.info("fovwatch: %s not found yet, now checking once a minute",
                                  [n for n in settings if n not in addrs])
            for name in list(addrs):
                if hold(mem, addrs[name], name, settings[name]) is None:
                    once(f"lost:{name}", f"fovwatch: {name} unreadable, looking again")
                    del addrs[name]
        except PermissionError as ex:
            try:
                scope = open("/proc/sys/kernel/yama/ptrace_scope").read().strip()
            except OSError:
                scope = "?"
            once("perm", f"fovwatch: no access to the game's memory ({ex}); ptrace_scope={scope}")
            stop.wait(5.0)
            pid = mem = None
            addrs = {}
        except Exception as ex:
            once(f"err:{type(ex).__name__}", f"fovwatch: {type(ex).__name__}: {ex}")
            stop.wait(2.0)
        stop.wait(interval)


# ── entry point ──────────────────────────────────────────────────────────────

def _child_env():
    """The game's own environment: prelaunch.sh hid LD_PRELOAD and LD_LIBRARY_PATH
    from this Python and passed them in DECKOPS_* variables."""
    env = dict(os.environ)
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH"):
        saved = env.pop("DECKOPS_" + name, None)
        if saved:
            env[name] = saved
    return env


def main(argv):
    if "--" not in argv or argv.index("--") < 2 or argv.index("--") == len(argv) - 1:
        print(__doc__)
        return 2
    sep = argv.index("--")
    exe, cmd = argv[0], argv[sep + 1:]
    try:
        settings = parse_settings(argv[1:sep])
    except ValueError as ex:
        _log.warning("fovwatch: %s, running the game without it", ex)
        settings = {}

    child = subprocess.Popen(cmd, env=_child_env())
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda s, f: child.send_signal(s))

    stop = threading.Event()
    if settings:
        threading.Thread(target=watch, args=(os.getpid(), settings, exe, stop), daemon=True).start()
    rc = child.wait()
    stop.set()
    return rc


if __name__ == "__main__":
    setup_logging()
    sys.exit(main(sys.argv[1:]))
