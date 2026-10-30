#!/bin/bash
# launcher_offline.sh - DeckOps Plutonium Offline Launcher wrapper
#
# Entry point for the non-Steam shortcut. Runs DeckOps_Offline.exe inside
# Proton (for XInput gamepad support), then reads an intent file written
# by the exe to determine which game to launch. Each invocation has its own
# intent directory, so two Steam sessions cannot consume each other's choice.
# If an intent is found, exec's into the game's LAN wrapper script.
#
# Process chain (from Steam's perspective):
#   Steam -> launcher_offline.sh -> proton run DeckOps_Offline.exe (UI)
#                                -> exe exits, writes its private intent
#                                -> exec bash <lan_wrapper_path>
#                                -> lan wrapper exec's proton run bootstrapper
#
# Steam sees launcher_offline.sh as the running process the entire time.
# The final exec replaces this shell with Proton running the game, so
# Steam tracks it correctly and shows the game as running.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# assets/LAN/launcher_offline.sh -> two levels up -> DeckOps[-Nightly]/
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DECKOPS_JSON="$PROJECT_ROOT/deckops.json"
LAUNCHER_EXE="$SCRIPT_DIR/DeckOps_Offline.exe"

# ── Read config ──────────────────────────────────────────────────────────────

if [ ! -f "$DECKOPS_JSON" ]; then
    echo "ERROR: deckops.json not found at $DECKOPS_JSON" >&2
    exit 1
fi

# Parse GE-Proton version and Steam root from deckops.json.
# `or ''` matters: a null value printed as the string "None", which is not
# empty, so the check below passed and the run died with "Proton not found at
# None/compatibilitytools.d/..." instead of naming what was actually missing.
GE_VERSION=$(python3 - "$DECKOPS_JSON" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('ge_proton_version') or '')
except Exception:
    pass
PY
)

STEAM_ROOT=$(python3 - "$DECKOPS_JSON" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('steam_root') or '')
except Exception:
    pass
PY
)

if [ -z "$GE_VERSION" ] || [ -z "$STEAM_ROOT" ]; then
    echo "ERROR: Could not read ge_proton_version or steam_root from deckops.json" >&2
    exit 1
fi

PROTON_PATH="$STEAM_ROOT/compatibilitytools.d/$GE_VERSION/proton"

if [ ! -f "$PROTON_PATH" ]; then
    echo "ERROR: Proton not found at $PROTON_PATH" >&2
    exit 1
fi

if [ ! -f "$LAUNCHER_EXE" ]; then
    echo "ERROR: Launcher exe not found at $LAUNCHER_EXE" >&2
    exit 1
fi

# ── Private launch request ───────────────────────────────────────────────────
# Create the directory atomically; the UI writes intent.json there via rename.
# Wine's Z: drive exposes Linux paths to the Windows exe.
INTENT_DIR=$(mktemp -d "$PROJECT_ROOT/.lan_intent.XXXXXXXX") || exit 1
INTENT_FILE="$INTENT_DIR/intent.json"
INTENT_WIN="Z:${INTENT_FILE//\//\\}"
cleanup_intent() { rm -f "$INTENT_FILE" "$INTENT_DIR"/.lan_intent-*.tmp; rmdir "$INTENT_DIR" 2>/dev/null || true; }
trap cleanup_intent EXIT

# ── Determine STEAM_COMPAT_DATA_PATH for the launcher exe ────────────────────
# The exe needs a Wine prefix to run in. We use a fixed prefix under
# the DeckOps data directory so it persists across installs and doesn't
# depend on Steam shortcut appids (which change when the exe path changes).

LAUNCHER_PREFIX="$HOME/.local/share/deckops/launcher_prefix"
mkdir -p "$LAUNCHER_PREFIX"

export STEAM_COMPAT_DATA_PATH="$LAUNCHER_PREFIX"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAM_ROOT"

# ── Launch the UI ────────────────────────────────────────────────────────────
# Run the exe inside Proton. The exe shows the game selection UI and
# writes only this invocation's intent when the user picks a game, then exits.
# PROTON_NO_FSYNC/ESYNC prevents hangs on older kernels.

PROTON_NO_FSYNC=1 PROTON_NO_ESYNC=1 "$PROTON_PATH" run "$LAUNCHER_EXE" --intent-file "$INTENT_WIN" "$@"

# ── Check for launch intent ─────────────────────────────────────────────────

if [ ! -f "$INTENT_FILE" ]; then
    # User quit without selecting a game
    exit 0
fi

# Trust only a real wrapper registered for a Plutonium setup in deckops.json.
# Compare resolved paths so aliases/symlinks cannot select another script.
LAN_WRAPPER=$(python3 - "$DECKOPS_JSON" "$INTENT_FILE" <<'PY'
import json
import os
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        games = json.load(f).get("setup_games", {})
    with open(sys.argv[2], encoding="utf-8") as f:
        selected = json.load(f)["lan_wrapper_path"]
    if not isinstance(selected, str) or not os.path.isabs(selected) or not os.path.isfile(selected):
        raise ValueError("selected wrapper is not an absolute file path")
    path = os.path.realpath(selected)
    if not isinstance(games, dict) or not any(
        isinstance(game, dict) and game.get("client") == "plutonium"
        and isinstance(game.get("lan_wrapper_path"), str)
        and os.path.isabs(game["lan_wrapper_path"])
        and os.path.realpath(game["lan_wrapper_path"]) == path
        for game in games.values()
    ):
        raise ValueError("selected wrapper is not configured for a Plutonium game")
    print(path)
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f"ERROR: Invalid LAN launch intent: {exc}", file=sys.stderr)
    sys.exit(1)
PY
) || exit 1

cleanup_intent
trap - EXIT

# ── Launch the game ──────────────────────────────────────────────────────────
# exec replaces this shell with bash running the LAN wrapper. The LAN
# wrapper itself exec's proton run bootstrapper, so the final process
# is Proton running the game. Steam tracks it as the original shortcut.

exec bash "$LAN_WRAPPER"
