"""Third sweep: install pipeline (ui_install) bookkeeping regressions.

Each test drives the real InstallScreen._run() in-process with every installer,
Steam call and config write replaced, so nothing touches a real Steam, game
folder or ~/DeckOps-Nightly.
"""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qt import QApplication, QStackedWidget
    import ui_install
    import ui_constants
except ImportError:
    QApplication = None

_APP = None


class _Cfg:
    """The slice of config that the install pipeline reads or writes."""

    def __init__(self):
        self.marked = []
        self.first_run = False
        self.finish_error = None

    def is_lcd(self):
        return False

    def is_first_run(self):
        return self.first_run

    def complete_first_run(self, root):
        if self.finish_error:
            raise self.finish_error

    def is_game_setup_for_source(self, key, source):
        return False

    def get_offline_mode(self):
        return False

    def set_offline_mode(self, on):
        pass

    def set_ge_proton_version(self, version):
        pass

    def mark_game_setup(self, key, client, source="steam"):
        self.marked.append((key, client, source))

    def get_setup_games(self):
        return {}

    def get_gyro_mode(self):
        return "on"

    def get_deck_model(self):
        return "oled"


@unittest.skipIf(QApplication is None, "PyQt is not installed")
class InstallPipelineBookkeepingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        _APP = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cfg = _Cfg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for target in (ui_install, ui_constants):
            p = patch.object(target, "cfg", self.cfg)
            p.start(); self.addCleanup(p.stop)
        for name, value in (("_log_to_file", MagicMock()), ("inhibit", MagicMock())):
            p = patch.object(ui_install, name, value)
            p.start(); self.addCleanup(p.stop)
        self.stack = QStackedWidget()
        self.screen = ui_install.InstallScreen(self.stack)
        self.stack.addWidget(self.screen)
        self.addCleanup(self.stack.close)
        self.done = []
        self.logs = []
        self.screen._s.done.connect(self.done.append)
        self.screen._s.log.connect(self.logs.append)
        self.mocks = {}

    # ── helpers ───────────────────────────────────────────────────────────
    def _entry(self, key):
        gd = next(g for g in ui_constants.ALL_GAMES if key in g["keys"])
        game_dir = Path(self.root, "games", key)
        game_dir.mkdir(parents=True, exist_ok=True)
        return key, gd, {"install_dir": str(game_dir), "source": "steam", "name": key}

    def _select(self, steam=(), own=()):
        self.screen.steam_selected = [self._entry(k) for k in steam]
        self.screen.own_selected = {k: self._entry(k)[2] for k in own}
        self.screen.steam_root = self.root
        self.screen.offline_mode = None

    def _run(self, overrides=None, depot=False):
        """Run the worker with everything external mocked. overrides maps a
        patch target to either a MagicMock kwargs dict or a ready mock."""
        targets = {
            "wrapper.get_proton_path": {"return_value": "proton"},
            "wrapper.find_compatdata": {"return_value": self.root},
            "wrapper.kill_steam": {},
            "wrapper.set_compat_tool": {"return_value": True},
            "wrapper.clear_launch_options": {},
            "wrapper.clear_compat_tool": {},
            "wrapper.set_steam_input_enabled": {},
            "wrapper.set_default_launch_option": {},
            "wrapper.clear_default_launch_option": {},
            "cod4r.install_cod4r": {},
            "iw3sp.install_iw3sp": {},
            "iw3sp.apply_cod4_launch_option": {"return_value": False},
            "iw4x.install_iw4x": {},
            "t6sp_mod.install_t6sp_mod": {},
            "sp_mod.install_sp_mod": {},
            "ge_proton.install_ge_proton": {"return_value": "GE-Proton10-1"},
            "ge_proton.resolve_ge_version": {"return_value": "GE-Proton10-1"},
            "ge_proton.ensure_all_prefix_deps": {"return_value": 99},
            "ge_proton.cleanup_old_ge_versions": {},
            "plutonium.launch_bootstrapper": {},
            "plutonium.is_plutonium_ready": {"return_value": False},
            "plutonium.install_plutonium": {},
            "plutonium.cleanup_plut_from_non_plut_prefixes": {"return_value": 0},
            "game_config.apply_game_configs": {"return_value": (0, 0, 0)},
            "controller_profiles.install_controller_templates": {},
            "controller_profiles.assign_controller_profiles": {},
            "shortcut.create_shortcuts": {},
            "shortcut.create_launcher_shortcut": {},
            "shortcut.apply_steam_artwork": {},
            "shortcut.enrich_own_games": {"side_effect": lambda own_games, **kw: own_games},
            "shortcut.write_own_shortcuts": {},
        }
        if depot:
            targets.update({
                "depot_downgrade.is_downgrade_needed": {"return_value": True},
                "depot_downgrade.detect_dlc_status": {"return_value": {}},
                "depot_downgrade.check_free_space_gb": {"return_value": 10000.0},
                "depot_downgrade.required_space_gb": {"return_value": 1.0},
                "depot_downgrade.migrate_legacy_iw5": {"return_value": False},
            })
        for target, value in (overrides or {}).items():
            targets[target] = value
        with contextlib.ExitStack() as stack:
            for target, spec in targets.items():
                m = spec if isinstance(spec, MagicMock) else MagicMock(**spec)
                stack.enter_context(patch(target, m))
                self.mocks[target] = m
            self.screen._run()
        _APP.processEvents()

    def _status(self, key):
        return next(r["status"] for r in self.screen._results if r["key"] == key)

    # ── tests ─────────────────────────────────────────────────────────────
    def test_compat_tool_failure_is_not_overwritten_by_later_client_success(self):
        """set_compat_tool failing used to flip only games that were already ok
        (CoD4); IW4x/Plutonium/T6SP-MOD/IW-Pad then marked themselves ok."""
        self._select(steam=("iw4mp", "iw4sp"))
        self._run({"wrapper.set_compat_tool": {"return_value": False}})
        self.assertEqual(self._status("iw4mp"), "failed")
        self.assertEqual(self._status("iw4sp"), "failed")
        self.assertEqual(self.done, [False])
        self.assertFalse(self.screen.retry_btn.isHidden())

    def test_successful_compat_tool_still_reports_ready(self):
        self._select(steam=("iw4mp",))
        self._run()
        self.assertEqual(self._status("iw4mp"), "ok")
        self.assertEqual(self.done, [True])

    def test_steam_still_running_leaves_early_games_retryable(self):
        """CoD4 installs before Steam is closed. If Steam then can't be closed
        the game was reported ready with no Retry button, and Select Games
        greys it out as already set up: a dead end."""
        self._select(steam=("cod4mp",))
        self._run({"wrapper.kill_steam": {"side_effect": TimeoutError("still running")}})
        self.mocks["cod4r.install_cod4r"].assert_called_once()
        self.assertEqual(self._status("cod4mp"), "failed")
        self.assertEqual(self.done, [False])
        self.assertFalse(self.screen.retry_btn.isHidden())
        self.assertEqual({r["key"] for r in self.screen._retryable()}, {"cod4mp"})
        self.mocks["wrapper.set_compat_tool"].assert_not_called()

    def test_unexpected_crash_leaves_finished_games_retryable(self):
        # Everything installed, then recording the finished first run fails.
        self.cfg.first_run = True
        self.cfg.finish_error = OSError("config not writable")
        self._select(steam=("iw4mp",))
        self._run()
        self.assertEqual(self.done, [False])
        self.assertEqual(self._status("iw4mp"), "failed")
        self.assertFalse(self.screen.retry_btn.isHidden())

    def test_failed_own_mod_client_gets_no_shortcut(self):
        self._select(own=("iw4mp", "cod4sp"))
        self._run({"iw4x.install_iw4x": {"side_effect": RuntimeError("download failed")}})
        self.assertEqual(self._status("iw4mp"), "failed")
        self.assertEqual(self._status("cod4sp"), "ok")
        keys = self.mocks["shortcut.write_own_shortcuts"].call_args.kwargs["selected_keys"]
        self.assertEqual(keys, ["cod4sp"])

    def test_failed_iw_pad_keeps_its_shortcut(self):
        """IW-Pad failing leaves a launchable vanilla game; that stays shortcut-able."""
        self._select(own=("iw4sp",))
        self._run({"sp_mod.install_sp_mod": {"side_effect": RuntimeError("no network")}})
        self.assertEqual(self._status("iw4sp"), "failed")
        keys = self.mocks["shortcut.write_own_shortcuts"].call_args.kwargs["selected_keys"]
        self.assertEqual(keys, ["iw4sp"])

    def test_cancelled_downgrade_does_not_ask_for_a_plutonium_login(self):
        """MW3 MP only, downgrade cancelled: nothing Plutonium is left to install,
        yet the user was still made to launch Plutonium, log in and confirm."""
        self._select(steam=("iw5mp",))
        with patch.object(self.screen, "_run_batch_depot_downgrade",
                          return_value={"iw5": "downgrade cancelled"}) as dg:
            # Pre-confirmed so a regression fails the asserts instead of hanging.
            self.screen._plut_event.set()
            self._run(depot=True)
        dg.assert_called_once()
        self.mocks["plutonium.launch_bootstrapper"].assert_not_called()
        self.mocks["plutonium.install_plutonium"].assert_not_called()
        self.assertEqual(self._status("iw5mp"), "failed")
        self.assertEqual(self.done, [False])

    def test_other_plutonium_games_still_get_the_login_when_one_downgrade_fails(self):
        """The login is only skipped when every Plutonium game is already failed."""
        self._select(steam=("iw5mp", "t5mp"))
        self.screen.steam_selected = [e for e in self.screen.steam_selected]
        event_set = []

        def bootstrap(*a, **kw):
            # Confirm the login window on behalf of the user.
            self.screen._plut_event.set()
            event_set.append(True)
        with patch.object(self.screen, "_run_batch_depot_downgrade",
                          return_value={"iw5": "downgrade cancelled"}):
            self._run({"plutonium.launch_bootstrapper": {"side_effect": bootstrap},
                       "plutonium.is_plutonium_ready": {"side_effect": [False, True]}},
                      depot=True)
        self.assertEqual(event_set, [True])
        self.mocks["plutonium.install_plutonium"].assert_called_once()
        self.assertEqual(self.mocks["plutonium.install_plutonium"].call_args.args[1], "t5mp")

    def test_finishing_a_run_hides_a_leftover_qr_code(self):
        """A QR download that raised never emitted iw5_qr_hide, which left the
        install log collapsed to zero height for the rest of the session."""
        self.screen._show_iw5_qr("##\n##")
        self.assertEqual(self.screen.log.maximumHeight(), 0)
        self.screen._on_done(False)
        self.assertTrue(self.screen.iw5_qr_box.isHidden())
        self.assertGreater(self.screen.log.maximumHeight(), 0)


if __name__ == "__main__":
    unittest.main()
