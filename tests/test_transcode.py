import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vidcat import cli, db, disposal, media, tags, transcode
from vidcat.transcode import Settings, TranscodeError

runner = CliRunner()
FAST = Settings(preset="ultrafast")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture
def old_lib(tmp_path, old_clips):
    lib = tmp_path / "Old Tapes"
    lib.mkdir()
    shutil.copy(old_clips / "a.mpg", lib / "03_20_04 020.mpg")   # date only in the file name
    shutil.copy(old_clips / "b.wmv", lib / "kids.wmv")           # no date anywhere but the file time
    return lib


def streams(path):
    data = media.probe(path)
    return {s["codec_type"]: s for s in data["streams"]}, float(data["format"]["duration"]), data["format"]


def test_convert_produces_verified_h264_aac_mp4(old_lib):
    src = old_lib / "03_20_04 020.mpg"
    ts = int(datetime(2004, 3, 20).timestamp())
    progress = []
    dst, encoded = transcode.convert(src, FAST, creation_time=ts, on_progress=progress.append)

    assert encoded and dst == old_lib / "03_20_04 020.mp4"
    s, duration, fmt = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert (s["video"]["width"], s["video"]["height"]) == (160, 120)
    assert abs(duration - 2.0) < 0.3
    assert fmt["tags"]["creation_time"].startswith("2004-03-2")          # date carried into the file
    assert dst.stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1)  # mtime preserved
    assert src.exists()                                                  # original untouched
    assert not list(old_lib.glob(".*vidcat-tmp*"))                       # no temp leftovers
    assert progress and progress[-1] == pytest.approx(1.0, abs=0.1)


def test_wmv_converts_too(old_lib):
    dst, encoded = transcode.convert(old_lib / "kids.wmv", FAST)
    s, _, fmt = streams(dst)
    assert encoded and s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert "creation_time" not in fmt.get("tags", {})  # nothing trustworthy to record


def test_hevc_option(old_lib):
    dst, _ = transcode.convert(old_lib / "kids.wmv", Settings(codec="hevc", preset="ultrafast"))
    s, _, _ = streams(dst)
    assert s["video"]["codec_name"] == "hevc" and s["video"]["codec_tag_string"] == "hvc1"


def test_rerun_reuses_existing_good_output(old_lib):
    src = old_lib / "kids.wmv"
    dst, _ = transcode.convert(src, FAST)
    before = dst.stat().st_mtime_ns
    dst2, encoded = transcode.convert(src, FAST)
    assert dst2 == dst and encoded is False and dst.stat().st_mtime_ns == before


def test_unrelated_existing_mp4_is_never_overwritten(old_lib, clip_dir):
    src = old_lib / "kids.wmv"
    other = old_lib / "kids.mp4"
    shutil.copy(clip_dir / "b.mp4", other)  # 2s smpte clip: different length? make it clearly different
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=9", "-pix_fmt", "yuv420p", str(other))
    original_bytes = other.read_bytes()
    with pytest.raises(TranscodeError, match="doesn't match"):
        transcode.convert(src, FAST)
    assert other.read_bytes() == original_bytes


def test_unreadable_source_fails_cleanly(tmp_path):
    bad = tmp_path / "broken.mpg"
    bad.write_bytes(b"not video at all")
    with pytest.raises(TranscodeError, match="can't read"):
        transcode.convert(bad, FAST)
    assert not list(tmp_path.glob(".*"))


def test_ffmpeg_failure_leaves_no_partial_file(old_lib, monkeypatch):
    real_build = transcode.build_command
    monkeypatch.setattr(transcode, "build_command",
                        lambda *a, **k: real_build(*a, **k)[:-1] + ["/nonexistent-dir/out.mp4"])
    with pytest.raises(TranscodeError):
        transcode.convert(old_lib / "kids.wmv", FAST)
    assert not (old_lib / "kids.mp4").exists() and not list(old_lib.glob(".*vidcat-tmp*"))


def test_verify_output_catches_truncated_or_silent_results(old_lib):
    dst, _ = transcode.convert(old_lib / "kids.wmv", FAST)
    assert transcode.verify_output(dst, 2.0, need_audio=True) is None
    assert "length differs" in transcode.verify_output(dst, 60.0, need_audio=True)
    silent = old_lib / "silent.mp4"
    ffmpeg("-i", str(dst), "-an", "-c:v", "copy", str(silent))
    assert "audio" in transcode.verify_output(silent, 2.0, need_audio=True)
    assert transcode.verify_output(silent, 2.0, need_audio=False) is None


