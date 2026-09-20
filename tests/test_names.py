import pytest

from vidcat import names
from vidcat.names import name_quality, sanitize_stem


@pytest.mark.parametrize("stem", [
    "IMG_1234", "MVI_0042", "VID_20190704_123456", "PXL_20230704_123456789", "DSC00123", "GOPR0001",
    "20190704_123456", "2019-07-04", "123456", "Untitled", "video", "Video 12", "IMG_1234 copy",
    "WhatsApp Video 2019-07-04 at 12.30.15", "Screen Recording 2020-01-01 at 10.00.00",
    "3f2a9c1e8b7d4a6f9e0c1b2a3d4e5f60", "a3f9bc12-4d5e-4f60-8a1b-2c3d4e5f6a7b", "MOV0123",
])
def test_bad_names(stem):
    assert name_quality(stem) < 50, stem


@pytest.mark.parametrize("stem", [
    "Beach Trip", "Emma's 5th Birthday", "2019-07-04 Fireworks at the Lake", "Grandma Christmas 2015",
    "Graduation", "祖母の誕生日",
])
def test_good_names(stem):
    assert name_quality(stem) >= 50, stem


def test_sanitize():
    assert sanitize_stem('a/b:c*d?"e<f>g|h') == "a b c d e f g h"
    assert sanitize_stem("  ..hello..  ") == "hello"
    assert sanitize_stem("///") == ""


def test_suggest_uses_date_folder_and_caption(tmp_path):
    video = {"path": str(tmp_path / "Beach Trip" / "IMG_1.mov"), "created_at": 1562275930}
    s = names.suggest_name(video)
    assert s.startswith("20") and "Beach Trip" in s
    assert names.suggest_name(video, "Kids Building Sandcastles").endswith("- Kids Building Sandcastles")


def test_suggest_without_folder_adds_time(tmp_path):
    video = {"path": str(tmp_path / "DCIM" / "100APPLE" / "IMG_1.mov"), "created_at": 1562275930}
    s = names.suggest_name(video)
    assert "Beach" not in s and len(s.split()) == 2  # "YYYY-MM-DD HH-MM-SS"
