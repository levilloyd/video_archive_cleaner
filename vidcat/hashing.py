import hashlib
import os
from pathlib import Path

CHUNK = 8 * 1024 * 1024
SAMPLE = 1024 * 1024


def partial_hash(path: Path | str, size: int | None = None) -> str:
    """Cheap fingerprint: file size plus the first, middle, and last MiB. Used to rule out non-duplicates."""
    size = os.path.getsize(path) if size is None else size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        for offset in (0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)):
            f.seek(offset)
            h.update(f.read(SAMPLE))
    return h.hexdigest()


def full_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()
