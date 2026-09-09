from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .sdk_runtime import CtypesHikvisionSdk, FfmpegPublisher, StreamPipeline, _unprotect, list_channels, protect_secret, yuv_frame_to_jpeg

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "runtime"; DATA.mkdir(exist_ok=True)
PROFILE_FILE = DATA / "profiles.json"
TOKEN = os.getenv("HIKVISION_BRIDGE_TOKEN", os.getenv("BRIDGE_TOKEN", ""))
MAX_STREAMS = int(os.getenv("HIKVISION_BRIDGE_MAX_STREAMS", "4"))
LEASE_TTL = float(os.getenv("HIKVISION_LEASE_TTL_SECONDS", "90"))
REAPER_INTERVAL = float(os.getenv("HIKVISION_REAPER_INTERVAL_SEC", "30"))
app = FastAPI(title="智筑云AI 海康 NVR Windows桥接服务", docs_url="/docs")
# channel_id -> {owner_id: last_seen}; 后端每次acquire都会刷新心跳。
_leases: dict[str, dict[str, float]] = {}; _pipelines: dict[str, StreamPipeline] = {}; _lock = threading.RLock()


def _touch(channel_id: str, owner_id: str) -> None:
    _leases.setdefault(channel_id, {})[owner_id] = time.time()


def _reap_expired_leased_pipelines() -> None:
    """回收心跳超时的租约；全部owner失联的管线立即停止，释放4路额度。"""
    to_stop: list[StreamPipeline] = []
    with _lock:
        now = time.time()
        for channel_id in list(_leases):
            owners = _leases[channel_id]
            for owner in [item for item, seen in owners.items() if now - seen > LEASE_TTL]:
                owners.pop(owner, None)
            if not owners:
                _leases.pop(channel_id, None)
                pipeline = _pipelines.pop(channel_id, None)
                if pipeline:
                    to_stop.append(pipeline)
    for pipeline in to_stop:
        pipeline.stop()


def _reaper_loop() -> None:
    while True:
        time.sleep(REAPER_INTERVAL)
        try:
            _reap_expired_leased_pipelines()
        except Exception:
            pass


threading.Thread(target=_reaper_loop, name="lease-reaper", daemon=True).start()


def _shutdown() -> None:
    with _lock:
        pipelines = list(_pipelines.values())
        _pipelines.clear()
        _leases.clear()
    for pipeline in pipelines:
        pipeline.stop()


app.router.on_shutdown.append(_shutdown)


class ProfileIn(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=100); host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535); username: str = Field(min_length=1, max_length=100); password: str = Field(default="", max_length=256)


class LeaseIn(BaseModel):
    path_name: str = Field(min_length=1, max_length=200); profile_id: str = Field(min_length=1, max_length=100); channel_no: int = Field(ge=1, le=65535)


class SnapshotIn(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100)
    channel_no: int = Field(ge=1, le=65535)


def _guard(key: str | None) -> None:
    if not TOKEN or key != TOKEN: raise HTTPException(401, "桥接服务未授权：请检查 HIKVISION_BRIDGE_TOKEN")


def _load() -> dict[str, dict[str, Any]]:
    return json.loads(PROFILE_FILE.read_text("utf-8")) if PROFILE_FILE.exists() else {}


def _save(value: dict[str, dict[str, Any]]) -> None:
    PROFILE_FILE.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _protect(profile_id: str, password: str) -> str:
    secret_file = DATA / f"{profile_id}.secret.dpapi"
    protect_secret(secret_file, password)
    return secret_file.name


def _public(profile_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"id": profile_id, "name": value["name"], "host": value["host"], "port": value["port"], "username": value["username"], "configured": True}


def _status(channel_id: str) -> dict[str, Any]:
    p = _pipelines.get(channel_id)
    return {"channel_id": channel_id, "state": p.state if p else "stopped", "owners": len(_leases.get(channel_id, {})),
            "received_chunks": p.received_chunks if p else 0, "decoded_frames": p.decoded_frames if p else 0,
            "published_frames": p.published_frames if p else 0, "dropped_frames": p.dropped_frames if p else 0,
            "last_frame_at": p.last_frame_at if p else None, "last_error": p.last_error if p else None,
            "media_path": p.path_name if p else None, "stream_type": p.stream_type if p else None,
            "effective_channel_no": p.effective_channel_no if p else None}


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "platform": "windows", "max_concurrent_streams": MAX_STREAMS, "active_streams": len(_pipelines), "stream_status": [_status(k) for k in _pipelines]}


@app.post("/nvr-profiles")
def configure(profile: ProfileIn, x_bridge_token: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_bridge_token)
    items = _load()
    profile_id = profile.id or uuid.uuid4().hex
    if profile.id and profile.id not in items:
        raise HTTPException(404, "NVR配置不存在")
    if not profile.id and not profile.password:
        raise HTTPException(422, "新增NVR必须填写设备密码")
    secret_ref = _protect(profile_id, profile.password) if profile.password else items[profile_id]["secret_ref"]
    items[profile_id] = {"name": profile.name, "host": profile.host, "port": profile.port, "username": profile.username, "secret_ref": secret_ref}; _save(items)
    return _public(profile_id, items[profile_id])


