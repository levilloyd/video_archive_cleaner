"""Removing files on volumes that have no Trash (e.g. an SMB share), where macOS's trash call can hang."""
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vidcat import cli, config, db, disposal, scanner, tags

runner = CliRunner()

MOUNT_OUTPUT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
//levi@pensieve._smb._tcp.local/Memories on /Volumes/Memories (smbfs, nodev, nosuid, mounted by levi)
/dev/disk4s2 on /Volumes/My Backup (apfs, local, nobrowse)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
"""


@pytest.fixture
def no_trash_volume(monkeypatch, tmp_path):
    """Make everything under tmp_path look like it's on an SMB share without a Trash."""
    monkeypatch.setattr(disposal, "mounts", lambda: (("/", "apfs"), (str(tmp_path.resolve()), "smbfs")))
    monkeypatch.setattr(disposal, "send2trash", lambda p: pytest.fail(f"must not be sent to the Trash: {p}"))
    return tmp_path


# --------------------------------------------------------------------------- disposal module

def test_parse_mounts_reads_real_mount_output():
    mounts = dict(disposal.parse_mounts(MOUNT_OUTPUT))
    assert mounts["/Volumes/Memories"] == "smbfs"
    assert mounts["/Volumes/My Backup"] == "apfs"          # spaces in a mount point
    assert mounts["/"] == "apfs" and mounts["/System/Volumes/Data/home"] == "autofs"


def test_volume_lookup_uses_the_most_specific_mount(monkeypatch):
    monkeypatch.setattr(disposal, "mounts", lambda: tuple(disposal.parse_mounts(MOUNT_OUTPUT)))
    assert disposal.volume_of("/Volumes/Memories/Home Videos/x.mpg") == ("/Volumes/Memories", "smbfs")
    assert disposal.volume_of("/Volumes/Memories") == ("/Volumes/Memories", "smbfs")
    assert disposal.volume_of("/Volumes/Memories2/x.mpg") == ("/", "apfs")     # not a prefix match on the name
    assert disposal.volume_of("/Users/levi/x.mpg") == ("/", "apfs")
    assert disposal.volume_label("/Volumes/Memories/a") == "/Volumes/Memories (smbfs)"


