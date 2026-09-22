"""`vidcat names`: accepting AI suggestions in bulk, and undoing a batch. The vision model is faked."""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vidcat import cli, db, vision

runner = CliRunner()
CAPTIONS = {"IMG_0001.mp4": "Kids Building Sandcastles", "MVI_0002.mp4": "Sunset Over The Pier"}


def invoke(env, args, input=None):
    return runner.invoke(cli.app, ["--db", str(env.db), *args], input=input)


@pytest.fixture
def env(tmp_path, library, monkeypatch):
    """A scanned library with two poorly named videos (IMG_0001, MVI_0002) and one good one (Beach Trip)."""
    db_path = tmp_path / "c.db"
    assert runner.invoke(cli.app, ["--db", str(db_path), "scan", str(library)]).exit_code == 0
    e = SimpleNamespace(db=db_path, folder=library / "Summer Trip", calls=[], failing=set())

    def fake_caption(path, duration, model=None, host=None):
        name = Path(path).name
        e.calls.append(name)
        if name in e.failing:
            raise vision.VisionUnavailable("model unavailable")
        return CAPTIONS[name]

    monkeypatch.setattr(cli.vision, "check_ollama", lambda model, host=None: None)
    monkeypatch.setattr(cli.vision, "caption_video", fake_caption)
    return e


def files(env):
    return sorted(p.name for p in env.folder.rglob("*.mp4"))


ORIGINAL = ["Beach Trip.mp4", "IMG_0001.mp4", "MVI_0002.mp4"]


def test_accept_all_renames_every_poorly_named_video_and_nothing_else(env):
    r = invoke(env, ["names", "--ai", "--accept-all"])
    assert r.exit_code == 0, r.output
    names = files(env)
    assert "Beach Trip.mp4" in names                                   # already fine: untouched
    assert "IMG_0001.mp4" not in names and "MVI_0002.mp4" not in names
    assert any(n.endswith("Kids Building Sandcastles.mp4") for n in names)
    assert any(n.endswith("Sunset Over The Pier.mp4") for n in names)
    assert "vidcat undo-rename --count 2" in r.output                  # how to revert the batch

    conn = db.connect(env.db)
    rows = {row["name"]: row for row in conn.execute("SELECT * FROM videos")}
    assert set(rows) == set(names) and all(Path(row["path"]).exists() for row in rows.values())
    assert {row["caption"] for row in rows.values() if row["caption"]} == set(CAPTIONS.values())  # cached


def test_dry_run_shows_names_but_renames_nothing(env):
    r = invoke(env, ["names", "--ai", "--accept-all", "--dry-run"])
    assert r.exit_code == 0 and r.output.count("would rename") == 2 and "Would rename 2" in r.output
    assert files(env) == ORIGINAL
    assert "undo-rename" not in r.output                               # nothing to undo


def test_option_combinations_that_would_be_dangerous_or_meaningless_are_refused(env):
    assert invoke(env, ["names", "--accept-all", "--all"]).exit_code == 2      # would rename already-good names
    assert invoke(env, ["names", "--dry-run"]).exit_code == 2
    assert files(env) == ORIGINAL


def test_accept_all_stops_if_the_ai_is_unavailable_instead_of_using_plain_names(env, monkeypatch):
    def down(model, host=None):
        raise vision.VisionUnavailable("Can't reach Ollama")
    monkeypatch.setattr(cli.vision, "check_ollama", down)
    r = invoke(env, ["names", "--ai", "--accept-all"])
    assert r.exit_code == 1 and "Nothing was renamed" in r.output
    assert files(env) == ORIGINAL


@pytest.mark.parametrize("failing", ["IMG_0001.mp4", "MVI_0002.mp4"])
def test_a_failed_ai_description_skips_that_video_without_affecting_the_others(env, failing):
    env.failing = {failing}
    r = invoke(env, ["names", "--ai", "--accept-all"])
    assert r.exit_code == 0 and "no AI description" in r.output and "skipped 1" in r.output
    names = files(env)
    assert failing in names                                             # left alone, not given a caption-less name
    other = "MVI_0002.mp4" if failing == "IMG_0001.mp4" else "IMG_0001.mp4"
    assert other not in names and any(n.endswith(f"{CAPTIONS[other]}.mp4") for n in names)   # the rest still worked


