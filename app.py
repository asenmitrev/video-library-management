"""Native app entry point: FastAPI on a random localhost port inside a
pywebview window. This is also the PyInstaller entry script.

Dev mode:
    python app.py
"""

import multiprocessing

# Must run before anything else in a frozen app: a child process spawned via
# multiprocessing re-executes this binary, and without this guard it would
# run main() again — another window, another server, another 1.5GB model.
multiprocessing.freeze_support()

import fcntl
import logging
import socket
import sys
import threading
import time

import uvicorn
import webview

from backend import config
from backend import server as server_mod

log = logging.getLogger(__name__)

WINDOW_TITLE = "Video Library Search"


class WindowProvider:
    """Gives the API server access to the native window's folder dialog."""

    def __init__(self, window: "webview.Window"):
        self.window = window

    def choose_folder(self) -> str | None:
        folder_dialog = getattr(webview, "FOLDER_DIALOG", None)
        if folder_dialog is None:
            folder_dialog = webview.FileDialog.FOLDER
        result = self.window.create_file_dialog(folder_dialog)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else str(result)


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Backend server did not start in time")


def acquire_single_instance_lock():
    """Exit immediately if another instance is already running. Returns the
    lock file handle, which must stay referenced for the process lifetime."""
    lock_file = open(config.app_support_dir() / ".instance.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another instance is already running; exiting.")
        sys.exit(0)
    return lock_file


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _lock = acquire_single_instance_lock()

    port = server_mod.free_port()
    app = server_mod.create_app()
    uv_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(uv_config)
    threading.Thread(target=uv_server.run, daemon=True).start()
    _wait_for_port(port)
    log.info("Backend ready on http://127.0.0.1:%d", port)

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}",
        width=1150,
        height=780,
        min_size=(800, 600),
    )
    server_mod.window_provider = WindowProvider(window)

    # Blocks until the window is closed; daemon threads (server, indexer)
    # die with the process. Indexing commits per file, so this is safe.
    webview.start()
    uv_server.should_exit = True


if __name__ == "__main__":
    main()
