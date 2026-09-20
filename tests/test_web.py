import pytest
from fastapi.testclient import TestClient

from vidcat import scanner
from vidcat.db import connect
from vidcat.web.app import create_app


@pytest.fixture
def client(tmp_path, library):
    db = tmp_path / "web.db"
    conn = connect(db)
    scanner.scan(conn, [library], tmp_path / "thumbs")
    conn.close()
    with TestClient(create_app(db)) as c:
        yield c


def test_list_search_sort_and_filters(client):
    data = client.get("/api/videos").json()
    assert data["total"] == 3 and len(data["items"]) == 3
    assert client.get("/api/videos", params={"q": "beach"}).json()["total"] == 1
    assert client.get("/api/videos", params={"bad_name": True}).json()["total"] == 2
    assert client.get("/api/videos", params={"duplicates": True}).json()["total"] == 2
    names = [v["name"] for v in client.get("/api/videos", params={"sort": "name", "order": "asc"}).json()["items"]]
    assert names == sorted(names, key=str.lower)
    assert client.get("/api/videos", params={"date_from": "not-a-date"}).status_code == 422
    assert client.get("/api/exts").json()[0]["ext"] == "mp4"
    assert client.get("/api/folders").json()


def test_tags_roundtrip(client):
    vid = client.get("/api/videos").json()["items"][0]
    r = client.post(f"/api/videos/{vid['id']}/tags", json={"tags": ["family", "Holiday"]}).json()
    assert r["tags"] == ["family", "Holiday"]
    assert client.get("/api/videos", params={"tag": "family"}).json()["total"] == 1
    assert [t["name"] for t in client.get("/api/tags").json()] == ["family", "Holiday"]
    r = client.delete(f"/api/videos/{vid['id']}/tags", params={"tag": "family"}).json()
    assert r["tags"] == ["Holiday"]


def test_rename_via_api(client):
    vid = next(v for v in client.get("/api/videos").json()["items"] if v["name"] == "MVI_0002.mp4")
    r = client.patch(f"/api/videos/{vid['id']}", json={"name": "Sunset Pier"})
    assert r.status_code == 200 and r.json()["name"] == "Sunset Pier.mp4" and not r.json()["bad_name"]
    assert client.patch(f"/api/videos/{vid['id']}", json={"name": "///"}).status_code == 409
    assert client.patch("/api/videos/99999", json={"name": "x"}).status_code == 404


def test_suggest_name_without_ai(client):
    vid = next(v for v in client.get("/api/videos").json()["items"] if v["date_source"] == "metadata")
    r = client.post(f"/api/videos/{vid['id']}/suggest-name").json()
    assert r["suggestion"].startswith("2019-07-04") and r["warning"] is None


def test_media_supports_range_requests(client):
    vid = client.get("/api/videos").json()["items"][0]
    full = client.get(f"/media/{vid['id']}")
    assert full.status_code == 200 and full.headers["content-type"].startswith("video/")
    part = client.get(f"/media/{vid['id']}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"].startswith("bytes 0-99/")


def test_thumbnails_served_and_lazily_created(client, tmp_path):
    vid = client.get("/api/videos").json()["items"][0]
    (tmp_path / "thumbs" / f"{vid['id']}.jpg").unlink()
    r = client.get(f"/thumb/{vid['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_no_arbitrary_file_access(client):
    assert client.get("/media/../../etc/passwd").status_code in (404, 422)
    assert client.get("/media/999999").status_code == 404


def test_rejects_foreign_host_and_origin(client):
    assert client.get("/api/videos", headers={"host": "evil.example.com"}).status_code == 403
    vid = client.get("/api/videos").json()["items"][0]
    r = client.post(f"/api/videos/{vid['id']}/tags", json={"tags": ["x"]}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_concurrent_requests_do_not_trip_sqlite_thread_check(client):
    """FastAPI may open a request's DB connection in one worker thread and use it in another."""
    from concurrent.futures import ThreadPoolExecutor

    vid = client.get("/api/videos").json()["items"][0]["id"]
    urls = ["/api/videos", "/api/tags", f"/api/videos/{vid}", f"/thumb/{vid}"] * 40
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert set(codes) == {200}


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Video Library" in r.text
    assert client.get("/app.js").status_code == 200
