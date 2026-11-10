"""Sixth sweep: device setup / Select Games / Installation screen UX (offscreen Qt).

Every installer, Steam call and config write is replaced, so nothing touches a
real Steam, game folder or ~/DeckOps-Nightly.
"""

import contextlib
import inspect
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget, QWidget, QLabel, QPushButton, QCheckBox, Qt
    import ui_constants
    import ui_install
    import ui_setup
    import ui_qt
except ImportError:
    QApplication = None

_APP = None
_GLYPHS = "\u2713\u2714\u2717\u2718\u26a0\u23f3\u2190\u2192"


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class _Cfg:
    """The slice of config the selection and install screens read or write."""

    def __init__(self, first_run=True):
        self.first_run = first_run
        self.setup_for_source = set()
        self.marked = []
        self.values = {}

    def is_lcd(self): return False
    def is_first_run(self): return self.first_run
    def complete_first_run(self, root): self.first_run = False
    def is_game_setup_for_source(self, key, source): return (key, source) in self.setup_for_source
    def unmark_game_setup(self, key): pass
    def get_offline_mode(self): return False
    def set_offline_mode(self, on): pass
    def set_ge_proton_version(self, version): pass
    def mark_game_setup(self, key, client, source="steam"): self.marked.append((key, client, source))
    def get_setup_games(self): return {}
    def get_gyro_mode(self): return "on"
    def get_deck_model(self): return "oled"
    def get_extra_scan_paths(self): return []
    def add_extra_scan_path(self, path): pass

    # ui_setup's wizard reads and writes plain values
    def load(self): return dict(self.values)
    @staticmethod
    def clean_player_name(value): return value.strip()

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda value: self.values.__setitem__(name[4:], value)
        if name.startswith("get_"):
            return lambda: self.values.get(name[4:])
        raise AttributeError(name)


