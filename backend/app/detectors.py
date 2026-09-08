from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from .config import settings
from .logging_config import logger
from .schemas import Detection, FrameInfo


class Detector(ABC):
    provider_name = "base"

    @abstractmethod
    def detect(self, frame: FrameInfo, mode: str, annotated_dir: Path) -> list[Detection]:
        raise NotImplementedError


def load_prompt_classes() -> list[dict[str, Any]]:
    path = settings.root / "config" / "detector_prompts.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("classes", []))


class DisabledDetector(Detector):
    provider_name = "disabled"

    def detect(self, frame: FrameInfo, mode: str, annotated_dir: Path) -> list[Detection]:
        return []


class MockDetector(Detector):
    provider_name = "mock"

    def detect(self, frame: FrameInfo, mode: str, annotated_dir: Path) -> list[Detection]:
        width, height = frame.width, frame.height
        return [
            Detection(
                object_id=f"{frame.frame_id}_person_1",
                label="person",
                display_name="人员",
                score=0.91,
                bbox_xyxy=[int(width * 0.2), int(height * 0.1), int(width * 0.6), int(height * 0.95)],
                frame_id=frame.frame_id,
            ),
            Detection(
                object_id=f"{frame.frame_id}_head_1",
                label="head",
                display_name="头部",
                score=0.84,
                bbox_xyxy=[int(width * 0.32), int(height * 0.12), int(width * 0.46), int(height * 0.30)],
                frame_id=frame.frame_id,
            ),
        ]


class UltralyticsDetector(Detector):
    def __init__(self, world: bool) -> None:
        self.world = world
        self.provider_name = "yolo_world" if world else "yolo"
        self._models: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._prompt_classes = load_prompt_classes()
        self._display_names = {str(item["id"]): str(item["name"]) for item in self._prompt_classes}
        self._mode_classes = {
            "realtime": [item for item in self._prompt_classes if item.get("realtime")],
            "inspection": self._prompt_classes,
        }

    def _load(self, mode: str):
        mode_key = "realtime" if mode == "realtime" else "inspection"
        if mode_key in self._models:
            return self._models[mode_key]
        if not settings.detector_model.exists():
            raise FileNotFoundError(
                f"小模型权重不存在：{settings.detector_model}。请先运行 scripts/download_models.py"
            )
        if self.world:
            from ultralytics import YOLOWorld

            prepared = settings.model_dir / f"{settings.detector_model.stem}-{mode_key}.pt"
            if prepared.exists():
                model = YOLOWorld(str(prepared))
            else:
                model = YOLOWorld(str(settings.detector_model))
                model.set_classes([str(item["prompt"]) for item in self._mode_classes[mode_key]])
                logger.warning(
                    "未找到预编码词表模型%s，首次初始化会很慢；建议重新运行download_models.bat",
                    prepared,
                )
        else:
            from ultralytics import YOLO

            model = YOLO(str(settings.detector_model))
        self._models[mode_key] = model
        return model

    @staticmethod
    def _device() -> str:
        if settings.detector_device != "auto":
            return settings.detector_device
        try:
            import torch

            return "0" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def detect(self, frame: FrameInfo, mode: str, annotated_dir: Path) -> list[Detection]:
        mode_key = "realtime" if mode == "realtime" else "inspection"
        mode_classes = self._mode_classes[mode_key]
        class_ids = [str(item["id"]) for item in mode_classes]
        model = self._load(mode_key)
        imgsz = settings.detector_realtime_imgsz if mode == "realtime" else settings.detector_inspection_imgsz
        with self._lock:
            results = model.predict(
                source=frame.path,
                imgsz=imgsz,
                conf=settings.detector_confidence,
                iou=settings.detector_iou,
                device=self._device(),
                verbose=False,
            )
        detections: list[Detection] = []
        if not results:
            return detections
        result = results[0]
        names = result.names
        if result.boxes is not None:
            for index, box in enumerate(result.boxes):
                class_index = int(box.cls.item())
                raw_name = str(names[class_index])
                label = class_ids[class_index] if self.world and class_index < len(class_ids) else raw_name
                xyxy = [int(round(value)) for value in box.xyxy[0].tolist()]
                detections.append(
                    Detection(
                        object_id=f"{frame.frame_id}_{label}_{index + 1}",
                        label=label,
                        display_name=self._display_names.get(label, raw_name),
                        score=round(float(box.conf.item()), 4),
                        bbox_xyxy=xyxy,
                        frame_id=frame.frame_id,
                    )
                )
        annotated_dir.mkdir(parents=True, exist_ok=True)
        annotated = self._draw(frame, detections)
        target = annotated_dir / f"{frame.frame_id}_detected.jpg"
        cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tofile(str(target))
        frame.annotated_path = str(target)
        return detections

    @staticmethod
    def _draw(frame: FrameInfo, detections: list[Detection]) -> np.ndarray:
        image = cv2.imdecode(np.fromfile(frame.path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"无法读取检测帧：{frame.path}")
        for item in detections:
            x1, y1, x2, y2 = item.bbox_xyxy
            cv2.rectangle(image, (x1, y1), (x2, y2), (31, 180, 84), 2)
            cv2.putText(
                image,
                f"{item.label} {item.score:.2f}",
                (x1, max(20, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (31, 180, 84),
                2,
                cv2.LINE_AA,
            )
        return image


def create_detector() -> Detector:
    provider = settings.detector_provider
    if provider == "mock":
        return MockDetector()
    if provider == "disabled":
        return DisabledDetector()
    if provider == "yolo_world":
        return UltralyticsDetector(world=True)
    if provider == "yolo":
        return UltralyticsDetector(world=False)
    raise ValueError(f"不支持的DETECTOR_PROVIDER：{provider}")


@lru_cache(maxsize=1)
def get_shared_detector() -> Detector:
    """进程内复用同一份模型权重，避免每个任务重复占用内存/显存。"""
    return create_detector()


def route_categories(detections: list[Detection]) -> set[str]:
    prompt_classes = load_prompt_classes()
    route_map = {str(item["id"]): set(item.get("routes", [])) for item in prompt_classes}
    categories: set[str] = set()
    for detection in detections:
        categories.update(route_map.get(detection.label, set()))
    return categories
