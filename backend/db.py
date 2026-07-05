"""SQLite + sqlite-vec storage layer.

Single local file. Tables:
  files    — one row per indexed video (path, mtime, size for incremental checks)
  segments — one row per sampled segment (start/end seconds, thumbnail path)
  vec_segments — sqlite-vec virtual table; rowid == segments.id
"""

import os
import sqlite3
from pathlib import Path

import numpy as np
import sqlite_vec

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    duration_sec REAL,
    indexed_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    thumb_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_file ON segments(file_id);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
"""

VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_segments USING vec0(
    embedding float[{config.EMBEDDING_DIM}] distance_metric=cosine
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.db_path())
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.execute(VEC_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def get_file(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()


def is_unchanged(conn: sqlite3.Connection, path: str, mtime: float, size: int) -> bool:
    row = get_file(conn, path)
    return row is not None and abs(row["mtime"] - mtime) < 1e-6 and row["size"] == size


def remove_file(conn: sqlite3.Connection, file_id: int) -> list[str]:
    """Delete a file's rows and vectors; returns thumbnail paths to clean up."""
    seg_rows = conn.execute(
        "SELECT id, thumb_path FROM segments WHERE file_id = ?", (file_id,)
    ).fetchall()
    conn.executemany(
        "DELETE FROM vec_segments WHERE rowid = ?", [(r["id"],) for r in seg_rows]
    )
    conn.execute("DELETE FROM segments WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    return [r["thumb_path"] for r in seg_rows if r["thumb_path"]]


def add_file(
    conn: sqlite3.Connection,
    path: str,
    mtime: float,
    size: int,
    duration_sec: float | None,
) -> int:
    existing = get_file(conn, path)
    if existing:
        remove_file(conn, existing["id"])
    cur = conn.execute(
        "INSERT INTO files (path, mtime, size, duration_sec) VALUES (?, ?, ?, ?)",
        (path, mtime, size, duration_sec),
    )
    return cur.lastrowid


def add_segment(
    conn: sqlite3.Connection,
    file_id: int,
    start_sec: float,
    end_sec: float,
    thumb_path: str | None,
    embedding: np.ndarray,
) -> int:
    cur = conn.execute(
        "INSERT INTO segments (file_id, start_sec, end_sec, thumb_path) VALUES (?, ?, ?, ?)",
        (file_id, start_sec, end_sec, thumb_path),
    )
    seg_id = cur.lastrowid
    conn.execute(
        "INSERT INTO vec_segments (rowid, embedding) VALUES (?, ?)",
        (seg_id, embedding.astype(np.float32).tobytes()),
    )
    return seg_id


def search(
    conn: sqlite3.Connection,
    query_embedding: np.ndarray,
    limit: int,
    folder: str | None = None,
) -> list[dict]:
    # Folder scoping isn't pushed into the ANN index — over-fetch a wider
    # candidate set and filter by path prefix in Python instead.
    fetch_k = limit if folder is None else min(max(limit * 25, 200), 2000)
    rows = conn.execute(
        """
        SELECT s.id, f.path, s.start_sec, s.end_sec, s.thumb_path, v.distance
        FROM vec_segments v
        JOIN segments s ON s.id = v.rowid
        JOIN files f ON f.id = s.file_id
        WHERE v.embedding MATCH ? AND v.k = ?
        ORDER BY v.distance
        """,
        (query_embedding.astype(np.float32).tobytes(), fetch_k),
    ).fetchall()
    if folder is not None:
        prefix = folder.rstrip(os.sep) + os.sep
        rows = [r for r in rows if r["path"] == folder or r["path"].startswith(prefix)]
    rows = rows[:limit]
    return [
        {
            "segment_id": r["id"],
            "path": r["path"],
            "start_sec": r["start_sec"],
            "end_sec": r["end_sec"],
            "thumb_path": r["thumb_path"],
            # cosine distance -> similarity in [0, 1]-ish for display
            "score": 1.0 - r["distance"],
        }
        for r in rows
    ]


def add_folder(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("INSERT OR IGNORE INTO folders (path) VALUES (?)", (path,))
    conn.commit()


def list_folders(conn: sqlite3.Connection) -> list[str]:
    return [
        r["path"]
        for r in conn.execute("SELECT path FROM folders ORDER BY added_at").fetchall()
    ]


def remove_folder(conn: sqlite3.Connection, path: str) -> list[str]:
    """Forget a watched folder and delete everything indexed under it.

    Returns thumbnail paths to clean up. Prefix matching is done in Python
    (as in `search`) rather than SQL LIKE, since folder paths can contain
    '%'/'_' which are LIKE wildcards.
    """
    prefix = path.rstrip(os.sep) + os.sep
    rows = conn.execute("SELECT id, path FROM files").fetchall()
    thumbs = []
    for r in rows:
        if r["path"] == path or r["path"].startswith(prefix):
            thumbs.extend(remove_file(conn, r["id"]))
    conn.execute("DELETE FROM folders WHERE path = ?", (path,))
    conn.commit()
    return thumbs


def all_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM files ORDER BY path").fetchall()


def file_browse_info(conn: sqlite3.Connection, path: str) -> dict | None:
    """Indexing summary for one path, for the folder browser. None if unindexed."""
    row = get_file(conn, path)
    if row is None:
        return None
    seg_count = conn.execute(
        "SELECT COUNT(*) c FROM segments WHERE file_id = ?", (row["id"],)
    ).fetchone()["c"]
    thumb = conn.execute(
        """SELECT thumb_path FROM segments
           WHERE file_id = ? AND thumb_path IS NOT NULL
           ORDER BY start_sec LIMIT 1""",
        (row["id"],),
    ).fetchone()
    return {
        "duration_sec": row["duration_sec"],
        "segments": seg_count,
        "thumb_path": thumb["thumb_path"] if thumb else None,
    }


def stats(conn: sqlite3.Connection) -> dict:
    nfiles = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    nsegs = conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"]
    return {"files": nfiles, "segments": nsegs, "db_path": str(config.db_path())}
