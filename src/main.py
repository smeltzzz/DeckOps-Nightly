"""
main.py — DeckOps entry point

Recovers interrupted updates before loading the app, then hands off
to the Qt UI.
"""

import os
import sys

# Ensure src/ is on the path when run directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The installed desktop shortcut launches main.py directly. Recover before
# importing logging, Qt, or anything else an interrupted update may have touched.
from update_manifest import recover_update, update_lock
try:
    _install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with update_lock(_install_dir):
        _rolled_back = recover_update(_install_dir)
except Exception as ex:
    print(f"DeckOps could not open safely: {ex}", file=sys.stderr)
    import shutil, subprocess
    if shutil.which("zenity"):
        message = ("Another DeckOps update is running. Wait for it to finish before opening DeckOps again."
                   if isinstance(ex, BlockingIOError) else
                   "Could not restore an interrupted update. Your files were kept; please contact DeckOps support before reinstalling.")
        subprocess.run(["zenity", "--error", "--title=DeckOps", f"--text={message}"], check=False)
    sys.exit(1)

if _rolled_back:
    # The current main.py was loaded before the rollback. Start it again from
    # disk so it matches the modules we just restored.
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])

from log import setup_logging, get_logger
setup_logging()

# Uncaught exceptions (any thread) and hard crashes go to the log from here on
import crash
crash.install()

# Icons from older installers bypass the launcher, which can repair a broken
# venv. This start worked, so the launcher is there to switch them to.
try:
    from update_manifest import upgrade_desktop_entries
    for _icon in upgrade_desktop_entries(_install_dir):
        get_logger(__name__).info("desktop icon now opens through launcher.sh: %s", _icon)
except Exception:
    get_logger(__name__).warning("desktop icon upgrade skipped", exc_info=True)


def _ensure_qt6():
    # Updates only copy files, so venvs made before the PyQt6 move still
    # have PyQt5 alone. Any failure leaves the app on PyQt5 (qt.py).
    try:
        import PyQt6.QtWidgets  # noqa: F401
        return
    except ImportError:
        pass
    # A working PyQt5 installation can open DeckOps right now. Upgrading it
    # here would block every offline launch on pip's network timeout.
    try:
        import PyQt5.QtWidgets  # noqa: F401
        return
    except ImportError:
        pass
    import shutil, subprocess
    log = get_logger(__name__)
    # Only ever install into the virtualenv this process runs from. Started
    # with the distro interpreter (a stray `python3 src/main.py`, a desktop
    # file from an older installer), dirname(dirname(sys.executable)) is /usr:
    # /usr/bin/pip exists on most images, so this put PyQt6 into the OS Python
    # and the uninstall two blocks down would remove distro packages. PEP 668
    # images refuse, but the failure was only logged.
    if sys.prefix == sys.base_prefix:
        log.info("not running from a virtualenv: PyQt6 upgrade skipped")
        return
    venv = sys.prefix
    pip = os.path.join(venv, "bin", "pip")
    # Set when the wheel installed but could not open a window here
    marker = os.path.join(venv, ".qt6_unsupported")
    if not os.path.exists(pip) or os.path.exists(marker):
        return
    z = None
    if shutil.which("zenity"):
        z = subprocess.Popen(["zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close",
                              "--title=DeckOps", "--text=Updating DeckOps components...", "--width=320"],
                             stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        ok = subprocess.run([pip, "install", "--quiet", "--disable-pip-version-check", "PyQt6"],
                            capture_output=True, timeout=900).returncode == 0
        # A wheel that installs but can't load the platform plugin would abort the app, so try it in a child
        if ok and subprocess.run([sys.executable, "-c", "from PyQt6.QtWidgets import QApplication; QApplication([])"],
                                 capture_output=True, timeout=60).returncode != 0:
            subprocess.run([pip, "uninstall", "-y", "-q", "PyQt6", "PyQt6-Qt6", "PyQt6-sip"], capture_output=True)
            open(marker, "w").close()
            log.warning("PyQt6 installed but could not open a window, staying on PyQt5")
            return
        if ok:
            import importlib; importlib.invalidate_caches()
        log.info("PyQt6 installed" if ok else "PyQt6 install failed, staying on PyQt5 (retry next launch)")
    except Exception as ex:
        log.warning("PyQt6 install skipped: %s", ex)
    finally:
        if z:
            z.stdin.close(); z.terminate()


_ensure_qt6()

from ui_qt import run

if __name__ == "__main__":
    run()
