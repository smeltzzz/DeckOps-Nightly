"""Focused first-run wizard tests (no real settings or hardware required)."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget, QWidget, QPushButton, QScrollArea
    import ui_setup
except ImportError:
    QApplication = None


_APP = None


class Settings:
    def __init__(self):
        self.values = {"first_run_complete": False}

    def load(self):
        return self.values.copy()

    def is_first_run(self):
        return not self.values.get("first_run_complete", False)

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda value: self.values.__setitem__(name[4:], value)
        if name.startswith("get_"):
            return lambda: self.values.get(name[4:])
        raise AttributeError(name)

    @staticmethod
    def clean_player_name(value):
        return value.strip()


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class SetupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = Settings()
        self.patches = [
            patch.object(ui_setup, "cfg", self.settings),
            patch.object(ui_setup, "detect_os", return_value=None),
            patch.object(ui_setup, "detect_device", return_value=None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.wizard = ui_setup.SetupFlowScreen(self.stack)
        self.stack.addWidget(self.wizard)
        self.stack.show()
        _APP.processEvents()
        self.addCleanup(self.stack.close)

    def test_compact_scrollable_steps_and_visible_navigation(self):
        self.assertEqual(self.wizard._current_section, "_os_section")
        self.assertIn("OPERATING SYSTEM", self.wizard._step_label.text())
        self.assertIsInstance(self.wizard._scrolls["_os_section"], QScrollArea)
        self.assertLessEqual(self.wizard._scrolls["_os_section"].height(), 440)
        self.assertGreater(self.wizard._os_section.sizeHint().height(),
                           self.wizard._scrolls["_os_section"].viewport().height())
        area = self.wizard._scrolls["_os_section"]
        options = {b.text(): b for b in self.wizard._os_section.findChildren(QPushButton)}
        # Every OS option is reachable without scrolling on an 800x500 window.
        for option in options.values():
            point = option.mapTo(area.viewport(), option.rect().bottomRight())
            self.assertLess(point.y(), area.viewport().height())
            self.assertLess(point.x(), area.viewport().width())
        options["Armada"].click()
        _APP.processEvents()
        self.assertEqual(self.settings.values["os_type"], "armada")
        self.assertTrue(self.wizard._footer_back.isVisible())
        self.wizard._footer_back.click()
        self.assertEqual(self.wizard._current_section, "_os_section")
        self.assertFalse(self.wizard._footer_back.isVisible())
        options["Other Linux"].click()
        self.assertEqual(self.settings.values["os_type"], "other_linux")

    def test_choices_do_not_overflow_right_edge(self):
        for name, area in self.wizard._scrolls.items():
            self.wizard._show(name)
            _APP.processEvents()
            for button in getattr(self.wizard, name).findChildren(QPushButton):
                if button.isHidden():
                    continue
                with self.subTest(step=name, button=button.text()):
                    right = button.mapTo(area.viewport(), button.rect().bottomRight()).x()
                    self.assertLess(right, area.viewport().width())

    def test_status_strip_leaves_first_choices_and_footer_visible(self):
        # DeckOpsWindow reserves 38px of its 800x500 minimum for QStatusBar.
        self.stack.resize(800, 462)
        _APP.processEvents()
        for name in ("_os_section", "_confirm_section"):
            self.wizard._show(name)
            _APP.processEvents()
            area = self.wizard._scrolls[name]
            for button in getattr(self.wizard, name).findChildren(QPushButton):
                with self.subTest(step=name, button=button.text()):
                    point = button.mapTo(area.viewport(), button.rect().bottomRight())
                    self.assertLess(point.x(), area.viewport().width())
                    self.assertLess(point.y(), area.viewport().height())
        self.wizard._show("_model_section")
        _APP.processEvents()
        self.assertTrue(self.wizard._footer_back.isVisible())
        self.assertLess(self.wizard._footer_back.mapTo(self.wizard,
                        self.wizard._footer_back.rect().bottomRight()).y(), self.wizard.height())
        for name, area in self.wizard._scrolls.items():
            self.wizard._show(name)
            _APP.processEvents()
            for button in getattr(self.wizard, name).findChildren(QPushButton):
                if not button.isHidden():
                    with self.subTest(step=name, button=button.text()):
                        self.assertLess(button.mapTo(area.viewport(), button.rect().bottomRight()).x(),
                                        area.viewport().width())

    def test_unknown_hardware_is_not_assumed_to_be_a_pc(self):
        self.assertEqual(self.wizard._current_section, "_os_section")
        self.wizard._pick_os("other_linux")
        self.wizard._show_device_picker()
        buttons = [b.text() for b in self.wizard._device_section.findChildren(QPushButton)]
        self.assertIn("Other handheld", buttons)
        self.assertIn("Desktop / laptop PC", buttons)
        self.wizard._pick_device("other_handheld")
        self.wizard._pick_gyro("off")
        self.wizard._name_input.setText("Custom player")
        self.wizard._save_player_name()
        self.assertEqual(self.wizard._current_section, "_resolution_section")
        self.wizard._pick_resolution("1280x800")
        self.assertEqual(self.settings.values["other_device"], "1280x800")
        self.assertEqual(self.wizard._current_section, "_play_section")
        self.wizard._go_back()
        self.assertEqual(self.wizard._current_section, "_resolution_section")
        self.wizard._go_back()
        self.assertEqual(self.wizard._name_input.text(), "Custom player")

    def test_config_reset_restarts_but_normal_back_preserves_name(self):
        self.wizard._pick_os("steamos")
        self.wizard._pick_device("sd_lcd")
        self.wizard._pick_gyro("off")
        self.wizard._name_input.setText("My player")
        self.wizard._save_player_name()
        self.wizard._go_back()
        self.assertEqual(self.wizard._name_input.text(), "My player")
        self.assertEqual(self.wizard._current_section, "_name_section")
        self.wizard._save_player_name()
        # Settings reset elsewhere; same QStackedWidget instance is reused.
        self.settings.values = {"first_run_complete": False}
        other = QWidget()
        self.stack.addWidget(other)
        self.stack.setCurrentWidget(other)
        self.stack.setCurrentWidget(self.wizard)
        _APP.processEvents()
        self.assertEqual(self.wizard._current_section, "_os_section")
        self.assertEqual(self.wizard._name_input.text(), "")
        self.assertIsNone(self.wizard._selected_device)

    def test_real_detection_requires_both_values_for_confirmation(self):
        with patch.object(ui_setup, "detect_os", return_value="steamos"), \
             patch.object(ui_setup, "detect_device", return_value=None):
            second = ui_setup.SetupFlowScreen(self.stack)
            self.assertEqual(second._current_section, "_os_section")
            self.assertIsNone(second._detected_device)
            second.close()
        with patch.object(ui_setup, "detect_os", return_value="armada"), \
             patch.object(ui_setup, "detect_device", return_value="sd_oled"):
            second = ui_setup.SetupFlowScreen(self.stack)
            self.assertEqual(second._current_section, "_confirm_section")
            second._accept_autodetect()
            self.assertEqual(self.settings.values["os_type"], "armada")
            self.assertEqual(self.settings.values["deck_model"], "oled")
            self.assertEqual(second._current_section, "_gyro_section")
            second._go_back()
            self.assertEqual(second._current_section, "_confirm_section")
            second.close()

    def test_switching_device_clears_incompatible_display_values(self):
        self.wizard._pick_os("steamos")
        self.wizard._pick_device("other_handheld")
        self.wizard._pick_gyro("off")
        self.wizard._save_player_name()
        self.wizard._pick_resolution("1280x800")
        self.wizard._back_to_prev_from_play()
        self.wizard._back_to_prev_from_res()
        self.wizard._back_to_gyro_from_name()
        self.wizard._back_to_device_from_gyro()
        self.wizard._pick_device("steam_machine")
        self.wizard._pick_gyro("off")
        self.wizard._save_player_name()
        self.assertFalse(self.wizard._choosing_native_resolution)
        self.wizard._pick_resolution("1920x1080")
        self.assertEqual(self.settings.values["other_device"], "1920x1080")


if __name__ == "__main__":
    unittest.main()