def _texts(root):
    out = []
    for cls in (QLabel, QPushButton, QCheckBox):
        for w in root.findChildren(cls):
            out.append(w.text())
    return out


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.cfg = _Cfg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for target in (ui_install, ui_constants, ui_setup):
            p = patch.object(target, "cfg", self.cfg)
            p.start(); self.addCleanup(p.stop)
        for name in ("_log_to_file", "inhibit"):
            p = patch.object(ui_install, name, MagicMock())
            p.start(); self.addCleanup(p.stop)
        for name, value in (("detect_os", None), ("detect_device", None)):
            p = patch.object(ui_setup, name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.stack.resize(800, 500)
        self.addCleanup(self.stack.close)

    def add(self, widget):
        self.stack.addWidget(widget)
        return widget

    def placeholder(self, name):
        w = QWidget(); w.screen_name = name
        return self.add(w)

    def game(self, key, source="steam"):
        d = Path(self.root, "games", key); d.mkdir(parents=True, exist_ok=True)
        return {"name": key, "install_dir": str(d), "source": source}


# ── Installation screen: waits, pulse, results ───────────────────────────────

class InstallScreenUXTests(_Base):
    def setUp(self):
        super().setUp()
        self.screen = self.add(ui_install.InstallScreen(self.stack))
        self.selection = self.placeholder("SetupScreen")
        self.selection._choices = {}; self.selection._extra_choices = {}
        self.library = self.placeholder("ManagementScreen")

    def _show_dg(self, copied=True):
        with patch("depot_downgrade.copy_to_clipboard", return_value=copied):
            self.screen._show_iw5_dg_wait("download_depot 1 2 3", "Depot 1 of 2")

    # UI-1
    def test_double_tap_on_depot_continue_does_not_confirm_the_next_depot(self):
        s = self.screen
        ev = s._iw5_dg_event
        self._show_dg()
        self.assertFalse(s.iw5_dg_btn.isEnabled(), "shown disarmed")
        s._arm_iw5_dg(s._dg_token)
        self.assertTrue(s.iw5_dg_btn.isEnabled())
        ev.clear()
        s.iw5_dg_btn.click()
        self.assertTrue(ev.is_set())
        self.assertFalse(s.iw5_dg_btn.isEnabled(), "disabled before the worker moves on")
        # The worker clears the event and shows the next depot; the second tap lands now
        ev.clear()
        stale = s._dg_token
        self._show_dg()
        s.iw5_dg_btn.click()
        s._confirm_iw5_dg()
        self.assertFalse(ev.is_set(), "a second tap confirmed the next depot")
        s._arm_iw5_dg(stale - 1)          # a timer from an earlier wait
        self.assertFalse(s.iw5_dg_btn.isEnabled())
        s._arm_iw5_dg(s._dg_token)
        self.assertTrue(s.iw5_dg_btn.isEnabled())
        self.assertEqual(s._DG_ARM_MS, 800)

    # UI-7
    def test_failed_copy_shows_a_selectable_command_and_a_copy_button(self):
        s = self.screen
        self._show_dg(copied=False)
        self.assertEqual(s.dg_cmd.text(), "download_depot 1 2 3")
        self.assertFalse(s.dg_cmd.isHidden())
        flags = s.dg_cmd.textInteractionFlags()
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByMouse)
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.assertFalse(s.dg_copy_btn.isHidden())
        self.assertGreaterEqual(s.manual_step.font().pointSize(), ui_constants.FONT_ACTION)
        # Instructions sit above the action buttons
        clay = s.manual_step.parentWidget().layout()
        self.assertLess(clay.indexOf(s.manual_step), clay.indexOf(s.dg_cmd))
        self.assertLess(s.manual_step.parentWidget().layout().indexOf(s.dg_cmd),
                        clay.indexOf(s.summary_scroll))
        QApplication.clipboard().clear()
        with patch("depot_downgrade.copy_to_clipboard", return_value=False):
            s.dg_copy_btn.click()
        self.assertEqual(QApplication.clipboard().text(), "download_depot 1 2 3")
        self.assertIn("copied", s.manual_step.text().lower())

    # UI-10 / UX-L3e
    def test_pulse_does_not_overwrite_live_progress_text(self):
        s = self.screen
        s._reset_and_run_state = None
        s._running = True
        s._t0 = time.time()
        s._plan_set([("prefix", "Creating Proton prefixes", 1)])
        s._phase("prefix")
        s._start_pulse("Installing prefix dependencies")
        self.assertEqual(s.bar.maximum(), 0, "busy bar while nothing reports a percentage")
        s._on_progress(s._last_pct, "Installing vcrun2005")
        s._do_pulse(); s._do_pulse()
        self.assertTrue(s.cur.text().startswith("Installing prefix dependencies"))
        self.assertEqual(s.detail.text(), "Installing vcrun2005")
        self.assertFalse(s.detail.isHidden())
        s._on_progress(s._last_pct + 10, "40% done")
        self.assertEqual(s.bar.maximum(), 100, "a real percentage ends the busy state")
        self.assertEqual(s.bar.value(), s._last_pct)
        s._stop_pulse()
        self.assertEqual(s.bar.maximum(), 100)
        self.assertTrue(s.detail.isHidden())
        s._pulse_timer.stop()

    def test_step_checklist_and_last_log_line(self):
        s = self.screen
        s._running = True
        s._t0 = time.time()
        s._plan_set([("ge", "Installing GE-Proton", 1), ("cod4", "Setting up CoD4", 1),
                     ("prefix", "Creating Proton prefixes", 1), ("finalize", "Finishing up", 1)])
        s._phase("ge"); s._phase("prefix")
        s._append_log("\u2713  Preparing prefix 7940")
        _APP.processEvents()
        text = s.steps.text()
        self.assertIn("DONE", text); self.assertIn("SKIPPED", text)
        self.assertIn("NOW", text); self.assertIn("NEXT", text)
        self.assertIn("OK  Preparing prefix 7940", text)
        self.assertTrue(s.log.isHidden(), "raw log collapsed by default")
        s.details_btn.click()
        self.assertFalse(s.log.isHidden())
        s.details_btn.click()
        self.assertTrue(s.log.isHidden())

    def test_wait_prompts_take_focus(self):
        s = self.screen
        self.stack.setCurrentWidget(s)
        self.stack.show()
        s._show_plut_wait(); _APP.processEvents()
        self.assertIs(s.focus_target(), s.plut_btn)
        self.assertIs(QApplication.focusWidget(), s.plut_btn)
        s._hide_plut_wait()
        s._show_iw5_qr("##\n##"); _APP.processEvents()
        self.assertIs(QApplication.focusWidget(), s.qr_skip_btn)
        s._hide_iw5_qr()
        self._show_dg(copied=True); _APP.processEvents()
        self.assertIs(QApplication.focusWidget(), s.dg_copy_btn)
        s._arm_iw5_dg(s._dg_token)
        self.assertIs(QApplication.focusWidget(), s.iw5_dg_btn)

    def test_skip_buttons_release_the_waits(self):
        s = self.screen
        s._show_plut_wait()
        s._plut_event.clear()
        s.plut_skip_btn.click()
        self.assertTrue(s._plut_event.is_set())
        self.assertTrue(s._plut_skip)
        self.assertFalse(s.plut_btn.isEnabled())
        self._show_dg()
        s._iw5_dg_event.clear()
        s.dg_skip_btn.click()
        self.assertTrue(s._iw5_dg_event.is_set())
        self.assertTrue(s._dg_skip)
        s._arm_iw5_dg(s._dg_token)
        self.assertFalse(s.iw5_dg_btn.isEnabled(), "Continue stays off after Skip MW3")
        s._dg_skip = False
        s._show_iw5_qr("##")
        s.qr_skip_btn.click()
        self.assertTrue(s._dg_skip)
        self.assertTrue(s.iw5_qr_box.isHidden())
        s._show_iw5_qr("##")       # a refreshed code after Skip does not come back
        self.assertTrue(s.iw5_qr_box.isHidden())

    def _finish_failed(self):
        s = self.screen
        with patch.object(ui_install.QTimer, "singleShot"):
            s._selection_entry = True
            s._reset_and_run()
        self.assertTrue(s._running)
        s._results = [{"key": "t4mp", "name": "World at War", "status": "ok", "reason": ""},
                      {"key": "iw4mp", "name": "MW2", "status": "failed", "reason": "HTTP 503"},
                      {"key": "t5mp", "name": "Black Ops", "status": "skipped",
                       "reason": "Plutonium games skipped by user"}]
        s._on_done(False)

    # UX-L3c/f/g
    def test_results_take_full_height_and_retry_is_primary(self):
        self.stack.setCurrentWidget(self.screen)
        self.stack.show()
        self._finish_failed()
        _APP.processEvents()
        s = self.screen
        self.assertTrue(s.steps_scroll.isHidden())
        self.assertFalse(s.summary_scroll.isHidden())
        self.assertGreater(s.summary_scroll.maximumHeight(), 10000, "no 132px cap")
        self.assertGreater(s.summary_scroll.height(), 132)
        self.assertIn("What to do", s.summary.text())
        self.assertIn(ui_constants.C_IW, s.retry_btn.styleSheet())
        self.assertIn(ui_constants.C_DARK_BTN, s.cont_btn.styleSheet())
        row = s._btn_row
        self.assertEqual(row.stretch(row.indexOf(s.retry_btn)), 1)
        self.assertEqual(row.stretch(row.indexOf(s.cont_btn)), 0)
        self.assertEqual(s.cont_btn.text(), ui_constants.LABELS["back_to_selection"])
        self.assertIs(s.focus_target(), s.retry_btn)
        self.assertIs(QApplication.focusWidget(), s.retry_btn)
        self.assertLess(s.cont_btn.mapTo(s, s.cont_btn.rect().bottomRight()).y(), s.height())

    def test_escape_routing_and_no_escape_while_running(self):
        s = self.screen
        with patch.object(ui_install.QTimer, "singleShot"):
            s._selection_entry = True
            s._reset_and_run()
        self.assertFalse(s.escape_allowed())
        self.assertIsNone(ui_constants._screen_back_action(s))
        s._results = [{"key": "t4mp", "name": "WaW", "status": "failed", "reason": "x"}]
        s._on_done(False)
        self.assertTrue(s.escape_allowed())
        self.stack.setCurrentWidget(s)
        s.back_action()
        self.assertIs(self.stack.currentWidget(), self.selection)
        # Success from the library returns there
        with patch.object(ui_install.QTimer, "singleShot"):
            s._return_to_management = True
            s._reset_and_run()
        s._results = [{"key": "t4mp", "name": "WaW", "status": "ok", "reason": ""}]
        s._on_done(True)
        self.assertEqual(s.cont_btn.text(), ui_constants.LABELS["back_to_library"])
        self.stack.setCurrentWidget(s)
        s.back_action()
        self.assertIs(self.stack.currentWidget(), self.library)

    def test_install_screen_text_has_no_unsupported_glyphs(self):
        self._finish_failed()
        self.screen._notes = ["\u26a0 something"]
        self.screen._render_summary()
        self.screen._show_plut_wait()
        self._show_dg(copied=False)
        for text in _texts(self.screen):
            self.assertFalse(set(text) & set(_GLYPHS), text)


