"""Shared pytest hooks.

Qt test files each reuse one QApplication, but a widget a test leaves visible
stays alive for the rest of the run. A later ``QApplication.setStyleSheet``
repolishes every visible top-level widget, and on Windows that touched widgets
whose patches and parents from another file were already gone, crashing the
run with an access violation. Closing leftover top-level widgets after each
test keeps the tests isolated. (Deleting them is not safe: Python may still
own the wrappers.)
"""

import os
import sys

import pytest


def _close_leftovers():
    qt = sys.modules.get("qt")
    app_cls = getattr(qt, "QApplication", None) if qt else None
    app = app_cls.instance() if app_cls is not None else None
    if app is None:
        return
    for w in list(app.topLevelWidgets()):
        try:
            w.close()
        except RuntimeError:  # C++ side already deleted
            pass
    app.processEvents()


def pytest_runtest_teardown(item, nextitem):
    _close_leftovers()


_EXIT = {"status": None}


def pytest_sessionfinish(session, exitstatus):
    _close_leftovers()
    _EXIT["status"] = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    # PyQt tears down the QApplication and the module-level widget wrappers in
    # an arbitrary order at interpreter exit, which intermittently segfaults on
    # Windows after every test has passed. Once pytest has reported, leave
    # without that teardown so the exit code is the real result.
    qt = sys.modules.get("qt")
    app_cls = getattr(qt, "QApplication", None) if qt else None
    if _EXIT["status"] is not None and app_cls is not None and app_cls.instance() is not None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_EXIT["status"])
