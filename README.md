# vidcat — home video archive manager

Scan a folder of home videos into a catalog, remove duplicate files, fix uninformative file names,
tag videos, and browse everything in a local web UI.

## Setup

```sh
brew install ffmpeg uv          # ffmpeg/ffprobe read metadata and make thumbnails
uv sync                         # creates .venv and installs dependencies
uv run vidcat --help
```

Optional, for AI-suggested names: [Ollama](https://ollama.com) running with a vision-capable model
(default `gemma4:e4b`; override with `--model` or `VIDCAT_VISION_MODEL`).

The catalog lives in `~/.vidcat/catalog.db` (thumbnails alongside it). Use `--db PATH` or `VIDCAT_DB` to change it.
Your video files are never modified except by the rename and remove actions you confirm.

## Usage

```sh
vidcat scan ~/Movies /Volumes/Archive/Videos   # add new/changed files; re-runs skip unchanged ones
vidcat dupes [--dry-run]                       # interactively pick the one copy to keep; the rest go to the Trash
vidcat names [--ai]                            # find poorly named videos and rename them with suggestions
vidcat undo-rename                             # revert the most recent rename
vidcat tag add 12 family "2019 trip"           # tag by id (see `vidcat ls`), path, or file name
vidcat tag remove 12 family
vidcat tag list                                # all tags with counts
vidcat ls beach --tag family --bad-names       # search the catalog from the terminal
vidcat serve --open                            # web UI at http://127.0.0.1:8000
```

### Duplicates
Files are grouped by size, then a quick fingerprint (first/middle/last MiB), then a full SHA-256, so only
byte-identical files are ever offered. For each set you choose which copy to keep (the best-named, earliest
copy is pre-selected — press Enter to accept). The others are moved to the macOS Trash, and their tags are
merged onto the copy you kept. Hard links to the same file are not treated as duplicates.

### Names
A name is flagged as not useful when it's a camera default (`IMG_1234`, `MVI_0042`, `PXL_2023…`), a bare date
or number, a UUID/hash, or generic words like "Video" or "Untitled". Suggestions look like
`2019-07-04 Lake Trip - Kids Building Sandcastles`, built from the capture date, a meaningful parent folder,
and (with `--ai`) a short description from sampled video frames. Frames are sent only to your local Ollama.
At each prompt you can accept, edit, type your own, skip, open the video, or "keep name forever".
Existing files are never overwritten (a ` (2)` suffix is added instead).

### Web UI
Search names/paths/tags/descriptions; sort by date, name, size, length; filter by tag, format, folder, date range,
length, "needs a better name", and "possible duplicates". Click a video to play it, edit its tags, or rename it
(with the same suggestions, including AI). Browsers can't play every format (AVI, MTS, WMV…); for those use
"Show in Finder" or Download. The server only listens on localhost and rejects foreign Host/Origin headers,
because it can rename files and has no login.

## Tests

```sh
uv run pytest
```
