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


# --------------------------------------------------------------------------- same name, different format

def test_output_names_avoid_collisions_between_formats(tmp_path, old_clips):
    d = tmp_path / "d"
    d.mkdir()
    for name, src in (("clip.mpg", "a.mpg"), ("clip.wmv", "b.wmv"), ("Mix.MPG", "a.mpg"), ("mix.wmv", "b.wmv"),
                      ("solo.mpg", "a.mpg"), ("other.mpg", "a.mpg"), ("other.mp4", "a.mpg")):
        shutil.copy(old_clips / src, d / name)

    assert transcode.output_path(d / "clip.mpg").name == "clip (mpg).mp4"
    assert transcode.output_path(d / "clip.wmv").name == "clip (wmv).mp4"
    assert transcode.output_path(d / "Mix.MPG").name == "Mix (mpg).mp4"      # names compare ignoring case
    assert transcode.output_path(d / "mix.wmv").name == "mix (wmv).mp4"
    assert transcode.output_path(d / "solo.mpg").name == "solo.mp4"          # nothing to clash with: plain name
    assert transcode.output_path(d / "other.mpg").name == "other.mp4"        # an .mp4 sibling isn't a source


def test_cli_converts_same_name_different_format_files_separately(tmp_path, old_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(old_clips / "a.mpg", lib / "tummy.mpg")
    shutil.copy(old_clips / "b.wmv", lib / "tummy.wmv")   # same clip, other encoding: near-identical length
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(lib)).exit_code == 0

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep")
    assert r.exit_code == 0, r.output
    assert "Converted 2 file(s)" in r.output
    assert sorted(p.name for p in lib.iterdir()) == ["tummy (mpg).mp4", "tummy (wmv).mp4", "tummy.mpg", "tummy.wmv"]
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE ext = 'mp4'").fetchone()[0] == 2

    # Each original now has its own output, so removing both originals loses nothing.
    r = run(db_path, "transcode", "--originals", "archive")
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in lib.iterdir() if p.is_file()) == ["tummy (mpg).mp4", "tummy (wmv).mp4"]
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2


def test_cataloging_the_same_path_twice_is_harmless(conn, tmp_path, clip_dir, thumbs):
    from vidcat import scanner
    f = tmp_path / "x.mp4"
    shutil.copy(clip_dir / "a.mp4", f)
    stats = scanner.scan_files(conn, [f, f, tmp_path / "." / "x.mp4"], thumbs)   # three spellings of one file
    assert stats.added == 1 and conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1


# --------------------------------------------------------------------------- AVI (DV and MPEG-4)

def test_avi_is_converted_by_default():
    assert "avi" in transcode.DEFAULT_EXTS


def test_dv_avi_keeps_its_widescreen_shape_and_is_deinterlaced(tmp_path, avi_clips, monkeypatch):
    src = tmp_path / "tape.avi"
    shutil.copy(avi_clips / "dv.avi", src)
    assert transcode.detect_interlaced(src) is True            # tape footage: fields are detected

    commands = []
    real_build = transcode.build_command
    monkeypatch.setattr(transcode, "build_command", lambda *a, **k: commands.append(real_build(*a, **k)) or commands[-1])
    dst, encoded = transcode.convert(src, FAST)
    assert encoded and "yadif" in " ".join(commands[0])        # deinterlacing was applied

    s, duration, _ = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert (s["video"]["width"], s["video"]["height"]) == (720, 480)
    assert s["video"]["display_aspect_ratio"] == "16:9"        # not squashed to 4:3
    assert abs(duration - 1.0) < 0.3
    assert dst.stat().st_size < src.stat().st_size / 5         # DV is ~3.5 MB per second


def test_xvid_avi_is_left_progressive(tmp_path, avi_clips):
    src = tmp_path / "old.avi"
    shutil.copy(avi_clips / "xvid.avi", src)
    assert transcode.detect_interlaced(src) is False
    dst, _ = transcode.convert(src, FAST)
    s, _, _ = streams(dst)
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert s["video"]["display_aspect_ratio"] == "4:3"


def test_cli_picks_up_avi_files_without_asking(tmp_path, avi_clips):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(avi_clips / "dv.avi", lib / "tape one.avi")
    shutil.copy(avi_clips / "xvid.avi", lib / "clip.AVI")
    db_path = tmp_path / "c.db"
    assert run(db_path, "scan", str(lib)).exit_code == 0
    r = run(db_path, "transcode", "--dry-run")
    assert "2 video(s) to convert" in r.output and "tape one.mp4" in r.output and "clip.mp4" in r.output

    r = run(db_path, "transcode", "--preset", "ultrafast", "--originals", "keep")
    assert r.exit_code == 0 and "Converted 2 file(s)" in r.output
    assert sorted(p.name for p in lib.iterdir()) == ["clip.AVI", "clip.mp4", "tape one.avi", "tape one.mp4"]
