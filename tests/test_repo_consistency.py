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
    # update from another.
    pattern = re.compile(r'''\bGITHUB_USER\s*=\s*["']([^"']+)["']''')
    seen = {}
    for rel in _tracked(".sh", ".py"):
        if rel.startswith("tests"):
            continue
        for m in pattern.finditer(_read(rel)):
            seen.setdefault(m.group(1), []).append(rel)
    assert set(seen) == {identity.GITHUB_USER}, seen


def test_desktop_entry_points_at_this_repo():
    exec_line = next(l for l in _read("DeckOps-Nightly.desktop").splitlines() if l.startswith("Exec="))
    assert f"{identity.GITHUB_USER}/{identity.GITHUB_REPO}/main/launcher.sh" in exec_line


def test_gitattributes_forces_lf_for_scripts():
    # CRLF in a .sh or .desktop file breaks it on the Deck. Windows checkouts
    # (core.autocrlf=true) would otherwise convert them.
    attrs = _read(".gitattributes")
    for pattern in ("*.sh", "*.desktop", "*.py"):
        assert re.search(rf"^{re.escape(pattern)}\s+text\s+eol=lf\s*$", attrs, re.M), pattern


def test_version_file_is_a_git_sha():
    assert re.fullmatch(r"[0-9a-f]{40}", _read("VERSION").strip())
