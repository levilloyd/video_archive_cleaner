"""ffmpeg puts a terminal it can read from into raw mode. Several of them running at once can leave it that way
(no echo, Enter shows as ^M). vidcat must never let ffmpeg near the user's terminal, so run each ffmpeg-using
command on a real pseudo-terminal and check the terminal's line settings come back unchanged."""
import os
import pty
import shutil
import sys
import termios

import pytest


def line_settings(fd):
    attrs = termios.tcgetattr(fd)
    return {"ICRNL": bool(attrs[0] & termios.ICRNL), "ECHO": bool(attrs[3] & termios.ECHO),
            "ICANON": bool(attrs[3] & termios.ICANON)}


def run_on_pty(argv):
    """Run argv attached to a pseudo-terminal; return (settings_before, settings_after, exit_status)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    before = line_settings(fd)
    while True:  # drain output so the child never blocks on a full buffer
        try:
            if not os.read(fd, 65536):
                break
        except OSError:
            break
    _, status = os.waitpid(pid, 0)
    return before, line_settings(fd), os.waitstatus_to_exitcode(status)


CLI = [sys.executable, "-c", "from vidcat.cli import app; app()"]


@pytest.fixture
def many_clips(tmp_path, clip_dir):
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(12):  # enough that several thumbnails are being made in parallel
        shutil.copy(clip_dir / "a.mp4", lib / f"clip{i}.mp4")
    return lib


def test_scan_leaves_the_terminal_alone(tmp_path, many_clips):
    before, after, code = run_on_pty([*CLI, "--db", str(tmp_path / "c.db"), "scan", str(many_clips)])
    assert code == 0
    assert before == {"ICRNL": True, "ECHO": True, "ICANON": True}
    assert after == before


def test_transcode_leaves_the_terminal_alone(tmp_path, old_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(3):
        shutil.copy(old_clips / "a.mpg", lib / f"tape{i}.mpg")
    db = str(tmp_path / "c.db")
    assert run_on_pty([*CLI, "--db", db, "scan", str(lib)])[2] == 0
    before, after, code = run_on_pty([*CLI, "--db", db, "transcode", "--preset", "ultrafast", "--originals", "keep"])
    assert code == 0 and (lib / "tape0.mp4").exists()
    assert after == before


def test_frame_grabbing_for_ai_names_leaves_the_terminal_alone(clip_dir):
    code = "import sys; from vidcat import media; assert len(media.extract_frames(sys.argv[1], 1.0)) == 4"
    before, after, status = run_on_pty([sys.executable, "-c", code, str(clip_dir / "a.mp4")])
    assert status == 0
    assert after == before
