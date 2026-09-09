from __future__ import annotations

import base64
import json
import os

import numpy as np
from abc import ABC, abstractmethod
from pathlib import Path

from openai import OpenAI

from .config import settings
from .logging_config import logger
from .media import mime_type
from .schemas import Detection, FrameInfo, HazardLabel, VLMResponse
from .algorithm_adapters import AlgorithmContext, create_algorithm_adapter


class VLMProvider(ABC):
    provider_name = "base"

    @abstractmethod
    def analyze(
        self,
        frames: list[FrameInfo],
        labels: list[HazardLabel],
        detections: list[Detection],
        context: AlgorithmContext | None = None,
    ) -> VLMResponse:
        raise NotImplementedError


class MockVLMProvider(VLMProvider):
    provider_name = "mock"

    def analyze(self, frames: list[FrameInfo], labels: list[HazardLabel], detections: list[Detection], context: AlgorithmContext | None = None) -> VLMResponse:
        return VLMResponse(scene_summary="mock模式：未调用云端模型", findings=[])


class QwenVLMProvider(VLMProvider):
    provider_name = "qwen"

    def __init__(self) -> None:
        if not settings.qwen_api_key:
            raise RuntimeError("未设置DASHSCOPE_API_KEY，请复制.env.example为.env并填写API Key")
        self.client = OpenAI(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            timeout=settings.vlm_timeout_sec,
        )
        self.system_prompt = (settings.root / "prompts" / "system_prompt.txt").read_text(encoding="utf-8")

    @staticmethod
    def _data_url(path: Path) -> str:
        """大图先降采样再上传：原图base64后可达数MB，是VLM单次调用延迟的主因之一。"""
        max_px = int(os.getenv("VLM_MAX_IMAGE_PX", "1280"))
        data, mime = path.read_bytes(), mime_type(path)
        if max_px > 0 and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
            try:
                import cv2
                image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is not None:
                    height, width = image.shape[:2]
                    if max(width, height) > max_px:
                        scale = max(width, height) / max_px
                        image = cv2.resize(image, (round(width / scale), round(height / scale)), interpolation=cv2.INTER_AREA)
                        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        if ok:
                            data, mime = buffer.tobytes(), "image/jpeg"
            except Exception as exc:
                logger.warning("VLM图片降采样失败，改用原图：%s", exc)
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _label_text(labels: list[HazardLabel]) -> str:
        return "\n".join(
            f"- {item.id} | {item.category} | {item.name} | 检查方法：{item.inspection_method}"
            for item in labels
        )

    @staticmethod
    def _detection_text(detections: list[Detection]) -> str:
        if not detections:
            return "小模型未提供候选对象；这不代表画面中不存在对象。"
        return "\n".join(
            f"- {item.frame_id}: {item.object_id}, {item.label}, score={item.score}, bbox={item.bbox_xyxy}"
            for item in detections
        )

    def analyze(self, frames: list[FrameInfo], labels: list[HazardLabel], detections: list[Detection], context: AlgorithmContext | None = None) -> VLMResponse:
        if settings.decision_mode == "direct":
            decision_policy = "\u5f53\u524d\u4e3a\u6a21\u578b\u76f4\u51fa\u4f18\u5148\u6a21\u5f0f\uff1a\u53ea\u8981\u56fe\u7247\u4e2d\u5b58\u5728\u6e05\u6670\u3001\u5177\u4f53\u3001\u53ef\u89c1\u7684\u8bc1\u636e\uff0c\u4e14\u80fd\u5339\u914d\u672c\u6b21\u6807\u7b7e\u5e93\uff0c\u8bf7\u8fd4\u56deconfirmed_hazard\u3002\u4e0d\u8981\u4ec5\u56e0\u7f3a\u5c11\u6d4b\u91cf\u5c3a\u3001\u53f0\u8d26\u3001\u8bd5\u9a8c\u62a5\u544a\u6216\u7b2c\u4e8c\u5e27\u5c31\u964d\u4e3areview_required\uff1b\u53ea\u6709\u753b\u9762\u6a21\u7cca\u3001\u906e\u6321\u3001\u5bf9\u8c61\u8fc7\u8fdc\u3001\u68c0\u67e5\u4f4d\u7f6e\u4e0d\u53ef\u89c1\u6216\u8bc1\u636e\u4e92\u76f8\u77db\u76fe\u65f6\u624d\u8fd4\u56dereview_required\u3002"
        else:
            decision_policy = "\u5f53\u524d\u4e3a\u4e25\u683c\u9a8c\u6536\u6a21\u5f0f\uff1a\u7f3a\u5c11\u5916\u90e8\u8bc1\u636e\u65f6\u9700\u8fd4\u56dereview_required\u3002"
        user_text = f"""{decision_policy}

证据必须是画面中已经出现的对象和状态。严禁用“如果、若、可能、推断为、疑似存在、通常会有”等假设补全不可见对象；例如画面没有配电箱或塔吊时，不得输出配电箱或塔吊隐患。无法从画面直接证明的标签不要输出。

请核验以下图片。图片引用ID为：{', '.join(frame.frame_id for frame in frames)}。

本次允许的隐患标签：
{self._label_text(labels)}

小模型候选对象：
{self._detection_text(detections)}

输出JSON结构：
{{
  "scene_summary": "简短场景描述",
  "findings": [
    {{
      "label_id": "H001",
      "status": "confirmed_hazard或review_required",
      "evidence": "图片中实际可见证据",
      "visible_objects": ["可见对象"],
      "source_frame_ids": ["frame_0001"],
      "inspection_visibility": "clear或partial或unclear",
      "missing_external_evidence": ["缺少的测量/台账/试验数据"],
      "severity": "general或major，必须二选一",
      "severity_reason": "依据画面可见危险后果、影响范围和紧迫程度给出的等级理由",
      "severity_source": "model"
    }}
  ]
}}
若没有明确隐患，findings必须返回空数组。"""
        content: list[dict] = [{"type": "text", "text": user_text}]
        for frame in frames:
            content.append({"type": "text", "text": f"图片引用：{frame.frame_id}"})
            content.append({"type": "image_url", "image_url": {"url": self._data_url(Path(frame.path))}})
            # The source image is authoritative; sending its annotated twin doubles
            # tokens and can bias the VLM. Enable only for troubleshooting.
            if os.getenv("VLM_INCLUDE_ANNOTATED_IMAGES", "false").lower() in {"1", "true", "yes"} and frame.annotated_path and Path(frame.annotated_path).exists():
                content.append({"type": "text", "text": f"{frame.frame_id}的小模型标框参考图（可能存在误检）"})
                content.append({"type": "image_url", "image_url": {"url": self._data_url(Path(frame.annotated_path))}})
        completion = self.client.chat.completions.create(
            model=settings.qwen_model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=settings.vlm_temperature,
            response_format={"type": "json_object"},
        )
        raw = completion.choices[0].message.content or "{}"
        try:
            return VLMResponse.model_validate_json(raw)
        except Exception as exc:
            logger.error("VLM返回JSON解析失败：%s", raw[:2000])
            raise ValueError(f"Qwen返回内容不符合结构化格式：{exc}") from exc


def create_vlm_provider() -> VLMProvider:
    adapter = create_algorithm_adapter()
    if adapter is not None:
        return ExternalAlgorithmVLMProvider(adapter)
    if settings.vlm_provider == "mock":
        return MockVLMProvider()
    if settings.vlm_provider == "qwen":
        return QwenVLMProvider()
    raise ValueError(f"不支持的VLM_PROVIDER：{settings.vlm_provider}")


class ExternalAlgorithmVLMProvider(VLMProvider):
    """Makes a complete external algorithm look like the existing VLM stage."""

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter
        self.provider_name = getattr(adapter, "provider_name", "external_algorithm")

    def analyze(self, frames: list[FrameInfo], labels: list[HazardLabel], detections: list[Detection], context: AlgorithmContext | None = None) -> VLMResponse:
        return self.adapter.analyze(frames, labels, detections, context)

