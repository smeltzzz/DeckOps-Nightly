# AGENTS.md

DeckOps Nightly is a Linux/SteamOS installer and PyQt desktop app that sets up Call of Duty mod clients on Steam Deck-class devices. This checkout is the source; production installs run from `~/DeckOps-Nightly`. Windows is only for development, tests, and building the offline launcher.

## Commands

- `python -m ruff check .` — lint. `pyproject.toml` selects correctness rules only (`E9`, `F`, `B`), target py311, 120 columns.
- `python -m pytest -q` — full suite. Tests are `unittest` style; pytest collects them cleanly.
- Single test: `python -m pytest tests/test_reliability.py::TransactionTests::test_failed_full_apply_restores_all_old_files -q`
- Linux-only shell tests: run `tests/test_prelaunch_shell.py`, `tests/test_lan_server_shell.py`, and `tests/test_offline_launcher_shell.py` on Linux with `bash`/`flock`; they skip on Windows.
- Rebuild the tracked offline launcher with `powershell -ExecutionPolicy Bypass -File scripts/build_offline_launcher.ps1 -Publish` on Windows x64. See `docs/offline-launcher-build.md`.

There is no CI, pre-commit, or formatter config. Run the two checks above before finishing.

## Layout and wiring

- `src/main.py` is the app entry point. It must recover interrupted updates before importing logging or Qt. `src/update_manifest.py` is deliberately standard-library-only because `launcher.sh` and `install.sh` run it with system Python before the venv exists.
- `src/ui_qt.py` owns the window and screen stack. Screens live in `ui_setup.py`, `ui_install.py`, and `ui_manage.py`; shared constants/helpers are in `ui_constants.py`.
- UI modules import Qt through `src/qt.py`, never PyQt directly; it intentionally supports PyQt6 with a PyQt5 fallback.
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
