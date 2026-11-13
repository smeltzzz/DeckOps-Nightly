# AGENTS.md

DeckOps Nightly is a Linux/SteamOS installer and PyQt desktop app that sets up Call of Duty mod clients on Steam Deck-class devices. This checkout is the source; production installs run from `~/DeckOps-Nightly`. Windows is only for development, tests, and building the offline launcher.

## Commands

- `python -m ruff check .` — lint. `pyproject.toml` selects correctness rules only (`E9`, `F`, `B`), target py311, 120 columns.
- `python -m pytest -q` — full suite. Tests are `unittest` style; pytest collects them cleanly.
- Single test: `python -m pytest tests/test_reliability.py::TransactionTests::test_failed_full_apply_restores_all_old_files -q`
- A Windows run is not the full suite. Shell, `flock`, symlink, and file-mode tests skip there: all of `tests/test_*_shell.py`, plus `_POSIX_ONLY`/`POSIX_ONLY`/`LINUX_BASH`-marked classes and `skipUnless` cases across `test_reliability.py` and the `test_sweep*` files.
- To run everything on a Windows dev box, use a Linux container (`python:3.12-slim` plus apt `libglib2.0-0 libgl1 libegl1 libfontconfig1 libxkbcommon0 libdbus-1-3`, pip `pytest PyQt6 PyQt5`, `QT_QPA_PLATFORM=offscreen`; PyQt5 is needed for the offline-launcher tests) over a copy of the repo with `.sh`/`.py` converted to LF: a CRLF checkout breaks the shell tests even though git stores LF. Run as a non-root user, because a few `test_sweep5_startup.py` lock tests skip when `geteuid() == 0`.
- Rebuild the tracked offline launcher with `powershell -ExecutionPolicy Bypass -File scripts/build_offline_launcher.ps1 -Publish` on Windows x64. See `docs/offline-launcher-build.md`.

There is no CI, pre-commit, or formatter config. Run the two checks above before finishing.

## Releases

- There is exactly one GitHub release, `v1`, kept up to date in place. Never create new tags or releases. Installs update from `main` via `launcher.sh`; the release only ships `DeckOps-Nightly.desktop`.
- When `DeckOps-Nightly.desktop` changes, refresh the asset with `gh release upload v1 DeckOps-Nightly.desktop -R smeltzzz/DeckOps-Nightly --clobber` (LF file, as committed) and update its notes with `gh release edit v1 --notes-file ...`. The README links to `releases/latest/download/DeckOps-Nightly.desktop`.
- For future sweeps, run the required checks, then commit on `main` and push directly to `origin/main`; no PR or merge commit is needed. Other changes land on `main` through a PR merged with a merge commit unless the user requests a different workflow.

## Layout and wiring

- `src/main.py` is the app entry point. It must recover interrupted updates before importing logging or Qt. `src/update_manifest.py` is deliberately standard-library-only because `launcher.sh` and `install.sh` run it with system Python before the venv exists.
- `src/ui_qt.py` owns the window and screen stack. Screens live in `ui_setup.py`, `ui_install.py`, and `ui_manage.py`; shared constants/helpers are in `ui_constants.py`.
- UI modules import Qt through `src/qt.py`, never PyQt directly; it intentionally supports PyQt6 with a PyQt5 fallback.
- The UI must work controller-only (D-pad arrives as arrow keys, A as Return). `ui_qt.py` installs a navigation event filter; screens opt in with `focus_target()`, `back_action()` (B/Escape), and `escape_allowed()` (False while work runs). Wrap every `QScrollArea` with `ui_constants._no_focus_scroll` (or create it with `_scroll_area`) so it can't trap the D-pad, and order rebuilt widgets with `_chain_focus`.
- The bundled fonts lack ✓ ✗ ⚠ ← ⏳, so never put them in UI text: use `MARK_*`/`_mark()`/`_plain_marks()` from `ui_constants`. Use `screen_title()`/`LABELS` for screen names and Back/Continue labels, and the `FONT_*`/`BTN_MIN_H` size constants (Deck minimums).
- `src/identity.py` is the Python source of truth for branch, install paths, repo URLs, and branding. `deckops_identity.sh` is the shell counterpart. Hardcoded fallbacks in `launcher.sh`, `install.sh`, and `deckops_uninstall.sh` must stay in sync. Change branch only through `BRANCH`.
- This fork's identity points at `smeltzzz/DeckOps-Nightly`; `upstream` is `GalvarinoDev/DeckOps-Nightly`. Do not "correct" URLs to upstream.
- `identity.py` has no internal imports and `log.py` only imports `identity`; avoid adding app imports to either.
- Production starts through shell: `launcher.sh` handles install/update, `install.sh` creates `.venv`, and `prelaunch.sh` runs `prelaunch.py` before managed game launches. A game launch must still proceed if the prelaunch update fails.
- `VERSION` is a commit SHA used by the update check, not a semantic version; the launcher rewrites it when applying an update.
- Runtime config and logs live in `~/DeckOps-Nightly/deckops.json` and `~/DeckOps-Nightly/logs/install.log`, not the repo.
- Game keys are string IDs such as `cod4mp`, `iw5mp_ds`, or `t6zm`. They are duplicated across `detect_games.GAMES`, `ui_constants.ALL_GAMES`/`KEY_CLIENT`/`KEY_EXES`, `shortcut.SHORTCUTS`, and Plutonium modules; update all affected lists when changing support.
- Use `steam_common` helpers (`atomic_write`, `backup_file`, `keep_original`) for Steam VDF edits, and record changes in the VDF ledger. Shortcut/controller changes must run while Steam is closed.
- `.gitattributes` forces LF for `.sh`, `.desktop`, and `.py`. Keep them LF even on Windows.

## Tests and data safety

- Modules are flat, not a package; tests insert `src/` on `sys.path`. Follow the existing `unittest` style.
- Qt tests set `QT_QPA_PLATFORM=offscreen` and skip if PyQt is unavailable. Reuse one `QApplication` per run, as existing tests do.
- Never point tests at the real `~/DeckOps-Nightly`, real Steam `userdata`, or real game installs. Use `tempfile`, patched `cfg`, and temporary HOME/Steam roots like the existing tests.
- `tests/test_reliability.py` is the main safety net for update, repair, and partial-failure behavior.
- `tests/test_uninstall_safety.py` executes the Python embedded in `deckops_uninstall.sh`; keep those embedded blocks valid if editing the uninstaller.
- `assets/LAN/DeckOps_Offline.exe` is a tracked binary built from `src/launcher_plut_win.py`. Rebuild it with the script instead of editing or replacing it by hand.
