"""Validate and apply DeckOps updates without leaving a half-updated install.

Only the standard library is used: the desktop launcher needs this before the
app's virtual environment is available. A journal and backups in .update let
a later launch undo an interrupted update before running any updated modules.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PROTECTED = {"deckops.json", "VERSION", "assets/music/background.mp3"}
_JOURNAL = "transaction.json"


@contextmanager
def update_lock(install_dir):
    """Prevent another launcher or app instance from touching the update area.

    Linux's flock and the desktop launcher's flock use the same lock file.
    On Windows this is only used by local tests, never by a DeckOps install.
    """
    path = Path(install_dir) / ".deckops_update.lock"
    with open(path, "a+") as stream:
        if os.name == "posix":
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(stream, fcntl.LOCK_UN)


def _path(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError("invalid update path")
    if any(part in ("", ".", "..") for part in value.split("/")) or any(ord(c) < 32 for c in value):
        raise ValueError("unsafe update path")
    if ":" in value.split("/")[0]:
        raise ValueError("unsafe update path")
    if value.startswith("logs/") or value in _PROTECTED or value == ".update" or value.startswith(".update/"):
        raise ValueError("protected update path")
    return value


def compare(data):
    """Yield (M|D, path, blob SHA) from a complete GitHub comparison."""
    files = data.get("files")
    if not isinstance(files, list) or data.get("status") not in ("ahead", "identical"):
        raise ValueError("incomplete GitHub comparison")
    # GitHub caps a comparison at 300 files; use the full archive in that case.
    if len(files) >= 300:
        raise ValueError("GitHub comparison may be truncated")
    for entry in files:
        status = entry.get("status")
        path = _path(entry.get("filename"))
        if status == "removed":
            yield "D", path, "-"
            continue
        if status == "renamed":
            yield "D", _path(entry.get("previous_filename")), "-"
        elif status not in ("added", "modified", "changed", "copied"):
            raise ValueError(f"unknown GitHub change status: {status}")
        sha = entry.get("sha", "")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ValueError("GitHub comparison omitted a file hash")
        yield "M", path, sha


def tree_paths(data):
    """Return file paths from a complete GitHub git-tree response.

    A truncated list could hide files, so it is rejected; the caller then
    leaves obsolete files alone instead of guessing.
    """
    tree = data.get("tree") if isinstance(data, dict) else None
    if not isinstance(tree, list) or data.get("truncated"):
        raise ValueError("incomplete GitHub file list")
    return [e["path"] for e in tree
            if isinstance(e, dict) and e.get("type") == "blob"
            and isinstance(e.get("path"), str) and "\n" not in e["path"]]


def obsolete_files(install_dir, current_files, previous_files):
    """Files an older DeckOps shipped that the current version no longer has.

    Only paths named by the previous version's own file list qualify, so a
    file DeckOps never shipped (settings, logs, music, .venv, user data) is
    never a candidate. Protected, unsafe and symlinked paths are skipped.
    """
    keep = set(current_files)
    found = []
    for relative in dict.fromkeys(previous_files):
        if relative in keep:
            continue
        try:
            _path(relative)
            target = _file(install_dir, relative)
        except ValueError:
            continue
        if target.is_file():
            found.append(relative)
    return sorted(found)


def prune_obsolete(install_dir, stage_dir, previous_files):
    """Remove obsolete files after a plain copy-over install (install.sh).

    Refuses to run unless the staged archive looks like a real DeckOps tree,
    so a broken extraction can never delete the installation.
    """
    stage = Path(stage_dir)
    if not (stage / "src" / "main.py").is_file() or not (stage / "deckops_identity.sh").is_file():
        raise ValueError("archive is missing DeckOps files; nothing removed")
    current = [Path(base, name).relative_to(stage).as_posix()
               for base, _, names in os.walk(stage) for name in names]
    removed = []
    for relative in obsolete_files(install_dir, current, previous_files):
        try:
            _file(install_dir, relative).unlink()
            removed.append(relative)
        except OSError:
            pass
    return removed


def verify(path, sha):
    if not _SHA.fullmatch(sha):
        raise ValueError("invalid blob hash")
    h = hashlib.sha1(b"blob %d\0" % os.path.getsize(path))
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == sha


def _file(root, relative):
    """Reject symlinked parents and destinations, even for removed files."""
    path = Path(root)
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError(f"update path is a symlink: {relative}")
    return path


def _sync_dir(path):
    """Best-effort directory sync for an interrupted update after a power cut."""
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some removable filesystems do not support fsync on directories.
        pass


def _durable_json(path, value):
    tmp = path.with_name(path.name + ".new")
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _replace_file(source, target):
    """Copy to the destination filesystem, then replace without a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".deckops-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, name)
        mode = stat.S_IMODE(os.stat(source).st_mode)
        if target.is_file() and not mode & 0o111:
            mode = stat.S_IMODE(target.stat().st_mode)
        elif not target.exists() and target.suffix == ".sh":
            mode |= 0o111
        os.chmod(name, mode)
        if os.name == "posix":
            with open(name, "rb") as stream:
                os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.remove(name)


