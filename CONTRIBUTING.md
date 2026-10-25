# Contributing

## Setup

You don't need a Steam Deck to run the checks below. They cover the logic that
doesn't touch Steam, Proton or a display. Anything that does still needs
testing on a device.

```
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

## Checks (the same ones CI runs)

```
ruff check .                    # pyflakes + syntax errors
python -m compileall -q src tests
pytest
shellcheck --severity=error *.sh assets/LAN/*.sh
```

- Tests live in `tests/` and import modules from `src/` directly (see
  `pyproject.toml`). They must never read or write the real
  `~/DeckOps-Nightly`: point `config.CONFIG_PATH` and similar paths at
  `tmp_path` (see the `cfg` fixture in `tests/test_config.py`).
- A few tests pin values that must never change (shortcut appids, Heroic app
  names). If one fails, the code is wrong, not the test: existing installs and
  Steam shortcuts depend on those values.

## Line endings

`.sh`, `.desktop` and `.py` files are committed with LF endings
(`.gitattributes`). On Windows keep `core.autocrlf` as you like; git converts
on commit, and CI fails if a CRLF reaches a shell script.
