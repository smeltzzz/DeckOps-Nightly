#!/usr/bin/env python3
"""
fovwatch.py - run a game and hold its cg_fov dvar at a value

    fovwatch.py <fov> <game exe name> -- <the game's launch command...>

For MW3 SP (iw5sp.exe, 64-bit Steam build), where cg_fov can't be changed from
the config or the console. This is what FOV changers do: find the dvar's record
in the game's memory and write the value into it. Runs on the host (Linux), not
inside Wine:

  1. start the game as a child, so it is a descendant of this process and
     /proc/<pid>/mem is readable even with ptrace_scope=1;
  2. find the game's process (comm == the exe name);
  3. find the "cg_fov" string in the exe's image, then the dvar_t whose name
     pointer points at it (dvar_t: name at +0, current value at +0x10, as in
     IW-Pad's table for this build);
  4. write the float, and again whenever the game puts 65 back (level loads).

The launch never depends on any of this: the game is started first, and every
failure is only logged (install.log).
"""

import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time

from log import setup_logging, get_logger

_log = get_logger("fovwatch")

DVAR_NAME = b"cg_fov"
VALUE_OFFSET = 0x10                 # dvar_t current value (x64 iw5sp, IW-Pad's dvarValue)
IMAGE_WINDOW = 0x6000000            # how far past the image base to look (image + .bss)
SANE_FOV = (20.0, 200.0)            # a real cg_fov record holds a value in here
HEAP_SCAN_LIMIT = 512 * 1024 * 1024  # fallback: anonymous memory to search at most


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


# ── finding the dvar ─────────────────────────────────────────────────────────

def _chunks(mem, start, end, size=16 * 1024 * 1024):
    pos = start
    while pos < end:
        n = min(size, end - pos)
        data = mem.read(pos, n)
        if not data:
            break
        yield pos, data
        pos += len(data)


def find_cg_fov(mem, maps, exe_name, log=_log.info):
    """Address of the cg_fov dvar_t's current value, or None."""
    img = [m for m in maps if m[3].lower().endswith(exe_name.lower())]
    base = min((m[0] for m in img), default=0x140000000)
    window = [m for m in maps if base <= m[0] < base + IMAGE_WINDOW and m[2][0] == "r"]

    strings = []
    needle = b"\x00" + DVAR_NAME + b"\x00"
    for s, e, perms, path in window:
        for pos, data in _chunks(mem, s, e):
            i = data.find(needle)
            while i != -1:
                strings.append(pos + i + 1)
                i = data.find(needle, i + 1)
    log(f"cg_fov: base {base:#x}, {len(window)} readable image regions, {len(strings)} name string(s)")
    if not strings:
        return None

    pointers = {struct.pack("<Q", a): a for a in strings}

    def scan(regions, label):
        hits = []
        for s, e, perms, path in regions:
            if perms[1] != "w":
                continue
            for pos, data in _chunks(mem, s, e):
                for needle_bytes in pointers:
                    i = data.find(needle_bytes)
                    while i != -1:
                        if (pos + i) % 8 == 0:
                            rec = pos + i
                            raw = mem.read(rec + VALUE_OFFSET, 4)
                            if len(raw) == 4:
                                v = struct.unpack("<f", raw)[0]
                                if SANE_FOV[0] <= v <= SANE_FOV[1]:
                                    hits.append((rec, v))
                        i = data.find(needle_bytes, i + 1)
        log(f"cg_fov: {label}: {len(hits)} record(s) {[(hex(r), round(v, 2)) for r, v in hits[:4]]}")
        return hits

    hits = scan(window, "image")
    if not hits:
        budget, heap = HEAP_SCAN_LIMIT, []
        for m in maps:
            size = m[1] - m[0]
            if m[2][0] == "r" and m[2][1] == "w" and not m[3] and size <= budget:
                heap.append(m)
                budget -= size
        hits = scan(heap, "heap")
    return hits[0][0] + VALUE_OFFSET if hits else None


# ── the watch loop ───────────────────────────────────────────────────────────

def hold(mem, addr, fov):
    """Write fov if the dvar has another value. Returns True if it wrote, None if unreadable."""
    raw = mem.read(addr, 4)
    if len(raw) != 4:
        return None
    if abs(struct.unpack("<f", raw)[0] - fov) < 0.01:
        return False
    return mem.write(addr, struct.pack("<f", fov))


def watch(root_pid, fov, exe, stop, interval=0.2, find_every=5.0):
    pid = mem = addr = None
    last_find = 0.0
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
                pid = mem = addr = None
                continue
            if addr is None:
                if time.monotonic() - last_find >= find_every:
                    last_find = time.monotonic()
                    addr = find_cg_fov(mem, read_maps(pid), exe)
                    if addr:
                        _log.info("fovwatch: cg_fov at %#x, holding %s", addr, fov)
                stop.wait(interval)
                continue
            if hold(mem, addr, fov) is None:
                once("lost", "fovwatch: cg_fov unreadable, looking again")
                addr = None
        except PermissionError as ex:
            try:
                scope = open("/proc/sys/kernel/yama/ptrace_scope").read().strip()
            except OSError:
                scope = "?"
            once("perm", f"fovwatch: no access to the game's memory ({ex}); ptrace_scope={scope}")
            stop.wait(5.0)
            pid = mem = addr = None
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
    if "--" not in argv or len(argv) < 4:
        print(__doc__)
        return 2
    sep = argv.index("--")
    fov, exe, cmd = float(argv[0]), argv[1], argv[sep + 1:]

    child = subprocess.Popen(cmd, env=_child_env())
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda s, f: child.send_signal(s))

    stop = threading.Event()
    threading.Thread(target=watch, args=(os.getpid(), fov, exe, stop), daemon=True).start()
    rc = child.wait()
    stop.set()
    return rc


if __name__ == "__main__":
    setup_logging()
    sys.exit(main(sys.argv[1:]))