def _changes(stage, rows, full, install=None, previous=()):
    if full:
        if not (stage / "src" / "main.py").is_file() or not (stage / "deckops_identity.sh").is_file():
            raise ValueError("full update archive is missing DeckOps files")
        rows = []
        for base, dirs, files in os.walk(stage):
            if any((Path(base) / d).is_symlink() for d in dirs):
                raise ValueError("symlinked directory in update archive")
            for name in files:
                path = Path(base) / name
                relative = path.relative_to(stage).as_posix()
                if relative in _PROTECTED or relative.startswith("logs/"):
                    continue
                rows.append(("M", relative, "-"))
        # A complete archive replaces the installation: also drop files the
        # old version shipped that this one no longer has, as a partial
        # update does. These deletions are journaled like any other change.
        if install is not None and previous:
            rows.extend(("D", relative, "-") for relative in
                        obsolete_files(install, [row[1] for row in rows], previous))
    selected = {}
    for action, relative, sha in rows:
        _path(relative)
        if action not in ("M", "D") or (action == "M" and not (sha == "-" and full or _SHA.fullmatch(sha))):
            raise ValueError("invalid update instruction")
        # A rename may delete an old path and create a new one. Never let a
        # duplicate deletion override an addition to the same path.
        if action == "M" or relative not in selected:
            selected[relative] = (action, sha)
    if not selected and full:
        raise ValueError("update contains no files")
    for relative, (action, sha) in selected.items():
        if action == "M":
            source = _file(stage, relative)
            if not source.is_file() or (sha != "-" and not verify(source, sha)):
                raise ValueError(f"update file missing or corrupt: {relative}")
            if relative.endswith(".py"):
                with open(source, "rb") as stream:
                    compile(stream.read(), relative, "exec")
            elif relative.endswith(".sh") and os.name == "posix":
                subprocess.run(["bash", "-n", str(source)], check=True, capture_output=True)
    return selected


def recover_update(install_dir):
    """Restore all old files if an update was interrupted, including VERSION."""
    install = Path(install_dir)
    update = install / ".update"
    journal = update / _JOURNAL
    if not journal.exists():
        return False
    with open(journal, encoding="utf-8") as stream:
        data = json.load(stream)
    backups = update / "rollback"
    for relative, existed in data["files"]:
        _path(relative)
        target = _file(install, relative)
        if existed:
            source = _file(backups, relative)
            if not source.is_file():
                raise RuntimeError(f"Cannot restore {relative}; rollback copy is missing")
            _replace_file(source, target)
        elif target.is_file():
            target.unlink()
    version = _file(install, "VERSION")
    if data["version_existed"]:
        old = _file(backups, "VERSION")
        if not old.is_file():
            raise RuntimeError("Cannot restore VERSION; rollback copy is missing")
        _replace_file(old, version)
    elif version.is_file():
        version.unlink()
    journal.unlink()
    _sync_dir(update)
    shutil.rmtree(backups, ignore_errors=True)
    return True


