import sqlite3
from datetime import datetime

import pytest

from vidcat import db, media, names, scanner


def d(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@pytest.mark.parametrize("name, expected", [
    ("02_19_04 017.mpg", "2004-02-19"),          # month_day_year, the camcorder-export style
    ("03_20_04 020.mpg", "2004-03-20"),
    ("2-19-2004 birthday.mpg", "2004-02-19"),
    ("Party 12.25.99.avi", "1999-12-25"),
    ("25_12_04.mpg", "2004-12-25"),               # day-first only when month-first is impossible
    ("VID_20190704_123456.mp4", "2019-07-04"),
    ("2019-07-04 Fireworks.mov", "2019-07-04"),
    ("Clip 2019_07_04.mov", "2019-07-04"),
])
def test_date_from_filename(name, expected):
    assert d(media.date_from_filename(name)) == expected


@pytest.mark.parametrize("name", [
    "priest_lake 023.mpg", "IMG_4821.MOV", "otterstroms2.mpg", "Wendy's sixtieth B-day Party.MPG",
    "13_45_04.mpg",      # no valid month/day reading
    "02_30_04.mpg",      # Feb 30
    "1_2_3.mpg",         # not a plausible year form
    "123456.mp4",
])
def test_no_date_in_filename(name):
    assert media.date_from_filename(name) is None


def test_date_source_precedence():
    mtime = datetime(2004, 9, 15, 14, 9).timestamp()
    meta = {"format": {"duration": "3", "tags": {"creation_time": "2019-07-04T21:32:10Z"}},
            "streams": [{"codec_type": "video"}]}
    bare = {"format": {"duration": "3"}, "streams": [{"codec_type": "video"}]}

    assert media.parse_probe(meta, "02_19_04 017.mpg", mtime)["date_source"] == "metadata"
    info = media.parse_probe(bare, "02_19_04 017.mpg", mtime)
    assert info["date_source"] == "filename" and d(info["created_at"]) == "2004-02-19"
    info = media.parse_probe(bare, "kids.wmv", mtime)
    assert info["date_source"] == "mtime" and info["created_at"] == int(mtime)
    assert media.parse_probe(None, "kids.wmv", mtime)["date_source"] == "mtime"  # unreadable file


def test_bogus_metadata_date_is_ignored():
    mtime = datetime(2004, 9, 15).timestamp()
    data = {"format": {"tags": {"creation_time": "1970-01-01T00:00:00Z"}}, "streams": [{"codec_type": "video"}]}
    assert media.parse_probe(data, "x.mpg", mtime)["date_source"] == "mtime"


def test_suggestions_do_not_present_a_copy_date_as_capture_date(tmp_path):
    ts = int(datetime(2004, 9, 15, 14, 9).timestamp())
    base = {"path": str(tmp_path / "Otterstroms" / "IMG_1.mpg"), "created_at": ts}

    # mtime-only date + a descriptive folder: leave the date out
    assert names.suggest_name({**base, "date_source": "mtime"}) == "Otterstroms"
    # ...unless an AI caption/folder isn't available, then the date (with time) is the best we have
    bare = {**base, "path": "/Volumes/DCIM/100APPLE/IMG_1.mpg", "date_source": "mtime"}  # nothing descriptive
    assert names.suggest_name(bare) == "2004-09-15 14-09-00"
    # trustworthy dates are used; filename dates carry no time of day
    assert names.suggest_name({**base, "date_source": "metadata"}) == "2004-09-15 Otterstroms"
    assert names.suggest_name({**bare, "date_source": "filename"}) == "2004-09-15"
    assert names.suggest_name({**base, "date_source": "mtime"}, "Wedding Dance") == "Otterstroms - Wedding Dance"


def test_scan_records_date_source(conn, tmp_path, clip_dir, thumbs):
    import shutil
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "b.mp4", lib / "03_20_04 020.mp4")   # no embedded date -> from file name
    shutil.copy(clip_dir / "a.mp4", lib / "with_meta.mp4")      # embedded creation_time
    shutil.copy(clip_dir / "b.mp4", lib / "kids.mp4")           # nothing -> file modified time
    scanner.scan(conn, [lib], thumbs)
    got = {r["name"]: (r["date_source"], d(r["created_at"])) for r in conn.execute("SELECT * FROM videos")}
    assert got["03_20_04 020.mp4"] == ("filename", "2004-03-20")
    assert got["with_meta.mp4"] == ("metadata", "2019-07-04")
    assert got["kids.mp4"][0] == "mtime"


def test_old_catalog_is_migrated_and_refreshed_by_rescan(tmp_path, clip_dir, thumbs):
    """A catalog made before date_source existed gains the column, and the next scan fills it in without
    losing tags/captions or invalidating cached hashes."""
    import shutil
    from vidcat import tags

    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "b.mp4", lib / "03_20_04 020.mp4")
    path = tmp_path / "old.db"

    conn = db.connect(path)
    scanner.scan(conn, [lib], thumbs)
    vid = conn.execute("SELECT id FROM videos").fetchone()["id"]
    tags.add_tags(conn, vid, ["wedding"])
    conn.execute("UPDATE videos SET caption = 'kept', sha256 = 'abc', created_at = 0, date_source = NULL WHERE id = ?", (vid,))
    conn.commit()
    # Simulate the pre-migration schema: drop the column entirely.
    conn.execute("ALTER TABLE videos DROP COLUMN date_source")
    conn.commit()
    conn.close()

    conn = db.connect(path)  # migration adds the column back
    assert "date_source" in {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    s = scanner.scan(conn, [lib], thumbs)
    assert (s.updated, s.added, s.unchanged) == (1, 0, 0)

    row = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    assert (row["date_source"], d(row["created_at"])) == ("filename", "2004-03-20")
    assert row["caption"] == "kept" and row["sha256"] == "abc"  # nothing else was disturbed
    assert tags.tags_for(conn, [vid])[vid] == ["wedding"]
    assert scanner.scan(conn, [lib], thumbs).unchanged == 1  # and it settles


def test_keep_name_forever_survives_a_metadata_refresh(conn, tmp_path, clip_dir, thumbs):
    import shutil
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(clip_dir / "a.mp4", lib / "IMG_0001.mp4")
    scanner.scan(conn, [lib], thumbs)
    conn.execute("UPDATE videos SET name_score = 100, date_source = NULL")  # user chose "keep", old-style row
    conn.commit()
    scanner.scan(conn, [lib], thumbs)
    assert conn.execute("SELECT name_score FROM videos").fetchone()[0] == 100