# ── Worker: skip buttons end the waits as skipped, never as success ──────────

class InstallSkipWorkerTests(_Base):
    def setUp(self):
        super().setUp()
        self.cfg.first_run = False
        self.screen = self.add(ui_install.InstallScreen(self.stack))
        self.done = []
        self.screen._s.done.connect(self.done.append)
        self.mocks = {}

    def _entry(self, key):
        gd = next(g for g in ui_constants.ALL_GAMES if key in g["keys"])
        return key, gd, self.game(key)

    def _run(self, steam, overrides=None, depot=False):
        s = self.screen
        s.steam_selected = [self._entry(k) for k in steam]
        s.own_selected = {}
        s.steam_root = self.root
        s.offline_mode = None
        targets = {
            "wrapper.get_proton_path": {"return_value": "proton"},
            "wrapper.find_compatdata": {"return_value": self.root},
            "wrapper.kill_steam": {}, "wrapper.set_compat_tool": {"return_value": True},
            "wrapper.clear_launch_options": {}, "wrapper.clear_compat_tool": {},
            "wrapper.set_steam_input_enabled": {}, "wrapper.set_default_launch_option": {},
            "wrapper.clear_default_launch_option": {},
            "iw4x.install_iw4x": {}, "iw4x.is_iw4x_dlc_installed": {"return_value": True},
            "ge_proton.install_ge_proton": {"return_value": "GE-Proton10-1"},
            "ge_proton.resolve_ge_version": {"return_value": "GE-Proton10-1"},
            "ge_proton.ensure_all_prefix_deps": {"return_value": 99},
            "ge_proton.cleanup_old_ge_versions": {},
            "plutonium.launch_bootstrapper": {},
            "plutonium.is_plutonium_ready": {"return_value": False},
            "plutonium.install_plutonium": {},
            "plutonium.cleanup_plut_from_non_plut_prefixes": {"return_value": 0},
            "game_config.apply_game_configs": {"return_value": (1, 0, 0)},
            "controller_profiles.install_controller_templates": {},
            "controller_profiles.assign_controller_profiles": {"return_value": 1},
            "shortcut.create_shortcuts": {"return_value": None},
            "shortcut.create_launcher_shortcut": {},
            "shortcut.apply_steam_artwork": {},
        }
        if depot:
            targets.update({
                "depot_downgrade.is_downgrade_needed": {"return_value": True},
                "depot_downgrade.detect_dlc_status": {"return_value": {}},
                "depot_downgrade.check_free_space_gb": {"return_value": 10000.0},
                "depot_downgrade.required_space_gb": {"return_value": 1.0},
                "depot_downgrade.migrate_legacy_iw5": {"return_value": False},
                "depot_downgrade.open_steam_console": {},
                "depot_downgrade.copy_to_clipboard": {"return_value": True},
                "depot_downgrade.ensure_depotdownloader": {},
                "depot_downgrade.find_depot_stagings": {"return_value": self.root},
                "depot_downgrade.merge_depots": {"return_value": True},
                "depot_downgrade.cleanup_depotdownloader": {"return_value": 0},
            })
        targets.update(overrides or {})
        with contextlib.ExitStack() as stack:
            for target, spec in targets.items():
                m = spec if isinstance(spec, MagicMock) else MagicMock(**spec)
                stack.enter_context(patch(target, m))
                self.mocks[target] = m
            s._run()
        _APP.processEvents()

    def _row(self, key):
        return next(r for r in self.screen._results if r["key"] == key)

    def _choose(self, method):
        s = self.screen
        s._s.iw5_dg_choose.disconnect()

        def choose():
            s._iw5_method = method
            s._iw5_dg_event.set()
        s._s.iw5_dg_choose.connect(choose)

    def test_skip_plutonium_games_marks_them_skipped_and_finishes_the_rest(self):
        def bootstrap(*a, **kw):
            self.screen._skip_plut()            # the user presses Skip on the wait
        self._run(("t5mp", "iw4mp"), {"plutonium.launch_bootstrapper": {"side_effect": bootstrap}})
        self.assertEqual(self._row("t5mp")["status"], "skipped")
        self.assertIn("skipped by user", self._row("t5mp")["reason"])
        self.assertEqual(self._row("iw4mp")["status"], "ok")
        self.mocks["plutonium.install_plutonium"].assert_not_called()
        self.mocks["shortcut.create_launcher_shortcut"].assert_not_called()
        self.assertEqual(self.done, [False], "a skip is never reported as success")
        self.assertIn("t5mp", {r["key"] for r in self.screen._retryable()})
        self.assertNotIn(("t5mp", "plutonium", "steam"), self.cfg.marked)

    def test_skip_mw3_on_the_manual_depot_wait(self):
        self._choose("manual")
        self.screen._s.iw5_dg_wait.connect(lambda _cmd, _step: self.screen._skip_mw3_manual())
        self._run(("iw5mp", "iw4mp"), depot=True)
        self.assertEqual(self._row("iw5mp")["status"], "skipped")
        self.assertIn("skipped by user", self._row("iw5mp")["reason"])
        self.mocks["depot_downgrade.merge_depots"].assert_not_called()
        self.mocks["depot_downgrade.find_depot_stagings"].assert_not_called()
        self.mocks["plutonium.launch_bootstrapper"].assert_not_called()
        self.mocks["plutonium.install_plutonium"].assert_not_called()
        self.assertEqual(self._row("iw4mp")["status"], "ok")
        self.assertEqual(self.done, [False])
        self.assertTrue(self.screen.iw5_dg_btn.isHidden())

    def test_skip_mw3_on_the_qr_wait(self):
        self._choose("qr")
        screen = self.screen

        def download(**kw):
            kw["on_qr"]("##\n##")
            screen._skip_mw3_qr()               # pressed while DepotDownloader waits
            kw["on_log"]("Waiting for the QR scan...")
            return "user"
        self._run(("iw5mp",), {"depot_downgrade.run_depot_download_qr": {"side_effect": download}},
                  depot=True)
        self.assertEqual(self._row("iw5mp")["status"], "skipped")
        self.mocks["depot_downgrade.merge_depots"].assert_not_called()
        self.mocks["depot_downgrade.cleanup_depotdownloader"].assert_called_once()
        self.assertEqual(self.done, [False])
        self.assertTrue(screen.iw5_qr_box.isHidden())

    def test_qr_skip_stops_depotdownloader(self):
        """The skip is raised from a callback: run_depot_download_qr must let it out
        and kill DepotDownloader on the way (its finally)."""
        import depot_downgrade

        class _Proc:
            pid = 1
            returncode = None
            killed = False

            def __init__(self):
                self.stdout = iter(["Connecting to Steam3...\n", "more\n"])

            def poll(self): return None
            def kill(self): _Proc.killed = True
            def wait(self, timeout=None): return 0

        calls = []

        def boom(_m):
            # The first log line comes before DepotDownloader starts; the skip
            # lands while it runs (waiting for the scan)
            calls.append(_m)
            if len(calls) > 1:
                raise ui_install._DowngradeSkipped()
        with patch.object(depot_downgrade.subprocess, "Popen", return_value=_Proc()), \
             patch.object(depot_downgrade, "_watch_stall", lambda *a: None):
            with self.assertRaises(ui_install._DowngradeSkipped):
                depot_downgrade.run_depot_download_qr(
                    staging_dir=os.path.join(self.root, "staging"),
                    depot_info={"depot": 1, "manifest": "2", "app": 3},
                    on_qr=lambda q: None, on_auth_success=lambda u: None,
                    on_progress=boom, on_log=boom)
        self.assertTrue(_Proc.killed)


