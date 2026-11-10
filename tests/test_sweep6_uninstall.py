"""Regression tests for uninstaller fixes (sweep 6).

Like test_sweep5_uninstall.py these run parts of deckops_uninstall.sh (its
embedded Python, or bash fragments with stub commands on PATH) against
temporary HOME / Steam folders, never real ones. Bash tests only run on Linux.
"""
import glob
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deckops_uninstall.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")

# python3 lives in /usr/local/bin in some images
SYS_PATH = f"{os.path.dirname(sys.executable)}:/usr/bin:/bin"
LINUX_BASH = unittest.skipUnless(sys.platform == "linux" and shutil.which("bash"), "requires Linux bash")

HELPER = TEXT.split("cat > \"$DECKOPS_PY_HELPERS/deckops_vdf.py\" <<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
OWN_CLEANUP = TEXT.split('DECKOPS_DLC_KEPT_FLAG="$DECKOPS_PY_HELPERS/iw4x_dlc_kept" python3 - << \'PYEOF\'\n',
                         1)[1].split("\nPYEOF", 1)[0]


def func(name):
    """A top-level bash function's source."""
    return re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", TEXT, re.S | re.M).group(0)


STUB_UI = 'info() { echo "INFO $1"; }; success() { echo "OK $1"; }; skip() { echo "SKIP $1"; }\n'
WARN = 'warn() { LAST_WARN="$1"; echo "WARN $1"; }\n'


def entry(index, name, exe, tag=False):
    raw = b"\x00" + str(index).encode() + b"\x00"
    for key, value in ((b"AppName", name), (b"exe", exe)):
        raw += b"\x01" + key + b"\x00" + value.encode() + b"\x00"
    raw += b"\x02appid\x00\x08\x00\x00\x00"
    if tag:
        raw += b"\x00tags\x00\x010\x00DeckOps\x00\x08"
    return raw + b"\x08"


def shortcuts(*entries):
    return b"\x00shortcuts\x00" + b"".join(entries) + b"\x08\x08"


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.stubs = self.home / "stubs"
        self.stubs.mkdir()
        self.log = self.home / "calls.log"

    def stub(self, name, body):
        p = self.stubs / name
        p.write_text(f"#!/bin/sh\necho \"{name} $*\" >> \"{self.log}\"\n{body}\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def bash(self, code, path=None, timeout=60):
        env = dict(os.environ, HOME=str(self.home),
                   PATH=path or f"{self.stubs}:{SYS_PATH}")
        # A new session has no controlling terminal: a "Press Enter" or
        # [y/N] read from /dev/tty can never block a test run
        return subprocess.run(["bash", "-c", code], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, start_new_session=True, timeout=timeout)


# ── UNIN-1: CoD4 userraw/ (identity key) and Mods/mp_bots ────────────────────

class Cod4OwnCopyTests(_Tmp):
    """The own-copy (non-Steam) cleanup pass, run for real."""

    def _game(self, cod4r):
        game = self.home / "Games" / "CoD4"
        (game / "userraw").mkdir(parents=True)
        (game / "userraw" / "cod4r_id.key").write_bytes(b"identity")
        (game / "Mods" / "mp_bots").mkdir(parents=True)
        (game / "Mods" / "mp_bots" / "bots.iwd").write_bytes(b"bots")
        (game / "iw3mp.exe").write_bytes(b"MZ")
        if cod4r:
            (game / "deckops_cod4r.json").write_text("{}")
        else:
            (game / "deckops_cod4x.json").write_text("{}")
        root = self.home / "steam"
        config = root / "userdata" / "12345" / "config"
        config.mkdir(parents=True)
        (config / "shortcuts.vdf").write_bytes(shortcuts(
            entry(0, "Call of Duty 4", f'"{game / "iw3mp.exe"}"', True)))
        (self.home / "deckops_vdf.py").write_text(HELPER, encoding="utf-8")
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                   DECKOPS_STEAM_ROOT=str(root), PYTHONPATH=str(self.home), PYTHONIOENCODING="utf-8")
        env.pop("DECKOPS_DLC_KEPT_FLAG", None)
        result = subprocess.run([sys.executable, "-c", OWN_CLEANUP], env=env, capture_output=True,
                                text=True, encoding="utf-8", stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return game

    def test_identity_key_survives_and_cod4r_bots_go(self):
        game = self._game(cod4r=True)
        self.assertEqual((game / "userraw" / "cod4r_id.key").read_bytes(), b"identity")
        self.assertFalse((game / "Mods" / "mp_bots").exists())
        self.assertFalse((game / "deckops_cod4r.json").exists())

    def test_bots_kept_without_cod4r_metadata(self):
        game = self._game(cod4r=False)
        self.assertTrue((game / "userraw" / "cod4r_id.key").exists())
        self.assertTrue((game / "Mods" / "mp_bots" / "bots.iwd").exists())

    def test_map_never_lists_userraw(self):
        block = OWN_CLEANUP.split('"iw3mp.exe": {', 1)[1].split('"subdirs"', 1)[0]
        self.assertNotIn('"userraw"', block)


class Cod4SteamCopyTests(_Tmp):
    """The Steam-install pass (bash), with find_install_dir stubbed."""

    SECTION = TEXT.split("    cod4_dir=$(find_install_dir 7940) || true\n", 1)[1] \
                  .split("    # This is player data, not a DeckOps-owned cache.", 1)[0]

    def test_userraw_never_removed_by_the_shell_pass(self):
        self.assertNotIn('"userraw"', self.SECTION)
        self.assertLess(self.SECTION.index('[ -f "$cod4_dir/deckops_cod4r.json" ] && _had_cod4r=1'),
                        self.SECTION.index('"deckops_cod4r.json" "pbgame.htm"'))

    def _run(self, cod4r):
        game = self.home / "cod4"
        (game / "userraw").mkdir(parents=True)
        (game / "userraw" / "cod4r_id.key").write_bytes(b"identity")
        (game / "Mods" / "mp_bots").mkdir(parents=True)
        if cod4r:
            (game / "deckops_cod4r.json").write_text("{}")
        code = (STUB_UI + WARN + "FAILED=0\n" + func("rm_path") +
                f'find_install_dir() {{ echo "{game}"; }}\nrestore_cod4_audio() {{ :; }}\n'
                "cod4_dir=$(find_install_dir 7940) || true\n" + self.SECTION)
        result = self.bash(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return game, result.stdout

    @LINUX_BASH
    def test_cod4r_install_removes_bots_and_keeps_key(self):
        game, out = self._run(cod4r=True)
        self.assertTrue((game / "userraw" / "cod4r_id.key").exists())
        self.assertFalse((game / "Mods" / "mp_bots").exists())
        self.assertFalse((game / "deckops_cod4r.json").exists())
        self.assertIn("Kept userraw/", out)

    @LINUX_BASH
    def test_no_cod4r_metadata_keeps_bots(self):
        game, _ = self._run(cod4r=False)
        self.assertTrue((game / "Mods" / "mp_bots").exists())
        self.assertTrue((game / "userraw" / "cod4r_id.key").exists())


# ── UNIN-2: failures and the side-by-side result stay on screen ──────────────

def _real_steam_nearby():
    """Paths the full script would look at outside the temp HOME."""
    return (os.path.isdir("/home/deck/.local/share/Steam")
            or os.path.isdir("/run/media/mmcblk0p1/.local/share/Steam")
            or glob.glob("/run/media/*/steamapps") or glob.glob("/run/media/*/*/steamapps"))


class ExitTrapTests(_Tmp):
    def test_one_exit_trap_and_it_keeps_the_status(self):
        self.assertEqual(len(re.findall(r"^trap .*EXIT", TEXT, re.M)), 1)
        self.assertIn("trap on_exit EXIT", TEXT)
        self.assertIn('exit "$rc"', func("on_exit"))
        self.assertIn("[ -t 1 ] || return 0", func("pause_if_terminal"))

    @LINUX_BASH
    def test_failed_exit_shows_last_warning_and_does_not_block(self):
        self.stub("zenity", "exit 0")
        code = (WARN + 'APP_TITLE="DeckOps Nightly"\nCLEAN_PY_HELPERS=""\n' + func("pause_if_terminal")
                + func("on_exit") + "trap on_exit EXIT\n"
                'warn "Steam did not close within 120 seconds."\nexit 3\n')
        result = self.bash(code, timeout=20)
        self.assertEqual(result.returncode, 3)
        calls = self.calls()
        self.assertIn("zenity --error", calls)
        self.assertIn("Steam did not close within 120 seconds.", calls)

    @LINUX_BASH
    def test_success_exit_shows_nothing(self):
        self.stub("zenity", "exit 0")
        code = (WARN + 'APP_TITLE="x"\nCLEAN_PY_HELPERS=""\n' + func("pause_if_terminal")
                + func("on_exit") + "trap on_exit EXIT\nwarn 'harmless'\nexit 0\n")
        result = self.bash(code, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), "")

    @LINUX_BASH
    def test_helpers_dir_is_still_cleaned_up(self):
        helpers = self.home / "helpers"
        helpers.mkdir()
        code = (WARN + 'CLEAN_PY_HELPERS="' + str(helpers) + '"\n' + func("pause_if_terminal")
                + func("on_exit") + "trap on_exit EXIT\nexit 0\n")
        self.bash(code, timeout=20)
        self.assertFalse(helpers.exists())


@unittest.skipIf(sys.platform == "linux" and _real_steam_nearby(), "a real Steam install is visible")
class FullScriptTests(_Tmp):
    """The whole uninstaller, from a copy outside the repo (no identity file
    beside it), against an empty temp HOME and a stub zenity."""

    def _run(self):
        script = self.home / "bin" / "deckops_uninstall.sh"
        script.parent.mkdir()
        script.write_text(TEXT, encoding="utf-8")
        self.stub("zenity", "exit 0")
        return self.bash(f'exec bash "{script}"', timeout=120)

    @LINUX_BASH
    def test_no_steam_root_failure_is_shown(self):
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("zenity --error", calls)
        self.assertIn("Steam root not found", calls)

    @LINUX_BASH
    def test_side_by_side_success_is_shown(self):
        (self.home / "DeckOps" / "src").mkdir(parents=True)
        (self.home / "DeckOps" / "src" / "main.py").write_text("")
        (self.home / ".local/share/Steam/steamapps").mkdir(parents=True)
        (self.home / "DeckOps-Nightly").mkdir()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("zenity --info", calls)
        self.assertIn("DeckOps Nightly removed.", calls)
        self.assertNotIn("--error", calls)
        self.assertFalse((self.home / "DeckOps-Nightly").exists())
        self.assertTrue((self.home / "DeckOps" / "src" / "main.py").exists())


# ── UNIN-3: closing Flatpak Steam (no `steam` on PATH) ───────────────────────

class CloseSteamTests(_Tmp):
    CODE = TEXT.split("STEAM_CLIENT_PATTERN=", 1)[1].split("\nSTEAM_ROOTS=(", 1)[0]

    def _run(self, flatpak=None):
        if shutil.which("steam", path=SYS_PATH):
            self.skipTest("a real steam command is installed")
        if flatpak is None and shutil.which("flatpak", path=SYS_PATH):
            self.skipTest("a real flatpak command is installed")
        closed = self.home / "steam_closed"
        # Running until the flag exists
        self.stub("pgrep", f'[ -e "{closed}" ] && exit 1\nexit 0')
        self.stub("pkill", f'touch "{closed}"')
        if flatpak is not None:
            self.stub("flatpak", flatpak.replace("CLOSED", str(closed)))
        code = (STUB_UI + WARN +
                # The wait loop advances the clock instead of sleeping
                "sleep() { SECONDS=$((SECONDS + 1)); }\nsync() { :; }\n"
                "STEAM_CLIENT_PATTERN=" + self.CODE + "\nclose_steam\necho DONE\n")
        return self.bash(code, timeout=60)

    @LINUX_BASH
    def test_flatpak_steam_is_asked_to_shut_down(self):
        result = self._run(flatpak='touch "CLOSED"')
        self.assertIn("DONE", result.stdout, result.stdout + result.stderr)
        self.assertIn("Steam closed.", result.stdout)
        self.assertIn("flatpak run com.valvesoftware.Steam -shutdown", self.calls())
        self.assertNotIn("pkill", self.calls())

    @LINUX_BASH
    def test_without_steam_or_flatpak_the_client_gets_sigterm(self):
        result = self._run()
        self.assertIn("DONE", result.stdout, result.stdout + result.stderr)
        self.assertIn("pkill -TERM -f ubuntu12_32/steam( |$)", self.calls())

    @LINUX_BASH
    def test_accepted_request_that_does_nothing_is_followed_by_sigterm(self):
        result = self._run(flatpak="exit 0")
        self.assertIn("DONE", result.stdout, result.stdout + result.stderr)
        self.assertIn("pkill -TERM", self.calls())

    def test_pattern_matches_wrapper_kill_steam(self):
        self.assertIn("STEAM_CLIENT_PATTERN='ubuntu12_32/steam( |$)'", TEXT)
        wrapper = (SCRIPT.parent / "src" / "wrapper.py").read_text(encoding="utf-8")
        self.assertIn('steam_pattern = r"ubuntu12_32/steam( |$)"', wrapper)


# ── UNIN-4: the final summary only claims what happened ──────────────────────

class SummaryTests(_Tmp):
    SECTION = TEXT.split('if [ "$FAILED" -ne 0 ]; then\n    echo -e "${YELLOW}', 1)[1]
    SECTION = 'if [ "$FAILED" -ne 0 ]; then\n    echo -e "${YELLOW}' + SECTION.split('COD4_VERIFY_TEXT=""', 1)[0]

    def _summary(self, failed=0, verify=0, kept=""):
        code = (f'YELLOW=""; GREEN=""; BOLD=""; CLEAR=""\nFAILED={failed}\n'
                f'COD4_NEEDS_VERIFY={verify}\nIW4X_DLC_KEPT="{kept}"\n' + self.SECTION)
        result = self.bash(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    @LINUX_BASH
    def test_clean_run_claims_everything(self):
        out = self._summary()
        self.assertIn("fully uninstalled", out)
        self.assertIn("Your Steam games are untouched.", out)
        self.assertIn("All IW3SP-MOD and IW4x client files removed.", out)

    @LINUX_BASH
    def test_failed_run_claims_nothing_was_all_removed(self):
        out = self._summary(failed=1)
        self.assertIn("with warnings", out)
        self.assertNotIn("All ", out)
        self.assertNotIn("untouched", out)

    @LINUX_BASH
    def test_cod4_verify_qualifies_untouched(self):
        out = self._summary(verify=1)
        self.assertIn("except Call of Duty 4 needs a file check", out)
        self.assertNotIn("Your Steam games are untouched.\n", out)

    @LINUX_BASH
    def test_kept_dlc_is_not_claimed_removed(self):
        out = self._summary(kept="1")
        self.assertIn("IW4x DLC maps kept", out)
        self.assertIn("IW4x DLC maps were kept", self._summary(failed=1, kept="1"))

    def test_both_dlc_prompts_record_a_kept_answer(self):
        self.assertIn("$_remove_mw2_dlc || IW4X_DLC_KEPT=1", TEXT)
        self.assertIn('[ ! -f "$DECKOPS_PY_HELPERS/iw4x_dlc_kept" ] || IW4X_DLC_KEPT=1', TEXT)
        self.assertIn('os.environ.get("DECKOPS_DLC_KEPT_FLAG")', OWN_CLEANUP)


# ── UNIN-5: questions without zenity ─────────────────────────────────────────

class NoZenityPromptTests(_Tmp):
    def test_only_the_helper_and_the_two_default_prompts_call_zenity_question(self):
        # The confirmation and the save backup question have their own
        # terminal fallbacks; every Remove/Keep question goes through the helper
        self.assertEqual(TEXT.count("zenity --question"), 3)
        self.assertIn("zenity --question", func("ask_no_default"))
        for text in ("Remove free MW2 DLC maps", "Remove Plutonium mods, custom maps",
                     "Remove MW3 32-bit depot files"):
            line = TEXT.split(text, 1)[0].rsplit("\n", 1)[1]
            self.assertIn("ask_no_default", line, text)

    @LINUX_BASH
    def test_without_zenity_or_terminal_the_answer_is_no(self):
        if shutil.which("zenity", path=SYS_PATH):
            self.skipTest("zenity is installed")
        code = 'APP_TITLE=x\n' + func("ask_no_default") + 'ask_no_default "Remove?" Remove Keep; echo "rc=$?"\n'
        result = self.bash(code, timeout=20)
        self.assertIn("rc=1", result.stdout)
        self.assertIn("[y/N]", result.stdout)

    @LINUX_BASH
    def test_with_zenity_its_answer_is_used(self):
        self.stub("zenity", "exit 0")
        code = 'APP_TITLE=x\n' + func("ask_no_default") + 'ask_no_default "Remove?" Remove Keep; echo "rc=$?"\n'
        result = self.bash(code, timeout=20)
        self.assertIn("rc=0", result.stdout)
        self.assertIn("--ok-label=Remove", self.calls())


# ── LCD grid artwork: exact appid match ──────────────────────────────────────

class LcdGridTests(_Tmp):
    SECTION = TEXT.split("# ── Remove stale shadercache and grid artwork for LCD appids", 1)[1]
    SECTION = SECTION.split("\n", 1)[1].split('    skip "No LCD appids for shadercache/grid cleanup."\nfi\n', 1)[0] \
        + '    skip "No LCD appids for shadercache/grid cleanup."\nfi\n'

    @LINUX_BASH
    def test_only_exact_appid_artwork_is_removed(self):
        root = self.home / "steam"
        grid = root / "userdata" / "12345" / "config" / "grid"
        grid.mkdir(parents=True)
        for n in ("3000000001p.png", "3000000001_hero.jpg", "30000000012p.png", "999.png"):
            (grid / n).write_bytes(b"x")
        ids = self.home / "ids.txt"
        ids.write_text("3000000001\n../evil\n")
        code = (STUB_UI + f'STEAM_ROOT="{root}"\nLCD_APPIDS_FILE="{ids}"\n' + self.SECTION)
        result = self.bash(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in grid.iterdir()), ["30000000012p.png", "999.png"])


class EmbeddedPythonTests(unittest.TestCase):
    def test_all_embedded_python_compiles(self):
        for body in re.findall(r"<<\s*'PYEOF'\n(.*?)\nPYEOF", TEXT, re.S):
            compile(body, "deckops_uninstall.sh/PYEOF", "exec")


if __name__ == "__main__":
    unittest.main()
