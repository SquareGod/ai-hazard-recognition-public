from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _deep_get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    value: Any = data
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _resolve(path_value: str | Path, *, base: Path = ROOT) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str
    port: int
    catalog_excel: Path
    catalog_sheet: str
    catalog_first_row: int
    catalog_label_count: int
    data_dir: Path
    log_dir: Path
    model_dir: Path
    max_upload_mb: int
    detector_provider: str
    detector_model: Path
    detector_device: str
    detector_realtime_imgsz: int
    detector_inspection_imgsz: int
    detector_confidence: float
    detector_iou: float
    default_video_fps: float
    realtime_fps: float
    max_frames: int
    camera_duration_sec: int
    blur_threshold: float
    min_brightness: float
    max_brightness: float
    duplicate_hamming_threshold: int
    vlm_provider: str
    qwen_api_key: str
    qwen_base_url: str
    qwen_model: str
    vlm_temperature: float
    vlm_timeout_sec: int
    vlm_max_images_per_call: int
    full_scan_every_n_frames: int
    video_confirm_frames: int
    allow_single_image_confirmation: bool
    downgrade_external_evidence_labels: bool
    max_labels_per_call: int
    decision_mode: str
    event_cooldown_sec: int
    snapshot_quality: int
    mediamtx_control_url: str
    mediamtx_api_user: str
    mediamtx_api_password: str
    mediamtx_rtsp_base: str
    mediamtx_read_user: str
    mediamtx_read_password: str
    mediamtx_webrtc_base: str
    mediamtx_hls_base: str
    control_api_key: str
    log_level: str
    email_smtp_host: str
    email_smtp_port: int
    email_smtp_username: str
    email_smtp_password: str
    email_smtp_use_ssl: bool
    email_from_name: str
    hikvision_bridge_url: str
    hikvision_bridge_token: str
    inspection_interval_sec: int
    test_stream_interval_sec: int
    stream_start_ready_timeout_sec: float
    stream_first_inspection_delay_sec: float
    order_cooldown_sec: int
    hikvision_heartbeat_interval_sec: int


