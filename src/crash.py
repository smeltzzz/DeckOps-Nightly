"""
crash.py -- last-resort error handling for DeckOps

Without it an unexpected exception was invisible:
  - in a Qt slot (button handler, signal, showEvent) PyQt aborts the whole
    app (qFatal) when no sys.excepthook is set: the window just vanishes
  - in a worker thread the thread dies with a traceback on stderr only,
    which nobody sees, and the screen waits forever
  - a hard crash in Qt/C code left nothing at all
and none of it reached install.log, so "Copy Log" had nothing to show.

install()       -- at startup, before the UI: every uncaught exception (main
                   and worker threads) is logged with its traceback, and
                   faulthandler records hard crashes in logs/crash.log
install_qt()    -- once the QApplication exists: also shows an error dialog
                   on the UI thread, and DeckOps keeps running
"""

import faulthandler
import os
import sys
import threading
import traceback

from identity import LOG_DIR
from log import get_logger

_log = get_logger(__name__)

CRASH_LOG = os.path.join(LOG_DIR, "crash.log")

_crash_file = None   # kept open for faulthandler
_notify = None       # set by install_qt: callable(str) safe from any thread


def _report(where: str, etype, value, tb):
    text = "".join(traceback.format_exception(etype, value, tb)).rstrip()
    try:
        _log.critical("Unexpected error in %s:\n%s", where, text)
    except Exception:
        pass
    if _notify:
        try:
            _notify(f"{etype.__name__}: {value}" if str(value) else etype.__name__)
        except Exception:
            pass


def _excepthook(etype, value, tb):
    if issubclass(etype, KeyboardInterrupt):
        sys.__excepthook__(etype, value, tb)
        return
    _report("the main thread", etype, value, tb)


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "a background thread"
    _report(f"thread {name}", args.exc_type, args.exc_value, args.exc_traceback)


def _fold_previous_crash():
    """Move a hard-crash dump from the last run into install.log. It goes
    through the logger so it is redacted like everything else (crash.log
    itself has raw paths and isn't what Copy Log shares)."""
    try:
        with open(CRASH_LOG, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        return
    if text:
        _log.error("DeckOps crashed hard last time (faulthandler):\n%s", text)
    try:
        open(CRASH_LOG, "w").close()
    except OSError:
        pass


def install():
    """Log every uncaught exception and record hard crashes. Safe to call
    more than once."""
    global _crash_file
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    if _crash_file is None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            _fold_previous_crash()
            _crash_file = open(CRASH_LOG, "a", encoding="utf-8")
            faulthandler.enable(file=_crash_file, all_threads=True)
        except (OSError, RuntimeError, ValueError):
            _log.debug("faulthandler not enabled", exc_info=True)


def install_qt(parent_getter=None):
    """
    Show an error dialog for uncaught exceptions; call once the QApplication
    exists. The dialog is raised on the UI thread through a queued signal,
    so worker-thread errors are safe. One dialog at a time: errors that
    arrive while it is open are logged and counted in it, not stacked.
    """
    global _notify
    from qt import QObject, QMessageBox, pyqtSignal

    class _Notifier(QObject):
        error = pyqtSignal(str)

    state = {"open": False, "more": 0}

    def show(summary):
        if state["open"]:
            state["more"] += 1
            return
        state["open"] = True
        try:
            parent = parent_getter() if parent_getter else None
            box = QMessageBox(parent)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("DeckOps hit an unexpected error")
            box.setText("Something went wrong, and the last step may not have finished.")
            box.setInformativeText(
                f"{summary}\n\nDeckOps is still running. The details are in the log: "
                "use Copy Log to share it when asking for help.")
            copy = box.addButton("Copy Log", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is copy:
                from ui_constants import _copy_log_to_clipboard
                _copy_log_to_clipboard()
            if state["more"]:
                _log.warning("%d more error(s) while the error dialog was open", state["more"])
        except Exception:
            _log.exception("could not show the error dialog")
        finally:
            state["open"] = False
            state["more"] = 0

    notifier = _Notifier()
    notifier.error.connect(show)
    install_qt._notifier = notifier      # keep it alive
    _notify = notifier.error.emit
