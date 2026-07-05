"""FastAPI server exposing the indexing/search core over localhost.

Run in dev mode with:
    python -m backend.server [--port 8765]
"""

import argparse
import logging
import os
import socket
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, embedder, jobs
from . import search as search_mod

log = logging.getLogger(__name__)

# Set by the pywebview shell (Phase 4) so /select-folder can use the native
# in-window dialog. When absent we fall back to an AppleScript chooser.
window_provider = None

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class FolderRequest(BaseModel):
    folder: str


class PathRequest(BaseModel):
    path: str
    start_sec: float | None = None


def _open_in_quicktime_at(path: str, start_sec: float) -> bool:
    """Open in QuickTime Player seeked to a timestamp. AppleScript is the
    only portable way to seek, and only QuickTime scripts reliably; other
    default players fall back to a plain `open` at 0:00."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "QuickTime Player"\n'
        "  activate\n"
        f'  set doc to open POSIX file "{escaped}"\n'
        f"  set current time of doc to {start_sec:.2f}\n"
        "  play doc\n"
        "end tell"
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=20
        )
        return out.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _choose_folder_applescript() -> str | None:
    script = 'POSIX path of (choose folder with prompt "Choose a folder of videos to index")'
    try:
        out = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:  # user cancelled
        return None
    return out.stdout.strip() or None


def _is_within_roots(path: str, roots: list[str]) -> bool:
    rp = os.path.realpath(path)
    for root in roots:
        rroot = os.path.realpath(root)
        if rp == rroot or rp.startswith(rroot + os.sep):
            return True
    return False


def create_app() -> FastAPI:
    app = FastAPI(title="Video Library Search")
    manager = jobs.IndexManager()
    app.state.manager = manager

    # Warm the model in the background so the first search/index is fast.
    threading.Thread(target=embedder.load_model, daemon=True).start()

    @app.get("/api/health")
    def health():
        return {"ok": True, "device": embedder.device()}

    @app.post("/api/select-folder")
    def select_folder():
        if window_provider is not None:
            folder = window_provider.choose_folder()
        else:
            folder = _choose_folder_applescript()
        return {"folder": folder}  # null when the user cancels

    @app.post("/api/index")
    def start_index(req: FolderRequest):
        folder = os.path.expanduser(req.folder)
        if not os.path.isdir(folder):
            raise HTTPException(status_code=400, detail=f"Not a folder: {folder}")
        manager.enqueue(folder)
        return {"queued": folder}

    @app.post("/api/rescan")
    def rescan():
        return {"queued": manager.rescan_all()}

    @app.get("/api/index/status")
    def index_status():
        return manager.status()

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1),
        k: int = config.DEFAULT_SEARCH_LIMIT,
        folder: str | None = None,
    ):
        if folder is not None:
            folder = os.path.expanduser(folder)
            conn = db.connect()
            try:
                roots = db.list_folders(conn)
            finally:
                conn.close()
            if not _is_within_roots(folder, roots):
                raise HTTPException(status_code=403, detail="Not an indexed folder")
        results = search_mod.search(q, limit=min(max(k, 1), 100), folder=folder)
        for r in results:
            r["filename"] = os.path.basename(r["path"])
            r["exists"] = os.path.exists(r["path"])
            thumb = r.pop("thumb_path")
            r["thumb_url"] = (
                f"/thumbnails/{os.path.basename(thumb)}" if thumb else None
            )
        return {"query": q, "results": results}

    @app.get("/api/folders")
    def folders():
        conn = db.connect()
        try:
            return {"folders": db.list_folders(conn), "stats": db.stats(conn)}
        finally:
            conn.close()

    @app.post("/api/folders/remove")
    def remove_folder(req: FolderRequest):
        folder = os.path.expanduser(req.folder)
        conn = db.connect()
        try:
            if folder not in db.list_folders(conn):
                raise HTTPException(status_code=404, detail="Not a watched folder")
        finally:
            conn.close()
        try:
            manager.remove_folder(folder)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True}

    @app.get("/api/browse")
    def browse(path: str | None = Query(default=None)):
        conn = db.connect()
        try:
            roots = db.list_folders(conn)

            if not path:
                entries = [
                    {
                        "type": "folder",
                        "name": os.path.basename(os.path.normpath(r)) or r,
                        "path": r,
                    }
                    for r in roots
                ]
                entries.sort(key=lambda e: e["name"].lower())
                return {"path": None, "parent": None, "entries": entries}

            folder = os.path.expanduser(path)
            if not _is_within_roots(folder, roots):
                raise HTTPException(status_code=403, detail="Not an indexed folder")
            if not os.path.isdir(folder):
                raise HTTPException(status_code=404, detail="Folder not found")

            dirs, video_files = [], []
            for name in sorted(os.listdir(folder)):
                if name.startswith("."):
                    continue
                full = os.path.join(folder, name)
                if os.path.isdir(full):
                    dirs.append({"type": "folder", "name": name, "path": full})
                elif os.path.splitext(name)[1].lower() in config.VIDEO_EXTENSIONS:
                    info = db.file_browse_info(conn, full)
                    thumb = info["thumb_path"] if info else None
                    video_files.append({
                        "type": "file",
                        "name": name,
                        "path": full,
                        "indexed": info is not None,
                        "duration_sec": info["duration_sec"] if info else None,
                        "segments": info["segments"] if info else 0,
                        "thumb_url": f"/thumbnails/{os.path.basename(thumb)}" if thumb else None,
                    })

            is_root = any(
                os.path.realpath(folder) == os.path.realpath(r) for r in roots
            )
            parent = None if is_root else os.path.dirname(folder)
            return {"path": folder, "parent": parent, "entries": dirs + video_files}
        finally:
            conn.close()

    @app.get("/api/settings")
    def settings():
        conn = db.connect()
        try:
            s = db.stats(conn)
        finally:
            conn.close()
        s["data_dir"] = str(config.app_support_dir())
        s["model"] = config.MODEL_ID
        s["device"] = embedder.device()
        return s

    @app.post("/api/reset")
    def reset():
        status = manager.status()
        if status["state"] in ("indexing", "loading_model") or status["queued_folders"]:
            raise HTTPException(status_code=409, detail="Indexing is running")
        conn = db.connect()
        try:
            conn.execute("DELETE FROM vec_segments")
            conn.execute("DELETE FROM segments")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM folders")
            conn.commit()
        finally:
            conn.close()
        for f in config.thumbnails_dir().glob("*.jpg"):
            f.unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/open")
    def open_file(req: PathRequest):
        if not os.path.exists(req.path):
            raise HTTPException(status_code=404, detail="File no longer exists")
        if req.start_sec and req.start_sec > 0 and _open_in_quicktime_at(
            req.path, req.start_sec
        ):
            return {"ok": True, "seeked": True}
        subprocess.run(["open", req.path], check=False)
        return {"ok": True, "seeked": False}

    @app.post("/api/reveal")
    def reveal_file(req: PathRequest):
        if not os.path.exists(req.path):
            raise HTTPException(status_code=404, detail="File no longer exists")
        subprocess.run(["open", "-R", req.path], check=False)
        return {"ok": True}

    @app.get("/thumbnails/{name}")
    def thumbnail(name: str):
        if "/" in name or ".." in name:
            raise HTTPException(status_code=400, detail="Bad name")
        path = config.thumbnails_dir() / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.exception_handler(Exception)
    def on_error(request, exc):
        log.exception("Unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    if UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

    return app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    port = args.port or free_port()
    print(f"Serving on http://127.0.0.1:{port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
