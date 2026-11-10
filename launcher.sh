#!/bin/bash
# launcher.sh — DeckOps entry point

# Source deckops_identity.sh if available, otherwise fallback.
# Only ever load THIS branch's identity -- never the other branch's install
# dir, or the nightly launcher would manage/install stable (see install.sh).
EXPECTED_BRANCH="nightly"
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
BRANCH=""
# Only a sibling that is part of a DeckOps tree we own. The first-run desktop
# file used to run this script from /tmp, where anyone can drop a
# deckops_identity.sh for it to source.
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/deckops_identity.sh" ] && \
   [ -O "$SCRIPT_DIR/deckops_identity.sh" ] && [ -f "$SCRIPT_DIR/src/identity.py" ]; then
    source "$SCRIPT_DIR/deckops_identity.sh"
elif [ -f "$HOME/DeckOps-Nightly/deckops_identity.sh" ]; then
    source "$HOME/DeckOps-Nightly/deckops_identity.sh"
fi
if [ "$BRANCH" != "$EXPECTED_BRANCH" ]; then
    INSTALL_DIR="$HOME/DeckOps-Nightly"
    VENV_PYTHON="$INSTALL_DIR/.venv/bin/python3"
    ENTRY_POINT="$INSTALL_DIR/src/main.py"
    APP_TITLE="DeckOps Nightly"
fi
# This script's own repo always wins. Launched from the desktop file the script
# runs from /tmp, so the identity comes from ~/DeckOps-Nightly; on an install
# made from upstream (or from before the fork) that file has the same BRANCH but
# another GITHUB_USER, and updates and reinstalls kept coming from that repo.
GITHUB_USER="smeltzzz"
GITHUB_REPO="DeckOps-Nightly"

GITHUB_RAW="https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/main"
# Named after this install: Stable and Nightly can be installed side by side, and a
# shared marker made one branch's launcher finish (and then clear) the other's
# interrupted install.
LOCKFILE="$HOME/.$(basename "$INSTALL_DIR")_installing"
VERSION_FILE="$INSTALL_DIR/VERSION"
UPDATE_DIR="$INSTALL_DIR/.update"

# The installed desktop icon runs `launcher.sh --desktop`: open DeckOps right
# away, without the menu or the update prompt (DeckOps checks for updates
# itself). It used to start the venv's python directly, so a venv broken by a
# SteamOS Python update did nothing at all and could not be repaired from it.
DESKTOP_MODE=false
[ "${1:-}" = "--desktop" ] && DESKTOP_MODE=true

venv_ok() {
    "$INSTALL_DIR/.venv/bin/python3" -c "import PyQt6.QtWidgets" &>/dev/null \
        || "$INSTALL_DIR/.venv/bin/python3" -c "import PyQt5.QtWidgets" &>/dev/null
}

# The icon has no terminal window. An install or repair shows its progress in
# one, so open a terminal that runs this launcher again.
if $DESKTOP_MODE && [ ! -t 1 ] && \
   { [ ! -d "$INSTALL_DIR" ] || [ -f "$LOCKFILE" ] || ! venv_ok; }; then
    zenity --info --title="$APP_TITLE" --width=380 \
        --text="DeckOps needs to repair itself (a SteamOS update can cause this).\n\nA terminal window opens and shows the progress. It can take several minutes; do not close it. DeckOps opens when it is done." \
        2>/dev/null
    SELF="${BASH_SOURCE[0]}"
    for term in konsole gnome-terminal xterm; do
        command -v "$term" >/dev/null 2>&1 || continue
        case "$term" in
            gnome-terminal) "$term" --wait -- bash "$SELF" --desktop ;;
            *)              "$term" -e bash "$SELF" --desktop ;;
        esac
        exit 0
    done
    zenity --error --title="$APP_TITLE" --width=380 \
        --text="DeckOps needs to repair itself, but no terminal program was found.\n\nOpen a terminal (Konsole) and run:\nbash ~/$(basename "$INSTALL_DIR")/launcher.sh" \
        2>/dev/null
    exit 1
fi

# An install or repair started from the icon runs in its own terminal window,
# which closes when it ends: say so if it failed.
repair_failed() {
    $DESKTOP_MODE || return 0
    zenity --error --title="$APP_TITLE" --width=380 \
        --text="DeckOps could not finish repairing itself.\n\nCheck your internet connection, then open DeckOps again to retry. If your connection works, GitHub may be limiting downloads; try again in about an hour." \
        2>/dev/null
}

