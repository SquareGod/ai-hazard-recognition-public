from __future__ import annotations

import uuid
import os
import ipaddress
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from . import __version__
from .catalog import load_catalog
from .config import settings
from .jobs import job_manager
from .logging_config import logger
from .media import is_image, is_video, safe_filename, test_camera
from .media_proxy import MediaProxy
from .monitoring import MonitorError, MonitorManager
from .notifications import RECIPIENT_ROLES, build_hazard_summary_email, resolve_recipients, send_email, status as notification_status
from .schemas import CameraRequest, EmailDispatchRequest, EmailTestRequest, HazardReviewRequest, HazardVerificationRequest, HikvisionChannelUpdateRequest, HikvisionProfileRequest, LoginRequest, PasswordChangeRequest, RectificationSubmitRequest, StreamSessionStartRequest, StreamStartRequest, UserCreateRequest, UserUpdateRequest, VideoSourceCreateRequest
from .auth import auth_store, require_session
from .severity import severity_resolver
from .storage import job_store
from .stream_sessions import StreamSessionStartError, stream_session_manager
from .streams import stream_manager
from .video_sources import VideoSourceSpec, VideoSourceUnavailable
from .hikvision_client import HikvisionBridgeError, hikvision_bridge_client
from .hikvision_store import hikvision_store
from .workflow_store import workflow_store
from .projects import project_store, current_project, DEFAULT_PROJECT
from .project_access import authorize as authorize_project


monitor_manager = MonitorManager(
    leases=stream_session_manager.leases,
    source_lookup=stream_session_manager.get_source,
    source_list=lambda: [s for s in stream_session_manager.source_specs() if project_store.owner("source", s.id) == current_project.get()],
)
media_proxy = MediaProxy(
    monitor=monitor_manager,
    read_username=settings.mediamtx_read_user,
    read_password=settings.mediamtx_read_password,
)


def _user_can_access_group(user: dict, group: dict) -> bool:
    if project_store.owner("group", group["id"]) != current_project.get():
        return False
    if user["role"] == "system_admin":
        return True
    if user["work_area"] not in {"全部工区", group.get("work_area")} and user["role"] not in {"project_manager", "safety_director"}:
        return False
    allowed: set[str] = set()
    for hazard in group.get("hazards", []):
        allowed.update(RECIPIENT_ROLES.get(hazard.get("severity", "general"), ()))
        if hazard.get("status") not in {"待核实", "已误报"}:
            allowed.add("work_area_manager")
    for notification in group.get("notifications", []):
        allowed.update(str(record.get("role")) for record in notification.get("delivery_records", []) if record.get("role"))
    return user["role"] in allowed


def _require_group_access(request: Request, group_id: str, *, csrf: bool = False) -> tuple[dict, dict]:
    user = require_session(request, csrf=csrf)
    group = workflow_store.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="隐患组不存在")
    if not _user_can_access_group(user, group):
        raise HTTPException(status_code=403, detail="当前账号无权查看该隐患组")
    return user, group


