"""FastAPI app: JSON API + media/thumbnail endpoints + the static frontend."""
import mimetypes
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, media, names, queries, tags, vision
from ..db import connect
from ..scanner import thumb_path

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}


class RenameBody(BaseModel):
    name: str | None = None
    caption: str | None = None


class TagsBody(BaseModel):
    tags: list[str]


def create_app(db: Path | str | None = None, allow_any_host: bool = False) -> FastAPI:
    db = Path(db) if db else config.db_path()
    connect(db).close()  # create schema once
    thumbs = config.thumbs_dir(db)

    app = FastAPI(title="vidcat", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Defend against DNS-rebinding and cross-site requests: this API can rename files."""
        if not allow_any_host:
            host = request.headers.get("host", "")
            hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if hostname not in LOCAL_HOSTS:
                return JSONResponse({"detail": "Forbidden host"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and request.method not in ("GET", "HEAD") and urlparse(origin).hostname not in LOCAL_HOSTS:
                return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
        return await call_next(request)

    def get_conn():
        conn = connect(db, init=False, cross_thread=True)
        try:
            yield conn
        finally:
            conn.close()

    def video_or_404(conn, video_id: int):
        item = queries.get_video(conn, video_id)
        if item is None:
            raise HTTPException(404, "Video not found")
        return item

    @app.get("/api/videos")
    def list_videos(
        q: str = "",
        tag: list[str] = Query(default=[]),
        ext: list[str] = Query(default=[]),
        folder: str | None = None,
        min_dur: float | None = None,
        max_dur: float | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        bad_name: bool = False,
        duplicates: bool = False,
        sort: str = "date",
        order: str = "desc",
        page: int = 1,
        page_size: int = 60,
        conn=Depends(get_conn),
    ):
        try:
            return queries.search_videos(
                conn, q=q, tags=tag, exts=ext, folder=folder, min_dur=min_dur, max_dur=max_dur,
                date_from=date_from, date_to=date_to, bad_name=bad_name, duplicates=duplicates,
                sort=sort, order=order, page=page, page_size=page_size,
            )
        except ValueError as e:  # bad date format
            raise HTTPException(422, str(e)) from e

    @app.get("/api/videos/{video_id}")
    def get_video(video_id: int, conn=Depends(get_conn)):
        return video_or_404(conn, video_id)

    @app.patch("/api/videos/{video_id}")
    def update_video(video_id: int, body: RenameBody, conn=Depends(get_conn)):
        video_or_404(conn, video_id)
        if body.caption is not None:
            with conn:
                conn.execute("UPDATE videos SET caption = ? WHERE id = ?", (body.caption.strip() or None, video_id))
        if body.name is not None:
            try:
                names.rename_video(conn, video_id, body.name)
            except (ValueError, OSError) as e:
                raise HTTPException(409, str(e)) from e
        return video_or_404(conn, video_id)

    @app.post("/api/videos/{video_id}/tags")
    def add_video_tags(video_id: int, body: TagsBody, conn=Depends(get_conn)):
        video_or_404(conn, video_id)
        tags.add_tags(conn, video_id, body.tags)
        return video_or_404(conn, video_id)

    @app.delete("/api/videos/{video_id}/tags")
    def remove_video_tag(video_id: int, tag: str, conn=Depends(get_conn)):
        video_or_404(conn, video_id)
        tags.remove_tags(conn, video_id, [tag])
        return video_or_404(conn, video_id)

    @app.post("/api/videos/{video_id}/suggest-name")
    def suggest_name(video_id: int, ai: bool = False, conn=Depends(get_conn)):
        item = video_or_404(conn, video_id)
        caption, warning = item["caption"], None
        if ai and not caption:
            try:
                caption = vision.caption_video(item["path"], item["duration"])
                with conn:
                    conn.execute("UPDATE videos SET caption = ? WHERE id = ?", (caption, video_id))
            except (vision.VisionUnavailable, media.MediaToolError) as e:
                warning = str(e)
        return {"suggestion": names.suggest_name(item, caption), "caption": caption, "warning": warning}

    @app.post("/api/videos/{video_id}/reveal")
    def reveal(video_id: int, conn=Depends(get_conn)):
        item = video_or_404(conn, video_id)
        subprocess.run(["open", "-R", item["path"]], check=False)
        return {"ok": True}

    @app.get("/api/tags")
    def get_tags(conn=Depends(get_conn)):
        return tags.all_tags(conn)

    @app.get("/api/folders")
    def get_folders(conn=Depends(get_conn)):
        return queries.list_folders(conn)

    @app.get("/api/exts")
    def get_exts(conn=Depends(get_conn)):
        return queries.list_exts(conn)

    @app.get("/media/{video_id}")
    def stream(video_id: int, conn=Depends(get_conn)):
        item = video_or_404(conn, video_id)
        if not Path(item["path"]).exists():
            raise HTTPException(404, "File is missing from disk")
        mime = mimetypes.guess_type(item["path"])[0] or "application/octet-stream"
        return FileResponse(item["path"], media_type=mime)  # Starlette serves HTTP Range requests

    @app.get("/thumb/{video_id}")
    def thumbnail(video_id: int, conn=Depends(get_conn)):
        item = video_or_404(conn, video_id)
        dest = thumb_path(thumbs, video_id)
        if not dest.exists():
            try:
                ok = media.make_thumbnail(item["path"], dest, item["duration"])
            except media.MediaToolError:
                ok = False
            if not ok:
                raise HTTPException(404, "No thumbnail")
        return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