def test_trash_available_rules(monkeypatch, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(disposal, "mounts", lambda: (("/", "apfs"), (str(share.resolve()), "smbfs")))
    f = share / "a.mpg"
    f.write_bytes(b"x")

    assert disposal.trash_available(f) is False                 # network volume, no .Trashes
    (share / ".Trashes").mkdir()
    assert disposal.trash_available(f) is True                  # network volume that does have one
    assert disposal.trash_available(tmp_path / "local.mpg") is True   # ordinary local disk
    monkeypatch.setattr(disposal, "mounts", lambda: ())
    assert disposal.trash_available(f) is True                  # unknown volume: don't get in the way


def test_archive_moves_beside_the_file_and_never_overwrites(tmp_path):
    a = tmp_path / "clip.mpg"
    a.write_bytes(b"one")
    first = disposal.archive(a, "Originals (vidcat)")
    assert first == tmp_path / "Originals (vidcat)" / "clip.mpg" and first.read_bytes() == b"one" and not a.exists()

    a.write_bytes(b"two")   # same name again
    second = disposal.archive(a, "Originals (vidcat)")
    assert second.name == "clip (2).mpg" and second.read_bytes() == b"two" and first.read_bytes() == b"one"


def test_dispose_modes(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(disposal, "send2trash", lambda p: (sent.append(p), os.remove(p)))
    monkeypatch.setattr(disposal, "mounts", lambda: (("/", "apfs"), (str(tmp_path.resolve() / "share"), "smbfs")))
    (tmp_path / "share").mkdir()

    local = tmp_path / "local.mpg"
    local.write_bytes(b"x")
    d = disposal.dispose(local, "trash", "F")
    assert (d.action, d.dest, sent) == ("trashed", None, [str(local)])

    on_share = tmp_path / "share" / "nas.mpg"
    on_share.write_bytes(b"x")
    d = disposal.dispose(on_share, "trash", "F")               # no Trash there: falls back, never calls the OS
    assert d.action == "archived" and Path(d.dest) == tmp_path / "share" / "F" / "nas.mpg" and len(sent) == 1

    forced = tmp_path / "forced.mpg"
    forced.write_bytes(b"x")
    assert disposal.dispose(forced, "archive", "F").action == "archived" and len(sent) == 1   # even with a Trash

    gone = tmp_path / "gone.mpg"
    gone.write_bytes(b"x")
    assert disposal.dispose(gone, "delete", "F").action == "deleted" and not gone.exists()

    with pytest.raises(ValueError):
        disposal.dispose(gone, "shred", "F")


def test_a_stuck_trash_call_times_out_instead_of_hanging(monkeypatch, tmp_path):
    monkeypatch.setattr(disposal, "send2trash", lambda p: time.sleep(5))
    monkeypatch.setattr(disposal, "TRASH_TIMEOUT", 0.2)
    f = tmp_path / "a.mpg"
    f.write_bytes(b"x")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="didn't respond"):
        disposal.dispose(f, "trash", "F")
    assert time.monotonic() - started < 2 and f.exists()


def test_trash_errors_are_reported_not_swallowed(monkeypatch, tmp_path):
    def boom(p):
        raise OSError("Volume doesn't support trash")
    monkeypatch.setattr(disposal, "send2trash", boom)
    f = tmp_path / "a.mpg"
    f.write_bytes(b"x")
    with pytest.raises(OSError, match="support trash"):
        disposal.dispose(f, "trash", "F")
    assert f.exists()


def test_scanner_ignores_the_holding_folders(conn, tmp_path, clip_dir, thumbs):
    lib = tmp_path / "lib"
    for folder in ("", config.ORIGINALS_DIR, config.DUPLICATES_DIR):
        (lib / folder).mkdir(parents=True, exist_ok=True)
        shutil.copy(clip_dir / "a.mp4", lib / folder / f"x{len(folder)}.mp4")
    assert scanner.scan(conn, [lib], thumbs).added == 1


# --------------------------------------------------------------------------- CLI: transcode originals

def old_lib_scanned(tmp_path, old_clips):
    lib = tmp_path / "Old Tapes"
    lib.mkdir()
    shutil.copy(old_clips / "a.mpg", lib / "03_20_04 020.mpg")
    shutil.copy(old_clips / "b.wmv", lib / "kids.wmv")
    db_path = tmp_path / "c.db"
    assert runner.invoke(cli.app, ["--db", str(db_path), "scan", str(lib)]).exit_code == 0
    return lib, db_path


def invoke(db_path, args, input=None):
    return runner.invoke(cli.app, ["--db", str(db_path), *args], input=input)


def interactive(monkeypatch):
    """Pretend stdin is a terminal so the 'ask' prompts run under the test runner."""
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))


def names_in(folder):
    return sorted(p.name for p in folder.iterdir())


def test_transcode_trash_on_a_volume_without_trash_uses_the_holding_folder(no_trash_volume, old_clips):
    lib, db_path = old_lib_scanned(no_trash_volume, old_clips)
    r = invoke(db_path, ["transcode", "--preset", "ultrafast", "--originals", "trash"])
    assert r.exit_code == 0, r.output
    assert "no Trash" in r.output and config.ORIGINALS_DIR in r.output
    assert names_in(lib) == ["03_20_04 020.mp4", config.ORIGINALS_DIR, "kids.mp4"]
    assert names_in(lib / config.ORIGINALS_DIR) == ["03_20_04 020.mpg", "kids.wmv"]

    conn = db.connect(db_path)
    assert sorted(r["name"] for r in conn.execute("SELECT name FROM videos")) == ["03_20_04 020.mp4", "kids.mp4"]
    invoke(db_path, ["scan", str(lib)])   # the holding folder isn't cataloged again
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2


