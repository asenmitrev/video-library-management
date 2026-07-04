# Video Library Search

Local semantic search over your video files, packaged as a native macOS app
(Apple Silicon). Videos are indexed by embedding sampled frames with SigLIP 2
on the MPS backend; you search with natural-language queries like
*"a dog catching a frisbee in a park"*. Everything runs locally — the only
network access is the one-time model download (~1.4 GB) on first run.

## Layout

```
backend/     core module (embedding, scene detection, SQLite+sqlite-vec,
             indexing, search) + FastAPI server (server.py, jobs.py)
ui/          plain HTML/CSS/JS front end (no build step)
packaging/   PyInstaller spec + app icon
app.py       native app entry point (pywebview window around the server)
```

## Development setup

Requires a Python built with SQLite extension loading enabled (the python.org
installer builds have it disabled, which breaks `sqlite-vec`). Homebrew Python
works:

```sh
brew install python@3.13 ffmpeg
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running in dev mode

```sh
# Full native app (pywebview window):
.venv/bin/python app.py

# Server only, use in a browser at the printed URL:
.venv/bin/python -m backend.server --port 8765

# Core module from the command line, no server:
.venv/bin/python -m backend.cli index ~/Movies/clips
.venv/bin/python -m backend.cli search "a dog catching a frisbee" -k 12
.venv/bin/python -m backend.cli status
.venv/bin/python -m backend.cli reset
```

Data (SQLite DB + cached thumbnails; for the packaged app also the model
weights) lives in `~/Library/Application Support/VideoLibrarySearch/`. The
settings dialog (⚙) shows this path and offers "Clear library". Set
`VLS_DATA_DIR` to override (useful for tests).

## Building the .app

```sh
.venv/bin/pyinstaller packaging/VideoLibrarySearch.spec --noconfirm
open dist   # → "Video Library Search.app"
```

The bundle is unsigned; on another Mac, first launch needs right-click →
Open (Gatekeeper). Model weights are not bundled — the app downloads them
once on first indexing and stores them in Application Support.

## Architecture notes

- **Model**: `google/siglip2-base-patch16-224` via Transformers, on `mps`
  with CPU fallback. No flash-attn / CUDA-only dependencies.
- **Segmentation**: PySceneDetect `ContentDetector`; falls back to fixed
  2-second interval sampling when a video has fewer than two detected scenes
  or detection fails. One representative (midpoint) frame per segment, capped
  at 300 segments per video.
- **Storage**: SQLite + `sqlite-vec` (cosine distance), one row per segment.
  Incremental: files whose path + mtime + size already match are skipped;
  entries for deleted files are pruned on each indexing pass.
- **Thumbnails**: pre-generated at index time (≤320 px JPEG) since the frame
  is already decoded then; stored on disk, path kept in the DB.
- **Search UX**: relevance is shown as 1–5 dots relative to the best hit in
  the result set (raw SigLIP cosine scores are ~0.03–0.2 and not meaningful
  to users). Default result count: 12.

## Deviations from the spec

- **Model download is ~1.4 GB**, not ~400 MB; the UI says so honestly.
- **Open-at-timestamp**: when a result is clicked, the app opens the video in
  QuickTime Player seeked to the matched moment via AppleScript (the only
  reliable scriptable seek). If that fails (QuickTime missing, unsupported
  codec), it falls back to opening the file in the default player at 0:00;
  "Show in Finder" is always available on each card.
- **Model download progress** is an indeterminate bar with an explanatory
  message rather than a percentage (Hugging Face Hub doesn't expose overall
  progress cleanly through Transformers).

## Known limitations

- Search quality is bounded by frame-level image-text matching: motion- or
  audio-defined moments ("the part where they sing") won't match well.
- No audio/transcript search in v1 (future work).
- mkv/avi/webm are indexed via OpenCV/ffmpeg; exotic codecs inside those
  containers may fail per-file — such files are reported in indexing status
  and skipped, never fatal.
- macOS only; the UI/backend split would port, but packaging is Mac-specific.
