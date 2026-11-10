"""Regression tests for the sixth sweep: helper modules (save_backup,
detect_games, detect_hw, setup_check, guide_text, Repair Shortcuts and
controller profiles finding own copies)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import controller_profiles as profiles  # noqa: E402
import detect_games  # noqa: E402
import detect_hw  # noqa: E402
import guide_text  # noqa: E402
import save_backup as sb  # noqa: E402
import setup_check  # noqa: E402
import shortcut  # noqa: E402

_POSIX_ONLY = unittest.skipUnless(os.name == "posix", "needs symlinks")


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _read(path):
    with open(path) as f:
        return f.read()


def _black_ops_folder(parent):
    """An own BO1 copy detect_games recognises (folder name + sentinel + exes)."""
    game = os.path.join(parent, "Call of Duty Black Ops")
    _write(os.path.join(game, "zone", "English", "mp_nuked.ff"))
    _write(os.path.join(game, "BlackOps.exe"))
    _write(os.path.join(game, "BlackOpsMP.exe"))
    return game


# ── HELP-1: own copies picked with Choose Folder ─────────────────────────────

class SavedExtraPathsTests(unittest.TestCase):
    """Backup/restore, Repair Shortcuts and controller profiles scanned for
    own copies without the folders the player picked with Choose Folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.picked = os.path.join(self.tmp.name, "MyGames")
        self.game = _black_ops_folder(self.picked)
        for p in (patch.object(detect_games, "OWN_SCAN_PATHS", []),
                  patch.object(config, "get_extra_scan_paths", return_value=[self.picked])):
            p.start()
            self.addCleanup(p.stop)
        sb._forget_detected_games()
        self.addCleanup(sb._forget_detected_games)

    def test_find_own_installed_saved_scans_the_picked_folders(self):
        found = detect_games.find_own_installed_saved()
        self.assertEqual(found["t5sp"]["install_dir"], self.game)
        # find_own_installed itself keeps its explicit-paths contract
        self.assertEqual(detect_games.find_own_installed(), {})

    def test_save_backup_detection_finds_the_picked_copy(self):
        with patch.object(sb, "STEAM_ROOT_DEFAULT", return_value=os.path.join(self.tmp.name, "nosteam")), \
             patch("detect_games.find_steam_root", return_value=None):
            own = sb._detected_games()[1]
        self.assertIn("t5sp", own)
        self.assertEqual(own["t5sp"]["install_dir"], self.game)

    def test_repair_shortcuts_scans_the_picked_folders(self):
        setup = {"t6sp": {"source": "own", "client": "t6sp_mod"}}
        with patch("config.get_setup_games", return_value=setup), \
             patch.object(shortcut, "expected_shortcuts",
                          return_value=({"iw4mp": ("own", "Own MW2")}, False)), \
             patch.object(shortcut, "_find_all_steam_uids", return_value=["1"]), \
             patch.object(shortcut, "remove_merged_mp_shortcuts"), \
             patch("plutonium.refresh_steam_menus"), \
             patch("iw4x.refresh_steam_menu"), \
             patch("sp_mod.repair_sp_launch_options", return_value=[]), \
             patch.object(shortcut, "migrate_own_t6sp", return_value=False), \
             patch.object(shortcut, "present_shortcut_names", return_value=set()), \
             patch.object(shortcut, "_remap_shortcut_ge", return_value=[]), \
             patch("config.get_gyro_mode", return_value="on"), \
             patch.object(detect_games, "find_own_installed", return_value={}) as scan:
            shortcut.repair_shortcuts(steam_root=self.tmp.name, on_progress=lambda m: None)
        self.assertGreaterEqual(scan.call_count, 2)       # t6sp migration + missing own shortcut
        for call in scan.call_args_list:
            self.assertEqual(call.kwargs.get("extra_paths"), [self.picked])

    def test_controller_profiles_scan_the_picked_folders(self):
        with patch.object(profiles, "_is_steam_deck", return_value=True), \
             patch.object(profiles, "find_all_steam_uids", return_value=["1"]), \
             patch.object(profiles, "_write_layout_safe", return_value=True), \
             patch.object(profiles, "_shortcut_appids", return_value={}), \
             patch("shortcut.get_shortcut_appid", return_value=None), \
             patch("config.get_setup_games", return_value={"t5sp": {"source": "own"}}), \
             patch.object(detect_games, "find_own_installed", return_value={}) as scan:
            profiles.assign_controller_profiles()
        scan.assert_called_once()
        self.assertEqual(scan.call_args.kwargs.get("extra_paths"), [self.picked])


