"""Thin wrappers around ffprobe/ffmpeg, plus capture-date detection."""
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
            capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
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


_NAME_DATE_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
# 02_19_04, 2-19-2004, 02.19.04: separators are required so bare digit runs aren't misread as dates.
_NAME_DATE_MDY = re.compile(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d{4}|\d{2})(?!\d)")


def _valid_name_date(year: int, month: int, day: int) -> int | None:
    try:
        dt = datetime(year, month, day)
    except ValueError:
        return None
    return int(dt.timestamp()) if 1950 <= year and dt.timestamp() <= time.time() + 86400 else None


def date_from_filename(name: str) -> int | None:
    """Date embedded in a file name: YYYYMMDD / YYYY-MM-DD, or month-first M_D_YY / M_D_YYYY (US style).

    The day-first reading is used only when month-first is impossible (e.g. 25_12_04).
    """
    if m := _NAME_DATE_YMD.search(name):
        if ts := _valid_name_date(int(m[1]), int(m[2]), int(m[3])):
            return ts
    for m in _NAME_DATE_MDY.finditer(name):
        a, b, y = int(m[1]), int(m[2]), m[3]
        month, day = (a, b) if a <= 12 else (b, a)
        if len(y) == 2:
            year = 2000 + int(y) if int(y) <= datetime.now().year % 100 else 1900 + int(y)
        else:
            year = int(y)
        if ts := _valid_name_date(year, month, day):
            return ts
    return None


def parse_probe(data: dict | None, name: str, mtime: float) -> dict:
    """Extract catalog fields from ffprobe output (or defaults if probing failed)."""
    info = {"duration": None, "width": None, "height": None, "codec": None,
            "created_at": None, "date_source": None, "has_video": None}
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
                info["created_at"], info["date_source"] = ts, "metadata"
                break
    if info["created_at"] is None:
        if ts := date_from_filename(name):
            info["created_at"], info["date_source"] = ts, "filename"
        else:  # last resort; often just the day the file was copied
            info["created_at"], info["date_source"] = int(mtime), "mtime"
    return info


def make_thumbnail(path: Path | str, dest: Path, duration: float | None, width: int = 320) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for t in ((duration or 0) * 0.1, 0):
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(dest)],
                capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
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
                ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                capture_output=True, timeout=60, check=True, stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
        except subprocess.SubprocessError:
            continue
        if out.stdout:
            frames.append(out.stdout)
    return frames
