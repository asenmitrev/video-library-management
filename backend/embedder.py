"""SigLIP 2 embedding of images and text queries, on MPS when available."""

import logging
import threading

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


def device() -> str:
    global _device
    if _device is None:
        _device = "mps" if torch.backends.mps.is_available() else "cpu"
    return _device


def load_model(progress_cb=None):
    """Load (downloading on first run) the SigLIP 2 model. Idempotent."""
    global _model, _processor
    with _lock:
        if _model is not None:
            return
        from transformers import AutoModel, AutoProcessor

        if progress_cb:
            progress_cb("loading_model")
        log.info("Loading %s on %s", config.MODEL_ID, device())
        _processor = AutoProcessor.from_pretrained(config.MODEL_ID)
        _model = AutoModel.from_pretrained(config.MODEL_ID, dtype=torch.float32)
        _model.eval().to(device())
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