def _group_action_url(group_id: str) -> str:
    base = os.getenv("FRONTEND_PUBLIC_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/?project_id={project_store.owner('group', group_id) or current_project.get()}&group={group_id}"


def _local_artifact_path(url: str | None) -> Path | None:
    """Resolve a server-owned artifact URL for CID email attachment."""
    if not url:
        return None
    clean = url.split("?", 1)[0]
    job_prefix = "/api/v1/jobs/"
    stream_prefix = "/api/v1/streams/"
    try:
        if clean.startswith(job_prefix) and "/artifact/" in clean:
            job_id, relative = clean[len(job_prefix):].split("/artifact/", 1)
            base = job_store.job_dir(job_id).resolve()
        elif clean.startswith(stream_prefix) and "/artifact/" in clean:
            stream_id, relative = clean[len(stream_prefix):].split("/artifact/", 1)
            base = (settings.data_dir / "streams" / stream_id).resolve()
        else:
            return None
        target = (base / relative).resolve()
        return target if base in target.parents and target.is_file() else None
    except (OSError, ValueError):
        return None


app = FastAPI(
    title="智筑云AI工地隐患零微调双链路算法基线",
    version=__version__,
    description="支持上传图片/视频、RTSP摄像头、实时PPE链路、定期抽帧+VLM复杂巡检。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv("BACKEND_CORS_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000").split(",") if item.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def restore_hikvision_channel_directory() -> None:
    """Keep the configured NVR directory after a business-backend restart."""
    first_migration = not project_store.owner("migration", "users")
    project_store.migrate_existing(project_store.path.parent)
    if first_migration:
        for user in auth_store.list_users():
            project_store.set_member(DEFAULT_PROJECT, user["id"], True)
        project_store.bind("migration", "users", DEFAULT_PROJECT)
    for spec in project_store.sources() + hikvision_store.specs():
        try:
            stream_session_manager.add_source(spec)
        except VideoSourceUnavailable:
            logger.warning("已保留海康通道目录，但通道配置暂不可用：%s", spec.id)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    protected_prefixes = (
        "/api/v1/video-sources",
        "/api/v1/stream-sessions",
        "/api/v1/streams",
        "/api/v1/monitor",
        "/api/v1/media",
        "/api/v1/hikvision",
        "/api/v1/hazards",
        "/api/v1/notifications",
    )
    if request.url.path.startswith(protected_prefixes):
        client_host = request.client.host if request.client else ""
        try:
            direct_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            direct_loopback = False
        direct_loopback = direct_loopback and not request.headers.get("X-Forwarded-For")
        supplied_key = request.headers.get("X-Control-Key", "")
        key_ok = bool(settings.control_api_key) and supplied_key == settings.control_api_key
        if not direct_loopback and not key_ok:
            status = 401 if settings.control_api_key else 503
            detail = "控制接口未授权" if settings.control_api_key else "控制接口尚未配置安全密钥"
            return JSONResponse(status_code=status, content={"detail": detail})
    token = None
    try:
        token = authorize_project(request)
        response = await call_next(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    finally:
        if token is not None:
            current_project.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/")
def root():
    """8010仅提供内部算法API，业务操作统一进入完整前端。"""
    return {
        "service": "智筑云AI隐患识别内部后端",
        "message": "业务人员请使用完整前端；本端口不再提供独立测试页面。",
        "health": "/api/v1/health",
        "docs": "/docs",
    }


@app.post("/api/v1/auth/login")
def login(request: LoginRequest):
    try:
        user, token, csrf_token = auth_store.authenticate(request.email, request.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response = JSONResponse({"user": user, "csrf_token": csrf_token})
    response.set_cookie("zzy_session", token, httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", max_age=12 * 3600, path="/")
    return response


@app.get("/api/v1/projects")
def list_projects(request: Request):
    return {"projects": project_store.list(require_session(request))}


@app.post("/api/v1/projects")
def create_project(request: Request, payload: dict):
    require_session(request, roles={"system_admin"}, csrf=True)
    return project_store.save(str(payload.get("name", "")))


@app.patch("/api/v1/projects/{project_id}")
def rename_project(project_id: str, request: Request, payload: dict):
    require_session(request, roles={"system_admin"}, csrf=True)
    return project_store.save(str(payload.get("name", "")), project_id)


@app.put("/api/v1/projects/{project_id}/members/{user_id}")
def set_project_member(project_id: str, user_id: str, request: Request, payload: dict):
    require_session(request, roles={"system_admin"}, csrf=True)
    if auth_store.get_user(user_id) is None:
        raise HTTPException(404, "账号不存在")
    project_store.set_member(project_id, user_id, payload.get("enabled") is True)
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(request: Request) -> dict:
    user = require_session(request)
    return {"user": user, "csrf_token": request.state.csrf_token}


@app.post("/api/v1/auth/logout")
def logout(request: Request):
    require_session(request, csrf=True)
    auth_store.logout(request.cookies.get("zzy_session", ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie("zzy_session", path="/")
    return response


@app.post("/api/v1/auth/change-password")
def change_password(request: Request, payload: PasswordChangeRequest):
    # Changing the initial password is the only operation allowed before the
    # must-change flag has been cleared.
    value = auth_store.session_user(request.cookies.get("zzy_session", ""))
    if value is None:
        raise HTTPException(status_code=401, detail="请先登录")
    user, csrf_token = value
    if request.headers.get("X-CSRF-Token", "") != csrf_token:
        raise HTTPException(status_code=403, detail="安全校验失败")
    try:
        auth_store.change_password(user["id"], payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response = JSONResponse({"ok": True, "login_required": True})
    response.delete_cookie("zzy_session", path="/")
    return response


@app.get("/api/v1/users")
def list_users(request: Request) -> list[dict]:
    require_session(request, roles={"system_admin"})
    return [u | {"project_member": project_store.member(current_project.get(), u)} for u in auth_store.list_users()]


@app.post("/api/v1/users")
def create_user(request: Request, payload: UserCreateRequest) -> dict:
    require_session(request, roles={"system_admin"}, csrf=True)
    try:
        user, temporary_password = auth_store.create_user(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    project_store.set_member(current_project.get(), user["id"], True)
    return {"user": user, "temporary_password": temporary_password}


@app.patch("/api/v1/users/{user_id}")
def update_user(user_id: str, request: Request, payload: UserUpdateRequest) -> dict:
    require_session(request, roles={"system_admin"}, csrf=True)
    item = auth_store.update_user(user_id, payload.model_dump(exclude_unset=True))
    if item is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return item


@app.post("/api/v1/users/{user_id}/reset-password")
def reset_user_password(user_id: str, request: Request) -> dict:
    require_session(request, roles={"system_admin"}, csrf=True)
    try:
        return {"temporary_password": auth_store.reset_password(user_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/health")
def health() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "catalog_size": len(load_catalog()),
        "detector_provider": settings.detector_provider,
        "detector_model": str(settings.detector_model),
        "detector_model_ready": settings.detector_provider in {"mock", "disabled"} or settings.detector_model.exists(),
        "vlm_provider": settings.vlm_provider,
        "vlm_model": settings.qwen_model,
        "api_key_configured": bool(settings.qwen_api_key),
        "email_notification": notification_status(),
        "warning": "这是零微调基线，检测结果必须经过现场测试和人工复核。",
    }


@app.get("/api/v1/catalog")
def catalog() -> list[dict]:
    return [item.model_dump() for item in load_catalog()]


@app.get("/api/v1/algorithm/settings")
def algorithm_settings() -> dict:
    return {"realtime_fps": 2, "inspection_interval_sec": settings.inspection_interval_sec,
            "test_stream_interval_sec": settings.test_stream_interval_sec,
            "max_frames": settings.max_frames, "vlm_parallelism": int(os.getenv("VLM_PARALLELISM", "2"))}


@app.get("/api/v1/algorithm/capabilities")
def algorithm_capabilities() -> dict:
    from .algorithm_adapters import create_algorithm_adapter
    adapter = create_algorithm_adapter()
    if adapter is not None:
        try:
            return {"provider": adapter.provider_name, "health": adapter.health(), "capabilities": adapter.capabilities()}
        except Exception:
            raise HTTPException(503, "外部算法服务不可用，请检查算法服务地址和运行状态")
    return {"provider": "local", "detector": settings.detector_provider, "vlm": settings.vlm_provider,
            "realtime_rules": [{"id": "H003", "name": "未佩戴安全帽"}],
            "inspection_labels": len(load_catalog()), "modes": ["realtime", "inspection", "test"],
            "notice": "现有实时规则仅覆盖安全帽；其余标签使用大模型巡检，实际准确率需要现场验证。",
            "remote_cancellation": False}


async def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "upload").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{upload.filename}")
    temp_dir = settings.data_dir / "incoming"
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / f"{uuid.uuid4().hex}_{safe_filename(upload.filename or 'upload') }"
    size = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_mb * 1024 * 1024:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"文件超过{settings.max_upload_mb}MB限制")
            handle.write(chunk)
    return target


@app.post("/api/v1/jobs/upload")
async def create_upload_jobs(
    files: list[UploadFile] | None = File(None, description="可一次上传一个或多个图片/视频"),
    file: UploadFile | None = File(None, description="兼容单文件上传"),
    mode: Literal["realtime", "inspection", "test"] = Form("inspection"),
    work_area: str = Form("测试工区", min_length=1, max_length=100),
) -> dict:
    uploads = [*(files or []), *([file] if file is not None else [])]
    if not uploads:
        raise HTTPException(status_code=400, detail="请在 files 或 file 字段中选择至少一个图片或视频文件")
    jobs = []
    for upload in uploads:
        path = await _save_upload(upload)
        source_type = "image" if is_image(path) else "video" if is_video(path) else "unknown"
        if source_type == "unknown":
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"无法判断文件类型：{upload.filename}")
        job_id = job_manager.submit_file(path, source_type=source_type, mode=mode, work_area=work_area)
        jobs.append({"job_id": job_id, "filename": upload.filename, "status_url": f"/api/v1/jobs/{job_id}"})
    return {"jobs": jobs}


@app.post("/api/v1/analyze/sync")
async def analyze_sync(
    file: UploadFile = File(...),
    mode: Literal["realtime", "inspection", "test"] = Form("inspection"),
    work_area: str = Form("测试工区", min_length=1, max_length=100),
) -> dict:
    path = await _save_upload(file)
    source_type = "image" if is_image(path) else "video" if is_video(path) else "unknown"
    if source_type == "unknown":
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    try:
        return job_manager.run_sync(path, source_type=source_type, mode=mode, work_area=work_area)
    except Exception as exc:
        logger.exception("同步分析失败")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/jobs/camera")
def create_camera_job(request: CameraRequest) -> dict:
    job_id = job_manager.submit_camera(
        request.source_url,
        camera_id=request.camera_id,
        mode=request.mode,
        work_area=getattr(request, "work_area", "测试工区"),
        sample_fps=request.sample_fps,
        duration_sec=request.duration_sec,
    )
    return {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}


@app.post("/api/v1/cameras/test")
def camera_test(request: CameraRequest) -> dict:
    try:
        return test_camera(request.source_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/jobs")
def list_jobs(limit: int = 50) -> list[dict]:
    return project_store.filter("job", [job.model_dump() for job in job_store.list(10000)])[:max(1, min(limit, 200))]


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.model_dump()


@app.get("/api/v1/jobs/{job_id}/result")
def get_result(job_id: str) -> dict:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status == "failed":
        raise HTTPException(status_code=422, detail=job.error or "任务失败")
    result = job_store.result(job_id)
    if result is None:
        raise HTTPException(status_code=202, detail="任务尚未完成")
    return result


@app.get("/api/v1/notifications/status")
def get_notification_status() -> dict:
    """返回脱敏后的邮件通知配置状态，不返回邮箱密码或完整邮箱。"""
    return notification_status()


@app.post("/api/v1/notifications/email/test")
def send_test_email(request: EmailTestRequest) -> dict:
    recipients = resolve_recipients("general", request.recipient_roles)
    records = send_email(recipients=recipients, subject=request.subject, body=request.body)
    return {"message": "邮件测试已执行", "records": records}


@app.post("/api/v1/jobs/{job_id}/notifications/email")
def dispatch_job_email(job_id: str, request: EmailDispatchRequest) -> dict:
    result = job_store.result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到已完成的识别结果")
    findings = [item for item in result.get("findings", []) if item.get("final_status") == "confirmed_hazard"]
    if request.finding_label_id:
        findings = [item for item in findings if item.get("label_id") == request.finding_label_id]
    if not findings:
        raise HTTPException(status_code=422, detail="该任务没有可分发的已确认隐患，请先确认识别结果")
    frames = result.get("frames") or [{}]
    first_frame = frames[0]
    frame_by_id = {str(frame.get("frame_id")): frame for frame in frames if frame.get("frame_id")}
    findings_by_frame: dict[str, list[dict]] = defaultdict(list)
    fallback_frame_id = str(first_frame.get("frame_id") or "image")
    for finding in findings:
        source_frames = finding.get("source_frame_ids") or []
        findings_by_frame[str(source_frames[0]) if source_frames else fallback_frame_id].append(finding)
    hazards: list[dict] = []
    for frame_id, frame_findings in findings_by_frame.items():
        frame = frame_by_id.get(frame_id, first_frame)
        before_image = frame.get("artifact_url") or frame.get("annotated_url")
        hazards.extend(workflow_store.create_from_findings(job_id=job_id, work_area=request.work_area, findings=frame_findings, before_image=before_image))
    dispatches: list[dict] = []
    all_records: list[dict] = []
    hazards_by_group: dict[str, list[dict]] = defaultdict(list)
    for hazard in hazards:
        hazards_by_group[hazard.get("group_id") or hazard["id"]].append(hazard)
    for group_id, group_hazards in hazards_by_group.items():
        roles: dict[str, list[dict]] = defaultdict(list)
        for hazard in group_hazards:
            finding = next((item for item in findings if item.get("label_id") == hazard["label_id"]), None)
            if finding is None:
                continue
            severity = str(finding.get("severity") or "general")
            if severity not in RECIPIENT_ROLES:
                raise HTTPException(status_code=422, detail="识别结果缺少有效的一般/重大隐患等级")
            for role in RECIPIENT_ROLES[severity]:
                roles[role].append(finding)
        group_records: list[dict] = []
        for role, role_findings in roles.items():
            recipients = resolve_recipients("general", [role], work_area=request.work_area)
            if not recipients:
                continue
            names = [str(item.get("name") or item.get("label_id")) for item in role_findings]
            has_major = any(item.get("severity") == "major" for item in role_findings)
            severity = "major" if has_major else "general"
            deadline = severity_resolver.deadline(severity)
            subject, body = build_hazard_summary_email(job_id=job_id, hazard_names=names, severity=severity, work_area=request.work_area, deadline=deadline)
            body += f"\n\n请登录系统逐项判断隐患是否存在、描述和等级是否正确：{_group_action_url(group_id)}"
            records = send_email(recipients=recipients, subject=subject, body=body, image_path=_local_artifact_path(group_hazards[0].get("before_image")), action_url=_group_action_url(group_id))
            group_records.extend(records)
        workflow_store.record_email_status([item["id"] for item in group_hazards], group_records)
        all_records.extend(group_records)
        # Preserve the original response contract while the actual delivery is
        # de-duplicated per recipient role above.
        group_findings = [item for item in findings if item.get("label_id") in {hazard["label_id"] for hazard in group_hazards}]
        for group_severity in ("general", "major"):
            severity_findings = [item for item in group_findings if item.get("severity") == group_severity]
            if not severity_findings:
                continue
            allowed_roles = set(RECIPIENT_ROLES[group_severity])
            dispatches.append({
                "group_id": group_id,
                "severity": group_severity,
                "severity_name": "重大隐患" if group_severity == "major" else "一般隐患",
                "deadline": severity_resolver.deadline(group_severity),
                "findings": severity_findings,
                "records": [record for record in group_records if record.get("role") in allowed_roles],
            })
    return {
        "job_id": job_id,
        "hazard": findings[0],
        "findings": findings,
        "dispatches": dispatches,
        "records": all_records,
        "hazards": hazards,
        "groups": [workflow_store.get_group(group_id) for group_id in hazards_by_group],
    }


@app.get("/api/v1/hazard-groups")
def list_hazard_groups(request: Request) -> list[dict]:
    user = require_session(request)
    return [group for group in workflow_store.groups() if _user_can_access_group(user, group)]


@app.get("/api/v1/hazard-groups/{group_id}")
def get_hazard_group(group_id: str, request: Request) -> dict:
    return _require_group_access(request, group_id)[1]


@app.get("/api/v1/hazard-groups/{group_id}/hazards/{hazard_id}/audit")
def get_hazard_audit(group_id: str, hazard_id: str, request: Request) -> list[dict]:
    _require_group_access(request, group_id)
    item = workflow_store.get(hazard_id)
    if item is None or item.get("group_id") != group_id:
        raise HTTPException(status_code=404, detail="隐患不存在")
    return workflow_store.audit_events(hazard_id)


@app.post("/api/v1/hazard-groups/{group_id}/hazards/{hazard_id}/verify")
def verify_group_hazard(group_id: str, hazard_id: str, request: Request, payload: HazardVerificationRequest) -> dict:
    user, group = _require_group_access(request, group_id, csrf=True)
    current = next((item for item in group["hazards"] if item["id"] == hazard_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail="隐患不存在")
    if current["status"] != "待核实":
        raise HTTPException(status_code=409, detail="该隐患已经完成初核，请刷新查看最新状态")
    allowed_roles = set(RECIPIENT_ROLES.get(current["severity"], ())) | {"system_admin"}
    if user["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="当前账号不是该隐患的收件人")
    passed = payload.passed if payload.passed is not None else payload.verified
    if passed is None:
        raise HTTPException(status_code=422, detail="请选择隐患是否存在")
    if not passed and not payload.reason.strip():
        raise HTTPException(status_code=422, detail="判定为不存在时必须填写误报原因")
    if passed and payload.description_correct is False and not (payload.corrected_name and payload.corrected_evidence and payload.corrected_severity):
        raise HTTPException(status_code=422, detail="描述不正确时必须填写修正后的名称、可见证据和等级")
    old_severity = current["severity"]
    try:
        saved = workflow_store.verify(
            hazard_id, user["name"], passed, payload.reason,
            expected_version=payload.version, corrected_name=payload.corrected_name,
            corrected_evidence=payload.corrected_evidence, corrected_severity=payload.corrected_severity,
            actor_id=user["id"],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="隐患不存在")
    if passed and saved["severity"] != old_severity:
        old_roles, new_roles = set(RECIPIENT_ROLES[old_severity]), set(RECIPIENT_ROLES[saved["severity"]])
        roles = list(old_roles | new_roles)
        recipients = resolve_recipients("general", roles, work_area=group["work_area"])  # explicit roles override severity
        old_name = "重大隐患" if old_severity == "major" else "一般隐患"
        new_name = "重大隐患" if saved["severity"] == "major" else "一般隐患"
        subject = f"【隐患等级更正】{group['order_id']} {old_name}变更为{new_name}"
        body = f"{group['work_area']}隐患经人工核实，等级由{old_name}更正为{new_name}。\n隐患：{saved['name']}\n请以系统最新整改单为准。"
        try:
            records = send_email(recipients=recipients, subject=subject, body=body, image_path=_local_artifact_path(group.get("before_image")), action_url=_group_action_url(group_id))
        except Exception:
            logger.exception("hazard correction email failed group=%s", group_id)
            records = [{"role": role, "status": "failed", "error": "更正邮件发送失败"} for role in roles]
        workflow_store.add_group_notification(group_id, title=subject, body=body, notification_type="correction", records=records)
    if passed:
        subject = f"【整改任务】{group['order_id']} {saved['name']}"
        body = f"{group['work_area']}隐患已完成初核，请所属工区负责人于{severity_resolver.deadline(saved['severity'])}内提交整改说明和整改后照片。"
        try:
            recipients = resolve_recipients("general", ["work_area_manager"], work_area=group["work_area"])
            records = send_email(recipients=recipients, subject=subject, body=body, image_path=_local_artifact_path(group.get("before_image")), action_url=_group_action_url(group_id))
        except Exception:
            logger.exception("rectification task email failed group=%s", group_id)
            records = [{"role": "work_area_manager", "status": "failed", "error": "整改任务邮件发送失败"}]
        workflow_store.add_group_notification(group_id, title=subject, body=body, notification_type="rectification", records=records)
    return {"group": workflow_store.get_group(group_id), "hazard": saved}


@app.post("/api/v1/hazard-groups/{group_id}/hazards/{hazard_id}/rectifications")
def submit_group_rectification(group_id: str, hazard_id: str, request: Request, payload: RectificationSubmitRequest) -> dict:
    user, group = _require_group_access(request, group_id, csrf=True)
    current = next((item for item in group["hazards"] if item["id"] == hazard_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail="隐患不存在")
    if current["status"] not in {"待整改", "整改中", "复核退回"}:
        raise HTTPException(status_code=409, detail="该隐患当前状态不能提交整改")
    if user["role"] not in {"work_area_manager", "system_admin"}:
        raise HTTPException(status_code=403, detail="只有所属工区负责人可以提交整改")
    if user["role"] != "system_admin" and user["work_area"] not in {"全部工区", group["work_area"]}:
        raise HTTPException(status_code=403, detail="不能处理其他工区的整改")
    item = workflow_store.submit_rectification(hazard_id, payload.description, user["name"], payload.after_image)
    if item is None:
        raise HTTPException(status_code=404, detail="隐患不存在或已结束")
    return {"group": workflow_store.get_group(group_id), "hazard": item}


@app.post("/api/v1/hazard-groups/{group_id}/hazards/{hazard_id}/reviews")
def review_group_hazard(group_id: str, hazard_id: str, request: Request, payload: HazardReviewRequest) -> dict:
    user, _group = _require_group_access(request, group_id, csrf=True)
    item = workflow_store.get(hazard_id)
    if item is None or item.get("group_id") != group_id:
        raise HTTPException(status_code=404, detail="隐患不存在")
    if item["status"] != "待复核":
        raise HTTPException(status_code=409, detail="该隐患尚未提交整改或已完成复核")
    required = "safety_director" if item["severity"] == "major" else "safety_officer"
    if user["role"] not in {required, "system_admin"}:
        raise HTTPException(status_code=403, detail="当前角色无权复核该隐患")
    saved = workflow_store.review(hazard_id, payload.passed, user["name"], payload.comment)
    return {"group": workflow_store.get_group(group_id), "hazard": saved}


@app.post("/api/v1/hazard-groups/{group_id}/hazards/{hazard_id}/major-confirmation")
def confirm_group_major(group_id: str, hazard_id: str, request: Request, payload: HazardReviewRequest) -> dict:
    user, group = _require_group_access(request, group_id, csrf=True)
    if not any(item["id"] == hazard_id for item in group["hazards"]):
        raise HTTPException(status_code=404, detail="隐患不存在")
    if user["role"] not in {"project_manager", "system_admin"}:
        raise HTTPException(status_code=403, detail="只有项目经理可以最终确认重大隐患")
    saved = workflow_store.confirm_major(hazard_id, user["name"])
    if saved is None:
        raise HTTPException(status_code=409, detail="该隐患尚未完成重大隐患复核")
    return {"group": workflow_store.get_group(group_id), "hazard": saved}


@app.get("/api/v1/jobs/{job_id}/artifact/{relative_path:path}")
def get_artifact(job_id: str, relative_path: str, request: Request):
    require_session(request)
    base = job_store.job_dir(job_id).resolve()
    target = (base / relative_path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.post("/api/v1/streams/start")
def start_stream(request: StreamStartRequest) -> dict:
    if not request.source_url.startswith("rtsp://"):
        return stream_manager.start(
            request.camera_id,
            request.source_url,
            request.inference_fps,
            work_area=request.work_area,
            auto_email=request.auto_email,
        )
    source_id = f"legacy-{request.camera_id}"
    try:
        project_store.bind("source", source_id)
        stream_session_manager.add_source(
            VideoSourceSpec(source_id, request.camera_id, "rtsp", request.source_url, request.work_area)
        )
        session = stream_session_manager.start(
            source_id, inference_fps=request.inference_fps, auto_email=request.auto_email
        )
        project_store.bind("session", session["session_id"])
        project_store.bind("stream", session["inference_stream_id"])
    except (VideoSourceUnavailable, StreamSessionStartError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "stream_id": session["inference_stream_id"],
        "camera_id": request.camera_id,
        "status": session["status"],
        "work_area": request.work_area,
        "auto_email": request.auto_email,
        "processed_frames": 0,
        "emitted_events": 0,
        "last_error": session.get("last_error"),
    }


@app.post("/api/v1/video-sources")
def create_video_source(request: VideoSourceCreateRequest) -> dict:
    try:
        spec = VideoSourceSpec(**request.model_dump())
        project_store.bind("source", spec.id)
        result = stream_session_manager.add_source(spec)
        project_store.save_source(spec)
        return result
    except VideoSourceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/hikvision/nvr-profiles")
def list_hikvision_profiles() -> list[dict]:
    try:
        return project_store.filter("profile", hikvision_bridge_client.profiles())
    except HikvisionBridgeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/hikvision/nvr-profiles")
def configure_hikvision_profile(request: HikvisionProfileRequest) -> dict:
    try:
        if request.model_dump().get("id"):
            project_store.check("profile", request.model_dump()["id"])
        result = hikvision_bridge_client.configure(request.model_dump())
        project_store.bind("profile", result["id"])
        return result
    except HikvisionBridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/hikvision/nvr-profiles/{profile_id}/test")
def test_hikvision_profile(profile_id: str) -> dict:
    try:
        return hikvision_bridge_client.test(profile_id)
    except HikvisionBridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/hikvision/nvr-profiles/{profile_id}/sync")
def sync_hikvision_channels(profile_id: str) -> dict:
    try:
        result = hikvision_bridge_client.sync(profile_id)
        channels = hikvision_store.upsert_channels(profile_id, list(result.get("channels") or []))
        for channel in channels:
            if channel["profile_id"] == profile_id:
                project_store.bind("source", channel["id"])
        for spec in hikvision_store.specs():
            stream_session_manager.add_source(spec)
        return {"profile_id": profile_id, "channels": project_store.filter("source", channels)}
    except (HikvisionBridgeError, VideoSourceUnavailable) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/hikvision/channels")
def list_hikvision_channels() -> list[dict]:
    return project_store.filter("source", hikvision_store.list())


@app.patch("/api/v1/hikvision/channels/{channel_id}")
def update_hikvision_channel(channel_id: str, request: HikvisionChannelUpdateRequest) -> dict:
    item = hikvision_store.update(channel_id, request.model_dump(exclude_unset=True))
    if item is None:
        raise HTTPException(status_code=404, detail="海康通道不存在")
    stream_session_manager.add_source(VideoSourceSpec(
        item["id"], item["name"], "hikvision", f"hikvision://{item['id']}",
        item["work_area"], item["enabled"], item["risk_point"], item["ai_enabled"],
        {"profile_id": item["profile_id"], "channel_no": item["channel_no"]},
    ))
    return item


@app.get("/api/v1/video-sources")
def list_video_sources() -> list[dict]:
    return project_store.filter("source", stream_session_manager.list_sources())


@app.get("/api/v1/monitor/cameras")
def list_monitor_cameras(work_area: str = "", limit: int = 4, offset: int = 0) -> dict:
    return monitor_manager.list_cameras(work_area=work_area, limit=limit, offset=offset)


@app.get("/api/v1/monitor/cameras/status")
def monitor_camera_status(ids: str = "") -> dict:
    return monitor_manager.camera_status([item for item in ids.split(",") if item and project_store.owner("source", item) == current_project.get()])


@app.get("/api/v1/monitor/cameras/{camera_id}/snapshot")
def monitor_camera_snapshot(camera_id: str, request: Request) -> Response:
    """Preview thumbnail only. It never creates a MediaMTX playback ticket or NVR stream lease."""
    require_session(request)
    project_store.check("source", camera_id)
    spec = stream_session_manager.get_source(camera_id)
    if spec is None or not spec.enabled:
        raise HTTPException(status_code=404, detail="摄像头不存在或未启用")
    if spec.source_type not in {"hikvision", "hcnetsdk"}:
        raise HTTPException(status_code=422, detail="当前辅助预览仅支持海康SDK通道，请点击后切为主画面播放")
    try:
        image = hikvision_bridge_client.snapshot(camera_id, str(spec.metadata["profile_id"]), int(spec.metadata["channel_no"]))
    except (KeyError, ValueError, HikvisionBridgeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/v1/monitor/playback-tickets")
def create_playback_ticket(request: dict) -> dict:
    camera_ids = request.get("camera_ids")
    purpose = request.get("purpose")
    if not isinstance(camera_ids, list) or not all(isinstance(item, str) for item in camera_ids) or not isinstance(purpose, str):
        raise HTTPException(status_code=422, detail="播放票据请求无效")
    try:
        for ident in camera_ids:
            project_store.check("source", ident)
        result = monitor_manager.create_ticket(camera_ids, purpose)
        project_store.bind("ticket", result["ticket_id"])
        return result
    except MonitorError as exc:
        status = 404 if "不存在" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.delete("/api/v1/monitor/playback-tickets/{ticket_id}")
def delete_playback_ticket(ticket_id: str) -> dict:
    result = monitor_manager.release_ticket(ticket_id)
    if result is None:
        raise HTTPException(status_code=404, detail="播放票据不存在")
    return result


@app.post("/api/v1/media/{ticket_id}/{camera_id}/whep")
async def proxy_whep_post(ticket_id: str, camera_id: str, request: Request):
    return media_proxy.whep(ticket_id, camera_id, "POST", await request.body(), request.headers.get("content-type", ""))


@app.delete("/api/v1/media/{ticket_id}/{camera_id}/whep/{session_id}")
async def proxy_whep_delete(ticket_id: str, camera_id: str, session_id: str, request: Request):
    return media_proxy.whep(ticket_id, camera_id, "DELETE", await request.body(), request.headers.get("content-type", ""), session_id)


@app.get("/api/v1/media/{ticket_id}/{camera_id}/hls/{asset:path}")
def proxy_hls(ticket_id: str, camera_id: str, asset: str, request: Request):
    query = request.url.query
    return media_proxy.hls(ticket_id, camera_id, f"{asset}?{query}" if query else asset)


def shutdown_monitor_manager() -> None:
    monitor_manager.shutdown()


app.router.on_shutdown.append(shutdown_monitor_manager)


@app.post("/api/v1/video-sources/{source_id}/test")
def test_video_source(source_id: str) -> dict:
    try:
        result = stream_session_manager.test_source(source_id)
    except VideoSourceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="视频源不存在")
    return result


@app.post("/api/v1/stream-sessions")
def create_stream_session(request: StreamSessionStartRequest) -> dict:
    try:
        project_store.check("source", request.source_id)
        kwargs = {"inference_fps": request.inference_fps, "auto_email": request.auto_email}
        if request.analysis_mode != "realtime": kwargs.update({"analysis_mode": request.analysis_mode, "inspection_interval_sec": request.inspection_interval_sec})
        result = stream_session_manager.start(request.source_id, **kwargs)
        project_store.bind("session", result["session_id"])
        project_store.bind("stream", result["inference_stream_id"])
        return result
    except StreamSessionStartError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/hazards")
def list_persistent_hazards() -> list[dict]:
    return project_store.filter("hazard", workflow_store.list())


@app.get("/api/v1/hazards/{hazard_id}")
def get_persistent_hazard(hazard_id: str) -> dict:
    item = workflow_store.get(hazard_id)
    if item is None: raise HTTPException(status_code=404, detail="隐患不存在")
    return item


@app.post("/api/v1/hazards/{hazard_id}/rectifications")
def submit_persistent_rectification(hazard_id: str, request: RectificationSubmitRequest, http_request: Request) -> dict:
    item = get_persistent_hazard(hazard_id)
    return submit_group_rectification(item["group_id"], hazard_id, http_request, request)["hazard"]


@app.post("/api/v1/hazards/{hazard_id}/rectification-evidence")
async def upload_rectification_evidence(hazard_id: str, request: Request, file: UploadFile = File(...)) -> dict:
    user = require_session(request, roles={"work_area_manager", "system_admin"}, csrf=True)
    hazard = workflow_store.get(hazard_id)
    if hazard is None:
        raise HTTPException(status_code=404, detail="隐患不存在")
    group = workflow_store.get_group(hazard.get("group_id")) if hazard.get("group_id") else None
    if group and user["role"] != "system_admin" and user["work_area"] not in {"全部工区", group["work_area"]}:
        raise HTTPException(status_code=403, detail="不能上传其他工区的整改证据")
    temporary = await _save_upload(file)
    if not is_image(temporary):
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="整改证据只支持图片")
    evidence_dir = settings.data_dir / "workflow" / hazard_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    suffix = temporary.suffix.lower() or ".jpg"
    target = evidence_dir / f"after-{uuid.uuid4().hex[:12]}{suffix}"
    shutil.move(str(temporary), str(target))
    return {"url": f"/api/v1/hazards/{hazard_id}/artifact/{target.name}"}


@app.get("/api/v1/hazards/{hazard_id}/artifact/{filename}")
def rectification_artifact(hazard_id: str, filename: str, request: Request):
    hazard = workflow_store.get(hazard_id)
    if hazard is None:
        raise HTTPException(status_code=404, detail="隐患不存在")
    if hazard.get("group_id"):
        _require_group_access(request, hazard["group_id"])
    else:
        require_session(request)
    base = (settings.data_dir / "workflow" / hazard_id).resolve()
    target = (base / Path(filename).name).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="整改证据不存在")
    return FileResponse(target)


@app.post("/api/v1/hazards/{hazard_id}/verify")
def verify_persistent_hazard(hazard_id: str, request: HazardVerificationRequest, http_request: Request) -> dict:
    item = get_persistent_hazard(hazard_id)
    return verify_group_hazard(item["group_id"], hazard_id, http_request, request)["hazard"]


@app.post("/api/v1/hazards/{hazard_id}/mark-false-positive")
def mark_false_positive(hazard_id: str, request: HazardVerificationRequest, http_request: Request) -> dict:
    return verify_persistent_hazard(hazard_id, request.model_copy(update={"passed": False}), http_request)


@app.post("/api/v1/hazards/{hazard_id}/reviews")
def review_persistent_hazard(hazard_id: str, request: HazardReviewRequest, http_request: Request) -> dict:
    item = get_persistent_hazard(hazard_id)
    return review_group_hazard(item["group_id"], hazard_id, http_request, request)["hazard"]


@app.post("/api/v1/hazards/{hazard_id}/major-confirmation")
def confirm_persistent_major(hazard_id: str, request: HazardReviewRequest, http_request: Request) -> dict:
    item = get_persistent_hazard(hazard_id)
    return confirm_group_major(item["group_id"], hazard_id, http_request, request)["hazard"]


@app.get("/api/v1/notifications")
def list_notifications(request: Request, mark_read: bool = False) -> dict:
    user = require_session(request, csrf=mark_read)
    items = workflow_store.notifications_for_user(user, mark_read=mark_read)
    return {"unread_count": sum(1 for item in items if not item["read"]), "items": items}


@app.post("/api/v1/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, request: Request) -> dict:
    user = require_session(request, csrf=True)
    item = workflow_store.mark_notification_read_for_user(notification_id, user)
    if item is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    return item


@app.get("/api/v1/stream-sessions")
def list_stream_sessions() -> list[dict]:
    return project_store.filter("session", stream_session_manager.list(), "session_id")


@app.post("/api/v1/stream-sessions/{session_id}/stop")
def stop_stream_session(session_id: str) -> dict:
    result = stream_session_manager.stop(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="流媒体会话不存在")
    return result


@app.post("/api/v1/streams/{stream_id}/stop")
def stop_stream(stream_id: str) -> dict:
    session = stream_session_manager.stop_by_inference(stream_id)
    if session is not None:
        return {"stream_id": stream_id, "status": session["status"], "last_error": session.get("last_error")}
    state = stream_manager.stop(stream_id)
    if state is None:
        raise HTTPException(status_code=404, detail="实时流任务不存在")
    return state


@app.get("/api/v1/streams")
def list_streams() -> list[dict]:
    return project_store.filter("stream", stream_manager.list(), "stream_id")


@app.get("/api/v1/streams/{stream_id}/events")
def stream_events(stream_id: str, limit: int = 100) -> list[dict]:
    return stream_manager.events(stream_id, max(1, min(limit, 1000)))


@app.get("/api/v1/streams/{stream_id}/artifact/{relative_path:path}")
def stream_artifact(stream_id: str, relative_path: str, request: Request):
    require_session(request)
    base = (settings.data_dir / "streams" / stream_id).resolve()
    target = (base / relative_path).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
