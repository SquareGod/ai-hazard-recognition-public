from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HazardLabel(BaseModel):
    id: str
    category: str
    name: str
    inspection_method: str


class Detection(BaseModel):
    object_id: str
    label: str
    display_name: str
    score: float = Field(ge=0, le=1)
    bbox_xyxy: list[int] = Field(min_length=4, max_length=4)
    frame_id: str


class FrameInfo(BaseModel):
    frame_id: str
    path: str
    source_time_sec: float = 0
    width: int
    height: int
    blur_score: float
    brightness: float
    quality_status: Literal["usable", "low_quality"] = "usable"
    detections: list[Detection] = Field(default_factory=list)
    annotated_path: str | None = None
    artifact_url: str | None = None
    annotated_url: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class VLMFinding(BaseModel):
    label_id: str
    status: Literal["confirmed_hazard", "review_required"]
    evidence: str = Field(min_length=1)
    visible_objects: list[str] = Field(default_factory=list)
    source_frame_ids: list[str] = Field(default_factory=list)
    inspection_visibility: Literal["clear", "partial", "unclear"] = "unclear"
    missing_external_evidence: list[str] = Field(default_factory=list)
    severity: Literal["general", "major"]
    severity_reason: str = Field(min_length=1)
    severity_source: Literal["model", "realtime_rule", "catalog_rule"] = "model"


class VLMResponse(BaseModel):
    scene_summary: str = ""
    findings: list[VLMFinding] = Field(default_factory=list)


class AggregatedFinding(BaseModel):
    label_id: str
    category: str
    name: str
    final_status: Literal["confirmed_hazard", "review_required"]
    evidence: list[str]
    source_frame_ids: list[str]
    occurrence_count: int
    missing_external_evidence: list[str] = Field(default_factory=list)
    harness_notes: list[str] = Field(default_factory=list)
    severity: Literal["general", "major"]
    severity_name: Literal["一般隐患", "重大隐患"]
    severity_reason: str
    severity_source: Literal["model", "realtime_rule", "catalog_rule"]
    severity_rule_version: str


class AnalysisResult(BaseModel):
    job_id: str
    mode: Literal["realtime", "inspection", "test"]
    source_type: Literal["image", "video", "camera"]
    created_at: str = Field(default_factory=utc_now)
    detector_provider: str
    detector_model: str
    vlm_provider: str
    vlm_model: str
    frames: list[FrameInfo]
    findings: list[AggregatedFinding]
    no_clear_hazard: bool
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class CameraRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=100)
    source_url: str = Field(min_length=1)
    work_area: str = Field(default="测试工区", min_length=1, max_length=100)
    duration_sec: int = Field(default=10, ge=1, le=300)
    sample_fps: float = Field(default=1.0, ge=0.1, le=10)
    mode: Literal["realtime", "inspection", "test"] = "inspection"

    @field_validator("source_url")
    @classmethod
    def supported_source(cls, value: str) -> str:
        if value.startswith(("rtsp://", "http://", "https://")) or value.isdigit():
            return value
        raise ValueError("source_url必须是RTSP/HTTP(S)地址，或本机摄像头数字编号")


class StreamStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(min_length=1, max_length=100)
    source_url: str = Field(min_length=1)
    inference_fps: float = Field(default=4.0, ge=0.2, le=15)
    work_area: str = Field(default="测试工区", min_length=1, max_length=100)
    auto_email: bool = True

    @field_validator("source_url")
    @classmethod
    def supported_source(cls, value: str) -> str:
        if value.startswith(("rtsp://", "http://", "https://")) or value.isdigit():
            return value
        raise ValueError("source_url必须是RTSP/HTTP(S)地址，或本机摄像头数字编号")


class VideoSourceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    source_type: Literal["rtsp", "hikvision", "hcnetsdk"] = "rtsp"
    source_url: str = Field(min_length=1)
    work_area: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    risk_point: str = Field(default="", max_length=100)
    ai_enabled: bool = False


class HikvisionProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(default="", max_length=256)


class HikvisionChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    work_area: str | None = Field(default=None, min_length=1, max_length=100)
    risk_point: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    ai_enabled: bool | None = None


class StreamSessionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=100)
    inference_fps: float = Field(default=4.0, ge=0.2, le=15)
    auto_email: bool = True
    analysis_mode: Literal["realtime", "inspection", "test"] = "realtime"
    inspection_interval_sec: int = Field(default=300, ge=30, le=86400)


class RectificationSubmitRequest(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    submitted_by: str = Field(default="整改责任人", min_length=1, max_length=100)
    after_image: str | None = Field(default=None, max_length=500)


class HazardReviewRequest(BaseModel):
    passed: bool
    reviewer: str = Field(default="安全员", min_length=1, max_length=100)
    comment: str = Field(default="", max_length=1000)


class HazardVerificationRequest(BaseModel):
    # ``verified`` is retained for compatibility with the existing console;
    # new clients should use ``passed``.
    passed: bool | None = None
    verified: bool | None = None
    verifier: str = Field(default="安全员", min_length=1, max_length=100)
    reason: str = Field(default="", max_length=1000)
    version: int | None = Field(default=None, ge=1)
    description_correct: bool | None = None
    corrected_name: str | None = Field(default=None, min_length=1, max_length=200)
    corrected_evidence: str | None = Field(default=None, min_length=1, max_length=2000)
    corrected_severity: Literal["general", "major"] | None = None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    role: Literal["system_admin", "safety_officer", "work_area_manager", "project_manager", "safety_director"]
    work_area: str = Field(default="全部工区", min_length=1, max_length=100)


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: Literal["system_admin", "safety_officer", "work_area_manager", "project_manager", "safety_director"] | None = None
    work_area: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None


class JobView(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed"]
    mode: str
    source_type: str
    created_at: str
    updated_at: str
    error: str | None = None
    result_url: str | None = None
    progress: dict[str, object] = Field(default_factory=dict)


class EmailDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_label_id: str | None = None
    work_area: str = Field(default="测试工区", min_length=1, max_length=100)


class EmailTestRequest(BaseModel):
    recipient_roles: list[Literal["safety_officer", "work_area_manager", "project_manager", "safety_director"]] | None = None
    subject: str = Field(default="【智筑云AI】邮件通知测试", min_length=1, max_length=120)
    body: str = Field(
        default="这是一封真实邮件发送测试。若您收到本邮件，说明智筑云AI隐患分发链路已可到达您的邮箱。",
        min_length=1,
        max_length=2000,
    )
