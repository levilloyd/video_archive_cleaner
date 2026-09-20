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
vidcat dupes [--dry-run]                       # interactively pick the one copy to keep; the rest are removed (Trash, or see below)
vidcat names [--ai]                            # find poorly named videos and rename them with suggestions
vidcat undo-rename                             # revert the most recent rename
vidcat transcode [--dry-run]                   # convert old .mpg/.wmv files to MP4 (H.264 + AAC)
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
See "Volumes without a Trash" below for network shares.

### Names
A name is flagged as not useful when it's a camera default (`IMG_1234`, `MVI_0042`, `PXL_2023…`), a bare date
or number, a UUID/hash, or generic words like "Video" or "Untitled". Suggestions look like
`2019-07-04 Lake Trip - Kids Building Sandcastles`, built from the capture date, a meaningful parent folder,
and (with `--ai`) a short description from sampled video frames. Frames are sent only to your local Ollama.
At each prompt you can accept, edit, type your own, skip, open the video, or "keep name forever".
Existing files are never overwritten (a ` (2)` suffix is added instead).

### Transcoding
`vidcat transcode` converts cataloged `.mpg`, `.mpeg`, `.mpe`, `.wmv` and `.asf` files (change with `--ext`) to
`.mp4` next to the original: H.264 video + AAC audio, which plays everywhere, including the web UI. Use
`--codec hevc` for smaller files (less browser support), `--crf` to trade quality for size (default 20 for
H.264; lower is better), and `--preset slow` for smaller output at the cost of time.
- **Safe by design.** Output is written to a hidden temp file and checked (readable, same length, audio
  present) before it's moved into place. An existing `.mp4` is never overwritten, and originals are never
  touched unless you say so.
- **Looks right.** Interlaced footage (common from camcorders) is detected and deinterlaced
  (`--deinterlace auto|always|never`). The capture date is written into the new file when it's trustworthy, and
  the file's modified time is preserved.
- **Catalog carries over.** New files are cataloged automatically and inherit the original's tags and
  description.
- **Originals.** `--originals ask` (default) offers to remove them when the run finishes (default answer:
  keep); `keep`, `trash`, `archive` and `delete` skip the question (`delete` still asks you to confirm).
  Re-running is cheap: files that already have a good `.mp4` are not re-encoded, so you can convert first, play
  a few results, and later run `vidcat transcode --originals trash` to clean up.

### Volumes without a Trash
Network shares (SMB, NFS, AFP) usually have no Trash, and macOS's trash call can hang forever on them, waiting on
a Finder dialog you can't see. vidcat detects this and never sends those files to the Trash. When something you
remove (a duplicate, or an original after transcoding) is on such a volume you're offered:
- **archive**: move it into a `Duplicates (vidcat)` / `Originals (vidcat)` folder beside the file. Instant and
  undoable, but frees no space until you delete that folder yourself. vidcat ignores these folders when scanning.
- **delete**: remove it permanently to reclaim space. You must type `delete` to confirm, and it's refused when
  there's no terminal to confirm on.
- **keep** (transcode) or **quit** (dupes).

Files on local disks still go to the Trash as before, and a Trash call that doesn't answer within a minute is
abandoned with an error instead of hanging.

### Web UI
Search names/paths/tags/descriptions; sort by date, name, size, length; filter by tag, format, folder, date range,
length, "needs a better name", and "possible duplicates". Click a video to play it, edit its tags, or rename it
(with the same suggestions, including AI). Browsers can't play every format (AVI, MTS, WMV…); for those use
"Show in Finder" or Download (or convert them with `vidcat transcode`). The server only listens on localhost and
rejects foreign Host/Origin headers, because it can rename files and has no login.

**Rotating.** Videos shot sideways can be turned in the player with the ⟲ / ⟳ buttons, or the `Shift+R` / `R`
keys. It's a viewing setting saved in the catalog: it applies to the player and the thumbnail, survives rescans,
moves, transcoding and duplicate removal, and never modifies the video file. Turned videos use a simple built-in
control bar (play, seek, mute, full screen) because the browser's own controls would rotate with the picture.
`/#v12` in the address bar opens video 12 directly.

## Tests

```sh
uv run pytest
```