@pytest.mark.parametrize("answers, expected_files, expected_message", [
    ("a\n", ["03_20_04 020.mp4", config.ORIGINALS_DIR, "kids.mp4"], "Some are on a volume with no Trash"),
    ("k\n", ["03_20_04 020.mp4", "03_20_04 020.mpg", "kids.mp4", "kids.wmv"], "Originals kept"),
    ("d\nnope\n", ["03_20_04 020.mp4", "03_20_04 020.mpg", "kids.mp4", "kids.wmv"], "Originals kept"),  # unconfirmed
    ("d\ndelete\n", ["03_20_04 020.mp4", "kids.mp4"], "Permanently deleted 2"),
])
def test_transcode_ask_offers_archive_delete_or_keep(monkeypatch, no_trash_volume, old_clips,
                                                     answers, expected_files, expected_message):
    interactive(monkeypatch)
    lib, db_path = old_lib_scanned(no_trash_volume, old_clips)
    r = invoke(db_path, ["transcode", "--preset", "ultrafast"], input=answers)
    assert r.exit_code == 0, r.output
    assert expected_message in r.output
    assert names_in(lib) == expected_files
    if answers == "a\n":
        assert names_in(lib / config.ORIGINALS_DIR) == ["03_20_04 020.mpg", "kids.wmv"]


def test_transcode_delete_flag_refuses_without_a_terminal(no_trash_volume, old_clips):
    lib, db_path = old_lib_scanned(no_trash_volume, old_clips)
    r = invoke(db_path, ["transcode", "--preset", "ultrafast", "--originals", "delete"])
    assert "interactive terminal" in r.output
    assert names_in(lib) == ["03_20_04 020.mp4", "03_20_04 020.mpg", "kids.mp4", "kids.wmv"]


def test_transcode_archive_flag_works_anywhere(tmp_path, old_clips, monkeypatch):
    lib, db_path = old_lib_scanned(tmp_path, old_clips)
    monkeypatch.setattr(disposal, "send2trash", lambda p: pytest.fail("archive mode must not use the Trash"))
    invoke(db_path, ["transcode", "--preset", "ultrafast", "--originals", "archive"])
    assert names_in(lib / config.ORIGINALS_DIR) == ["03_20_04 020.mpg", "kids.wmv"]


# --------------------------------------------------------------------------- CLI: dupes

def dupes_setup(tmp_path, library):
    db_path = tmp_path / "d.db"
    assert invoke(db_path, ["scan", str(library)]).exit_code == 0
    return db_path, library / "Summer Trip"


def test_dupes_on_a_volume_without_trash_archives(no_trash_volume, library):
    db_path, folder = dupes_setup(no_trash_volume, library)
    r = invoke(db_path, ["dupes"], input="a\n\n")      # choose "archive", then accept the suggested copy to keep
    assert r.exit_code == 0, r.output
    assert "no Trash" in r.output and "freed 0 B" in r.output
    assert names_in(folder / config.DUPLICATES_DIR) == ["IMG_0001.mp4"]
    assert (folder / "Beach Trip.mp4").exists() and not (folder / "IMG_0001.mp4").exists()


def test_dupes_on_a_volume_without_trash_can_delete_permanently(no_trash_volume, library):
    db_path, folder = dupes_setup(no_trash_volume, library)
    r = invoke(db_path, ["dupes"], input="d\ndelete\n\n")
    assert "Permanently deleted 1" in r.output and not (folder / config.DUPLICATES_DIR).exists()
    assert sorted(p.name for p in folder.glob("*.mp4")) == ["Beach Trip.mp4"]


def test_dupes_delete_needs_the_typed_word_and_quit_changes_nothing(no_trash_volume, library):
    db_path, folder = dupes_setup(no_trash_volume, library)
    before = sorted(p.name for p in folder.glob("*.mp4"))
    invoke(db_path, ["dupes"], input="d\nyes please\n")     # wrong confirmation: nothing happens
    invoke(db_path, ["dupes"], input="q\n")
    assert sorted(p.name for p in folder.glob("*.mp4")) == before and not (folder / config.DUPLICATES_DIR).exists()


def test_dupes_dry_run_never_asks_or_touches_anything(no_trash_volume, library):
    db_path, folder = dupes_setup(no_trash_volume, library)
    r = invoke(db_path, ["dupes", "--dry-run"], input="\n")
    assert r.exit_code == 0 and "no Trash" not in r.output
    assert len(list(folder.glob("*.mp4"))) == 2
