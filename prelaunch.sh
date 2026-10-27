#!/bin/bash
# Steam launch option prefix for DeckOps-managed games:
#   bash "<install dir>/prelaunch.sh" <game key> <original command...>
# Updates the game's mod client silently, then starts the game exactly as
# Steam would have. The game always starts, even if the update fails,
# times out or there is no network.

DIR="$(dirname "$(readlink -f "$0")")"
KEY="$1"

# Steam's launch environment (overlay preload, runtime libraries) is meant
# for the game, not for the host Python, so it is dropped for the check only.
timeout 120 env -u LD_PRELOAD -u LD_LIBRARY_PATH -u PYTHONHOME -u PYTHONPATH \
    "$DIR/.venv/bin/python3" "$DIR/src/prelaunch.py" "$@" >/dev/null 2>&1

shift
# Plutonium Offline in the Steam launch menu: the entry's file opens the DeckOps:
# Plutonium Offline launcher (plutonium.write_offline_menu_file). Run here on the host.
for a in "$@"; do
    case "$a" in *plut*_lan.exe) exec bash "$a" ;; esac
done

# Launch menu fallback (plutonium._setup_steam_menu, iw4x): DECKOPS_MENU holds
# "<stock exe>=<online file>" pairs. While our menu is in appinfo.vdf the stock entry
# carries -deckops_vanilla; Steam drops both on a refresh, so an untagged stock exe
# is swapped for its online file, the same command a menu pick would give.
# DECKOPS_MENU_ARGS is added after it (IW4x: --arch x86, like its menu entry).
if [ -n "$DECKOPS_MENU" ]; then
    args=(); tagged=
    for a in "$@"; do
        if [ "$a" = -deckops_vanilla ]; then tagged=1; else args+=("$a"); fi
    done
    [ -n "$tagged" ] && exec "${args[@]}"
    for p in $DECKOPS_MENU; do
        for i in "${!args[@]}"; do
            a=${args[$i]}
            if [ "${a##*/}" = "${p%%=*}" ] && [ -f "${a%/*}/${p#*=}" ]; then
                args[$i]="${a%/*}/${p#*=}"; exec "${args[@]}" $DECKOPS_MENU_ARGS
            fi
        done
    done
fi

# MW3 campaign: cg_fov can't be set from config or console in the 64-bit Steam build,
# so fovwatch.py runs the game as its child and holds the dvar at 90 (see its header).
# It gets Steam's LD_PRELOAD / LD_LIBRARY_PATH only for the game, like the check above.
# The game starts first; if the Python can't run at all, it is started as usual.
if [ "$KEY" = iw5sp ] && [ -x "$DIR/.venv/bin/python3" ] && [ -f "$DIR/src/fovwatch.py" ]; then
    exec env -u LD_PRELOAD -u LD_LIBRARY_PATH -u PYTHONHOME -u PYTHONPATH \
        DECKOPS_LD_PRELOAD="$LD_PRELOAD" DECKOPS_LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
        "$DIR/.venv/bin/python3" "$DIR/src/fovwatch.py" 90 iw5sp.exe -- "$@"
fi
exec "$@"