@app.get("/nvr-profiles")
def profiles(x_bridge_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _guard(x_bridge_token); return [_public(k, v) for k, v in _load().items()]


@app.post("/nvr-profiles/{profile_id}/test")
def test(profile_id: str, x_bridge_token: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_bridge_token); profile = _load().get(profile_id)
    if not profile: raise HTTPException(404, "NVR配置不存在")
    try:
        with socket.create_connection((profile["host"], int(profile["port"])), timeout=5): pass
    except OSError as exc: raise HTTPException(400, "无法连接NVR SDK端口，请检查映射地址、端口、专线或白名单") from exc
    return {"ok": True, "message": "SDK端口可达；同步通道将执行HCNetSDK登录和目录读取"}


@app.post("/nvr-profiles/{profile_id}/sync")
def sync(profile_id: str, x_bridge_token: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_bridge_token); profile = _load().get(profile_id)
    if not profile: raise HTTPException(404, "NVR配置不存在")
    try: channels = list_channels(profile, DATA / profile["secret_ref"])
    except RuntimeError as exc: raise HTTPException(400, str(exc)) from exc
    return {"profile_id": profile_id, "channels": channels}


@app.get("/channels/{channel_id}/status")
def channel_status(channel_id: str, x_bridge_token: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_bridge_token); return _status(channel_id)


@app.get("/streams")
def streams(x_bridge_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _guard(x_bridge_token); return [_status(k) for k in _pipelines]


_snapshot_locks: dict[str, threading.Lock] = {}
_snapshot_locks_guard = threading.Lock()


def _channel_snapshot_lock(channel_id: str) -> threading.Lock:
    with _snapshot_locks_guard:
        return _snapshot_locks.setdefault(channel_id, threading.Lock())


@app.post("/channels/{channel_id}/snapshot")
def snapshot(channel_id: str, payload: SnapshotIn, x_bridge_token: str | None = Header(default=None)) -> Response:
    """Low-frequency preview image; it shares the SDK login but never owns a streaming lease."""
    _guard(x_bridge_token)
    profile = _load().get(payload.profile_id)
    if not profile:
        raise HTTPException(404, "NVR配置不存在")
    lock = _channel_snapshot_lock(channel_id)
    # 上一张远程抓图还没返回时直接让调用方稍后再试：公网链路上堆积抓图
    # 请求会拖慢正在播放的实时流，这是“卡顿且延迟不稳”的诱因之一。
    if not lock.acquire(blocking=False):
        raise HTTPException(429, "上一次抓图尚未返回，请稍候")
    try:
        # 快路径：通道正被管线解码时直接用最近一帧，完全不打扰NVR。
        with _lock:
            pipeline = _pipelines.get(channel_id)
            frame = pipeline.last_frame if pipeline else None
            fresh = frame is not None and pipeline.last_frame_at is not None and time.time() - pipeline.last_frame_at < 10
        if fresh:
            try:
                image = yuv_frame_to_jpeg(frame)
            except (OSError, RuntimeError):
                image = None  # 本地编码失败时退回远程抓图兜底
            if image:
                return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})
        try:
            image = CtypesHikvisionSdk().capture_jpeg(profile, _unprotect(DATA / profile["secret_ref"]), payload.channel_no)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(400, f"抓取海康预览图失败：{exc}") from exc
        return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})
    finally:
        lock.release()


@app.post("/channels/{channel_id}/leases/{owner_id}")
def acquire(channel_id: str, owner_id: str, payload: LeaseIn, x_bridge_token: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_bridge_token)
    with _lock:
        owners = _leases.setdefault(channel_id, {})
        if owner_id in owners:
            _touch(channel_id, owner_id)  # 重复acquire即心跳续约
            return {"ok": True, **_status(channel_id)}
        if not owners and len(_pipelines) >= MAX_STREAMS: raise HTTPException(409, f"同时取流上限为{MAX_STREAMS}路，请先停止其他通道")
        if not owners:
            profile = _load().get(payload.profile_id)
            if not profile: raise HTTPException(404, "NVR配置不存在")
            try:
                password = _unprotect(DATA / profile["secret_ref"])
                adapter = CtypesHikvisionSdk()
                pipeline = StreamPipeline(profile, password, payload.channel_no, payload.path_name, adapter, FfmpegPublisher(), adapter.create_decoder)
                _pipelines[channel_id] = pipeline; pipeline.start()
            except (OSError, RuntimeError) as exc:
                _pipelines.pop(channel_id, None); raise HTTPException(400, f"启动海康通道失败：{exc}") from exc
        _touch(channel_id, owner_id); return {"ok": True, **_status(channel_id)}


@app.delete("/channels/{channel_id}/leases/{owner_id}")
def release(channel_id: str, owner_id: str, x_bridge_token: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_bridge_token)
    with _lock:
        owners = _leases.get(channel_id, {}); owners.pop(owner_id, None)
        if not owners:
            _leases.pop(channel_id, None); pipeline = _pipelines.pop(channel_id, None)
            if pipeline:
                # 后台线程停止：stop()会join取流线程（公网下可达数秒），
                # 同步执行会拖住HTTP请求，造成切换摄像头时的顿挫感。
                threading.Thread(target=pipeline.stop, daemon=True).start()
        return {"ok": True, **_status(channel_id)}
