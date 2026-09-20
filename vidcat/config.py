import os
from pathlib import Path

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".3gp",
    ".3g2", ".mpg", ".mpeg", ".mpe", ".vob", ".flv", ".webm", ".ogv", ".mod",
    ".tod", ".asf", ".divx", ".dv",
}

# Directories never worth descending into (NAS thumbnail/recycle folders, etc.).
SKIP_DIRS = {"@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information"}

# name_score is a 0-100 "how useful is this filename" score; below this is "bad".
BAD_NAME_THRESHOLD = 50

DEFAULT_VISION_MODEL = "gemma4:e4b"


def db_path() -> Path:
    override = os.environ.get("VIDCAT_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vidcat" / "catalog.db"


def thumbs_dir(db: Path) -> Path:
    return db.parent / "thumbs"


def vision_model() -> str:
    return os.environ.get("VIDCAT_VISION_MODEL", DEFAULT_VISION_MODEL)


def ollama_host() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")