# Download one of this repo's scripts to a file, then run it. `curl | bash`
# starts running whatever has arrived, so a connection that dropped part way
# used to run half of the installer or uninstaller. Here curl must finish
# (-f also turns an HTTP error such as a rate limit into a failure instead of
# running the error page) before anything executes. It runs from its own
# empty folder so the identity lookup behaves as it did under `curl | bash`
# and never picks up a stray deckops_identity.sh from /tmp.
run_remote_script() {
    local name="$1" dir rc
    dir="$(mktemp -d /tmp/deckops_script_XXXXXX)" || return 1
    if ! curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 \
            -o "$dir/$name" "$GITHUB_RAW/$name"; then
        rm -rf "$dir"
        echo "DeckOps: could not download $name. Check your internet connection, or try again in an hour (GitHub may be limiting downloads)." >&2
        zenity --error --title="$APP_TITLE" \
            --text="Could not download $name.\nCheck your internet connection and try again.\n\nIf your connection works, GitHub may be limiting downloads for a while; try again in about an hour." \
            2>/dev/null
        return 1
    fi
    bash "$dir/$name" 9>&-
    rc=$?
    rm -rf "$dir"
    return $rc
}

# ── Installing or first-time — run install directly ───────────────────────────
if [ ! -d "$INSTALL_DIR" ] || [ -f "$LOCKFILE" ]; then
    touch "$LOCKFILE"
    # Keep the marker if the install failed or was cut off, so the next launch
    # finishes it instead of treating a half-copied install as complete.
    if run_remote_script install.sh; then
        rm -f "$LOCKFILE"
        exit 0
    fi
    repair_failed
    exit 1
fi

# A running DeckOps update owns .update. A second launch must not delete its
# staging area or mistake its live journal for an interrupted update.
# A lock file left root-owned (a `sudo` run) can't be opened for writing; flock works
# on a read-only descriptor too. With neither, carry on unlocked as without flock: the
# old `|| exit 1` closed the launcher without a word on every start.
#
# The lock is held only while files are being changed: restoring an interrupted
# update, staging and applying one, uninstalling. Never while a menu, question
# or "done" message waits on the player: held through those, every other launch,
# DeckOps itself and the game pre-launch update thought an update was running.
UPDATE_LOCKED=false
LOCK_FILE="$INSTALL_DIR/.deckops_update.lock"
# Fails only when another process holds the lock.
take_update_lock() {
    $UPDATE_LOCKED && return 0
    command -v flock >/dev/null 2>&1 || return 0
    if { exec 9> "$LOCK_FILE"; } 2>/dev/null || { exec 9< "$LOCK_FILE"; } 2>/dev/null; then
        if ! flock -n 9; then
            exec 9>&-
            return 1
        fi
        UPDATE_LOCKED=true
    else
        echo "DeckOps: could not open $LOCK_FILE; continuing without the update lock." >&2
    fi
    return 0
}
updating_elsewhere() {
    zenity --warning --title="$APP_TITLE" \
        --text="DeckOps is updating in another window. Wait for it to finish before launching again." 2>/dev/null
}
take_update_lock || { updating_elsewhere; exit 1; }

# DeckOps checks this lock itself when it starts, so the launcher lets go of it
# before anything that starts DeckOps.
release_update_lock() {
    if $UPDATE_LOCKED; then
        flock -u 9
        exec 9>&-
        UPDATE_LOCKED=false
    fi
}

# Roll back an update interrupted by a crash or power loss. The caller holds the
# update lock. Fails only when the journal is there and could not be undone.
ROLLED_BACK=false
recover_interrupted_update() {
    [ -f "$UPDATE_DIR/transaction.json" ] || return 0
    local script="$INSTALL_DIR/src/update_manifest.py"
    [ -f "$script" ] || script="$UPDATE_DIR/files/src/update_manifest.py"
    if [ ! -f "$script" ] || ! python3 "$script" recover "$INSTALL_DIR"; then
        return 1
    fi
    ROLLED_BACK=true
    return 0
}

# Before reading its version or starting the app. Never erase .update while its
# journal is live.
if ! recover_interrupted_update; then
    release_update_lock
    zenity --error --title="$APP_TITLE" \
        --text="An interrupted update could not be restored. Your files were kept. Please contact DeckOps support before reinstalling." 2>/dev/null
    exit 1
