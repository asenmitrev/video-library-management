"""Frame sampling: scene-change detection with fixed-interval fallback."""

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
from PIL import Image

from . import config

log = logging.getLogger(__name__)


@dataclass
class Segment:
    start_sec: float
    end_sec: float
    frame: Image.Image  # representative frame (midpoint of the segment)


def video_duration_sec(path: str) -> float | None:
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
        nframes = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if fps and fps > 0 and nframes and nframes > 0:
            return nframes / fps
        return None
    finally:
        cap.release()


def _detect_scenes(path: str) -> list[tuple[float, float]]:
    """Return (start_sec, end_sec) scene spans, or [] on failure."""
    try:
        from scenedetect import ContentDetector, detect

        scenes = detect(path, ContentDetector(min_scene_len=15), show_progress=False)
        spans = [(s.get_seconds(), e.get_seconds()) for s, e in scenes]
        return [
            (s, e) for s, e in spans if e - s >= config.MIN_SCENE_LEN_SEC
        ]
    except Exception as exc:
        log.warning("Scene detection failed for %s: %s", path, exc)
        return []


def _fixed_interval_spans(duration: float) -> list[tuple[float, float]]:
    step = config.FIXED_INTERVAL_SEC
    spans = []
    t = 0.0
    while t < duration:
        spans.append((t, min(t + step, duration)))
        t += step
    return spans or [(0.0, duration)]


def _thin_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cap = config.MAX_SEGMENTS_PER_VIDEO
    if len(spans) <= cap:
        return spans
    stride = len(spans) / cap
    return [spans[int(i * stride)] for i in range(cap)]


def _grab_frame(cap: cv2.VideoCapture, at_sec: float) -> Image.Image | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, at_sec * 1000.0)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    # Downscale immediately: the model sees 224px and thumbnails are 320px,
    # so holding hundreds of full-res (possibly 4K) frames would waste GBs.
    h, w = frame.shape[:2]
    max_dim = config.WORKING_FRAME_MAX_DIM
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        frame = cv2.resize(
            frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def extract_segments(path: str) -> list[Segment]:
    """Sample representative frames.

    Scene detection first; fixed-interval fallback when it fails or the
    video is too short/uniform to yield at least two scenes.
    """
    duration = video_duration_sec(path)
    if duration is None:
        log.warning("Could not open video: %s", path)
        return []

    spans = _detect_scenes(path)
    if len(spans) < 2:
        spans = _fixed_interval_spans(duration)
    spans = _thin_spans(spans)

    cap = cv2.VideoCapture(path)
    try:
        segments = []
        for start, end in spans:
            frame = _grab_frame(cap, (start + end) / 2.0)
            if frame is None:
                # Seek can fail near EOF for some containers; try the start.
                frame = _grab_frame(cap, start)
            if frame is not None:
                segments.append(Segment(start, end, frame))
        return segments
    finally:
        cap.release()


def save_thumbnail(frame: Image.Image, dest: Path) -> None:
    thumb = frame.copy()
    thumb.thumbnail((config.THUMBNAIL_MAX_DIM, config.THUMBNAIL_MAX_DIM))
    thumb.save(dest, "JPEG", quality=config.THUMBNAIL_JPEG_QUALITY)
