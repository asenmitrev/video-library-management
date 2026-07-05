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


_SCENE_DETECT_CHUNK_SEC = 30.0


def _detect_scenes(path: str, on_progress=None) -> list[tuple[float, float]]:
    """Return (start_sec, end_sec) scene spans, or [] on failure.

    Runs in chunks rather than one blocking call so `on_progress(position_sec,
    total_sec)` can report how far through a long video detection has gotten
    — otherwise a two-hour movie looks identical to a hang.
    """
    try:
        from scenedetect import ContentDetector, open_video
        from scenedetect.scene_manager import SceneManager

        video = open_video(path)
        total_sec = video.duration.seconds if video.duration else None
        sm = SceneManager()
        sm.add_detector(ContentDetector(min_scene_len=15))

        while True:
            processed = sm.detect_scenes(video, duration=_SCENE_DETECT_CHUNK_SEC)
            if on_progress:
                on_progress(video.position.seconds, total_sec)
            if processed == 0:
                break

        spans = [(s.seconds, e.seconds) for s, e in sm.get_scene_list()]
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


def extract_segments(path: str, on_phase=None) -> list[Segment]:
    """Sample representative frames.

    Scene detection first; fixed-interval fallback when it fails or the
    video is too short/uniform to yield at least two scenes.

    `on_phase(phase, current, total)` reports progress through the
    sub-steps of indexing this one file — the caller otherwise only knows
    "we're on file N of M", which for a large video can sit unchanged for
    minutes and look frozen.
    """
    duration = video_duration_sec(path)
    if duration is None:
        log.warning("Could not open video: %s", path)
        return []

    def scene_progress(position_sec, total_sec):
        if on_phase and total_sec:
            on_phase("detecting_scenes", round(position_sec), round(total_sec))

    if on_phase:
        on_phase("detecting_scenes", 0, round(duration))
    spans = _detect_scenes(path, on_progress=scene_progress)
    if len(spans) < 2:
        spans = _fixed_interval_spans(duration)
    spans = _thin_spans(spans)

    cap = cv2.VideoCapture(path)
    try:
        segments = []
        for i, (start, end) in enumerate(spans):
            frame = _grab_frame(cap, (start + end) / 2.0)
            if frame is None:
                # Seek can fail near EOF for some containers; try the start.
                frame = _grab_frame(cap, start)
            if frame is not None:
                segments.append(Segment(start, end, frame))
            if on_phase:
                on_phase("extracting_frames", i + 1, len(spans))
        return segments
    finally:
        cap.release()


def save_thumbnail(frame: Image.Image, dest: Path) -> None:
    thumb = frame.copy()
    thumb.thumbnail((config.THUMBNAIL_MAX_DIM, config.THUMBNAIL_MAX_DIM))
    thumb.save(dest, "JPEG", quality=config.THUMBNAIL_JPEG_QUALITY)
