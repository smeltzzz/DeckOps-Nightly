"""Does the UI still come up? Imports every UI module and builds every screen offscreen.

Not a behaviour test: it catches the mistakes a refactor makes (a name that no
longer resolves, an import that went missing) at the cheapest place to find
them. Skipped when Qt can't be loaded (no PyQt6, or no system libraries for it).
"""

import importlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from qt import QApplication, QStackedWidget
except ImportError as ex:                                  # PyQt missing, or its system libs
    pytest.skip(f"Qt not available: {ex}", allow_module_level=True)

UI_MODULES = ["ui_constants", "ui_setup", "ui_install", "ui_manifest", "ui_manage", "ui_qt"]

# The screens DeckOpsWindow registers, in ui_qt.py order.
SCREENS = ["BootstrapScreen", "SetupFlowScreen", "ManifestModsScreen", "WelcomeScreen", "SetupScreen",
           "InstallScreen", "ManagementScreen", "ConfigureScreen", "SetupCompleteScreen", "UpdateScreen"]


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Screens read deckops.json while building; never the real one."""
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "deckops.json"))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_mtime", 0.0)


@pytest.mark.parametrize("name", UI_MODULES)
def test_ui_module_imports(name):
    importlib.import_module(name)


@pytest.mark.parametrize("name", SCREENS)
def test_screen_builds(app, name):
    ui_qt = importlib.import_module("ui_qt")
    cls = getattr(ui_qt, name)
    stack = QStackedWidget()
    screen = cls(stack)
    stack.addWidget(screen)
    assert getattr(screen, "screen_name", cls.__name__) == name


def test_window_builds_and_every_screen_can_be_navigated_to(app):
    import ui_constants
    ui_qt = importlib.import_module("ui_qt")
    win = ui_qt.DeckOpsWindow()
    assert win.stack.count() == len(SCREENS)
    for name in SCREENS:
        ui_constants.go_to(win.stack, name)
        assert getattr(win.stack.currentWidget(), "screen_name", None) == name
    win.deleteLater()
