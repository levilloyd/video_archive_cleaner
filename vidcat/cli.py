"""Command-line interface: scan, dupes, names, tag, ls, serve."""
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table

from . import config, duplicates, media, names as names_mod, queries, scanner, tags as tags_mod, vision
from .db import connect

app = typer.Typer(help="Manage a home video archive: scan, dedupe, rename, tag, browse.", no_args_is_help=True)
tag_app = typer.Typer(help="Add, remove and list tags.", no_args_is_help=True)
app.add_typer(tag_app, name="tag")
console = Console()


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Optional[Path], typer.Option("--db", envvar="VIDCAT_DB", help="Catalog database file.")] = None,
):
    ctx.obj = (db or config.db_path()).expanduser()


def open_db(ctx: typer.Context):
    return connect(ctx.obj)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_duration(s: float | None) -> str:
    if not s:
        return "-"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def human_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def open_in_viewer(path: str) -> None:
    subprocess.run(["open", path], check=False)


def progress_reporter():
    """Build a (progress_context, callback) pair that renders scan/hash stages as rich progress bars."""
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
        console=console, transient=True,
    )
    tasks: dict[str, int] = {}

    def callback(stage: str, done: int, total: int):
        if stage not in tasks:
            tasks[stage] = progress.add_task(stage, total=total or None)
        progress.update(tasks[stage], completed=done, total=total or None)

    return progress, callback


# --------------------------------------------------------------------------- scan

@app.command()
def scan(
    ctx: typer.Context,
    roots: Annotated[list[Path], typer.Argument(exists=True, file_okay=False, help="Folders to scan.")],
    no_thumbs: Annotated[bool, typer.Option("--no-thumbs", help="Skip thumbnail generation.")] = False,
    workers: Annotated[int, typer.Option(help="Parallel ffprobe/ffmpeg workers.")] = 4,
):
    """Add new/changed videos under ROOTS to the catalog (unchanged files are skipped)."""
    conn = open_db(ctx)
    progress, callback = progress_reporter()
    try:
        with progress:
            stats = scanner.scan(
                conn, roots, config.thumbs_dir(ctx.obj), workers=workers,
                make_thumbs=not no_thumbs, on_progress=callback,
            )
    except media.MediaToolError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    total = conn.execute("SELECT COUNT(*) FROM videos WHERE missing = 0").fetchone()[0]
    console.print(
        f"[green]Done.[/green] {stats.added} added, {stats.updated} updated, {stats.unchanged} unchanged, "
        f"{stats.moved} moved, {stats.missing} now missing, {stats.skipped} skipped (not video). "
        f"Catalog has {total} videos."
    )


# --------------------------------------------------------------------------- dupes

@app.command()
def dupes(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be removed; change nothing.")] = False,
):
    """Find identical files and interactively choose which single copy to keep (others go to the Trash)."""
    conn = open_db(ctx)
    progress, callback = progress_reporter()
    with progress:
        groups = duplicates.find_duplicate_groups(conn, callback)
    if not groups:
        console.print("[green]No duplicate videos found.[/green]")
        return
    reclaimable = sum(g[0]["size"] * (len(g) - 1) for g in groups)
    console.print(f"Found [bold]{len(groups)}[/bold] sets of identical files; removing extras would free "
                  f"[bold]{human_size(reclaimable)}[/bold].")
    if dry_run:
        console.print("[yellow]Dry run: nothing will be removed.[/yellow]")

    freed = removed = 0
    for n, group in enumerate(groups, 1):
        default = duplicates.pick_default_keep(group)
        while True:
            table = Table(title=f"Set {n} of {len(groups)}: {human_size(group[0]['size'])} each", title_justify="left")
            for col in ("#", "Path", "Captured", "Length", "Size"):
                table.add_column(col, overflow="fold")
            for i, r in enumerate(group):
                mark = " [green](suggested)[/green]" if i == default else ""
                table.add_row(str(i + 1), r["path"] + mark, human_date(r["created_at"]),
                              human_duration(r["duration"]), human_size(r["size"]))
            console.print(table)
            answer = Prompt.ask(
                f"Keep which copy? [1-{len(group)}], [bold]s[/bold]kip, [bold]o[/bold]pen N, [bold]q[/bold]uit",
                default=str(default + 1),
            ).strip().lower()
            if answer in ("q", "quit"):
                console.print(f"Removed {removed} file(s), freed {human_size(freed)}.")
                return
            if answer in ("s", "skip"):
                break
            if answer.startswith("o"):
                arg = answer[1:].strip()
                idx = int(arg) - 1 if arg.isdigit() else default
                if 0 <= idx < len(group):
                    open_in_viewer(group[idx]["path"])
                continue
            if answer.isdigit() and 1 <= int(answer) <= len(group):
                keep = group[int(answer) - 1]
                drop = [r for r in group if r["id"] != keep["id"]]
                if dry_run:
                    for r in drop:
                        console.print(f"  [yellow]would trash[/yellow] {r['path']}")
                    break
                try:
                    trashed = duplicates.remove_copies(conn, keep["id"], [r["id"] for r in drop])
                except Exception as e:  # report and continue with the next set
                    console.print(f"[red]Could not remove: {e}[/red]")
                    break
                removed += len(trashed)
                freed += keep["size"] * len(trashed)
                for p in trashed:
                    console.print(f"  [red]trashed[/red] {p}")
                break
            console.print("[red]Please enter a number from the list, s, o N, or q.[/red]")
    console.print(f"[green]Finished.[/green] Removed {removed} file(s), freed {human_size(freed)}.")