# ── Select Games ──────────────────────────────────────────────────────────────

class SelectGamesUXTests(_Base):
    def setUp(self):
        super().setUp()
        self.setup = self.add(ui_install.SetupScreen(self.stack))
        self.flow = self.placeholder("SetupFlowScreen")
        self.library = self.placeholder("ManagementScreen")
        self.setup.steam_installed = {k: self.game(k) for k in ("cod4mp", "cod4sp", "t4mp", "t4sp", "iw4mp")}
        self.stack.setCurrentWidget(self.setup)
        self.stack.show()
        _APP.processEvents()

    def _chain(self, start, stop, limit=80):
        out, w = [], start
        for _ in range(limit):
            if w.isVisible() and w.isEnabled() and (w.focusPolicy() & Qt.FocusPolicy.TabFocus) \
                    and self.setup.isAncestorOf(w):
                out.append(w)
            if w is stop:
                break
            w = w.nextInFocusChain()
        return out

    # UI-3
    def test_focus_order_is_select_buttons_list_then_footer(self):
        s = self.setup
        order = self._chain(s.sel_all, s.inst_btn)
        self.assertEqual(order[:2], [s.sel_all, s.sel_none])
        self.assertEqual(order[-3:], [s.back, s._folder_btn, s.inst_btn])
        middle = order[2:-3]
        visible = [w for w in s._focus_list if w.isVisible() and w.isEnabled()]
        self.assertEqual(middle, visible)
        ys = [w.mapTo(s._lw, w.rect().topLeft()).y() for w in middle]
        self.assertEqual(ys, sorted(ys), "list widgets follow the visual order")
        self.assertIsInstance(middle[0], QCheckBox)

    # UI-2 / UX-L1 / UX-L2
    def test_scroll_area_never_takes_focus_and_targets(self):
        s = self.setup
        self.assertEqual(s.scroll.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertIs(s.focus_target(), s.inst_btn)
        for b in (s.sel_all, s.sel_none, s.back, s._folder_btn, s.inst_btn):
            self.assertGreaterEqual(b.height(), ui_constants.BTN_MIN_H, b.text())
            self.assertGreaterEqual(b.font().pointSize(), ui_constants.FONT_ACTION, b.text())
        for cb, _gd, _src in s._checks.values():
            self.assertGreaterEqual(cb.font().pointSize(), ui_constants.FONT_ACTION)
        s.back_action()
        self.assertIs(self.stack.currentWidget(), self.flow)
        s._from_manage = True
        self.stack.setCurrentWidget(s)
        s.back_action()
        self.assertIs(self.stack.currentWidget(), self.library)

    # UX-S13
    def test_scroll_hint_only_when_the_list_scrolls(self):
        s = self.setup
        self.stack.resize(1280, 800); _APP.processEvents()
        bar = s.scroll.verticalScrollBar()
        self.assertEqual(s.scroll_hint.isHidden(), bar.maximum() == 0)
        self.stack.resize(800, 500); _APP.processEvents()
        self.assertGreater(bar.maximum(), 0)
        self.assertFalse(s.scroll_hint.isHidden())

    # UX-S10
    def test_dlc_tile_does_not_ask_to_tick_a_set_up_mw2(self):
        self.cfg.setup_for_source.add(("iw4mp", "steam"))
        with patch("iw4x.is_iw4x_dlc_installed", return_value=False):
            self.setup._build()
        notes = " ".join(lbl.text() for lbl in self.setup._lw.findChildren(QLabel, ))
        self.assertIn("Reinstall Game", notes)
        self.assertNotIn("tick MW2 Multiplayer", notes)
        self.cfg.setup_for_source.clear()
        with patch("iw4x.is_iw4x_dlc_installed", return_value=False):
            self.setup._build()
        self.setup._checks["iw4mp"][0].setChecked(False)
        notes = " ".join(lbl.text() for lbl in self.setup._lw.findChildren(QLabel))
        self.assertIn("tick MW2 Multiplayer", notes)

    # UX-S7
    def test_found_text_pluralizes(self):
        f = ui_install.SetupScreen._found_text
        self.assertEqual(f({"t4mp": {}}), "Found 1 game (1 mode).")
        self.assertEqual(f({"t4mp": {}, "t4sp": {}, "cod4mp": {}}), "Found 2 games (3 modes).")
        self.assertEqual(f({"iw5mp": {}, "iw5mp_ds": {}}), "Found 1 game (1 mode).")

    def test_review_dialog_has_safety_notes_and_mw3_notice_only_for_mw3(self):
        gd_waw = next(g for g in ui_constants.ALL_GAMES if "t4mp" in g["keys"])
        gd_mw3 = next(g for g in ui_constants.ALL_GAMES if "iw5mp" in g["keys"])

        def text_for(selected):
            with patch.object(ui_install, "QMessageBox") as dialog:
                box = dialog.return_value
                box.addButton.side_effect = [object(), object()]
                self.setup._review_install(selected, {})
                return box.setText.call_args.args[0]
        plain = text_for([("t4mp", gd_waw, {})])
        self.assertIn("Before you continue", plain)
        self.assertIn("plug it in", plain)
        self.assertNotIn("MW3", plain)
        self.assertIn("MW3", text_for([("iw5mp", gd_mw3, {})]))

    def test_select_games_text_has_no_unsupported_glyphs(self):
        self.cfg.setup_for_source.add(("t4mp", "steam"))
        self.setup._build()
        for cb, _gd, _src in self.setup._checks.values():
            self.assertFalse(set(cb.toolTip()) & set(_GLYPHS), cb.toolTip())
        for text in _texts(self.setup):
            self.assertFalse(set(text) & set(_GLYPHS), text)


# ── First-run flow ────────────────────────────────────────────────────────────

class FirstRunFlowTests(_Base):
    def setUp(self):
        super().setUp()
        self.wizard = self.add(ui_setup.SetupFlowScreen(self.stack))
        self.setup = self.add(ui_install.SetupScreen(self.stack))
        self.library = self.placeholder("ManagementScreen")
        self.stack.setCurrentWidget(self.wizard)
        self.stack.show()
        _APP.processEvents()

    def test_welcome_results_screen_is_gone(self):
        self.assertFalse(hasattr(ui_install, "WelcomeScreen"))
        self.assertNotIn("WelcomeScreen", inspect.getsource(ui_qt.DeckOpsWindow.__init__))

    def test_device_setup_scans_straight_into_select_games(self):
        found = {"t4mp": self.game("t4mp")}
        with patch.object(ui_install, "find_steam_root", return_value=self.root), \
             patch.object(ui_install, "find_all_games", return_value=found), \
             patch.object(ui_install.threading, "Thread") as thread:
            self.wizard._finish()
            self.assertIs(self.stack.currentWidget(), self.setup)
            thread.assert_called_once()
            self.assertTrue(self.setup._scanning)
            self.assertFalse(self.setup.scan_box.isHidden())
            self.assertTrue(self.setup.scroll.isHidden())
            self.assertFalse(self.setup.inst_btn.isEnabled())
            thread.call_args.kwargs["target"]()     # the scan, run here
        _APP.processEvents()
        self.assertFalse(self.setup._scanning)
        self.assertTrue(self.setup.scan_box.isHidden())
        self.assertTrue(self.setup._checks["t4mp"][0].isChecked())
        self.assertIn("Found 1 game", self.setup.intro.text())
        # Back from Select Games returns to device setup on first run
        self.setup.back_action()
        self.assertIs(self.stack.currentWidget(), self.wizard)

    def test_scan_error_and_no_steam_stay_on_select_games(self):
        s = self.setup
        with patch.object(ui_install.threading, "Thread"):
            s._start_scan()
        s.steam_root = self.root; s._scan_error = True
        s._on_scanned(True)
        self.assertIn("couldn't finish", s.scan_status.text())
        self.assertFalse(s.rescan_btn.isHidden())
        self.assertTrue(s._folder_btn.isEnabled())
        with patch.object(ui_install.threading, "Thread"):
            s._start_scan()
        s.steam_root = ""; s._scan_error = False
        s._on_scanned(False)
        self.assertIn("Steam not found", s.scan_status.text())
        self.assertFalse(s._folder_btn.isEnabled())
        self.assertFalse(s.inst_btn.isEnabled())

    def test_not_first_run_finish_goes_to_the_library(self):
        self.cfg.first_run = False
        self.wizard._finish()
        self.assertIs(self.stack.currentWidget(), self.library)

    def test_compact_header_after_the_first_step(self):
        def has_title(section):
            return any(lbl.text() == "DECKOPS" for lbl in getattr(self.wizard, section).findChildren(QLabel))
        self.assertTrue(has_title("_os_section"))
        self.assertTrue(has_title("_confirm_section"))
        for name in ("_model_section", "_device_section", "_name_section",
                     "_play_section", "_resolution_section"):
            self.assertFalse(has_title(name), name)
        # The device picker's first row is visible without scrolling at 800x500
        self.wizard._show_device_picker(); _APP.processEvents()
        area = self.wizard._scrolls["_device_section"]
        first = self.wizard._primary["_device_section"]
        self.assertLess(first.mapTo(area.viewport(), first.rect().bottomRight()).y(),
                        area.viewport().height())

    def test_each_step_focuses_its_first_choice_not_back(self):
        w = self.wizard
        self.assertEqual(w._footer_back.text(), "Back")
        self.assertGreaterEqual(w._footer_back.height(), ui_constants.BTN_MIN_H)
        for step in (lambda: w._pick_os("steamos"), w._show_device_picker,
                     lambda: w._pick_device("sd_oled"), w._save_player_name):
            step(); _APP.processEvents(); _APP.processEvents()
            section = w._current_section
            self.assertIs(w.focus_target(), w._primary[section])
            self.assertIs(QApplication.focusWidget(), w._primary[section], section)
            self.assertIsNot(QApplication.focusWidget(), w._footer_back)
        self.assertEqual(w._primary["_name_section"].text(), "Continue")
        # Escape / B is the footer Back
        w.back_action()
        self.assertEqual(w._current_section, "_name_section")
        self.assertEqual(w._scrolls["_name_section"].focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_setup_text_has_no_unsupported_glyphs(self):
        for text in _texts(self.wizard):
            self.assertFalse(set(text) & set(_GLYPHS), text)


if __name__ == "__main__":
    unittest.main()