def load_settings() -> Settings:
    config_path = ROOT / "config" / "default.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    data_dir = _resolve(_deep_get(raw, "storage.data_dir", "data"))
    log_dir = _resolve(_deep_get(raw, "storage.log_dir", "logs"))
    model_dir = ROOT / "models"
    detector_model_cfg = os.getenv(
        "DETECTOR_MODEL_PATH", str(_deep_get(raw, "detector.model_path", "models/yolov8s-worldv2.pt"))
    )

    settings = Settings(
        root=ROOT,
        host=str(_deep_get(raw, "server.host", "127.0.0.1")),
        port=int(_deep_get(raw, "server.port", 8010)),
        catalog_excel=_resolve(_deep_get(raw, "catalog.excel_path")),
        catalog_sheet=str(_deep_get(raw, "catalog.sheet", "逐项设计")),
        catalog_first_row=int(_deep_get(raw, "catalog.first_data_row", 3)),
        catalog_label_count=int(_deep_get(raw, "catalog.label_count", 65)),
        data_dir=data_dir,
        log_dir=log_dir,
        model_dir=model_dir,
        max_upload_mb=int(_deep_get(raw, "storage.max_upload_mb", 500)),
        detector_provider=os.getenv("DETECTOR_PROVIDER", str(_deep_get(raw, "detector.provider", "yolo_world"))).strip().lower(),
        detector_model=_resolve(detector_model_cfg),
        detector_device=os.getenv("DETECTOR_DEVICE", str(_deep_get(raw, "detector.device", "auto"))).strip(),
        detector_realtime_imgsz=int(_deep_get(raw, "detector.realtime_imgsz", 960)),
        detector_inspection_imgsz=int(_deep_get(raw, "detector.inspection_imgsz", 1280)),
        detector_confidence=float(_deep_get(raw, "detector.confidence", 0.18)),
        detector_iou=float(_deep_get(raw, "detector.iou", 0.50)),
        default_video_fps=float(_deep_get(raw, "sampling.default_video_fps", 1.0)),
        realtime_fps=float(_deep_get(raw, "sampling.realtime_fps", 4.0)),
        max_frames=int(_deep_get(raw, "sampling.max_frames", 12)),
        camera_duration_sec=int(_deep_get(raw, "sampling.camera_duration_sec", 10)),
        blur_threshold=float(_deep_get(raw, "sampling.blur_threshold", 35.0)),
        min_brightness=float(_deep_get(raw, "sampling.min_brightness", 18.0)),
        max_brightness=float(_deep_get(raw, "sampling.max_brightness", 242.0)),
        duplicate_hamming_threshold=int(_deep_get(raw, "sampling.duplicate_hamming_threshold", 5)),
        vlm_provider=os.getenv("VLM_PROVIDER", str(_deep_get(raw, "vlm.provider", "qwen"))).strip().lower(),
        qwen_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        qwen_base_url=os.getenv("QWEN_BASE_URL", str(_deep_get(raw, "vlm.base_url"))).rstrip("/"),
        qwen_model=os.getenv("QWEN_MODEL", str(_deep_get(raw, "vlm.model", "qwen3-vl-plus"))).strip(),
        vlm_temperature=float(_deep_get(raw, "vlm.temperature", 0.1)),
        vlm_timeout_sec=int(_deep_get(raw, "vlm.timeout_sec", 180)),
        vlm_max_images_per_call=int(_deep_get(raw, "vlm.max_images_per_call", 4)),
        full_scan_every_n_frames=int(_deep_get(raw, "vlm.full_scan_every_n_frames", 5)),
        video_confirm_frames=int(_deep_get(raw, "harness.video_confirm_frames", 2)),
        allow_single_image_confirmation=bool(_deep_get(raw, "harness.allow_single_image_confirmation", True)),
        downgrade_external_evidence_labels=bool(_deep_get(raw, "harness.downgrade_external_evidence_labels", True)),
        max_labels_per_call=int(os.getenv("HARNESS_MAX_LABELS_PER_CALL", str(_deep_get(raw, "harness.max_labels_per_call", 15)))),
        decision_mode=os.getenv(
            "HAZARD_DECISION_MODE", str(_deep_get(raw, "harness.decision_mode", "direct"))
        ).strip().lower(),
        event_cooldown_sec=int(_deep_get(raw, "streams.event_cooldown_sec", 60)),
        snapshot_quality=int(_deep_get(raw, "streams.snapshot_quality", 95)),
        mediamtx_control_url=os.getenv("MEDIAMTX_CONTROL_URL", str(_deep_get(raw, "stream_gateway.control_url", "http://127.0.0.1:9997"))).rstrip("/"),
        mediamtx_api_user=os.getenv("MEDIAMTX_API_USER", "").strip(),
        mediamtx_api_password=os.getenv("MEDIAMTX_API_PASSWORD", "").strip(),
        mediamtx_rtsp_base=os.getenv("MEDIAMTX_RTSP_BASE", str(_deep_get(raw, "stream_gateway.rtsp_base", "rtsp://127.0.0.1:8554"))).rstrip("/"),
        mediamtx_read_user=os.getenv("MEDIAMTX_READ_USER", "").strip(),
        mediamtx_read_password=os.getenv("MEDIAMTX_READ_PASSWORD", "").strip(),
        mediamtx_webrtc_base=os.getenv("MEDIAMTX_WEBRTC_BASE", str(_deep_get(raw, "stream_gateway.webrtc_base", "http://127.0.0.1:8889"))).rstrip("/"),
        mediamtx_hls_base=os.getenv("MEDIAMTX_HLS_BASE", str(_deep_get(raw, "stream_gateway.hls_base", "http://127.0.0.1:8888"))).rstrip("/"),
        control_api_key=os.getenv("CONTROL_API_KEY", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        email_smtp_host=os.getenv("EMAIL_SMTP_HOST", "").strip(),
        email_smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "465")),
        email_smtp_username=os.getenv("EMAIL_SMTP_USERNAME", "").strip(),
        email_smtp_password=os.getenv("EMAIL_SMTP_PASSWORD", "").strip(),
        email_smtp_use_ssl=os.getenv("EMAIL_SMTP_USE_SSL", "true").strip().lower() in {"1", "true", "yes"},
        email_from_name=os.getenv("EMAIL_FROM_NAME", "智筑云AI隐患提醒").strip(),
        hikvision_bridge_url=os.getenv("HIKVISION_BRIDGE_URL", "http://127.0.0.1:8020").rstrip("/"),
        hikvision_bridge_token=os.getenv("HIKVISION_BRIDGE_TOKEN", "").strip(),
        inspection_interval_sec=int(os.getenv("INSPECTION_INTERVAL_SEC", str(_deep_get(raw, "sampling.inspection_interval_sec", 300)))),
        test_stream_interval_sec=int(os.getenv("TEST_STREAM_INTERVAL_SEC", str(_deep_get(raw, "sampling.test_stream_interval_sec", 3)))),
        stream_start_ready_timeout_sec=float(os.getenv("STREAM_START_READY_TIMEOUT_SEC", "25")),
        stream_first_inspection_delay_sec=float(os.getenv("STREAM_FIRST_INSPECTION_DELAY_SEC", "5")),
        order_cooldown_sec=int(os.getenv("ORDER_COOLDOWN_SEC", str(_deep_get(raw, "streams.order_cooldown_sec", 600)))),
        hikvision_heartbeat_interval_sec=int(os.getenv("HIKVISION_HEARTBEAT_INTERVAL_SEC", "30")),
    )
    if settings.decision_mode not in {"direct", "strict"}:
        raise ValueError("HAZARD_DECISION_MODE must be direct or strict")
    if settings.host not in {"127.0.0.1", "localhost", "::1"} and not settings.control_api_key:
        raise ValueError("对外监听后端时必须配置 CONTROL_API_KEY，并由登录网关注入 X-Control-Key")
    for directory in (settings.data_dir, settings.log_dir, settings.model_dir, ROOT / "runtime"):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


settings = load_settings()

