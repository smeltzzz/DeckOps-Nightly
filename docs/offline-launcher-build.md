# Windows offline launcher build

`src/launcher_plut_win.py` is the source of `assets/LAN/DeckOps_Offline.exe`.
Build **on Windows x64** with the `py -3.12` launcher, PowerShell and network access to PyPI:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_offline_launcher.ps1 -Publish
```

The script creates a disposable virtual environment under `%TEMP%`, installs pinned PyQt5 5.15.11, PyInstaller 6.16.0, XInput-Python 0.4.0, pygame 2.6.1 and their pinned dependencies, syntax-checks the source, runs `tests/test_offline_launcher.py` headlessly, builds a windowed one-file executable, verifies its PE header/size, then replaces the shipped exe only on success. Without `-Publish` it verifies a disposable build but does not replace the tracked binary. Record the printed SHA-256 in release notes if needed.

`src/ui_theme.py` is bundled as an imported Python module in the executable (the script sets `--paths` and `--hidden-import ui_theme`); it is not an external runtime dependency. The launcher shares the Linux app's palette and uses dark text on the light lime/amber action buttons.

**Artwork and fonts are deliberately external to the exe.** Ship the resulting exe at `assets/LAN/DeckOps_Offline.exe` alongside `assets/LAN/launcher_offline.sh`. Keep `assets/fonts/RussoOne-Regular.ttf`, `assets/fonts/AtkinsonHyperlegible-{Regular,Bold}.ttf` and `assets/fonts/AtkinsonHyperlegible-OFL.txt`, plus the four `assets/images/heroes/{waw,bo1,bo2,mw3}-banner.png` files, in their existing relative locations. The launcher derives the DeckOps root from the executable path under Proton's `Z:` mapping, reads `deckops.json` there and writes `.lan_intent.json` with only `lan_wrapper_path`. Missing/corrupt hero art paints a local fallback; no network request is needed at runtime.

Before shipping, run the targeted checks in the build environment (or install the same pinned dependencies in a separate test environment):

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m unittest discover -s tests -p test_offline_launcher.py -v
```

Finally test the non-Steam shortcut **on a Deck with Proton**: launch each installed mode with D-pad/A and arrow/Enter, B/Esc exit, Y/M mute, mouse click, 1280×800 and a smaller resolution. Verify the wrapper consumes the intent, launches the per-game LAN wrapper, and shows a retryable error if the DeckOps directory is unwritable. A Windows PE sanity check alone cannot prove Proton/XInput or a game's setup works.