def test_interlace_detection(tmp_path):
    progressive = tmp_path / "p.mp4"
    interlaced = tmp_path / "i.mpg"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3", "-pix_fmt", "yuv420p", str(progressive))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=50:duration=3", "-vf", "tinterlace=mode=interleave_top",
           "-flags", "+ilme+ildct", "-c:v", "mpeg2video", "-b:v", "4M", str(interlaced))
    assert transcode.detect_interlaced(progressive) is False
    assert transcode.detect_interlaced(interlaced) is True


# --------------------------------------------------------------------------- CLI end to end

def run(db_path, *args):
    result = runner.invoke(cli.app, ["--db", str(db_path), *args])
    return result


def test_cli_dry_run_changes_nothing(tmp_path, old_lib):
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(old_lib)).exit_code == 0
    r = run(db_path, "transcode", "--dry-run")
    assert r.exit_code == 0 and "kids.mp4" in r.output and "Dry run" in r.output
    assert sorted(p.name for p in old_lib.iterdir()) == ["03_20_04 020.mpg", "kids.wmv"]


def test_cli_rejects_bad_options(tmp_path, old_lib):
    db_path = tmp_path / "c.db"
    run(db_path, "scan", str(old_lib))
    assert run(db_path, "transcode", "--codec", "vp9").exit_code == 2
    assert run(db_path, "transcode", "--originals", "shred").exit_code == 2


def test_cli_transcode_then_trash_originals(tmp_path, old_lib, monkeypatch):
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(old_lib)).exit_code == 0
    conn = db.connect(db_path)
    orig = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM videos")}
    tags.add_tags(conn, orig["03_20_04 020.mpg"], ["wedding", "2004"])
    conn.execute("UPDATE videos SET caption = 'Dancing', rotation = 90 WHERE id = ?", (orig["03_20_04 020.mpg"],))
    conn.commit()
    conn.close()

    # 1) Convert, keep originals (non-interactive default for --originals ask).
    r = run(db_path, "transcode", "--preset", "ultrafast")
    assert r.exit_code == 0, r.output
    assert "Converted 2 file(s)" in r.output and "Originals kept" in r.output
    assert sorted(p.name for p in old_lib.iterdir()) == [
        "03_20_04 020.mp4", "03_20_04 020.mpg", "kids.mp4", "kids.wmv"]

    conn = db.connect(db_path)
    new = conn.execute("SELECT * FROM videos WHERE name = '03_20_04 020.mp4'").fetchone()
    assert tags.tags_for(conn, [new["id"]])[new["id"]] == ["2004", "wedding"]   # tags followed
    assert new["caption"] == "Dancing" and new["rotation"] == 90   # same picture, same turn needed
    assert new["date_source"] == "filename"  # still "day only, from the file name", not a made-up midnight
    assert datetime.fromtimestamp(new["created_at"]).strftime("%Y-%m-%d") == "2004-03-20"
    kids_old = conn.execute("SELECT * FROM videos WHERE name = 'kids.wmv'").fetchone()
    kids_new = conn.execute("SELECT * FROM videos WHERE name = 'kids.mp4'").fetchone()
    assert kids_new["date_source"] == "mtime" and abs(kids_new["created_at"] - kids_old["created_at"]) <= 1
    conn.close()

    # 2) Re-run with trash: nothing is re-encoded, originals are trashed and their catalog rows dropped.
    trashed = []
    monkeypatch.setattr(disposal, "send2trash", lambda p: (trashed.append(Path(p).name), os.remove(p)))
    mtimes = {p.name: p.stat().st_mtime_ns for p in old_lib.glob("*.mp4")}
    r = run(db_path, "transcode", "--originals", "trash")
    assert r.exit_code == 0, r.output
    assert sorted(trashed) == ["03_20_04 020.mpg", "kids.wmv"]
    assert {p.name: p.stat().st_mtime_ns for p in old_lib.glob("*.mp4")} == mtimes  # not re-encoded
    assert sorted(p.name for p in old_lib.iterdir()) == ["03_20_04 020.mp4", "kids.mp4"]

    conn = db.connect(db_path)
    assert sorted(r["name"] for r in conn.execute("SELECT name FROM videos")) == ["03_20_04 020.mp4", "kids.mp4"]
    assert tags.tags_for(conn, [new["id"]])[new["id"]] == ["2004", "wedding"]
    conn.close()


def test_cli_never_trashes_original_when_conversion_fails(tmp_path, old_lib, monkeypatch):
    db_path = tmp_path / "c.db"
    run(db_path, "scan", str(old_lib))
    monkeypatch.setattr(disposal, "send2trash", lambda p: pytest.fail("must not trash anything"))
    monkeypatch.setattr(transcode, "encode", lambda *a, **k: (_ for _ in ()).throw(TranscodeError("boom")))
    r = run(db_path, "transcode", "--originals", "trash")
    assert "Skipped" in r.output and "boom" in r.output
    assert sorted(p.name for p in old_lib.iterdir()) == ["03_20_04 020.mpg", "kids.wmv"]
