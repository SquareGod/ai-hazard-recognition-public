from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from .algorithm_adapters import AlgorithmContext
from .catalog import load_catalog
from .config import settings
from .detectors import get_shared_detector
from .logging_config import logger
from .media import image_quality
from .notifications import RECIPIENT_ROLES, build_hazard_summary_email, resolve_recipients, send_email
from .pipeline import HazardPipeline
from .schemas import FrameInfo, VLMFinding
from .severity import severity_resolver
from .video_sources import redact_source_url
from .workflow_store import workflow_store


def redact_source(source_url: str) -> str:
    return redact_source_url(source_url)


# RTSP拉流强制TCP并设置5秒读超时：断流时capture.read()最多阻塞5秒即返回失败，
# 停止/重连线程不再可能永久卡死。setdefault不覆盖现场已有的自定义配置。
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")


@dataclass(frozen=True)
class CaptureReconnectPolicy:
    delays: tuple[float, ...] = (1, 2, 4, 8, 15, 30)


class LatestFrameSlot:
    """A single overwrite slot; it makes capture latency independent of inference."""
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._sequence = 0
        self._value: tuple[int, float, object] | None = None

    def put(self, image: object, timestamp: float) -> int:
        with self._condition:
            self._sequence += 1
            self._value = (self._sequence, timestamp, image)
            self._condition.notify()
            return self._sequence

    def take_after(self, sequence: int, stop: threading.Event, timeout: float = .25):
        with self._condition:
            self._condition.wait_for(lambda: stop.is_set() or (self._value and self._value[0] > sequence), timeout)
            return None if stop.is_set() or not self._value or self._value[0] <= sequence else self._value


