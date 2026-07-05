"""Folder scanning and incremental indexing."""

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, embedder, frames

log = logging.getLogger(__name__)


@dataclass
class IndexProgress:
    state: str = "idle"  # idle | loading_model | indexing | done | error
    total_files: int = 0
    done_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    current_file: str | None = None
    started_at: float | None = None
    error: str | None = None
    errors: list[str] = field(default_factory=list)
    # Sub-progress within current_file: phase is one of
    # detecting_scenes | extracting_frames | embedding, or None between
    # files. For detecting_scenes, current/total are seconds into the
    # video; for the other two, they're counts of segments/frames done.
    phase: str | None = None
    phase_current: int = 0
    phase_total: int = 0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        if self.started_at and self.done_files and self.state == "indexing":
            elapsed = time.time() - self.started_at
            remaining = self.total_files - self.done_files - self.skipped_files
            per_file = elapsed / max(self.done_files, 1)
            d["eta_sec"] = round(per_file * remaining)
        return d


def scan_folder(folder: str) -> list[str]:
    """Recursively find video files, following the extension allowlist."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in config.VIDEO_EXTENSIONS:
                found.append(os.path.join(root, name))
    return sorted(found)


def prune_missing(conn) -> int:
    """Drop DB entries (and cached thumbnails) for videos that no longer exist."""
    removed = 0
    for row in db.all_files(conn):
        if not os.path.exists(row["path"]):
            for thumb in db.remove_file(conn, row["id"]):
                Path(thumb).unlink(missing_ok=True)
            removed += 1
    conn.commit()
    return removed


def index_file(conn, path: str, progress: IndexProgress | None = None) -> int:
    """Index one video; returns the number of segments stored."""
    st = os.stat(path)

    def on_phase(phase, current, total):
        if progress is not None:
            progress.phase = phase
            progress.phase_current = current
            progress.phase_total = total

    segments = frames.extract_segments(path, on_phase=on_phase)
    if not segments:
        raise RuntimeError("no frames could be extracted")

    embeddings = embedder.embed_images(
        [s.frame for s in segments],
        on_progress=lambda current, total: on_phase("embedding", current, total),
    )
    duration = frames.video_duration_sec(path)
    thumbs_dir = config.thumbnails_dir()

    file_id = db.add_file(conn, path, st.st_mtime, st.st_size, duration)
    for seg, emb in zip(segments, embeddings):
        thumb_path = thumbs_dir / f"{uuid.uuid4().hex}.jpg"
        try:
            frames.save_thumbnail(seg.frame, thumb_path)
        except Exception:
            thumb_path = None
        db.add_segment(
            conn, file_id, seg.start_sec, seg.end_sec,
            str(thumb_path) if thumb_path else None, emb,
        )
    conn.commit()
    return len(segments)


def index_folder(
    folder: str,
    progress: IndexProgress | None = None,
    should_stop=lambda: False,
) -> IndexProgress:
    """Index every video under `folder`, skipping unchanged files.

    Designed to be run in a background thread; reports through `progress`.
    """
    progress = progress or IndexProgress()
    conn = db.connect()
    try:
        embedder.load_model(lambda state: setattr(progress, "state", state))

        files = scan_folder(folder)
        prune_missing(conn)

        progress.state = "indexing"
        progress.total_files = len(files)
        progress.started_at = time.time()

        for path in files:
            if should_stop():
                break
            st = os.stat(path)
            if db.is_unchanged(conn, path, st.st_mtime, st.st_size):
                progress.skipped_files += 1
                continue
            progress.current_file = path
            try:
                n = index_file(conn, path, progress=progress)
                log.info("Indexed %s (%d segments)", path, n)
                progress.done_files += 1
            except Exception as exc:
                log.warning("Failed to index %s: %s", path, exc)
                progress.failed_files += 1
                progress.errors.append(f"{path}: {exc}")
            finally:
                progress.phase = None
                progress.phase_current = 0
                progress.phase_total = 0
                embedder.release_cache()

        progress.current_file = None
        progress.state = "done"
    except Exception as exc:
        progress.state = "error"
        progress.error = str(exc)
        raise
    finally:
        conn.close()
    return progress
