"""Cross-file checks: things that have to agree but live in different files."""

import os
import re

import identity

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _tracked(*suffixes):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache")]
        for name in files:
            if name.endswith(suffixes):
                yield os.path.relpath(os.path.join(base, name), ROOT)


def test_github_user_is_the_same_everywhere():
    # A fork that updated only some of these would install from one repo and
    # update from another. The shell entry points run under `curl | bash`
    # before anything else is on disk, so each keeps its own copy; this test
    # is what keeps those copies (and identity.py) in agreement.
    pattern = re.compile(r'''\b_?GITHUB_USER\s*=\s*["']([^"']+)["']''')
    seen = {}
    for rel in _tracked(".sh", ".py"):
        if rel.startswith("tests"):
            continue
        for m in pattern.finditer(_read(rel)):
            seen.setdefault(m.group(1), []).append(rel)
    assert set(seen) == {identity.GITHUB_USER}, seen
    # And it is actually found in the places we expect, so a refactor that
    # renames the variable can't make the check pass by matching nothing.
    found_in = {rel for files in seen.values() for rel in files}
    assert {"install.sh", "launcher.sh", "deckops_uninstall.sh", "deckops_identity.sh",
            os.path.join("src", "identity.py"), os.path.join("src", "launcher_plut_win.py")} <= found_in


def test_each_shell_script_sets_the_user_and_repo_once():
    # The fallback block used to repeat them right before the unconditional
    # override; one definition per script is enough.
    for rel in ("install.sh", "launcher.sh", "deckops_uninstall.sh"):
        text = _read(rel)
        assert len(re.findall(r'^\s*GITHUB_USER=', text, re.M)) == 1, rel
        assert len(re.findall(r'^\s*GITHUB_REPO=', text, re.M)) == 1, rel


def test_windows_launcher_mirrors_identity():
    # launcher_plut_win.py is built into a standalone exe and keeps its own
    # copy of the branch identity ("keep in sync" in the source).
    text = _read(os.path.join("src", "launcher_plut_win.py"))
    branch = re.search(r'^_BRANCH\s*=\s*"(\w+)"', text, re.M).group(1)
    assert branch == identity.BRANCH
    assert re.search(r'^_INSTALL_DIR_NAME\s*=\s*"DeckOps-Nightly" if _BRANCH == "nightly" else "DeckOps"',
                     text, re.M)
    assert identity.INSTALL_DIR_NAME == ("DeckOps-Nightly" if branch == "nightly" else "DeckOps")


def test_shell_identity_matches_python_identity():
    sh = _read("deckops_identity.sh")
    assert f'BRANCH="{identity.BRANCH}"' in sh
    assert f'GITHUB_REPO="{identity.GITHUB_REPO}"' in sh
    assert f'INSTALL_DIR_NAME="{identity.INSTALL_DIR_NAME}"' in sh
    assert f'APP_TITLE="{identity.APP_TITLE}"' in sh


def test_readme_download_link_points_at_this_repo():
    readme = _read("README.md")
    assert f"github.com/{identity.GITHUB_USER}/{identity.GITHUB_REPO}/releases/" in readme


def test_desktop_entry_points_at_this_repo():
    exec_line = next(l for l in _read("DeckOps-Nightly.desktop").splitlines() if l.startswith("Exec="))
    assert f"{identity.GITHUB_USER}/{identity.GITHUB_REPO}/main/launcher.sh" in exec_line


def test_gitattributes_forces_lf_for_scripts():
    # CRLF in a .sh or .desktop file breaks it on the Deck. Windows checkouts
    # (core.autocrlf=true) would otherwise convert them.
    attrs = _read(".gitattributes")
    for pattern in ("*.sh", "*.desktop", "*.py"):
        assert re.search(rf"^{re.escape(pattern)}\s+text\s+eol=lf\s*$", attrs, re.M), pattern


def _logical_lines(rel):
    """Non-comment lines of a script, with backslash continuations joined."""
    text = _read(rel).replace("\\\n", " ")
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


SCRIPTS = ("install.sh", "launcher.sh", "deckops_uninstall.sh", "DeckOps-Nightly.desktop")


def test_no_script_is_piped_from_curl_into_a_shell():
    # `curl | bash` runs whatever has arrived when the connection drops, so a
    # half-downloaded installer or uninstaller would execute. Download to a
    # file first (launcher.sh: run_remote_script).
    for rel in SCRIPTS:
        for line in _logical_lines(rel):
            assert not re.search(r"curl\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", line), (rel, line)


def test_curl_downloads_to_a_file_fail_on_http_errors():
    # Without -f an error page (404, rate limit) is saved as if it were the
    # file. The one exception reads the status code itself to tell a file
    # deleted upstream (404) from a real failure.
    for rel in SCRIPTS:
        for line in _logical_lines(rel):
            if "curl" in line and re.search(r"\s-o\s", line) and "%{http_code}" not in line:
                assert re.search(r"curl\s+(-[A-Za-z]*f[A-Za-z]*|--fail)\b", line), (rel, line)


def test_launcher_update_checks_curl_exit_status_not_just_the_http_code():
    # A stream cut off after the headers still reports 200; only curl's exit
    # status says the whole file arrived.
    text = _read("launcher.sh")
    assert "CURL_RC=$?" in text
    assert re.search(r'\[ "\$CURL_RC" -ne 0 \]', text)


def test_launcher_fetches_scripts_through_run_remote_script():
    text = _read("launcher.sh")
    assert "run_remote_script install.sh" in text
    assert "run_remote_script deckops_uninstall.sh" in text


def test_version_file_is_a_git_sha():
    assert re.fullmatch(r"[0-9a-f]{40}", _read("VERSION").strip())
