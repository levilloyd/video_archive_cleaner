import shutil
import subprocess
from pathlib import Path

import pytest

from vidcat.db import connect


def make_clip(path: Path, seconds: int = 1, pattern: str = "testsrc", creation_time: str | None = None) -> Path:
    """Create a tiny real video with ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{pattern}=size=160x120:rate=10:duration={seconds}"]
    if creation_time:
        cmd += ["-metadata", f"creation_time={creation_time}"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def clip_dir(tmp_path_factory):
    """Two visually different real clips, generated once."""
    d = tmp_path_factory.mktemp("clips")
    make_clip(d / "a.mp4", 1, "testsrc", "2019-07-04T21:32:10Z")
    make_clip(d / "b.mp4", 2, "smptebars")
    return d


@pytest.fixture
def library(tmp_path, clip_dir):
    """A fresh archive folder: a duplicate pair (IMG_0001 / Beach Trip), a different clip, and a nested clip."""
    lib = tmp_path / "Videos" / "Summer Trip"
    lib.mkdir(parents=True)
    shutil.copy(clip_dir / "a.mp4", lib / "IMG_0001.mp4")
    shutil.copy(clip_dir / "a.mp4", lib / "Beach Trip.mp4")
    (lib / "sub").mkdir()
    shutil.copy(clip_dir / "b.mp4", lib / "sub" / "MVI_0002.mp4")
    return tmp_path / "Videos"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def thumbs(tmp_path):
    return tmp_path / "thumbs"


@pytest.fixture(scope="session")
def old_clips(tmp_path_factory):
    """A real MPEG-2 file and a real WMV file, like the ones off an old camcorder/PC."""
    d = tmp_path_factory.mktemp("old")
    src = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    for codecs, name in ((["-c:v", "mpeg2video", "-c:a", "mp2"], "a.mpg"), (["-c:v", "wmv2", "-c:a", "wmav2"], "b.wmv")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *src, *codecs, str(d / name)], check=True)
    return d


@pytest.fixture(scope="session")
def avi_clips(tmp_path_factory):
    """Two kinds of AVI found in home-video archives: widescreen interlaced DV from a tape camcorder, and
    MPEG-4 (Xvid-style) with MP3 audio."""
    d = tmp_path_factory.mktemp("avi")
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30000/1001:duration=1",
                    *audio, "-vf", "tinterlace=mode=interleave_top,setsar=32/27", "-aspect", "16:9",
                    "-target", "ntsc-dv", "-flags", "+ilme+ildct", str(d / "dv.avi")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=25:duration=1",
                    *audio, "-c:v", "mpeg4", "-vtag", "XVID", "-c:a", "libmp3lame", str(d / "xvid.avi")], check=True)
    return d