# --------------------------------------------------------------------------- names

def input_prefilled(prompt: str, text: str) -> str:
    """Line input with editable pre-filled text (falls back to a plain prompt if readline is unavailable)."""
    try:
        import readline
        readline.set_startup_hook(lambda: readline.insert_text(text))
        try:
            return input(prompt)
        finally:
            readline.set_startup_hook()
    except (ImportError, AttributeError):
        return Prompt.ask(prompt, default=text)


@app.command()
def names(
    ctx: typer.Context,
    ai: Annotated[bool, typer.Option("--ai", help="Also describe each video with a local vision model (Ollama).")] = False,
    model: Annotated[str, typer.Option(help="Ollama vision model.")] = config.vision_model(),
    all_files: Annotated[bool, typer.Option("--all", help="Review every video, not just poorly named ones.")] = False,
    limit: Annotated[Optional[int], typer.Option(help="Stop after this many files.")] = None,
    folder: Annotated[Optional[Path], typer.Option(help="Only videos under this folder.")] = None,
):
    """Find videos without useful names and interactively rename them, with suggestions."""
    conn = open_db(ctx)
    sql, params = "SELECT * FROM videos WHERE missing = 0", []
    if not all_files:
        sql += " AND name_score < ?"
        params.append(config.BAD_NAME_THRESHOLD)
    if folder:
        sql += " AND (dir = ? OR path LIKE ?)"
        root = str(folder.expanduser().resolve())
        params += [root, root.rstrip("/") + "/%"]
    rows = conn.execute(sql + " ORDER BY dir, name", params).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("[green]No poorly named videos found.[/green]")
        return
    console.print(f"[bold]{len(rows)}[/bold] video(s) to review.")

    if ai:
        try:
            vision.check_ollama(model)
        except vision.VisionUnavailable as e:
            console.print(f"[yellow]{e}\nContinuing without AI suggestions.[/yellow]")
            ai = False

    renamed = 0
    for n, r in enumerate(rows, 1):
        if not os.path.exists(r["path"]):
            continue
        caption = r["caption"]

        def get_caption(force: bool = False):
            nonlocal caption, ai
            if caption and not force:
                return
            try:
                with console.status(f"Asking {model} to describe the video..."):
                    caption = vision.caption_video(r["path"], r["duration"], model)
                with conn:
                    conn.execute("UPDATE videos SET caption = ? WHERE id = ?", (caption, r["id"]))
            except (vision.VisionUnavailable, media.MediaToolError) as e:
                console.print(f"[yellow]{e}[/yellow]")
                ai = False

        if ai:
            get_caption()
        while True:
            suggestion = names_mod.suggest_name(r, caption)
            info = (f"[bold]{r['name']}[/bold]\n{r['dir']}\n"
                    f"{human_date(r['created_at'])} · {human_duration(r['duration'])} · {human_size(r['size'])}")
            if caption:
                info += f"\nAI description: {caption}"
            console.print(Panel(info, title=f"{n}/{len(rows)}", title_align="left"))
            console.print(f"Suggested: [green]{suggestion}{Path(r['name']).suffix}[/green]")
            options = ["[bold]Enter[/bold] accept", "[bold]e[/bold]dit", "[bold]o[/bold]pen video"]
            if ai:
                options.append("[bold]r[/bold]egenerate AI")
            options += ["[bold]k[/bold]eep name forever", "[bold]s[/bold]kip", "[bold]q[/bold]uit", "or type a new name"]
            keys = ", ".join(options)
            answer = Prompt.ask(keys, default="", show_default=False).strip()
            low = answer.lower()
            if low == "q":
                console.print(f"Renamed {renamed} file(s).")
                return
            if low == "s":
                break
            if low == "o":
                open_in_viewer(r["path"])
                continue
            if low == "r" and ai:
                get_caption(force=True)
                continue
            if low == "k":
                with conn:
                    conn.execute("UPDATE videos SET name_score = 100 WHERE id = ?", (r["id"],))
                break
            if low == "e":
                answer = input_prefilled("Name: ", suggestion).strip()
                if not answer:
                    continue
            new_name = answer or suggestion
            try:
                new_path = names_mod.rename_video(conn, r["id"], new_name)
            except (ValueError, OSError) as e:
                console.print(f"[red]{e}[/red]")
                continue
            console.print(f"  [green]renamed[/green] → {os.path.basename(new_path)}")
            renamed += 1
            break
    console.print(f"[green]Done.[/green] Renamed {renamed} file(s).")


