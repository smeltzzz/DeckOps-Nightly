"""Focused regression tests for the uninstaller's self-contained Python passes.

The shell script embeds these programs because src/ can disappear during uninstall.
Run the actual embedded code against temporary Steam folders, not copies of it.
"""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deckops_uninstall.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def block(after):
    return TEXT.split(after, 1)[1].split("PYEOF", 1)[0]


HELPER = block("cat > \"$DECKOPS_PY_HELPERS/deckops_vdf.py\" <<'PYEOF'\n")
SHORTCUTS = block("remove_steam_shortcuts() {\n    python3 - << 'PYEOF'\n")
LEDGER = TEXT.split("# The VDF edit ledger records", 1)[1].split("python3 - << 'PYEOF'\n", 1)[1].split("PYEOF", 1)[0]
REPOINT = TEXT.split("REPOINT_PY='\n", 1)[1].split("\n'\n\nrepoint_to_other()", 1)[0]
LCD_REMOVE = TEXT.split('info "Removing DeckOps LCD shortcuts from Steam..."', 1)[1].split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
DOWNGRADE_RECEIPT = block('if ! python3 - "$game_dir/downgrade/deckops_depot.json" 2>/dev/null <<\'PYEOF\'\n')


def entry(index, name, exe, tag=False):
    raw = b"\x00" + str(index).encode() + b"\x00"
    for key, value in ((b"AppName", name), (b"exe", exe)):
        raw += b"\x01" + key + b"\x00" + value.encode() + b"\x00"
    # An integer containing 0x08 must never be mistaken for an end marker.
    raw += b"\x02appid\x00\x08\x00\x00\x00"
    if tag:
        raw += b"\x00tags\x00\x010\x00DeckOps\x00\x08"
    return raw + b"\x08"


def shortcuts(*entries):
    return b"\x00shortcuts\x00" + b"".join(entries) + b"\x08\x08"


class UninstallSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / "custom-steam"
        self.config = self.root / "userdata" / "12345" / "config"
        self.config.mkdir(parents=True)
        self.helper = self.home / "deckops_vdf.py"
        self.helper.write_text(HELPER, encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        DECKOPS_STEAM_ROOT=str(self.root),
                        PYTHONPATH=str(self.home))

    def run_code(self, code, *args, ok=True):
        result = subprocess.run([sys.executable, "-c", code, *args],
                                env=self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_only_owned_mw3_downgrade_folder_is_offered_for_deletion(self):
        receipt = self.home / "deckops_depot.json"
        self.run_code(DOWNGRADE_RECEIPT, str(receipt), ok=False)
        receipt.write_text(json.dumps({"game_id": "bo2", "files": {"file": 1}}))
        self.run_code(DOWNGRADE_RECEIPT, str(receipt), ok=False)
        receipt.write_text(json.dumps({"game_id": "iw5", "files": {}}))
        self.run_code(DOWNGRADE_RECEIPT, str(receipt), ok=False)
        receipt.write_text(json.dumps({"game_id": "iw5", "files": {"file": 1}}))
        self.run_code(DOWNGRADE_RECEIPT, str(receipt))

    def test_shortcuts_keep_user_entry_and_reject_malformed_file(self):
        path = self.config / "shortcuts.vdf"
        ours = entry(0, "DeckOps Nightly", '"/DeckOps-Nightly/src/main.py"', True)
        user = entry(1, "DeckOps Nightly", '"/games/my-game"')
        path.write_bytes(shortcuts(ours, user))
        self.run_code(SHORTCUTS)
        self.assertEqual(path.read_bytes(), shortcuts(entry(0, "DeckOps Nightly", '"/games/my-game"')))

        corrupt = shortcuts(ours, user)[:-3] + b"\xff\x08\x08"
        path.write_bytes(corrupt)
        self.run_code(SHORTCUTS, ok=False)
        self.assertEqual(path.read_bytes(), corrupt)

    def test_repoint_rejects_malformed_shortcuts_and_keeps_install(self):
        ours = self.home / "DeckOps-Nightly"
        other = self.home / "DeckOps"
        ours.mkdir()
        other.mkdir()
        (ours / "deckops.json").write_text(json.dumps({"setup_games": {}}))
        (other / "deckops.json").write_text(json.dumps({"setup_games": {}}))
        path = self.config / "shortcuts.vdf"
        invalid = shortcuts(entry(0, "DeckOps Nightly", str(ours / "src/main.py")))[:-2]
        path.write_bytes(invalid)
        self.env.update(DECKOPS_INSTALL_DIR=str(ours), DECKOPS_OTHER_DIR=str(other))
        self.run_code(REPOINT, "check", ok=False)
        self.assertEqual(path.read_bytes(), invalid)

    def test_repoint_valid_shortcuts_is_a_checked_string_edit(self):
        ours = self.home / "DeckOps-Nightly"
        other = self.home / "DeckOps"
        ours.mkdir()
        other.mkdir()
        (other / "prelaunch.sh").write_text("replacement")
        (ours / "deckops.json").write_text(json.dumps({"setup_games": {}}))
        (other / "deckops.json").write_text(json.dumps({"setup_games": {}}))
        path = self.config / "shortcuts.vdf"
        original = shortcuts(entry(0, "DeckOps Nightly", str(ours) + "/prelaunch.sh"),
                             entry(1, "My game", "/games/my-game"))
        path.write_bytes(original)
        self.env.update(DECKOPS_INSTALL_DIR=str(ours), DECKOPS_OTHER_DIR=str(other))
        self.assertIn("NEED_STEAM", self.run_code(REPOINT, "check").stdout)
        self.run_code(REPOINT, "apply")
        self.assertEqual(path.read_bytes(), original.replace(str(ours).encode(), str(other).encode()))

    def test_repoint_requires_other_branch_to_have_the_configured_game(self):
        ours = self.home / "DeckOps-Nightly"
        other = self.home / "DeckOps"
        ours.mkdir()
        other.mkdir()
        (ours / "deckops.json").write_text(json.dumps({"setup_games": {
            "t4mp": {"client": "plutonium", "source": "steam", "lan_wrapper_path": "/games/lan.sh"}}}))
        (other / "deckops.json").write_text(json.dumps({"setup_games": {}}))
        self.env.update(DECKOPS_INSTALL_DIR=str(ours), DECKOPS_OTHER_DIR=str(other))
        result = self.run_code(REPOINT, "check", ok=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("t4mp", result.stdout)

    def test_repoint_requires_other_branch_offline_mode(self):
        ours = self.home / "DeckOps-Nightly"
        other = self.home / "DeckOps"
        ours.mkdir()
        other.mkdir()
        setup = {"t4mp": {"client": "plutonium", "source": "steam"}}
        (ours / "deckops.json").write_text(json.dumps({"setup_games": setup, "offline_mode": True}))
        (other / "deckops.json").write_text(json.dumps({"setup_games": setup, "offline_mode": False}))
        self.env.update(DECKOPS_INSTALL_DIR=str(ours), DECKOPS_OTHER_DIR=str(other))
        result = self.run_code(REPOINT, "check", ok=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("Enable Offline Mode", result.stdout)

    def test_ledger_preserves_changed_controller_compat_and_configset(self):
        ledger = self.home / ".config/deckops-nightly/vdf_edits.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            "localconfig": {"12345": {"7940": {"UseSteamControllerConfig": "1"}}},
            "config_vdf": {"CompatToolMapping": {"7940": "GE-Proton-old"}},
            "configsets": {"configset_controller_neptune.vdf": {"7940": "controller_old_deckops.vdf"}},
        }))
        local = self.config / "localconfig.vdf"
        local.write_text('"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "7940" { "UseSteamControllerConfig" "0" } } } } } }')
        config = self.root / "config/config.vdf"
        config.parent.mkdir()
        config.write_text('"CompatToolMapping"\n{\n\t"7940"\n\t{\n\t\t"name" "GE-Proton-new"\n\t}\n}\n')
        cs = self.root / "steamapps/common/Steam Controller Configs/12345/config/configset_controller_neptune.vdf"
        cs.parent.mkdir(parents=True)
        cs.write_text('\t"7940"\n\t{\n\t\t"template" "controller_user.vdf"\n\t}\n')
        before = (local.read_bytes(), config.read_bytes(), cs.read_bytes())
        self.run_code(LEDGER)
        self.assertEqual((local.read_bytes(), config.read_bytes(), cs.read_bytes()), before)

    def test_lcd_shortcuts_refuse_bad_binary_and_keep_user_shortcut(self):
        path = self.config / "shortcuts.vdf"
        user = entry(0, "My Heroic", '"/usr/bin/flatpak"')
        path.write_bytes(shortcuts(user))
        self.run_code(LCD_REMOVE, str(self.root))
        self.assertEqual(path.read_bytes(), shortcuts(user))
        bad = shortcuts(user)[:-1]
        path.write_bytes(bad)
        self.run_code(LCD_REMOVE, str(self.root), ok=False)
        self.assertEqual(path.read_bytes(), bad)

    def test_invalid_ledger_is_not_silently_ignored(self):
        ledger = self.home / ".config/deckops-nightly/vdf_edits.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("{truncated")
        self.run_code(LEDGER, ok=False)
        self.assertTrue(ledger.exists())

    def test_ledger_removes_only_matching_values(self):
        ledger = self.home / ".config/deckops-nightly/vdf_edits.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            "localconfig": {"12345": {"7940": {
                "UseSteamControllerConfig": "1",
                "DefaultLaunchOption": json.dumps({"hash_key": "abc", "index": "1"})}}},
            "config_vdf": {"CompatToolMapping": {"7940": "GE-Proton-old"}},
            "configsets": {"configset_controller_neptune.vdf": {"7940": "controller_old_deckops.vdf"}},
        }))
        local = self.config / "localconfig.vdf"
        local.write_text('"Deck_ConfiguratorInterstitialApps_AppLauncherInteractionIssues" "[7940]" "apps" { "7940" { "DefaultLaunchOption" { "abc" "1" } } }\n'
                         '"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "7940" { "UseSteamControllerConfig" "1" } } } } } }')
        config = self.root / "config/config.vdf"
        config.parent.mkdir()
        config.write_text('"CompatToolMapping"\n{\n\t"7940"\n\t{\n\t\t"name" "GE-Proton-old"\n\t}\n}\n')
        cs = self.root / "steamapps/common/Steam Controller Configs/12345/config/configset_controller_neptune.vdf"
        cs.parent.mkdir(parents=True)
        cs.write_text('\t"7940"\n\t{\n\t\t"template" "controller_old_deckops.vdf"\n\t}\n')
        self.run_code(LEDGER)
        self.assertNotIn('"UseSteamControllerConfig" "1"', local.read_text())
        self.assertNotIn('"DefaultLaunchOption"', local.read_text())
        self.assertNotIn('"7940"', config.read_text())
        self.assertNotIn('"7940"', cs.read_text())

    def test_all_embedded_python_compiles(self):
        for body in re.findall(r"<<\s*'PYEOF'\n(.*?)\nPYEOF", TEXT, re.S):
            compile(body, "deckops_uninstall.sh/PYEOF", "exec")
        compile(REPOINT, "deckops_uninstall.sh/REPOINT_PY", "exec")

    def test_guarded_cleanup_locations(self):
        self.assertIn('[[ "$appid" = 42690 || "$appid" = 42750 ]]', TEXT)
        self.assertNotIn('find "$plut_dir/storage" -type l -delete', TEXT)
        self.assertNotIn('rm -rf "$COD4_APPDATA"', TEXT)
        self.assertIn('[ -e "$OTHER_DIR" ] || [ -L "$OTHER_DIR" ]', TEXT)


if __name__ == "__main__":
    unittest.main()