def apply_update(install_dir, stage_dir, remote_sha, rows=(), full=False, previous_files=()):
    """Apply staged files and version as one recoverable operation.

    On an exception restore immediately; on a hard crash, recover_update() on
    the next launch reads the journal before any new code is run.
    `previous_files` (full updates only) lists what the installed version
    shipped, so files the new version dropped can be removed.
    """
    if not _SHA.fullmatch(remote_sha):
        raise ValueError("invalid target version")
    install, stage = Path(install_dir), Path(stage_dir)
    update = install / ".update"
    journal = update / _JOURNAL
    if journal.exists():
        raise RuntimeError("unfinished update must be recovered before applying another")
    selected = _changes(stage, rows, full, install, previous_files)
    backups = update / "rollback"
    if backups.exists():
        shutil.rmtree(backups)
    backups.mkdir(parents=True)
    snapshot = []
    version = _file(install, "VERSION")
    for relative in selected:
        target = _file(install, relative)
        if target.exists() and not target.is_file():
            raise ValueError(f"update target is not a file: {relative}")
        existed = target.is_file()
        if existed:
            backup = _file(backups, relative)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            if os.name == "posix":
                with open(backup, "rb") as stream:
                    os.fsync(stream.fileno())
        snapshot.append((relative, existed))
    version_existed = version.is_file()
    if version_existed:
        shutil.copy2(version, backups / "VERSION")
        if os.name == "posix":
            with open(backups / "VERSION", "rb") as stream:
                os.fsync(stream.fileno())
    # Sync backup names before publishing the journal. From here on, any
    # interruption is recoverable, including one after VERSION is changed.
    for base, _, _ in os.walk(backups, topdown=False):
        _sync_dir(base)
    _sync_dir(update)
    _durable_json(journal, {"files": snapshot, "version_existed": version_existed})
    try:
        for relative, (action, _) in selected.items():
            target = _file(install, relative)
            if action == "M":
                _replace_file(_file(stage, relative), target)
            elif target.is_file():
                target.unlink()
        fd, name = tempfile.mkstemp(prefix=".deckops-version-", dir=install)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(remote_sha + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, version)
        finally:
            if os.path.exists(name):
                os.remove(name)
    except Exception:
        recover_update(install_dir)
        raise
    journal.unlink()
    _sync_dir(update)
    shutil.rmtree(backups, ignore_errors=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["compare"]:
            for row in compare(json.load(sys.stdin)):
                print("\t".join(row))
        elif len(sys.argv) == 4 and sys.argv[1] == "verify":
            if not verify(sys.argv[2], sys.argv[3]):
                raise ValueError("download does not match GitHub")
        elif len(sys.argv) == 3 and sys.argv[1] == "recover":
            recover_update(sys.argv[2])
        elif len(sys.argv) == 5 and sys.argv[1] == "prune":
            with open(sys.argv[4], encoding="utf-8") as stream:
                listed = [line.rstrip("\n") for line in stream if line.strip()]
            for path in prune_obsolete(sys.argv[2], sys.argv[3], listed):
                print(path)
        elif sys.argv[1:2] == ["tree"] and len(sys.argv) == 2:
            for path in tree_paths(json.load(sys.stdin)):
                print(path)
        elif len(sys.argv) in (6, 7) and sys.argv[1] == "apply":
            install_dir, staged, sha, changes = sys.argv[2:6]
            if changes == "full":
                previous = []
                if len(sys.argv) == 7 and sys.argv[6] != "none":
                    with open(sys.argv[6], encoding="utf-8") as stream:
                        previous = [line.rstrip("\n") for line in stream if line.strip()]
                apply_update(install_dir, staged, sha, full=True, previous_files=previous)
            else:
                with open(changes, encoding="utf-8") as stream:
                    rows = [line.rstrip("\n").split("\t") for line in stream]
                apply_update(install_dir, staged, sha, rows)
        else:
            raise ValueError("invalid update command")
    except (OSError, ValueError, IndexError, KeyError, TypeError, RuntimeError,
            SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"DeckOps update check failed: {exc}", file=sys.stderr)
        sys.exit(1)
