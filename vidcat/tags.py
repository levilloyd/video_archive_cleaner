import sqlite3
from collections.abc import Iterable


def _clean(names: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        n = " ".join(n.split())
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def add_tags(conn: sqlite3.Connection, video_id: int, names: Iterable[str]) -> None:
    with conn:
        for name in _clean(names):
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?, ?)", (video_id, tag_id))


def remove_tags(conn: sqlite3.Connection, video_id: int, names: Iterable[str]) -> None:
    with conn:
        for name in _clean(names):
            conn.execute(
                "DELETE FROM video_tags WHERE video_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)",
                (video_id, name),
            )
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM video_tags)")


def tags_for(conn: sqlite3.Connection, video_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {i: [] for i in video_ids}
    if not video_ids:
        return result
    marks = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"SELECT vt.video_id, t.name FROM video_tags vt JOIN tags t ON t.id = vt.tag_id "
        f"WHERE vt.video_id IN ({marks}) ORDER BY t.name COLLATE NOCASE",
        video_ids,
    )
    for r in rows:
        result[r[0]].append(r[1])
    return result


def all_tags(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT t.name, COUNT(v.id) AS count FROM tags t "
        "JOIN video_tags vt ON vt.tag_id = t.id JOIN videos v ON v.id = vt.video_id AND v.missing = 0 "
        "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
    )
    return [dict(r) for r in rows]


def merge_tags(conn: sqlite3.Connection, from_id: int, to_id: int) -> None:
    """Copy all tags from one video onto another (used when a duplicate is removed)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag_id) "
            "SELECT ?, tag_id FROM video_tags WHERE video_id = ?",
            (to_id, from_id),
        )
