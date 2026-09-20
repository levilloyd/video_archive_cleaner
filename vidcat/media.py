"""Thin wrappers around ffprobe/ffmpeg."""
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


class MediaToolError(RuntimeError):
    pass


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise MediaToolError(f"{', '.join(missing)} not found on PATH. Install with: brew install ffmpeg")


def probe(path: Path | str) -> dict | None:
    """Run ffprobe. Returns parsed JSON, or None if the file couldn't be probed."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=60, check=True,
        )
        return json.loads(out.stdout)
    except FileNotFoundError as e:
        raise MediaToolError("ffprobe not found on PATH. Install with: brew install ffmpeg") from e
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _parse_date(value: str) -> int | None:
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    ts = int(dt.timestamp())  # naive values are interpreted as local time
    # Cameras with unset clocks produce 1970/2000-ish dates; reject obviously bogus ones.
    if dt.year < 1995 or ts > time.time() + 86400:
        return None
    return ts


_NAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def date_from_filename(name: str) -> int | None:
    m = _NAME_DATE.search(name)
    if not m:
        return None
    try:
        return int(datetime(int(m[1]), int(m[2]), int(m[3])).timestamp())
    except ValueError:
        return None


def parse_probe(data: dict | None, name: str, mtime: float) -> dict:
    """Extract catalog fields from ffprobe output (or defaults if probing failed)."""
    info = {"duration": None, "width": None, "height": None, "codec": None,
            "created_at": None, "has_video": None}
    if data:
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        info["has_video"] = video is not None
        if video:
            info["width"] = video.get("width")
            info["height"] = video.get("height")
            info["codec"] = video.get("codec_name")
        fmt = data.get("format", {})
        try:
            info["duration"] = float(fmt["duration"])
        except (KeyError, TypeError, ValueError):
            pass
        tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
        candidates = [tags.get("com.apple.quicktime.creationdate"), tags.get("creation_time")]
        candidates += [s.get("tags", {}).get("creation_time") for s in streams]
        for c in candidates:
            if c and (ts := _parse_date(c)):
                info["created_at"] = ts
                break
    if info["created_at"] is None:
        info["created_at"] = date_from_filename(name) or int(mtime)
    return info


def make_thumbnail(path: Path | str, dest: Path, duration: float | None, width: int = 320) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for t in ((duration or 0) * 0.1, 0):
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(dest)],
                capture_output=True, timeout=60, check=True,
            )
        except FileNotFoundError as e:
            raise MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
        except subprocess.SubprocessError:
            continue
        if dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def extract_frames(path: Path | str, duration: float | None, count: int = 4, width: int = 512) -> list[bytes]:
    """Grab `count` evenly spaced JPEG frames."""
    frames = []
    for i in range(1, count + 1):
        t = (duration or 0) * i / (count + 1)
        try:
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                capture_output=True, timeout=60, check=True,
            )
        except FileNotFoundError as e:
            raise MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
        except subprocess.SubprocessError:
            continue
        if out.stdout:
            frames.append(out.stdout)
    return frames
