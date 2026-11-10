"""
inhibit.py - keep the device awake during long operations

Installs can run for hours and a Deck that suspends mid-install leaves
a half-written prefix behind. Two locks are taken because they cover
different things: logind blocks suspend and idle actions (the one that
matters in Game Mode), and org.freedesktop.ScreenSaver blocks the KDE
screen locker in Desktop Mode.

Both are best effort. A missing binary, bus or service is never fatal.
"""

import atexit
import os
import subprocess

from log import get_logger

_log = get_logger(__name__)

# Bounded so a leaked child can't hold the machine awake indefinitely.
MAX_HOLD = 6 * 3600
POLL = 5

# What systemd-inhibit runs while it holds the lock. A plain `sleep 6h` kept the
# machine awake for hours after DeckOps crashed or was killed (atexit never ran).
# This one also ends within POLL seconds of the DeckOps process ($1) going away.
# It can't notice os.execv (the PID stays the same): call stop() before exec-ing.
_WATCH = ('n=0; while [ "$n" -lt "$2" ] && kill -0 "$1" 2>/dev/null; '
          'do sleep "$3"; n=$((n + 1)); done')

_proc = None
_cookie = None
# Why the locks were asked for, even if none could be taken: the window asks
# before quitting while a long operation runs (see ui_qt closeEvent).
_busy = None


def busy():
    """The reason passed to start() while an operation runs, else None."""
    return _busy


def _ss_iface():
    from qt import QDBusConnection, QDBusInterface
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return None
    iface = QDBusInterface("org.freedesktop.ScreenSaver",
                           "/org/freedesktop/ScreenSaver",
                           "org.freedesktop.ScreenSaver", bus)
    return iface if iface.isValid() else None


def _ss_inhibit(why):
    try:
        iface = _ss_iface()
        if iface is None:
            return None
        reply = iface.call("Inhibit", "DeckOps", why)
        # An error reply (service present but refusing) carries its message in
        # arguments(): that string must not be kept as a cookie and sent back to UnInhibit.
        from qt import QDBusMessage
        if reply.type() != QDBusMessage.MessageType.ReplyMessage:
            return None
        args = reply.arguments()
        return args[0] if args and isinstance(args[0], int) else None
    except Exception as ex:
        _log.debug("screensaver inhibit unavailable: %s", ex)
        return None


def _ss_uninhibit(cookie):
    try:
        iface = _ss_iface()
        if iface is not None:
            iface.call("UnInhibit", cookie)
    except Exception as ex:
        _log.debug("screensaver uninhibit failed: %s", ex)


def start(why="DeckOps is installing"):
    """Take the locks. Safe to call when they are already held."""
    global _proc, _cookie, _busy
    _busy = why
    # systemd-inhibit that exited at once (polkit refused, no bus) left a dead _proc
    # that made every later start() think the lock was held and never retry.
    if _proc is not None and _proc.poll() is not None:
        _log.debug("systemd-inhibit exited (%s), taking the lock again", _proc.returncode)
        _proc = None
    if _proc is None:
        try:
            # Deliberately NOT detached. Every other subprocess here uses
            # start_new_session so it outlives DeckOps; this one must die
            # with it, or the lock outlasts the install.
            _proc = subprocess.Popen(
                ["systemd-inhibit",
                 "--what=idle:sleep:handle-lid-switch",
                 "--who=DeckOps", f"--why={why}", "--mode=block",
                 "sh", "-c", _WATCH, "deckops-inhibit",
                 str(os.getpid()), str(MAX_HOLD // POLL), str(POLL)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _log.info("sleep inhibited (%s)", why)
        except Exception as ex:
            _log.debug("systemd-inhibit unavailable: %s", ex)
            _proc = None
    if _cookie is None:
        _cookie = _ss_inhibit(why)
    return bool((_proc is not None and _proc.poll() is None) or _cookie)


def stop():
    """Release both locks. Safe to call when nothing is held, and more than once.

    Call it right before os.execv: exec runs no atexit handlers, and the
    inhibitor child would otherwise stay with the new image.
    """
    global _proc, _cookie, _busy
    _busy = None
    if _proc is not None:
        try:
            if _proc.poll() is None:
                _proc.terminate()
                _proc.wait(timeout=5)
        except Exception:
            try: _proc.kill()
            except Exception: pass
        _proc = None
        _log.info("sleep inhibit released")
    if _cookie is not None:
        _ss_uninhibit(_cookie)
        _cookie = None


atexit.register(stop)
