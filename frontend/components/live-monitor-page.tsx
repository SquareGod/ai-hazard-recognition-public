"use client";

import { ChevronLeft, ChevronRight, Expand, RefreshCcw, Video } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import ManagedStreamPlayer from "@/components/managed-stream-player";
import { consoleApi, type MonitorCamera, type MonitorPlaybackTicket, type StreamEvent, type StreamSession } from "@/lib/api";
import type { Device } from "@/lib/types";

const PAGE_SIZE = 4;
const MAX_SELECTED_CAMERAS = 4;

export type MonitorPageApi = Pick<typeof consoleApi, "listMonitorCameras" | "createMonitorPlaybackTicket" | "releaseMonitorPlaybackTicket"> & Partial<Pick<typeof consoleApi, "monitorSnapshotUrl" | "listStreamSessions" | "startStreamSession" | "stopStreamSession" | "getStreamEvents">>;

type Props = { api?: MonitorPageApi; devices?: Device[] };

const statusText = {
  online: "在线",
  reconnecting: "重连中",
  offline: "离线",
  unauthorized: "认证失败",
  codec_unsupported: "编码不兼容",
} as const;

function timeText(value?: number | null) {
  return value ? new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "—";
}

function AiBadge({ session }: { session?: StreamSession }) {
  if (!session) return <div className="monitor-badges"><span className="camera-state online">在线</span><span className="ai-state idle">AI 未启动</span></div>;
  const label: Record<string, string> = { connecting_stream: "连接内部流", waiting_first_frame: "等待首帧", first_frame_ready: "首帧已到达", inferencing: "实时推理", inspection: "VLM巡检", running: "AI 运行中", algorithm_error: "算法异常", vlm_error: "VLM异常" };
  return <div className="monitor-badges"><span className="camera-state online">在线</span><span className={`ai-state ${session.phase?.includes("error") ? "error" : ""}`}>{label[session.phase || ""] || "AI 启动中"}</span></div>;
}

function MonitorDiagnostics({ session, fallback }: { session?: StreamSession; fallback?: string }) {
  if (!session) return <div className="monitor-diagnostics"><span>视频取流：未启动 AI</span><span>最后在线：{fallback || "等待首帧"}</span><span>启动 AI 后显示抽帧、推理和建单状态</span></div>;
  return <div className="monitor-diagnostics"><span>取流：{session.connection_status === "connected" ? "正常" : session.connection_status || "连接中"} · 首帧 {timeText(session.last_frame_at)}</span><span>推理：{timeText(session.last_inference_at)} · {session.processed_frames || 0} 帧 · {session.actual_fps?.toFixed(1) || "0.0"} FPS</span><span>事件：{session.emitted_events || 0} · 已建单 {session.orders_created || 0} · 最近 {timeText(session.last_event_at)}</span><span>模型：{session.model_name || "加载中"}{session.last_error ? ` · ${session.last_error}` : ""}</span></div>;
}

function MonitorSnapshot({ camera, src, onSelect, session }: { camera: MonitorCamera; src: () => string; onSelect: () => void; session?: StreamSession }) {
  const [image, setImage] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = () => { if (active) { setImage(src()); setFailed(false); } };
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [src]);
  return <button className="monitor-preview" onClick={onSelect} aria-label={`切换 ${camera.name} 为主画面`} title={`切换 ${camera.name} 为主画面`}>
    {image && !failed ? <img src={image} alt={`${camera.name}辅助预览`} onError={() => setFailed(true)} /> : <div className="monitor-preview-empty">正在获取预览图</div>}
    <span><b>{camera.name}</b><small>{camera.work_area || "未分配工区"} · {session ? "AI运行" : "点击查看"}</small></span>
  </button>;
}