fi
release_update_lock
if $ROLLED_BACK; then
    zenity --info --title="$APP_TITLE" \
        --text="An interrupted update was rolled back. Your previous DeckOps version is ready; you can retry the update later." 2>/dev/null
fi

# DeckOps holds this lock while it is open (main.py). An update applied under a
# running DeckOps would leave it running old modules against new files, and it
# may be updating itself right now.
INSTANCE_LOCK="$INSTALL_DIR/.deckops_instance.lock"
deckops_running() {
    command -v flock >/dev/null 2>&1 || return 1
    [ -f "$INSTANCE_LOCK" ] || return 1
    flock -n -E 75 "$INSTANCE_LOCK" true 2>/dev/null
    [ $? -eq 75 ]
}

# ── Update check ──────────────────────────────────────────────────────────────
# Stage and apply the update check_for_updates found. The caller holds the update
# lock and shows the outcome after letting go of it; UPDATE_RESULT says which.
# Uses check_for_updates' locals (LOCAL_SHA, REMOTE_SHA, PARSED, PREVIOUS_SHA...).
stage_and_apply_update() {
    UPDATE_RESULT=""

    # Another launcher may have stopped part way while this one was asking:
    # restore first, never stage over a live journal.
    if ! recover_interrupted_update; then
        UPDATE_RESULT="unrecovered"
        return
    fi
    # The files changed while the question was open (another window updated):
    # what was compared no longer describes the installation.
    local NOW_SHA="0"
    [ -f "$VERSION_FILE" ] && NOW_SHA=$(tr -d '[:space:]' < "$VERSION_FILE")
    if [ "$NOW_SHA" != "$PREVIOUS_SHA" ]; then
        UPDATE_RESULT="skipped"
        return
    fi

    # ── Download changed files to staging dir ─────────────────────────────
    rm -rf "$UPDATE_DIR"
    mkdir -p "$UPDATE_DIR"

    local FAILED=0

    if [ "$LOCAL_SHA" = "0" ]; then
        # No compare available — download full repo zip
        local TMPZIP
        TMPZIP="$(mktemp /tmp/deckops_update_XXXXXX.zip)"
        # The commit itself, not main: it is the version recorded below
        # -f: an HTTP error must fail here, not be saved as the "zip"
        # A fixed 120 s cap failed every time on a slow connection (the archive is tens
        # of MB). Give up on a stalled transfer instead: under 1 KB/s for 30 s.
        curl -fsL --connect-timeout 15 --speed-limit 1024 --speed-time 30 --max-time 1800 \
            "https://github.com/$GITHUB_USER/$GITHUB_REPO/archive/$REMOTE_SHA.zip" \
            -o "$TMPZIP"

        if [ $? -ne 0 ]; then
            rm -f "$TMPZIP"
            rm -rf "$UPDATE_DIR"
            UPDATE_RESULT="download"
            return
        fi

        local TMPDIR_EXTRACT
        TMPDIR_EXTRACT="$(mktemp -d /tmp/deckops_extract_XXXXXX)"
        # A zip that won't open must not fall through to "update complete"
        # and a new VERSION with nothing installed.
        if ! unzip -qq "$TMPZIP" -d "$TMPDIR_EXTRACT"; then
            rm -f "$TMPZIP"
            rm -rf "$TMPDIR_EXTRACT" "$UPDATE_DIR"
            UPDATE_RESULT="download"
            return
        fi
        rm -f "$TMPZIP"

        local EXTRACTED
        EXTRACTED=$(find "$TMPDIR_EXTRACT" -maxdepth 1 -mindepth 1 -type d | head -1)
        [ -z "$EXTRACTED" ] && EXTRACTED="$TMPDIR_EXTRACT"

        mkdir -p "$UPDATE_DIR/files"
        cp -r "$EXTRACTED"/. "$UPDATE_DIR/files"/ || FAILED=1
        rm -rf "$TMPDIR_EXTRACT"
    else
        # The comparison identifies deletions explicitly. A raw-file 404 is a
        # failed download, never evidence that a local file should be deleted.
        while IFS=$'\t' read -r action filepath blob_sha; do
            [ "$action" = "M" ] || continue
            case "$filepath" in
                logs/*|deckops.json|VERSION|assets/music/background.mp3) continue ;;
            esac
            mkdir -p "$UPDATE_DIR/files/$(dirname "$filepath")"
            # Percent-encoded: a name with a space, #, ? or % would otherwise fail the update
            ENCODED_PATH=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/"))' "$filepath") \
                || { FAILED=1; break; }
            # Stall limits as for the full archive: a fixed 120 s cap failed a large
            # file (the offline launcher, music) on every slow connection.
            if ! curl -fLsS --retry 2 --connect-timeout 15 --speed-limit 1024 --speed-time 30 --max-time 1800 \
                "https://raw.githubusercontent.com/$GITHUB_USER/$GITHUB_REPO/$REMOTE_SHA/$ENCODED_PATH" \
                -o "$UPDATE_DIR/files/$filepath" || \
               ! python3 "$INSTALL_DIR/src/update_manifest.py" verify \
                "$UPDATE_DIR/files/$filepath" "$blob_sha"; then
                FAILED=1
                break
            fi
        done < <(printf '%s\n' "$PARSED")
    fi

    if [ "$FAILED" -ne 0 ]; then
        rm -rf "$UPDATE_DIR"
        UPDATE_RESULT="download"
        return
    fi

    # ── Apply all staged files as one recoverable transaction ────────────
    # Both a full archive and a partial update back up every replaced file
    # before writing a journal. If the process dies, the next launch rolls
    # back before reading VERSION or running any updated Python code.
    local APPLY_SCRIPT="$UPDATE_DIR/files/src/update_manifest.py"
    local CHANGES="full"
    local APPLY_EXTRA=()
    if [ "$LOCAL_SHA" != "0" ]; then
        APPLY_SCRIPT="$INSTALL_DIR/src/update_manifest.py"
        CHANGES="$UPDATE_DIR/changes.tsv"
        # Empty when only VERSION or player files changed: then only the new
        # version is recorded. No blank line, which older parsers rejected.
        if [ -n "$PARSED" ]; then
            printf '%s\n' "$PARSED" > "$CHANGES"
        else
            : > "$CHANGES"
        fi
    else
        # A full archive cannot say what was removed. Ask GitHub what the
        # installed commit shipped, so files the new version dropped do not
        # linger. Without a complete list nothing is removed.
        if [[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] && [ -f "$APPLY_SCRIPT" ] && \
           (set -o pipefail; curl -sf --max-time 20 \
                "https://api.github.com/repos/$GITHUB_USER/$GITHUB_REPO/git/trees/${PREVIOUS_SHA}?recursive=1" \
                | python3 "$APPLY_SCRIPT" tree > "$UPDATE_DIR/previous_files.txt" 2>/dev/null); then
            PREVIOUS_FILES="$UPDATE_DIR/previous_files.txt"
        fi
        APPLY_EXTRA=("$PREVIOUS_FILES")
    fi
    if [ ! -f "$APPLY_SCRIPT" ] || \
       ! python3 "$APPLY_SCRIPT" apply "$INSTALL_DIR" "$UPDATE_DIR/files" "$REMOTE_SHA" "$CHANGES" "${APPLY_EXTRA[@]}"; then
        # A journal left behind means the update stopped part way (the apply
        # process was killed, or its own rollback failed). Roll back now, the
        # same way the next launch would, and say what actually happened.
        if [ -f "$UPDATE_DIR/transaction.json" ]; then
            local RECOVER="$INSTALL_DIR/src/update_manifest.py"
            [ -f "$RECOVER" ] || RECOVER="$UPDATE_DIR/files/src/update_manifest.py"
            if [ -f "$RECOVER" ] && python3 "$RECOVER" recover "$INSTALL_DIR" \
               && [ ! -f "$UPDATE_DIR/transaction.json" ]; then
                rm -rf "$UPDATE_DIR"
                UPDATE_RESULT="restored"
                return
            fi
            # Never run a possibly mixed installation; the journal stays for the next try
            UPDATE_RESULT="stuck"
            return
        fi
        rm -rf "$UPDATE_DIR"
        UPDATE_RESULT="restored"
        return
    fi
    rm -rf "$UPDATE_DIR"

    # Make scripts executable
    chmod +x "$INSTALL_DIR/launcher.sh" 2>/dev/null
    chmod +x "$INSTALL_DIR/deckops_uninstall.sh" 2>/dev/null
    chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null
    UPDATE_RESULT="ok"
}

check_for_updates() {
    local LOCAL_SHA REMOTE_SHA CHANGED_FILES PARSED="" FILE_COUNT RESET_HINT="" UPDATE_TEXT=""
    local COMPARED=false
    UPDATE_RESULT=""

    # DeckOps is open (maybe updating itself; it shows available updates on its
    # own). Replacing its files under it is not safe: launch, and main.py says it
    # is already open.
    if deckops_running; then
        echo "DeckOps: DeckOps is already open; close it to update from here." >&2
        return 0
    fi

    # Read local version (commit SHA). "0" means never updated.
    LOCAL_SHA="0"
    [ -f "$VERSION_FILE" ] && LOCAL_SHA=$(cat "$VERSION_FILE" | tr -d '[:space:]')
    # LOCAL_SHA becomes "0" when a full archive is needed; the installed
    # commit is still needed to find files the new version no longer ships.
    local PREVIOUS_SHA="$LOCAL_SHA" PREVIOUS_FILES="none"

    # Fetch latest commit SHA from GitHub API (unauthenticated, 60 req/hr)
    REMOTE_SHA=$(curl -sf --max-time 10 \
        "https://api.github.com/repos/$GITHUB_USER/$GITHUB_REPO/commits/main" \
        | grep -m1 '"sha"' | cut -d'"' -f4)

    # If fetch failed or same version, skip
    [[ "$REMOTE_SHA" =~ ^[0-9a-f]{40}$ ]] || return 0
    [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && return 0

    # Older installs have no parser, and GitHub comparisons can fail or be
    # truncated. In either case a pinned full archive is safer than guessing.
    # Success is the exit status: an empty list is a valid comparison.
    if [[ "$LOCAL_SHA" =~ ^[0-9a-f]{40}$ ]] && \
       [ -f "$INSTALL_DIR/src/update_manifest.py" ]; then
        if PARSED=$(set -o pipefail; curl -sf --max-time 15 \
                "https://api.github.com/repos/$GITHUB_USER/$GITHUB_REPO/compare/${LOCAL_SHA}...${REMOTE_SHA}" \
                | python3 "$INSTALL_DIR/src/update_manifest.py" compare); then
            COMPARED=true
        else
            PARSED=""
        fi
    fi
    if ! $COMPARED; then
        LOCAL_SHA="0"
        UPDATE_TEXT="An update is available. DeckOps needs to refresh its files."
        CHANGED_FILES=""
    elif [ -z "$PARSED" ]; then
        # Nothing DeckOps installs changed (only VERSION or player files): no
        # download and nothing to ask, just record the new version. Another
        # window holding the lock is updating anyway.
        take_update_lock || return 0
        stage_and_apply_update
        release_update_lock
        case "$UPDATE_RESULT" in
            unrecovered|stuck) ;;
            *) return 0 ;;
        esac
    else
        CHANGED_FILES=$(printf '%s\n' "$PARSED" | awk -F '\t' '$1 == "M" {print $2}')
        FILE_COUNT=$(printf '%s\n' "$PARSED" | grep -c .)
        UPDATE_TEXT="An update is available. ${FILE_COUNT} file(s) changed."
        # Mirrors ui_manage._reset_hint: config.py or more than 3 src/ files changed
        if echo "$CHANGED_FILES" | grep -qx 'src/config.py' || \
           [ "$(echo "$CHANGED_FILES" | grep -c '^src/')" -gt 3 ]; then
            RESET_HINT="\n\nThis update changed how DeckOps sets up games.\nIf anything acts up, open Settings and use Reset Config,\nthen set up your games again."
        fi
    fi

    if [ -z "$UPDATE_RESULT" ]; then
        # Ask the player. No zenity means no way to ask: zenity's "command not found" (127)
        # used to read as the player choosing Skip, so such systems silently never updated.
        if ! command -v zenity >/dev/null 2>&1; then
            echo "DeckOps: an update is available. Use Check for Updates inside DeckOps to install it." >&2
            return 0
        fi
        # Asked without the update lock: an open question must not block anything else.
        zenity --question \
            --title="$APP_TITLE Update" \
            --text="${UPDATE_TEXT}\n\nUpdate now?" \
            --ok-label="Update" \
            --cancel-label="Skip" \
            --width=300 \
            2>/dev/null

        [ $? -ne 0 ] && return 0

        if ! take_update_lock; then
            updating_elsewhere
            return 1
        fi
        stage_and_apply_update
        release_update_lock
    fi

    # The lock is released: these messages wait on the player, nothing else does.
    case "$UPDATE_RESULT" in
        ok)
            zenity --info --title="$APP_TITLE" \
                --text="Update complete!${RESET_HINT}" \
                --width=300 \
                2>/dev/null
            ;;
        download)
            zenity --error --title="$APP_TITLE" \
                --text="Update download failed.\nContinuing with current version." \
                2>/dev/null
            ;;
        restored)
            zenity --error --title="$APP_TITLE" \
                --text="Update could not be installed. Your previous version was restored; please retry later or use Check for Updates in DeckOps." \
                2>/dev/null
            ;;
        stuck)
            zenity --error --title="$APP_TITLE" \
                --text="Update was interrupted and DeckOps could not restore its previous files yet. Your files were kept.\n\nOpen DeckOps again to retry the restore. If this message keeps appearing, please contact support." 2>/dev/null
            return 1
            ;;
        unrecovered)
            zenity --error --title="$APP_TITLE" \
                --text="An interrupted update could not be restored. Your files were kept. Please contact DeckOps support before reinstalling." 2>/dev/null
            return 1
            ;;
    esac
    return 0
}

# ── Already installed — ask what to do ───────────────────────────────────────
if $DESKTOP_MODE; then
    choice="Launch DeckOps"
elif command -v zenity >/dev/null 2>&1; then
    choice=$(zenity \
        --list \
        --title="$APP_TITLE" \
        --text="$APP_TITLE is already installed.\nWhat would you like to do?" \
        --column="Action" \
        --hide-header \
        "Launch DeckOps" \
        "Uninstall" \
        --width=300 --height=200 \
        2>/dev/null) || exit 0
    # OK with nothing highlighted used to close the launcher without a word.
    # Some zenity versions print a double-clicked row twice ("a|a").
    choice="${choice%%|*}"
    [ -z "$choice" ] && choice="Launch DeckOps"
else
    # Some supported desktops do not ship zenity. They should still launch.
    choice="Launch DeckOps"
fi

case "$choice" in
    "Launch DeckOps")
        # Repair only when neither Qt version can launch the app. A working
        # PyQt5 install must not require network access just to open DeckOps.
        if ! venv_ok; then
            # From the icon the player was already told, before the terminal opened
            $DESKTOP_MODE || zenity --info --title="$APP_TITLE Update" --width=360 \
                --text="DeckOps needs to repair its window components.\n\nIt downloads in the terminal window and can take several minutes.\n\nDo not close the terminal. DeckOps opens when it is done." \
                2>/dev/null
            touch "$LOCKFILE"
            # install.sh starts DeckOps just before it ends; DeckOps must not find the
            # launcher still holding the update lock
            release_update_lock
            if run_remote_script install.sh; then
                rm -f "$LOCKFILE"
                exit 0
            fi
            repair_failed
            exit 1
        fi

        # Check for updates before launching; do not launch if rollback failed.
        # The icon skips the prompt: DeckOps itself shows when one is available.
        if ! $DESKTOP_MODE; then
            check_for_updates || exit 1
        fi
        # Release the launcher lock before main.py checks for a running update.
        release_update_lock

        VENV_PYTHON="$INSTALL_DIR/.venv/bin/python3"
        ENTRY_POINT="$INSTALL_DIR/src/main.py"
        if [ -f "$VENV_PYTHON" ] && [ -f "$ENTRY_POINT" ]; then
            setsid nohup "$VENV_PYTHON" "$ENTRY_POINT" > /dev/null 2>&1 &
            disown
            exit 0
        else
            zenity --error --title="$APP_TITLE" \
                --text="$APP_TITLE installation appears incomplete.\nTry reinstalling." \
                2>/dev/null
        fi
        ;;
    "Uninstall")
        # Not while an update writes into the install. The uninstaller inherits
        # the lock (fd 9) and holds it until it is done.
        take_update_lock || { updating_elsewhere; exit 1; }
        if [ -f "$INSTALL_DIR/deckops_uninstall.sh" ]; then
            bash "$INSTALL_DIR/deckops_uninstall.sh"
        else
            run_remote_script deckops_uninstall.sh
        fi
        exit 0
        ;;
esac
