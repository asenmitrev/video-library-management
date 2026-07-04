"""Background indexing job manager for the API server.

One worker thread processes a queue of folders sequentially, so /index
returns immediately and search stays available while indexing runs.
"""

import logging
import queue
import threading

from . import db, embedder, indexer

log = logging.getLogger(__name__)


class IndexManager:
    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._progress = indexer.IndexProgress()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def enqueue(self, folder: str) -> None:
        conn = db.connect()
        try:
            db.add_folder(conn, folder)
        finally:
            conn.close()
        with self._lock:
            if folder in self._pending:
                return
            self._pending.append(folder)
        self._queue.put(folder)

    def rescan_all(self) -> list[str]:
        conn = db.connect()
        try:
            folders = db.list_folders(conn)
        finally:
            conn.close()
        for f in folders:
            self.enqueue(f)
        return folders

    def status(self) -> dict:
        with self._lock:
            d = self._progress.as_dict()
            d["queued_folders"] = list(self._pending)
            # errors can get long; cap what we ship to the UI
            d["errors"] = d["errors"][-20:]
        # The model may be downloading/loading via the server's warm-up
        # thread, independent of this job's own progress object (which
        # would otherwise still read "idle" while blocked behind it).
        if embedder.state() == "loading_model" and d["state"] != "indexing":
            d["state"] = "loading_model"
        if d["state"] == "loading_model":
            dl = embedder.download_progress()
            d["download_bytes"] = dl["downloaded_bytes"]
            d["download_total_bytes"] = dl["total_bytes"]
        return d

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                folder = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._progress = indexer.IndexProgress()
                progress = self._progress
            try:
                indexer.index_folder(
                    folder, progress=progress, should_stop=self._stop.is_set
                )
            except Exception as exc:
                log.exception("Indexing job failed for %s", folder)
                progress.state = "error"
                progress.error = str(exc)
            finally:
                with self._lock:
                    if folder in self._pending:
                        self._pending.remove(folder)