def test_accept_all_without_ai_uses_the_date_and_folder_names(env):
    r = invoke(env, ["names", "--accept-all"])
    assert r.exit_code == 0 and env.calls == []                         # the model was never called
    assert "2019-07-04 Summer Trip.mp4" in files(env)


def test_limit_applies_to_a_batch(env):
    r = invoke(env, ["names", "--ai", "--accept-all", "--limit", "1"])
    assert r.exit_code == 0 and len([n for n in files(env) if n in ORIGINAL]) == 2   # only one was renamed


def test_identical_suggestions_do_not_overwrite_each_other(tmp_path, clip_dir, monkeypatch):
    lib = tmp_path / "Trip"
    lib.mkdir()
    for name in ("IMG_1.mp4", "IMG_2.mp4", "IMG_3.mp4"):
        shutil.copy(clip_dir / "a.mp4", lib / name)                     # same date + folder + caption => same name
    db_path = tmp_path / "c.db"
    runner.invoke(cli.app, ["--db", str(db_path), "scan", str(lib)])
    monkeypatch.setattr(cli.vision, "check_ollama", lambda model, host=None: None)
    monkeypatch.setattr(cli.vision, "caption_video", lambda *a, **k: "Waves On The Beach")
    r = runner.invoke(cli.app, ["--db", str(db_path), "names", "--ai", "--accept-all"])
    assert r.exit_code == 0, r.output
    assert sorted(p.name for p in lib.iterdir()) == [
        "2019-07-04 Trip - Waves On The Beach (2).mp4", "2019-07-04 Trip - Waves On The Beach (3).mp4",
        "2019-07-04 Trip - Waves On The Beach.mp4"]


# --------------------------------------------------------------------------- the `a` key while reviewing

def test_pressing_a_accepts_this_and_all_remaining(env):
    r = invoke(env, ["names", "--ai"], input="a\n")                     # one keypress for the whole list
    assert r.exit_code == 0, r.output
    assert "IMG_0001.mp4" not in files(env) and "MVI_0002.mp4" not in files(env)
    assert "undo-rename --count 2" in r.output


def test_a_after_skipping_one_only_covers_the_rest(env):
    r = invoke(env, ["names", "--ai"], input="s\na\n")                  # skip IMG_0001, accept-all from MVI_0002
    assert r.exit_code == 0, r.output
    assert "IMG_0001.mp4" in files(env) and "MVI_0002.mp4" not in files(env)


def test_a_is_refused_when_the_ai_was_requested_but_is_down(env, monkeypatch):
    def down(model, host=None):
        raise vision.VisionUnavailable("Can't reach Ollama")
    monkeypatch.setattr(cli.vision, "check_ollama", down)
    r = invoke(env, ["names", "--ai"], input="a\ns\ns\n")
    assert "'accept all' is off" in r.output
    assert files(env) == ORIGINAL


# --------------------------------------------------------------------------- undoing a batch

def test_undo_rename_count_reverts_the_whole_batch(env):
    invoke(env, ["names", "--ai", "--accept-all"])
    assert files(env) != ORIGINAL
    r = invoke(env, ["undo-rename", "--count", "2"])
    assert r.exit_code == 0 and "Reverted 2 renames" in r.output
    assert files(env) == ORIGINAL
    conn = db.connect(env.db)
    assert sorted(row["name"] for row in conn.execute("SELECT name FROM videos")) == ORIGINAL


def test_undo_rename_stops_cleanly_when_there_is_nothing_left(env):
    invoke(env, ["names", "--ai", "--accept-all"])
    r = invoke(env, ["undo-rename", "--count", "10"])
    assert r.exit_code == 0 and "Nothing more to undo" in r.output and "Reverted 2 renames" in r.output
    assert files(env) == ORIGINAL
    assert "Nothing to undo" in invoke(env, ["undo-rename"]).output


def test_plain_undo_rename_still_reverts_just_one(env):
    invoke(env, ["names", "--ai", "--accept-all"])
    invoke(env, ["undo-rename"])
    assert len([n for n in files(env) if n in ORIGINAL]) == 2           # Beach Trip + one restored original
