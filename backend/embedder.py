"""SigLIP 2 embedding of images and text queries, on MPS when available."""

import logging
import threading
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from . import config

log = logging.getLogger(__name__)

_model = None
_processor = None
_device = None
# Serializes model load and inference: search requests may arrive on server
# threads while the indexing worker is embedding frames.
_lock = threading.Lock()

# Global load state, set regardless of who triggered the load. The server's
# background warm-up thread calls load_model() with no progress_cb; if it
# wins the race for _lock (it usually does — it starts before any folder is
# even added), a caller that *does* pass a progress_cb would otherwise block
# on _lock and, once _model is already set, return without progress_cb ever
# firing. Pollers must read this instead of relying on a callback.
_state = "idle"  # idle | loading_model | model_ready


def state() -> str:
    return _state


def device() -> str:
    global _device
    if _device is None:
        _device = "mps" if torch.backends.mps.is_available() else "cpu"
    return _device


# Download progress, sampled from disk rather than hooked into the
# downloader: transformers/huggingface_hub don't expose byte-level progress
# through from_pretrained, but the on-disk cache is a reliable proxy — the
# same thing we'd do by hand watching a blob file grow.
_progress_lock = threading.Lock()
_progress = {"downloaded_bytes": 0, "total_bytes": None}
_watcher_started = False


def download_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _model_cache_dir() -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    folder_name = "models--" + config.MODEL_ID.replace("/", "--")
    return Path(HF_HUB_CACHE) / folder_name


def _cleanup_stale_incomplete_files(cache_dir: Path) -> None:
    """Delete orphaned partial-download files left by a previous run that
    was killed abruptly (force-quit, crash) rather than exiting normally.
    huggingface_hub gives every download attempt a fresh random temp
    filename (`_download_to_tmp_and_move` in file_download.py), so an
    orphan from a prior process can never be resumed or reused — it's
    just dead disk space (these can reach hundreds of MB each for the
    weights file). Safe to call before a new attempt starts: nothing new
    has been written yet at that point, so there's nothing active to lose."""
    blobs_dir = cache_dir / "blobs"
    if not blobs_dir.is_dir():
        return
    for f in blobs_dir.glob("*.incomplete"):
        try:
            f.unlink()
        except OSError:
            pass


def _fetch_total_bytes() -> int | None:
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(config.MODEL_ID, files_metadata=True)
        sizes = [s.size for s in info.siblings if s.size]
        return sum(sizes) if sizes else None
    except Exception as exc:
        log.warning("Could not fetch model repo size for progress display: %s", exc)
        return None  # offline, rate-limited, etc. — progress degrades to bytes-only


def _sample_downloaded_bytes(cache_dir: Path) -> int:
    """Sum the active blob per target file. A crashed/restarted app leaves
    behind older `<hash>.<random>.incomplete` files for the same target
    alongside the new attempt — and the new attempt starts smaller than
    whatever the abandoned one reached, so picking the largest picks the
    dead one. Picking by most recent mtime picks whichever is actually
    still being written."""
    blobs_dir = cache_dir / "blobs"
    if not blobs_dir.is_dir():
        return 0
    latest_by_hash: dict[str, tuple[float, int]] = {}
    for f in blobs_dir.iterdir():
        try:
            st = f.stat()
        except OSError:
            continue
        blob_hash = f.name.split(".", 1)[0]
        prev = latest_by_hash.get(blob_hash)
        if prev is None or st.st_mtime > prev[0]:
            latest_by_hash[blob_hash] = (st.st_mtime, st.st_size)
    return sum(size for _mtime, size in latest_by_hash.values())


_TOTAL_FETCH_MAX_ATTEMPTS = 5


def _watch_download() -> None:
    cache_dir = _model_cache_dir()
    total = None
    attempts = 0
    tick = 0
    while _state == "loading_model":
        if total is None and attempts < _TOTAL_FETCH_MAX_ATTEMPTS and tick % 5 == 0:
            attempts += 1
            total = _fetch_total_bytes()
            with _progress_lock:
                _progress["total_bytes"] = total
        downloaded = _sample_downloaded_bytes(cache_dir)
        with _progress_lock:
            _progress["downloaded_bytes"] = downloaded
        if total is not None and downloaded >= total:
            break
        tick += 1
        time.sleep(0.4)
    with _progress_lock:
        _progress["downloaded_bytes"] = _sample_downloaded_bytes(cache_dir)


def _start_watcher_once() -> None:
    global _watcher_started
    with _progress_lock:
        if _watcher_started:
            return
        _watcher_started = True
    threading.Thread(target=_watch_download, daemon=True).start()


def load_model(progress_cb=None):
    """Load (downloading on first run) the SigLIP 2 model. Idempotent."""
    global _model, _processor, _state
    if _model is not None:
        return
    _state = "loading_model"
    if progress_cb:
        progress_cb("loading_model")
    with _lock:
        if _model is not None:
            return
        # Import here, before starting the progress-watcher thread below.
        # huggingface_hub has internal circular imports that only resolve
        # safely when the first import happens on a single thread; if the
        # watcher thread's own `import huggingface_hub` races this one,
        # both get "cannot import name X from partially initialized
        # module" and the load silently dies (uncaught, in a daemon
        # thread) — the model never loads and no download ever starts.
        from transformers import AutoModel, AutoProcessor

        _cleanup_stale_incomplete_files(_model_cache_dir())
        _start_watcher_once()
        log.info("Loading %s on %s", config.MODEL_ID, device())
        _processor = AutoProcessor.from_pretrained(config.MODEL_ID)
        _model = AutoModel.from_pretrained(config.MODEL_ID, dtype=torch.float32)
        _model.eval().to(device())
    _state = "model_ready"
    if progress_cb:
        progress_cb("model_ready")


def release_cache() -> None:
    """Return MPS-cached blocks to the OS; called between videos so long
    indexing runs don't accumulate GPU allocator memory."""
    if device() == "mps":
        torch.mps.empty_cache()


def _as_tensor(features) -> torch.Tensor:
    """get_*_features returns a tensor in transformers 4.x but a
    BaseModelOutputWithPooling in 5.x — accept both."""
    if torch.is_tensor(features):
        return features
    return features.pooler_output


@torch.no_grad()
def embed_images(images: list[Image.Image]) -> np.ndarray:
    """Return L2-normalized float32 embeddings, shape (n, EMBEDDING_DIM)."""
    load_model()
    out = []
    for i in range(0, len(images), config.EMBED_BATCH_SIZE):
        batch = images[i : i + config.EMBED_BATCH_SIZE]
        with _lock:
            inputs = _processor(images=batch, return_tensors="pt").to(device())
            feats = _as_tensor(_model.get_image_features(**inputs))
            feats = torch.nn.functional.normalize(feats, dim=-1)
            out.append(feats.float().cpu().numpy())
    return np.concatenate(out, axis=0)


@torch.no_grad()
def embed_text(query: str) -> np.ndarray:
    """Return one L2-normalized float32 embedding, shape (EMBEDDING_DIM,)."""
    load_model()
    with _lock:
        inputs = _processor(
            text=[query],
            padding="max_length",
            max_length=config.TEXT_MAX_LENGTH,
            truncation=True,
            return_tensors="pt",
        ).to(device())
        feats = _as_tensor(_model.get_text_features(**inputs))
        feats = torch.nn.functional.normalize(feats, dim=-1)
        return feats.float().cpu().numpy()[0]
