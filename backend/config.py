"""App-wide configuration and paths."""

import os
import sys
from pathlib import Path

APP_NAME = "VideoLibrarySearch"

# Model: SigLIP 2 base. Runs on MPS, no flash-attn / CUDA-only deps.
MODEL_ID = "google/siglip2-base-patch16-224"
EMBEDDING_DIM = 768
# SigLIP text towers are trained with fixed-length padding.
TEXT_MAX_LENGTH = 64

# Frame sampling
FIXED_INTERVAL_SEC = 2.0        # fallback sampling interval
MIN_SCENE_LEN_SEC = 1.0         # ignore scenes shorter than this
MAX_SEGMENTS_PER_VIDEO = 300    # safety cap for very long / cut-heavy videos
EMBED_BATCH_SIZE = 16

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}

# Frames are downscaled to this at extraction time; must stay >= the model
# input (224) and thumbnail size.
WORKING_FRAME_MAX_DIM = 512

THUMBNAIL_MAX_DIM = 320
THUMBNAIL_JPEG_QUALITY = 80

DEFAULT_SEARCH_LIMIT = 12


def app_support_dir() -> Path:
    """Directory for the database, thumbnails and other app state."""
    override = os.environ.get("VLS_DATA_DIR")
    base = Path(override) if override else (
        Path.home() / "Library" / "Application Support" / APP_NAME
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


# In the packaged .app, keep model weights next to the DB in Application
# Support rather than ~/.cache. An explicit HF_HOME env var still wins.
if getattr(sys, "frozen", False):
    os.environ.setdefault("HF_HOME", str(app_support_dir() / "models"))


def db_path() -> Path:
    return app_support_dir() / "library.db"


def thumbnails_dir() -> Path:
    d = app_support_dir() / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d
