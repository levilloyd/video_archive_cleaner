"""Convert old camcorder-era formats (MPEG-1/2, WMV, ASF) to modern MP4."""
import os
import re
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import media

DEFAULT_EXTS = ("mpg", "mpeg", "mpe", "wmv", "asf")
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
CODECS = {
    # name: (ffmpeg encoder, default CRF, extra args)
    "h264": ("libx264", 20, []),
    "hevc": ("libx265", 24, ["-tag:v", "hvc1", "-x265-params", "log-level=error"]),  # hvc1 tag: needed by Apple players
}


class TranscodeError(RuntimeError):
    pass


@dataclass
class Settings:
    codec: str = "h264"          # h264 | hevc
    crf: int | None = None       # None = codec default
    preset: str = "medium"
    deinterlace: str = "auto"    # auto | always | never


_IDET = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)")


def detect_interlaced(src: Path | str, frames: int = 300) -> bool:
    """Ask ffmpeg's idet filter whether most of the first frames are interlaced (typical for camcorder tapes)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(src), "-vf", "idet",
             "-frames:v", str(frames), "-an", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace", timeout=180,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    # ffmpeg prints the summary more than once (an empty one from a probing pass comes first); the last is real.
    matches = _IDET.findall(out.stderr)
    if not matches:
        return False
    tff, bff, prog = map(int, matches[-1])
    total = tff + bff + prog
    return total > 0 and (tff + bff) / total > 0.5


def build_command(src: Path, dst: Path, settings: Settings, deinterlace: bool, creation_time: int | None) -> list[str]:
    encoder, default_crf, extra = CODECS[settings.codec]
    filters = ["yadif=mode=0:parity=-1:deint=0"] if deinterlace else []
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # yuv420p needs even dimensions
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", ",".join(filters),
        "-c:v", encoder, "-preset", settings.preset, "-crf", str(settings.crf or default_crf),
        "-pix_fmt", "yuv420p", *extra,
        "-c:a", "aac", "-b:a", "160k",
        "-map_metadata", "0", "-movflags", "+faststart",
    ]
    if creation_time:
        stamp = datetime.fromtimestamp(creation_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        cmd += ["-metadata", f"creation_time={stamp}"]
    return cmd + ["-progress", "pipe:1", "-nostats", str(dst)]


def encode(cmd: list[str], duration: float | None, on_progress: Callable[[float], None] | None = None) -> None:
    """Run ffmpeg, reporting progress as a 0..1 fraction. Raises TranscodeError with ffmpeg's message on failure."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    except FileNotFoundError as e:
        raise media.MediaToolError("ffmpeg not found on PATH. Install with: brew install ffmpeg") from e
    tail: deque[str] = deque(maxlen=15)
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                value = line.split("=", 1)[1]
                if on_progress and duration and value.isdigit():
                    on_progress(min(int(value) / 1e6 / duration, 1.0))
            elif line and not re.match(r"^[a-z_0-9]+=", line):
                tail.append(line)  # anything that isn't a progress key=value is an error message
        code = proc.wait()
    except BaseException:  # includes Ctrl+C: don't leave ffmpeg running
        proc.kill()
        proc.wait()
        raise
    if code != 0:
        raise TranscodeError("\n".join(tail) or f"ffmpeg exited with status {code}")


def verify_output(path: Path, src_duration: float | None, need_audio: bool) -> str | None:
    """Sanity-check a converted file. Returns a problem description, or None if it looks right."""
    data = media.probe(path)
    if not data:
        return "the output can't be read"
    info = media.parse_probe(data, path.name, 0)
    if not info["has_video"]:
        return "the output has no video"
    if need_audio and not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        return "the audio track is missing from the output"
    duration = info["duration"]
    if not duration:
        return "the output has no length"
    if src_duration and abs(duration - src_duration) > max(1.0, 0.03 * src_duration):
        return f"length differs (original {src_duration:.1f}s, output {duration:.1f}s)"
    return None


def output_path(src: Path) -> Path:
    return src.with_suffix(".mp4")


def convert(
    src: Path,
    settings: Settings,
    *,
    creation_time: int | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[Path, bool]:
    """Convert `src` to a sibling .mp4 next to it. Returns (output_path, encoded).

    `encoded` is False when a matching .mp4 already exists (e.g. from an earlier run) and looks like a
    correct conversion; it is then reused as-is. The output is written to a hidden temp file, verified,
    and only then moved into place, so a failed or interrupted run never leaves a half-written .mp4.
    The original is never touched. `creation_time` is stored in the output's metadata, and the output's
    modified time is set to the original's.
    """
    src_data = media.probe(src)
    if not src_data:
        raise TranscodeError("ffprobe can't read the original")
    src_info = media.parse_probe(src_data, src.name, 0)
    if not src_info["has_video"]:
        raise TranscodeError("the original has no video stream")
    need_audio = any(s.get("codec_type") == "audio" for s in src_data.get("streams", []))
    duration = src_info["duration"]

    dst = output_path(src)
    if dst.exists():
        problem = verify_output(dst, duration, need_audio)
        if problem:
            raise TranscodeError(f"{dst.name} already exists but doesn't match this file ({problem}); left alone")
        return dst, False

    tmp = dst.with_name(f".{dst.stem}.vidcat-tmp.mp4")  # hidden, so scans ignore it
    tmp.unlink(missing_ok=True)
    try:
        if settings.deinterlace == "always":
            deinterlace = True
        elif settings.deinterlace == "never":
            deinterlace = False
        else:
            deinterlace = detect_interlaced(src)
        encode(build_command(src, tmp, settings, deinterlace, creation_time), duration, on_progress)
        problem = verify_output(tmp, duration, need_audio)
        if problem:
            raise TranscodeError(f"verification failed: {problem}")
        st = src.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
        if dst.exists():  # appeared while we were encoding; never overwrite
            raise TranscodeError(f"{dst.name} appeared during conversion; left alone")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst, True