export default function LiveMonitorPage({ api = consoleApi, devices = [] }: Props) {
  const [workArea, setWorkArea] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ items: MonitorCamera[]; total: number; workAreas: string[] }>({ items: [], total: 0, workAreas: [] });
  const [cameraDirectory, setCameraDirectory] = useState<MonitorCamera[]>([]);
  const [directoryLoading, setDirectoryLoading] = useState(true);
  const [viewMode, setViewMode] = useState<"page" | "selected">("page");
  const [draftCameraIds, setDraftCameraIds] = useState<string[]>([]);
  const [selectedCameraIds, setSelectedCameraIds] = useState<string[]>([]);
  const [primaryCameraId, setPrimaryCameraId] = useState("");
  const [ticket, setTicket] = useState<MonitorPlaybackTicket | null>(null);
  const [enlargedId, setEnlargedId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sessions, setSessions] = useState<StreamSession[]>([]);
  const [latestFindings, setLatestFindings] = useState<StreamEvent[]>([]);
  const [changingAi, setChangingAi] = useState<string | null>(null);
  const [sendEmailOnFinding, setSendEmailOnFinding] = useState(true);
  const wallRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let disposed = false;
    setLoading(true);
    setError("");
    setTicket(null);
    setPage((current) => ({ ...current, items: [] }));

    void (async () => {
      try {
        const cameraPage = await api.listMonitorCameras({ workArea, limit: PAGE_SIZE, offset });
        if (disposed) return;
        setPage({ items: cameraPage.items, total: cameraPage.total, workAreas: cameraPage.work_areas });
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : "实时画面加载失败");
      } finally {
        if (!disposed) setLoading(false);
      }
    })();

    return () => {
      disposed = true;
    };
  }, [api, offset, reloadKey, workArea]);

  useEffect(() => {
    let disposed = false;
    setDirectoryLoading(true);
    void (async () => {
      try {
        const all: MonitorCamera[] = [];
        let directoryOffset = 0;
        let total = 0;
        do {
          const response = await api.listMonitorCameras({ workArea, limit: PAGE_SIZE, offset: directoryOffset });
          total = response.total;
          all.push(...response.items);
          directoryOffset += response.items.length;
          if (response.items.length === 0) break;
        } while (directoryOffset < total);
        if (!disposed) setCameraDirectory(all);
      } catch {
        if (!disposed) setCameraDirectory([]);
      } finally {
        if (!disposed) setDirectoryLoading(false);
      }
    })();
    return () => { disposed = true; };
  }, [api, reloadKey, workArea]);

  const activeCameraIds = useMemo(
    () => viewMode === "selected" ? selectedCameraIds : page.items.map((camera) => camera.id),
    [page.items, selectedCameraIds, viewMode],
  );
  const activeCameraKey = activeCameraIds.join("|");
  const primaryId = activeCameraIds.includes(primaryCameraId) ? primaryCameraId : activeCameraIds[0] || "";

  useEffect(() => {
    let disposed = false;
    let ownedTicketId = "";
    let renewalTimer: number | undefined;
    const cameraIds = primaryId ? [primaryId] : [];

    const renewTicket = async () => {
      try {
        const renewed = await api.createMonitorPlaybackTicket(cameraIds, "monitor-wall");
        if (disposed) {
          await api.releaseMonitorPlaybackTicket(renewed.ticket_id).catch(() => undefined);
          return;
        }
        const previousTicketId = ownedTicketId;
        ownedTicketId = renewed.ticket_id;
        setTicket(renewed);
        setError("");
        if (previousTicketId) await api.releaseMonitorPlaybackTicket(previousTicketId).catch(() => undefined);
        renewalTimer = window.setTimeout(() => void renewTicket(), Math.max(15, renewed.expires_in - 30) * 1000);
      } catch (reason) {
        if (!disposed) {
          if (!ownedTicketId) setError(reason instanceof Error ? reason.message : "实时画面加载失败");
          renewalTimer = window.setTimeout(() => void renewTicket(), 5000);
        }
      }
    };

    if (cameraIds.length) void renewTicket();
    else setTicket(null);
    return () => {
      disposed = true;
      if (renewalTimer !== undefined) window.clearTimeout(renewalTimer);
      if (ownedTicketId) void api.releaseMonitorPlaybackTicket(ownedTicketId).catch(() => undefined);
    };
  }, [api, primaryId, reloadKey]);

  useEffect(() => {
    if (!api.listStreamSessions || !api.getStreamEvents) return;
    const listSessions = api.listStreamSessions;
    const getEvents = api.getStreamEvents;
    let disposed = false;
    const refreshAi = async () => {
      try {
        const active = (await listSessions()).filter((item) => ["starting", "running"].includes(item.status));
        if (disposed) return;
        setSessions(active);
        const eventGroups = await Promise.all(active.map((item) => getEvents(item.inference_stream_id).catch(() => [])));
        if (!disposed) setLatestFindings(eventGroups.flat().sort((a, b) => b.timestamp - a.timestamp).slice(0, 5));
      } catch { /* 监看画面仍可正常显示 */ }
    };
    void refreshAi();
    const timer = window.setInterval(() => void refreshAi(), 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [api, reloadKey]);

  const areas = page.workAreas;
  const activeCameras = useMemo(() => activeCameraIds.map((id) => cameraDirectory.find((camera) => camera.id === id) || page.items.find((camera) => camera.id === id)).filter((camera): camera is MonitorCamera => Boolean(camera)), [activeCameraIds, cameraDirectory, page.items]);
  const primary = ticket?.cameras.find((camera) => camera.id === primaryId);
  const primaryDevice = devices.find((item) => item.id === primaryId);
  const pageNumber = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / PAGE_SIZE));

  async function enterFullscreen() {
    await wallRef.current?.requestFullscreen?.();
  }

  async function toggleAi(cameraId: string) {
    if (!api.startStreamSession || !api.stopStreamSession) { setError("当前监控服务未提供 AI 任务控制接口"); return; }
    const active = sessions.find((item) => item.source_id === cameraId);
    setChangingAi(cameraId);
    try {
      if (active) {
        await api.stopStreamSession(active.session_id);
        setSessions((items) => items.filter((item) => item.session_id !== active.session_id));
      } else {
        const session = await api.startStreamSession({ source_id: cameraId, inference_fps: 2, auto_email: sendEmailOnFinding, analysis_mode: "realtime" });
        setSessions((items) => [...items.filter((item) => item.source_id !== cameraId), session]);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "AI 任务操作失败"); }
    finally { setChangingAi(null); }
  }

  function toggleDraftCamera(cameraId: string) {
    setDraftCameraIds((ids) => {
      if (ids.includes(cameraId)) return ids.filter((id) => id !== cameraId);
      return ids.length < MAX_SELECTED_CAMERAS ? [...ids, cameraId] : ids;
    });
  }

  function applyCameraSelection() {
    if (!draftCameraIds.length) return;
    setSelectedCameraIds(draftCameraIds);
    setPrimaryCameraId(draftCameraIds[0] || "");
    setViewMode("selected");
    setError("");
  }

  function returnToPagination() {
    setViewMode("page");
    setError("");
    setPrimaryCameraId(page.items[0]?.id || "");
  }

  return <div className="monitor-page">
    <div className="page-heading monitor-heading">
      <div><span className="section-kicker">流媒体实时监看</span><h2>摄像头实时监控</h2><p>最多四路同屏；监看与 AI 分析独立启停，互不影响。</p></div>
      <div className="monitor-actions">
        <select aria-label="筛选工区" value={workArea} onChange={(event) => { setWorkArea(event.target.value); setOffset(0); }}>
          <option value="">全部工区</option>{areas.map((area) => <option key={area}>{area}</option>)}
        </select>
        <button className="ghost-action" onClick={() => setReloadKey((value) => value + 1)}><RefreshCcw size={16}/>刷新</button>
        <button className="secondary-action" onClick={enterFullscreen}><Expand size={16}/>全屏监看</button>
      </div>
    </div>

    <section className="monitor-toolbar" aria-label="摄像头选择器">
      <span className="monitor-toolbar-label">自选监看</span>
      <div className="camera-chip-row">
        {directoryLoading && <span className="monitor-chip-hint">正在加载摄像头目录……</span>}
        {!directoryLoading && cameraDirectory.map((camera) => <label key={camera.id} className={`monitor-camera-chip${draftCameraIds.includes(camera.id) ? " selected" : ""}`} title={`${camera.work_area || "未分工区"} · 点击“应用选择”后切换画面`}><input type="checkbox" checked={draftCameraIds.includes(camera.id)} disabled={!draftCameraIds.includes(camera.id) && draftCameraIds.length >= MAX_SELECTED_CAMERAS} onChange={() => toggleDraftCamera(camera.id)} /><span>{camera.name}</span></label>)}
        {!directoryLoading && !cameraDirectory.length && <span className="monitor-chip-hint">当前筛选条件下暂无摄像头。</span>}
      </div>
      <div className="monitor-toolbar-actions">
        <span className="monitor-toolbar-count">{draftCameraIds.length} / {MAX_SELECTED_CAMERAS} 路</span>
        <button className="secondary-action" disabled={!draftCameraIds.length || draftCameraIds.length > MAX_SELECTED_CAMERAS} onClick={applyCameraSelection}>应用选择</button>
        {viewMode === "selected" && <button className="ghost-action" onClick={returnToPagination}>切回分页监看</button>}
        <label className="monitor-email-toggle" title="启动 AI 后按当前项目配置的人员分发邮件"><input type="checkbox" checked={sendEmailOnFinding} onChange={(event) => setSendEmailOnFinding(event.target.checked)} /><span>识别后发送邮件</span></label>
      </div>
    </section>

    <section className="monitor-wall" ref={wallRef}>
      {loading && <div className="monitor-empty">正在连接摄像头……</div>}
      {!loading && error && <div className="monitor-empty error">{error}</div>}
      {!loading && !error && activeCameraIds.length === 0 && <div className="monitor-empty"><Video size={30}/><strong>暂无可监看的摄像头</strong><span>请先在设备管理添加并启用视频源。</span></div>}
      {!loading && !error && primary && <article className="monitor-primary-card">
        <div className="monitor-card-title"><div><strong>{primary.name}</strong><span>{primary.work_area} · {primaryDevice?.point || "未配置点位"}</span></div><AiBadge session={sessions.find((item) => item.source_id === primary.id)} /></div>
        <ManagedStreamPlayer camera={{ id: primary.id, name: primary.name, webrtcUrl: primary.webrtc, hlsUrl: primary.hls }} onEnlarge={() => setEnlargedId(primary.id)} />
        <MonitorDiagnostics session={sessions.find((item) => item.source_id === primary.id)} fallback={primaryDevice?.lastSeen} />
        <div className="monitor-card-meta"><span>主画面保持实时播放；切换摄像头不会启动第二条 NVR 上游流。</span><button className={sessions.some((item) => item.source_id === primary.id) ? "danger-outline" : "secondary-action"} disabled={changingAi === primary.id} onClick={() => void toggleAi(primary.id)}>{sessions.some((item) => item.source_id === primary.id) ? "停止 AI" : "启动 AI"}</button></div>
      </article>}
      {!loading && !error && activeCameras.filter((camera) => camera.id !== primaryId).length > 0 && <aside className="monitor-preview-rail" aria-label="辅助摄像头预览">
        {activeCameras.filter((camera) => camera.id !== primaryId).map((camera) => <MonitorSnapshot key={camera.id} camera={camera} src={() => api.monitorSnapshotUrl?.(camera.id) || ""} onSelect={() => setPrimaryCameraId(camera.id)} session={sessions.find((item) => item.source_id === camera.id)} />)}
      </aside>}
    </section>

    <section className="surface monitor-findings"><div className="section-title"><div><span>AI 中央控制</span><h3>最新识别发现</h3></div><div className="section-action"><span>{sessions.length} 路 AI 运行中</span></div></div><div>{latestFindings.length ? latestFindings.map((finding) => <article key={`${finding.stream_id}-${finding.timestamp}-${finding.label_id}`}><b>{finding.name}</b><span>{finding.camera_id} · {finding.severity === "major" ? "重大隐患" : "一般隐患"}</span><small>{new Date(finding.timestamp * 1000).toLocaleString("zh-CN")}</small></article>) : <p>暂无最新发现。启动 AI 后，识别事件会在此处自动刷新。</p>}</div></section>

    {viewMode === "page" && <div className="monitor-pagination">
      <button aria-label="上一组" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={17}/>上一组</button>
      <span>第 {pageNumber} / {pageCount} 组，共 {page.total} 路</span>
      <button aria-label="下一组" disabled={offset + PAGE_SIZE >= page.total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一组<ChevronRight size={17}/></button>
    </div>}

    {enlargedId && primary && enlargedId === primary.id && <div className="modal-backdrop monitor-dialog-backdrop" role="dialog" aria-modal="true" aria-label={`${primary.name}放大画面`}>
      <div className="monitor-dialog">
        <header><div><strong>{primary.name}</strong><span>{primary.work_area} · {primaryDevice?.point || "未配置点位"}</span></div><button aria-label="关闭放大画面" onClick={() => setEnlargedId(null)}>关闭</button></header>
        <ManagedStreamPlayer enlarged camera={{ id: primary.id, name: primary.name, webrtcUrl: primary.webrtc, hlsUrl: primary.hls }} />
      </div>
    </div>}
  </div>;
}
