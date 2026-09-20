"""Getting rid of files safely: the Trash where the volume has one, otherwise a recoverable holding folder.

Network shares (SMB, NFS, AFP) usually have no Trash. macOS's trash call can then hang waiting on a hidden
Finder dialog or start a slow cross-volume copy, so we never send those files to the Trash. Instead they are
moved into a folder beside them (an instant rename on the same volume) or, only if the user says so, deleted.
"""
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

NETWORK_FS = {"smbfs", "nfs", "afpfs", "webdav", "cifs"}
TRASH_TIMEOUT = 60.0  # seconds; a Trash operation that takes longer than this is treated as stuck

_MOUNT_LINE = re.compile(r"^.+? on (?P<mp>/.*?) \((?P<fs>[^,)]+)")
_mounts_cache: tuple[tuple[str, str], ...] | None = None


@dataclass
class Disposal:
    path: str
    action: str            # "trashed" | "archived" | "deleted"
    dest: str | None = None  # where an archived file went


def parse_mounts(text: str) -> list[tuple[str, str]]:
    """Parse macOS `mount` output into (mount point, filesystem type) pairs."""
    return [(m["mp"], m["fs"]) for line in text.splitlines() if (m := _MOUNT_LINE.match(line))]


def mounts() -> tuple[tuple[str, str], ...]:
    global _mounts_cache
    if _mounts_cache is None:
        _mounts_cache = ()
        if sys.platform == "darwin":
            try:
                out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
                _mounts_cache = tuple(parse_mounts(out))
            except (OSError, subprocess.SubprocessError):
                pass
    return _mounts_cache


def volume_of(path: Path | str) -> tuple[str, str] | None:
    """(mount point, filesystem type) of the volume holding `path`, or None if unknown."""
    real = os.path.realpath(path)
    best = None
    for mp, fs in mounts():
        if (real == mp or real.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best[0])):
            best = (mp, fs)
    return best


def volume_label(path: Path | str) -> str:
    vol = volume_of(path)
    return f"{vol[0]} ({vol[1]})" if vol else ""


def trash_available(path: Path | str) -> bool:
    """False for network volumes that have no Trash folder. Unknown volumes are assumed to be fine."""
    vol = volume_of(path)
    if vol is None or vol[1] not in NETWORK_FS:
        return True
    trashes = Path(vol[0]) / ".Trashes"
    return trashes.is_dir() and os.access(trashes, os.W_OK)


def archive(path: Path | str, folder: str) -> Path:
    """Move a file into `<its directory>/<folder>/` (same volume, so it's an instant rename). Never overwrites."""
    src = Path(path)
    dest_dir = src.parent / folder
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / src.name
    n = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    os.rename(src, dest)
    return dest


def _trash_with_timeout(path: str, timeout: float) -> None:
    outcome: dict = {}

    def run():
        try:
            send2trash(path)
        except BaseException as e:  # reported to the caller below
            outcome["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(
            f"The Trash didn't respond within {timeout:.0f}s (is a Finder dialog waiting?). "
            f"{Path(path).name} was left where it is."
        )
    if "error" in outcome:
        raise outcome["error"]


def dispose(path: Path | str, mode: str, folder: str) -> Disposal:
    """Remove a file. `mode`: "trash" (Trash, or `folder` if the volume has none), "archive" (always `folder`),
    or "delete" (permanent). Raises OSError (including TimeoutError) if it couldn't be done."""
    if mode not in ("trash", "archive", "delete"):
        raise ValueError(f"unknown mode {mode!r}")
    path = str(path)
    if mode == "delete":
        os.remove(path)
        return Disposal(path, "deleted")
    if mode == "archive" or not trash_available(path):
        return Disposal(path, "archived", str(archive(path, folder)))
    _trash_with_timeout(path, TRASH_TIMEOUT)
    return Disposal(path, "trashed")