@dataclass
class StreamState:
    stream_id: str; camera_id: str; source_url: str; inference_fps: float; work_area: str; auto_email: bool
    analysis_mode: str = "realtime"; inspection_interval_sec: int = 300; status: str = "starting"; phase: str = "connecting_stream"
    started_at: float = field(default_factory=time.time); processed_frames: int = 0; emitted_events: int = 0
    last_error: str | None = None; connection_status: str = "connecting"; last_frame_at: float | None = None
    reconnect_count: int = 0; last_error_code: str | None = None
    inspection_submitted: int = 0; inspection_completed: int = 0; inspection_skipped: int = 0
    realtime_processed: int = 0; realtime_skipped: int = 0
    last_inference_at: float | None = None; last_event_at: float | None = None; orders_created: int = 0
    workflow_submitted: int = 0; workflow_completed: int = 0; workflow_failed: int = 0; last_workflow_error: str | None = None
    model_name: str = "本地小模型 + 实时规则"
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    ready_event: threading.Event = field(default_factory=threading.Event, repr=False)
    latest: LatestFrameSlot = field(default_factory=LatestFrameSlot, repr=False)
    pending_inspection: tuple[FrameInfo, float] | None = field(default=None, repr=False)
    inspection_running: bool = field(default=False, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def public(self) -> dict:
        keys = ("stream_id","camera_id","inference_fps","work_area","auto_email","analysis_mode","inspection_interval_sec","status","phase","started_at","processed_frames","emitted_events","last_error","connection_status","last_frame_at","reconnect_count","last_error_code","inspection_submitted","inspection_completed","inspection_skipped","realtime_processed","realtime_skipped","last_inference_at","last_event_at","orders_created","workflow_submitted","workflow_completed","workflow_failed","last_workflow_error","model_name")
        elapsed = max(time.time() - self.started_at, 0.001)
        return {key: getattr(self, key) for key in keys} | {"source_url": redact_source_url(self.source_url), "actual_fps": round(self.realtime_processed / elapsed, 2)}


class StreamManager:
    def __init__(self, *, capture_factory: object = cv2.VideoCapture, interruptible_wait: object | None = None, reconnect_policy: CaptureReconnectPolicy = CaptureReconnectPolicy()) -> None:
        self._states: dict[str, StreamState] = {}
        self._lock = threading.Lock(); self._detector = get_shared_detector(); self._capture_factory = capture_factory
        self._interruptible_wait = interruptible_wait or self._wait_for_stop; self._reconnect_policy = reconnect_policy
        self._inspection_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stream-inspection")
        self._workflow_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stream-workflow")
        self._email_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stream-email")
        self._email_slots = threading.BoundedSemaphore(2)

    @staticmethod
    def _wait_for_stop(stop: threading.Event, delay: float) -> bool: return stop.wait(delay)

    def start(self, camera_id: str, source_url: str, inference_fps: float, *, work_area: str, auto_email: bool, analysis_mode: str = "realtime", inspection_interval_sec: int = 300) -> dict:
        state = StreamState(uuid.uuid4().hex, camera_id, source_url, min(inference_fps, 2.0), work_area, auto_email, analysis_mode, inspection_interval_sec)
        from .projects import project_store
        project_store.bind("stream", state.stream_id)
        with self._lock: self._states[state.stream_id] = state
        threading.Thread(target=self._capture_loop, args=(state,), daemon=True).start()
        threading.Thread(target=self._processing_loop, args=(state,), daemon=True).start()
        return state.public()

    def stop(self, stream_id: str) -> dict | None:
        state = self._states.get(stream_id)
        if not state: return None
        with state.lock:
            state.stop_event.set(); state.pending_inspection = None; state.status = "stopping"
        return state.public()

    def wait_ready(self, stream_id: str, timeout: float = 10.0) -> dict:
        state = self._states.get(stream_id)
        if not state: return {"stream_id": stream_id, "status": "failed", "last_error": "算法任务不存在"}
        state.ready_event.wait(max(timeout, .1)); return state.public()
    def list(self) -> list[dict]: return [item.public() for item in self._states.values()]
    def events(self, stream_id: str, limit: int = 100) -> list[dict]:
        path = settings.data_dir / "streams" / stream_id / "events.jsonl"
        return [] if not path.exists() else [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()[-limit:] if x]

    def _capture_loop(self, state: StreamState) -> None:
        source = int(state.source_url) if state.source_url.isdigit() else state.source_url; retry = 0; reconnect_pending = False
        try:
            while not state.stop_event.is_set():
                if reconnect_pending:
                    state.reconnect_count += 1
                    reconnect_pending = False
                capture = None
                try: capture = self._capture_factory(source); opened = capture is not None and capture.isOpened()
                except Exception: opened = False; logger.exception("stream capture open failed")
                if not opened:
                    if capture: capture.release()
                    state.connection_status="reconnecting"; state.last_error="无法打开视频流"; state.last_error_code="capture_open_failed"
                    if self._wait_to_reconnect(state, retry): break
                    retry = min(retry + 1, len(self._reconnect_policy.delays)-1); reconnect_pending = True; continue
                if state.stop_event.is_set():
                    capture.release()
                    break
                state.connection_status="connected"; state.phase="waiting_first_frame"
                try:
                    while not state.stop_event.is_set():
                        try: ok, image = capture.read()
                        except Exception: ok, image = False, None; logger.exception("stream read failed")
                        if not ok or image is None:
                            state.connection_status="reconnecting"; state.last_error="取流中断，正在重试"; state.last_error_code="stream_read_failed"; break
                        state.last_frame_at=time.time(); state.latest.put(image.copy(), state.last_frame_at); retry=0
                        if not state.ready_event.is_set():
                            state.status="running"; state.phase="first_frame_ready"; state.ready_event.set()
                finally: capture.release()
                if not state.stop_event.is_set():
                    if self._wait_to_reconnect(state, retry): break
                    reconnect_pending = True
        finally: state.stop_event.set(); state.status="stopped"; state.ready_event.set()

    def _processing_loop(self, state: StreamState) -> None:
        sequence = 0; last_realtime = 0.
        interval, last_inspection = self._initial_inspection_clock(state)
        while not state.stop_event.is_set():
            item = state.latest.take_after(sequence, state.stop_event)
            if not item: continue
            sequence, timestamp, image = item
            if timestamp - last_realtime < 1 / state.inference_fps: state.realtime_skipped += 1; continue
            last_realtime = timestamp
            try:
                state.phase="inferencing"
                frame = self._save_frame(state, image)
                started = time.monotonic(); pipeline = HazardPipeline(detector=self._detector)
                whole = os.getenv("ALGORITHM_ADAPTER_SCOPE", "vlm").lower() == "whole" and os.getenv("ALGORITHM_ADAPTER_PROVIDER", "local").lower() not in {"", "local", "none"}
                if whole:
                    result = pipeline.analyze_frames(job_id=state.stream_id, frames=[frame], source_type="camera", mode="realtime", job_dir=Path(frame.path).parent.parent, detections_ready=True, context=self._context_for(state, timestamp))
                    findings = [VLMFinding(label_id=x.label_id,status=x.final_status,evidence="；".join(x.evidence),source_frame_ids=x.source_frame_ids,inspection_visibility="clear",severity=x.severity,severity_reason=x.severity_reason,severity_source=x.severity_source) for x in result.findings]
                    metrics = result.metadata
                else:
                    detector_mode = "realtime" if state.analysis_mode == "realtime" else "inspection"
                    frame.detections = self._detector.detect(frame, detector_mode, Path(frame.path).parent.parent / "annotated")
                    findings = pipeline._realtime_rules([frame]); metrics = {"capture_to_detect_ms": round((time.monotonic()-started)*1000,1)}
                # Test/inspection findings remain candidates until the VLM has
                # verified them.  Only realtime may publish rule-only findings.
                if whole or state.analysis_mode == "realtime":
                    self._emit(state, frame, findings, timestamp, metrics)
                state.realtime_processed += 1; state.last_inference_at = time.time(); state.phase="running"
                if not whole and timestamp - last_inspection >= interval:
                    last_inspection = timestamp; self._schedule_inspection(state, frame, timestamp)
            except Exception as exc:
                logger.exception("stream inference failed"); state.last_error = str(exc); state.last_error_code="algorithm_error"; state.phase="algorithm_error"

    @staticmethod
    def _initial_inspection_clock(state: StreamState) -> tuple[float, float]:
        """接入即首审：热身期(默认5秒，不超过一个巡检间隔)后立即执行第一次VLM全目录审核。

        旧逻辑首帧(画面往往未稳定)就审一次、之后要干等完整巡检周期，用户误以为
        “必须等第5分钟才识别重大隐患”。返回(巡检间隔秒, 首次到期时间戳)。
        """
        interval = float(settings.test_stream_interval_sec if state.analysis_mode == "test" else state.inspection_interval_sec)
        warmup = min(settings.stream_first_inspection_delay_sec, interval)
        return interval, state.started_at - interval + warmup

    def _schedule_inspection(self, state: StreamState, frame: FrameInfo, timestamp: float) -> None:
        with state.lock:
            if state.stop_event.is_set(): return
            if state.inspection_running:
                if state.pending_inspection: state.inspection_skipped += 1
                state.pending_inspection = (frame, timestamp); return
            state.inspection_running = True; state.inspection_submitted += 1
        self._inspection_executor.submit(copy_context().run, self._inspection_worker, state, frame, timestamp)

    def _inspection_worker(self, state: StreamState, frame: FrameInfo, timestamp: float) -> None:
        try:
            while not state.stop_event.is_set():
                try:
                    self._inspect_once(state, frame, timestamp)
                except Exception as exc:
                    logger.exception("流巡检任务失败 stream=%s", state.stream_id)
                    state.last_error = f"巡检分析失败：{type(exc).__name__}"; state.last_error_code="vlm_error"; state.phase="vlm_error"
                with state.lock:
                    state.inspection_completed += 1
                    next_item, state.pending_inspection = state.pending_inspection, None
                    if state.stop_event.is_set() or next_item is None:
                        return
                    frame, timestamp = next_item; state.inspection_submitted += 1
        finally:
            # An exception must not leave the one-slot scheduler permanently busy.
            with state.lock:
                if state.stop_event.is_set():
                    state.pending_inspection = None
                state.inspection_running = False

    def _inspect_once(self, state: StreamState, frame: FrameInfo, timestamp: float) -> None:
        started=time.monotonic(); from .projects import current_project, project_store
        project=project_store.owner("stream", state.stream_id); token=current_project.set(project) if project else None
        try:
            if state.stop_event.is_set():
                return
            state.phase="inspection"; state.model_name="本地小模型 + 规则 + VLM巡检"
            result=HazardPipeline(detector=self._detector).analyze_frames(job_id=state.stream_id,frames=[frame],source_type="camera",mode="inspection",job_dir=Path(frame.path).parent.parent,detections_ready=True,context=self._context_for(state, timestamp, project))
            findings=[VLMFinding(label_id=x.label_id,status=x.final_status,evidence="；".join(x.evidence),source_frame_ids=x.source_frame_ids,inspection_visibility="clear",severity=x.severity,severity_reason=x.severity_reason,severity_source=x.severity_source) for x in result.findings]
            self._emit(state, frame, findings, timestamp, {**result.metadata, "inspection_total_ms":round((time.monotonic()-started)*1000,1)})
            state.last_inference_at=time.time(); state.phase="running"
        finally:
            if token: current_project.reset(token)

    @staticmethod
    def _context_for(state: StreamState, timestamp: float, project: str | None = None) -> AlgorithmContext:
        if project is None:
            from .projects import project_store
            project = project_store.owner("stream", state.stream_id)
        return AlgorithmContext(project_id=project or "", work_area=state.work_area, camera_id=state.camera_id, task_id=state.stream_id, frame_timestamp=timestamp)

    def _save_frame(self, state: StreamState, image: object) -> FrameInfo:
        state.processed_frames += 1; frame_id=f"frame_{state.processed_frames:08d}"; directory=settings.data_dir/"streams"/state.stream_id/"snapshots"; directory.mkdir(parents=True,exist_ok=True); path=directory/f"{frame_id}.jpg"
        cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY,settings.snapshot_quality])[1].tofile(str(path)); blur,brightness,quality=image_quality(image); h,w=image.shape[:2]
        return FrameInfo(frame_id=frame_id,path=str(path),width=w,height=h,blur_score=blur,brightness=brightness,quality_status=quality)

    def _emit(self, state: StreamState, frame: FrameInfo, findings: list[VLMFinding], timestamp: float, metadata: dict) -> None:
        catalog={item.id:item for item in load_catalog()}; eligible=[]
        with state.lock:
            if state.stop_event.is_set(): return
            cooldown=getattr(state,"last_event_by_label",{}); state.last_event_by_label=cooldown
            order_cooldown=getattr(state,"last_order_by_label",{}); state.last_order_by_label=order_cooldown
            for item in findings:
                if timestamp-cooldown.get(item.label_id,0)>=settings.event_cooldown_sec: eligible.append(item); cooldown[item.label_id]=timestamp
            # 建单冷却独立于事件冷却：同一摄像头同一标签在窗口(默认10分钟)内只创建一次
            # 台账工单，防止持续隐患凭不同frame_id每个巡检周期重复刷单；事件流仍按
            # event_cooldown_sec刷新“最新识别发现”。
            confirmed=[item for item in eligible if item.status=="confirmed_hazard" and timestamp-order_cooldown.get(item.label_id,0)>=settings.order_cooldown_sec]
            for item in confirmed: order_cooldown[item.label_id]=timestamp
            payloads=[item.model_dump(mode="json") | {"name":catalog[item.label_id].name if item.label_id in catalog else item.label_id} for item in confirmed]
            if state.stop_event.is_set(): return
            if eligible: state.last_event_at=time.time()
            events_path=settings.data_dir/"streams"/state.stream_id/"events.jsonl"; events_path.parent.mkdir(parents=True,exist_ok=True)
            with events_path.open("a",encoding="utf-8") as out:
                for item in eligible:
                    label=catalog.get(item.label_id); out.write(json.dumps({"stream_id":state.stream_id,"camera_id":state.camera_id,"timestamp":timestamp,"label_id":item.label_id,"name":label.name if label else item.label_id,"category":label.category if label else "实时监测","status":item.status,"severity":item.severity,"severity_name":"重大隐患" if item.severity=="major" else "一般隐患","work_area":state.work_area,"evidence":item.evidence,"frame_id":frame.frame_id,"snapshot_url":f"/api/v1/streams/{state.stream_id}/artifact/snapshots/{Path(frame.path).name}","annotated_url":f"/api/v1/streams/{state.stream_id}/artifact/annotated/{Path(frame.annotated_path).name}" if frame.annotated_path else None,"metadata":metadata},ensure_ascii=False)+"\n"); state.emitted_events+=1
        # The event is now durable before any work-order or mail work begins.  Those slow
        # operations run outside the inference thread so the next camera frame is never held up.
        if payloads and not state.stop_event.is_set():
            state.workflow_submitted += 1
            self._workflow_executor.submit(copy_context().run, self._create_orders, state, payloads, frame, catalog)

    def _create_orders(self, state: StreamState, payloads: list[dict], frame: FrameInfo, catalog: dict) -> None:
        hazards: list[dict] = []
        try:
            if state.stop_event.is_set(): return
            hazards=workflow_store.create_from_findings(job_id=state.stream_id,work_area=state.work_area,findings=payloads,source_key_prefix=f"stream:{state.stream_id}",before_image=f"/api/v1/streams/{state.stream_id}/artifact/snapshots/{Path(frame.path).name}")
            state.orders_created += len(hazards); state.workflow_completed += 1; state.last_workflow_error = None
        except Exception as exc:
            logger.exception("stream workflow creation failed")
            state.workflow_failed += 1; state.last_workflow_error = str(exc); state.last_error_code="workflow_error"
            return
        if state.auto_email and hazards and not state.stop_event.is_set():
            if self._email_slots.acquire(blocking=False):
                findings=[VLMFinding(**{key:value for key,value in item.items() if key != "name"}) for item in payloads]
                self._email_executor.submit(copy_context().run, self._send_email, state, findings, hazards, Path(frame.path), catalog)
            else:
                workflow_store.record_email_status([item["id"] for item in hazards], [{"status":"skipped","error":"邮件队列繁忙"}])

    def _send_email(self,state,findings,hazards,image,catalog):
        records=[]
        try:
            from .projects import current_project
            group=hazards[0].get("group_id")
            url=f"{os.getenv('FRONTEND_PUBLIC_URL','http://localhost:3000').rstrip('/')}/?project_id={current_project.get()}&group={group}"
            # Group by actual recipient and then by the findings that recipient is
            # entitled to receive.  A shared mailbox must not receive a mixed,
            # over-broad severity summary merely because it has several roles.
            grouped: dict[str, dict[str, object]] = {}
            for finding in findings:
                for recipient in resolve_recipients(finding.severity, list(RECIPIENT_ROLES[finding.severity]), work_area=state.work_area):
                    group_item = grouped.setdefault(recipient.email, {"recipient": recipient, "findings": []})
                    group_item["findings"].append(finding)
            for group_item in grouped.values():
                recipient = group_item["recipient"]
                visible = group_item["findings"]
                if not visible or state.stop_event.is_set():
                    continue
                severity="major" if any(item.severity=="major" for item in visible) else "general"
                subject,body=build_hazard_summary_email(job_id=state.stream_id,hazard_names=[catalog.get(item.label_id).name if catalog.get(item.label_id) else item.label_id for item in visible],severity=severity,work_area=state.work_area,deadline=severity_resolver.deadline(severity))
                last=[]
                for attempt in range(2):
                    try:
                        last=send_email(recipients=[recipient],subject=subject,body=body,image_path=image,action_url=url)
                    except Exception:
                        logger.exception("邮件通知发送失败 recipient=%s", recipient.email)
                        last=[{"status":"failed","error":"邮件发送失败"}]
                    if last and last[0].get("status")=="sent" or state.stop_event.is_set():
                        break
                    if attempt == 0:
                        state.stop_event.wait(.2)
                records.extend(last)
        except Exception as exc: records=[{"status":"failed","error":str(exc)}]
        finally:
            workflow_store.record_email_status([x["id"] for x in hazards],records)
            self._email_slots.release()

    def _wait_to_reconnect(self,state,reconnect_index=0): return bool(self._interruptible_wait(state.stop_event,self._reconnect_policy.delays[min(reconnect_index,len(self._reconnect_policy.delays)-1)]))

stream_manager=StreamManager()
