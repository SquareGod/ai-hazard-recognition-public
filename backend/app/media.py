from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import settings
from .schemas import FrameInfo


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}


def safe_filename(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", stem).strip("._") or "media"
    return f"{stem[:100]}{suffix}"


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def image_quality(image: np.ndarray) -> tuple[float, float, str]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    usable = (
        blur >= settings.blur_threshold
        and settings.min_brightness <= brightness <= settings.max_brightness
    )
    return blur, brightness, "usable" if usable else "low_quality"


def difference_hash(image: np.ndarray) -> int:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = resized[:, 1:] > resized[:, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bit)
    return value


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _frame_info(frame_id: str, path: Path, image: np.ndarray, source_time_sec: float) -> FrameInfo:
    blur, brightness, status = image_quality(image)
    height, width = image.shape[:2]
    return FrameInfo(
        frame_id=frame_id,
        path=str(path),
        source_time_sec=round(source_time_sec, 3),
        width=width,
        height=height,
        blur_score=round(blur, 2),
        brightness=round(brightness, 2),
        quality_status=status,
    )


def prepare_image(source: Path, frame_dir: Path) -> list[FrameInfo]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    image = cv2.imdecode(np.fromfile(str(source), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片：{source.name}")
    target = frame_dir / f"frame_0001{source.suffix.lower() if source.suffix.lower() in IMAGE_EXTENSIONS else '.jpg'}"
    shutil.copy2(source, target)
    return [_frame_info("frame_0001", target, image, 0.0)]


def _capture_source(source: str | Path) -> cv2.VideoCapture:
    source_text = str(source)
    capture_source: int | str = int(source_text) if source_text.isdigit() else source_text
    capture = cv2.VideoCapture(capture_source)
    if not capture.isOpened():
        capture.release()
        raise ConnectionError("无法打开视频或摄像头，请检查地址、网络、账号密码、端口和取流权限")
    return capture


def sample_video(
    source: str | Path,
    frame_dir: Path,
    *,
    sample_fps: float,
    max_frames: int,
    duration_sec: int | None = None,
) -> list[FrameInfo]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    capture = _capture_source(source)
    native_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    if native_fps <= 0 or native_fps > 240:
        native_fps = 25.0
    step = max(1, int(round(native_fps / max(sample_fps, 0.1))))
    frame_index = 0
    kept: list[FrameInfo] = []
    recent_hashes: list[int] = []
    max_source_frames = int(native_fps * duration_sec) if duration_sec else None
    try:
        while len(kept) < max_frames:
            ok, image = capture.read()
            if not ok or image is None:
                break
            if max_source_frames is not None and frame_index >= max_source_frames:
                break
            if frame_index % step != 0:
                frame_index += 1
                continue
            frame_hash = difference_hash(image)
            if recent_hashes and min(hamming_distance(frame_hash, old) for old in recent_hashes[-5:]) <= settings.duplicate_hamming_threshold:
                frame_index += 1
                continue
            recent_hashes.append(frame_hash)
            frame_id = f"frame_{len(kept) + 1:04d}"
            target = frame_dir / f"{frame_id}.jpg"
            cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, settings.snapshot_quality])[1].tofile(str(target))
            kept.append(_frame_info(frame_id, target, image, frame_index / native_fps))
            frame_index += 1
    finally:
        capture.release()
    if not kept:
        raise ValueError("没有提取到可用视频帧")
    return kept


def test_camera(source_url: str) -> dict[str, object]:
    capture = _capture_source(source_url)
    try:
        ok, image = capture.read()
        if not ok or image is None:
            raise ConnectionError("摄像头已连接但未读取到画面")
        height, width = image.shape[:2]
        blur, brightness, status = image_quality(image)
        return {
            "ok": True,
            "width": width,
            "height": height,
            "fps": float(capture.get(cv2.CAP_PROP_FPS) or 0),
            "blur_score": round(blur, 2),
            "brightness": round(brightness, 2),
            "quality_status": status,
        }
    finally:
        capture.release()


def mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