@app.command("undo-rename")
def undo_rename(ctx: typer.Context):
    """Revert the most recent rename."""
    conn = open_db(ctx)
    try:
        result = names_mod.undo_last_rename(conn)
    except OSError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if result is None:
        console.print("Nothing to undo.")
    else:
        console.print(f"Restored {result[1]}")


# --------------------------------------------------------------------------- tags & listing

def resolve_video(conn, ref: str):
    """Find a video by catalog id, exact path, or unique file name."""
    if ref.isdigit():
        row = conn.execute("SELECT * FROM videos WHERE id = ? AND missing = 0", (int(ref),)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT * FROM videos WHERE path = ?", (str(Path(ref).expanduser().resolve()),)).fetchone()
    if row:
        return row
    rows = conn.execute("SELECT * FROM videos WHERE name = ? AND missing = 0", (ref,)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise typer.BadParameter(f"'{ref}' matches {len(rows)} files; use the id or full path (see `vidcat ls`).")
    raise typer.BadParameter(f"No video found for '{ref}'. Use an id from `vidcat ls`, a path, or a file name.")


@tag_app.command("add")
def tag_add(ctx: typer.Context, ref: Annotated[str, typer.Argument(help="Video id, path, or file name.")],
            tags: Annotated[list[str], typer.Argument(help="Tags to add.")]):
    """Add tags to a video."""
    conn = open_db(ctx)
    video = resolve_video(conn, ref)
    tags_mod.add_tags(conn, video["id"], tags)
    console.print(f"{video['name']}: {', '.join(tags_mod.tags_for(conn, [video['id']])[video['id']])}")


@tag_app.command("remove")
def tag_remove(ctx: typer.Context, ref: Annotated[str, typer.Argument(help="Video id, path, or file name.")],
               tags: Annotated[list[str], typer.Argument(help="Tags to remove.")]):
    """Remove tags from a video."""
    conn = open_db(ctx)
    video = resolve_video(conn, ref)
    tags_mod.remove_tags(conn, video["id"], tags)
    console.print(f"{video['name']}: {', '.join(tags_mod.tags_for(conn, [video['id']])[video['id']]) or '(no tags)'}")


@tag_app.command("list")
def tag_list(ctx: typer.Context, ref: Annotated[Optional[str], typer.Argument(help="Video (omit for all tags).")] = None):
    """List all tags with counts, or the tags on one video."""
    conn = open_db(ctx)
    if ref:
        video = resolve_video(conn, ref)
        console.print(", ".join(tags_mod.tags_for(conn, [video["id"]])[video["id"]]) or "(no tags)")
        return
    for t in tags_mod.all_tags(conn):
        console.print(f"{t['name']} [dim]({t['count']})[/dim]")


@app.command()
def ls(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search words (name, path, tags, description).")] = "",
    tag: Annotated[Optional[list[str]], typer.Option(help="Require this tag (repeatable).")] = None,
    bad_names: Annotated[bool, typer.Option("--bad-names", help="Only poorly named videos.")] = False,
    limit: Annotated[int, typer.Option(help="Max rows.")] = 50,
):
    """List catalog entries (with ids for use with `vidcat tag`)."""
    conn = open_db(ctx)
    res = queries.search_videos(conn, q=query, tags=tag or [], bad_name=bad_names, sort="path",
                                order="asc", page_size=limit)
    table = Table()
    for col in ("ID", "Name", "Captured", "Length", "Tags"):
        table.add_column(col, overflow="fold")
    for v in res["items"]:
        table.add_row(str(v["id"]), v["name"], human_date(v["created_at"]), human_duration(v["duration"]),
                      ", ".join(v["tags"]))
    console.print(table)
    console.print(f"[dim]Showing {len(res['items'])} of {res['total']}[/dim]")


# --------------------------------------------------------------------------- serve

@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Interface to bind. Keep the default unless you know why.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the UI in your browser.")] = False,
):
    """Start the web UI."""
    import uvicorn

    from .web.app import create_app

    local = host in ("127.0.0.1", "localhost", "::1")
    if not local:
        console.print("[yellow]Warning: the web UI can rename files and has no login. "
                      "Only expose it on a network you trust.[/yellow]")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    console.print(f"Serving on [bold]http://{host}:{port}[/bold]  (Ctrl+C to stop)")
    uvicorn.run(create_app(ctx.obj, allow_any_host=not local), host=host, port=port, log_level="warning")