class MissingOwnCopyBackupTests(unittest.TestCase):
    """An own copy that is set up but not found (drive out) is a failed
    backup, not "no save data found"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.backup = os.path.join(self.root, "backup")
        for p in (patch.object(sb, "BACKUP_ROOT", self.backup),
                  patch.object(sb, "HEROIC_PREFIX", os.path.join(self.root, "noheroic")),
                  patch.object(sb, "_detected_games", return_value=({}, {}))):
            p.start()
            self.addCleanup(p.stop)

    def _backup(self, setup):
        failures, msgs = [], []
        with patch.object(sb, "_load_config", return_value={"setup_games": setup}):
            done = sb.backup_saves(steam_root=self.root, on_progress=msgs.append, failures=failures)
        return done, failures, msgs

    def test_plutonium_group_with_missing_own_copy_fails(self):
        done, failures, msgs = self._backup({"t5sp": {"source": "own", "client": "plutonium"}})
        self.assertEqual(done, {})
        self.assertIn("t5", failures)
        self.assertTrue(any("Choose Folder" in m for m in msgs))
        self.assertTrue(any("incomplete" in m for m in msgs))

    def test_install_dir_group_with_missing_own_copy_fails(self):
        done, failures, _ = self._backup({"iw4mp": {"source": "own", "client": "iw4x"}})
        self.assertIn("mw2", failures)

    def test_existing_backup_is_kept_when_own_copy_is_missing(self):
        old = os.path.join(self.backup, "t5", "own_t5sp", "players", "stats.dat")
        _write(old, "OLD")
        self._backup({"t5sp": {"source": "own", "client": "plutonium"}})
        self.assertEqual(_read(old), "OLD")

    def test_steam_group_not_found_is_still_nothing_to_back_up(self):
        done, failures, _ = self._backup({"iw4mp": {"source": "steam", "client": "iw4x"}})
        self.assertEqual((done, failures), ({}, []))

    def test_own_copy_passed_by_the_install_flow_is_not_missing(self):
        game = os.path.join(self.root, "mw2")
        _write(os.path.join(game, "players", "config.cfg"), "x")
        failures = []
        with patch.object(sb, "_load_config",
                          return_value={"setup_games": {"iw4mp": {"source": "own"}}}):
            done = sb.backup_saves(steam_root=self.root, failures=failures,
                                   installed_games={"iw4mp": {"install_dir": game}})
        self.assertIn("mw2", done)
        self.assertEqual(failures, [])


# ── HELP-2: library indexes of Plutonium prefixes ────────────────────────────

class CompatdataLibraryIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.group = sb.SAVE_GROUPS["iw5"]

    def _storage(self, lib):
        return sb._plut_storage(os.path.join(lib, "steamapps", "compatdata", "42690"), "iw5")

    def test_same_library_listed_twice_counts_once(self):
        steam = os.path.join(self.root, "Steam")
        os.makedirs(os.path.join(steam, "steamapps", "compatdata"))
        # libraryfolders.vdf lists the root again under a different spelling
        same = steam.replace("\\", "/") + "/"
        _write(os.path.join(steam, "steamapps", "libraryfolders.vdf"),
               '"libraryfolders"\n{\n "0" { "path" "%s" }\n}\n' % same)
        self.assertEqual(len(sb._find_all_compatdata_dirs(steam)), 1)

    def test_missing_library_index_is_skipped_not_main_prefix(self):
        steam = os.path.join(self.root, "Steam")
        main = self._storage(steam)
        _write(os.path.join(main, "players", "stats.dat"), "CURRENT")
        self.assertIsNone(sb._plut_restore_dst(steam, self.group, "42690@1"))
        self.assertEqual(sb._plut_restore_dst(steam, self.group, "42690"), main)

        backup = os.path.join(self.root, "backup")
        _write(os.path.join(backup, "iw5", "42690@1", "players", "stats.dat"), "SDCARD")
        _write(os.path.join(backup, "backup_info.json"), json.dumps({"groups": ["iw5"]}))
        msgs = []
        with patch.object(sb, "BACKUP_ROOT", backup), \
             patch.object(sb, "HEROIC_PREFIX", os.path.join(self.root, "noheroic")), \
             patch.object(sb, "_load_config", return_value={"setup_games": {}}), \
             patch.object(sb, "_detected_games", return_value=({}, {})):
            restored = sb.restore_saves(steam_root=steam, on_progress=msgs.append)
        self.assertEqual(restored, {})
        self.assertEqual(_read(os.path.join(main, "players", "stats.dat")), "CURRENT")
        self.assertTrue(any("42690@1" in m and "skipped" in m for m in msgs))

    @_POSIX_ONLY
    def test_symlinked_steam_root_restores_each_library_to_itself(self):
        real = os.path.join(self.root, "debian-installation")
        alias = os.path.join(self.root, "steam")
        lib2 = os.path.join(self.root, "games")
        os.makedirs(os.path.join(real, "steamapps"))
        os.symlink(real, alias)
        _write(os.path.join(real, "steamapps", "libraryfolders.vdf"),
               '"libraryfolders"\n{\n "0" { "path" "%s" }\n "1" { "path" "%s" }\n}\n' % (real, lib2))
        for lib, stats in ((real, "INTERNAL"), (lib2, "LIB2")):
            _write(os.path.join(self._storage(lib), "players", "stats.dat"), stats)
        self.assertEqual(len(sb._find_all_compatdata_dirs(alias)), 2)
        backup = os.path.join(self.root, "backup")
        with patch.object(sb, "BACKUP_ROOT", backup), \
             patch.object(sb, "HEROIC_PREFIX", os.path.join(self.root, "noheroic")), \
             patch.object(sb, "_load_config", return_value={"setup_games": {"iw5mp": {"source": "steam"}}}), \
             patch.object(sb, "_detected_games", return_value=({}, {})):
            self.assertIn("iw5", sb.backup_saves(steam_root=alias, failures=[]))
            for lib in (real, lib2):
                _write(os.path.join(self._storage(lib), "players", "stats.dat"), "CURRENT")
            sb.restore_saves(steam_root=alias)
        self.assertEqual(_read(os.path.join(self._storage(real), "players", "stats.dat")), "INTERNAL")
        self.assertEqual(_read(os.path.join(self._storage(lib2), "players", "stats.dat")), "LIB2")


# ── HELP-3: DMI strings ──────────────────────────────────────────────────────

class DetectDeviceTests(unittest.TestCase):
    CASES = [
        ("ROG Ally RC71L_RC71L", "", "rog_ally"),
        ("RC71L", "", "rog_ally"),
        ("ROG Ally X RC72LA_RC72LA_000123206", "", "rog_ally_x"),
        ("ROG Xbox Ally RC73YA_RC73YA", "", "xbox_ally_x"),
        ("ROG Xbox Ally X RC73XA_RC73XA", "", "xbox_ally_x"),
        ("83L3", "", "legion_go_s"),
        ("83N6", "", "legion_go_s"),
        ("83Q2", "", "legion_go_s"),
        ("83Q3", "", "legion_go_s"),
        ("83E1", "", "legion_go"),
        ("Jupiter", "", "sd_lcd"),
        ("Claw A8", "MS-1T8K", "msi_claw_8"),
        ("Claw 8 AI+ A2VM", "MS-1T52", "msi_claw_8"),
        ("Claw A1M", "MS-1T41", "other_handheld"),
        ("Unknown", "", None),
    ]

    def _detect(self, product, board):
        vals = {"product_name": product, "board_name": board}
        with patch.object(detect_hw, "_read_dmi", side_effect=lambda f: vals.get(f, "")), \
             patch("builtins.open", side_effect=OSError):
            return detect_hw.detect_device()

    def test_real_dmi_strings(self):
        for product, board, want in self.CASES:
            with self.subTest(product=product, board=board):
                self.assertEqual(self._detect(product, board), want)

    def test_every_detected_key_is_a_setup_device(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            import ui_setup
        except Exception as ex:          # PyQt missing
            self.skipTest(f"ui_setup not importable: {ex}")
        keys = (set(detect_hw._DMI_EXACT.values()) | {k for _, k in detect_hw._DMI_CODES}
                | set(detect_hw._DMI_BOARD.values()) | set(detect_hw._DT_MODEL.values()))
        self.assertEqual(keys - set(ui_setup.DEVICES), set())


# ── HELP-4: setup check uses the copy that was set up ────────────────────────

class SetupCheckSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = os.path.join(self.tmp.name, "Call of Duty Black Ops")
        os.makedirs(self.folder)

    def _check(self, entry, game):
        return setup_check.check_game("t5sp", entry, game, self.tmp.name, {"t5sp": entry})

    def test_own_setup_with_only_the_steam_copy_found(self):
        r = self._check({"client": "plutonium", "source": "own"},
                        {"install_dir": self.folder, "source": "steam"})
        self.assertEqual(r["problems"], ["Game folder not found. Insert the drive or use Choose "
                                         "Folder to point DeckOps at your game folder."])
        self.assertFalse(r["fixable"])

    def test_steam_setup_with_only_an_own_copy_found(self):
        r = self._check({"client": "plutonium", "source": "steam"},
                        {"install_dir": self.folder, "source": "own"})
        self.assertEqual(len(r["problems"]), 1)
        self.assertIn("Install the game again in Steam", r["problems"][0])
        self.assertFalse(r["fixable"])

    def test_missing_own_folder_does_not_say_steam(self):
        r = self._check({"client": "plutonium", "source": "own"}, None)
        self.assertNotIn("Steam", r["problems"][0])
        self.assertIn("Choose Folder", r["problems"][0])
        self.assertFalse(r["fixable"])

    def test_matching_own_copy_is_checked_normally(self):
        r = self._check({"client": "plutonium", "source": "own"},
                        {"install_dir": self.folder, "source": "own"})
        self.assertEqual(r["problems"], ["DeckOps's plutonium files are missing from the game folder."])
        self.assertTrue(r["fixable"])


# ── HELP-5: Setup Complete guide text ────────────────────────────────────────

class GuideTextTests(unittest.TestCase):
    P = {"client": "plutonium"}

    def test_own_mw1_has_no_steam_play_line(self):
        text = guide_text.how_to_start_text({"cod4sp": {"client": "iw3sp", "source": "own"}}, False)
        self.assertNotIn("MW1:", text)
        self.assertIn("non-Steam", text)

    def test_lcd_bo2_modes_are_on_their_own_tiles(self):
        text = guide_text.how_to_start_text({"t6sp": {"client": "t6sp_mod"}, "t6mp": self.P,
                                             "t6zm": self.P}, True)
        self.assertNotIn("pick Campaign, Zombies or Multiplayer", text)
        self.assertEqual(text, "BO2: Play starts the campaign (T6SP-Mod). Multiplayer and Zombies "
                               "are on their own Steam tiles; press Play there.")

    def test_lcd_bo1_multiplayer_tile(self):
        text = guide_text.how_to_start_text({"t5sp": self.P, "t5mp": self.P}, True)
        self.assertEqual(text, "BO1: press Play and pick Campaign & Zombies from the menu. "
                               "Multiplayer is on its own Steam tile; press Play there.")

    def test_oled_bo2_with_mp_uses_the_host_menu(self):
        text = guide_text.how_to_start_text({"t6sp": {"client": "t6sp_mod"}, "t6mp": self.P}, False)
        self.assertEqual(text, "BO2: press Play and pick Campaign, Zombies or Multiplayer from the menu.")

    def test_t6sp_only_starts_the_campaign(self):
        for lcd in (False, True):
            with self.subTest(lcd=lcd):
                text = guide_text.how_to_start_text({"t6sp": {"client": "t6sp_mod"}}, lcd)
                self.assertEqual(text, "BO2: Play starts the campaign (T6SP-Mod).")

    def test_bo1_campaign_only_on_oled(self):
        text = guide_text.how_to_start_text({"t5sp": self.P}, False)
        self.assertEqual(text, "BO1: press Play and pick Campaign & Zombies from the menu.")

    def test_dedicated_server_only(self):
        text = guide_text.how_to_start_text({"iw5mp_ds": self.P}, False)
        self.assertIn("MW3 Dedicated Server", text)
        self.assertNotIn("MW3: Play starts the campaign", text)

    def test_no_unsupported_glyphs(self):
        setups = [
            {"cod4sp": {"client": "iw3sp"}, "cod4mp": {"client": "cod4r"}, "iw4mp": {"client": "iw4x"},
             "iw5mp": self.P, "iw5mp_ds": self.P, "t4sp": self.P, "t4mp": self.P,
             "t5sp": self.P, "t5mp": self.P, "t6sp": {"client": "t6sp_mod"}, "t6mp": self.P,
             "t6zm": self.P},
            {"cod4mp": {"client": "cod4x"}, "t6zm": {**self.P, "source": "own"}},
        ]
        for setup in setups:
            for lcd in (False, True):
                text = guide_text.how_to_start_text(setup, lcd)
                for glyph in ("\u2713", "\u2717", "\u26a0", "\u2190", "\u23f3"):
                    self.assertNotIn(glyph, text)


if __name__ == "__main__":
    unittest.main()
