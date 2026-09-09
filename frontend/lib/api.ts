/**
 * 统一业务后端接口适配层。
 *
 * 浏览器只调用本地算法与业务后端，不直接调用视觉大模型，也不保存邮箱
 * 授权码或摄像头密码。
 */

import { getSelectedProjectId } from "./project-selection";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";
const API_ORIGIN = API_BASE.replace(/\/api\/v1\/?$/, "");
let csrfToken = "";

export function businessRequestHeaders(method = "GET", initial?: HeadersInit) {
  const headers = new Headers(initial);
  // 项目上下文由单一选择器维护；即使是项目列表等全局接口也携带默认项目头。
  if (!headers.has("X-Project-ID")) headers.set("X-Project-ID", getSelectedProjectId());
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) headers.set("X-CSRF-Token", csrfToken);
  return headers;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const method = options?.method ?? "GET";
  const headers = businessRequestHeaders(method, options?.headers);
  if (!(options?.body instanceof FormData)) headers.set("Content-Type", "application/json");

  const response = await fetch(`${API_BASE}${path}`, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    const raw = await response.text().catch(() => "");
    let detail = raw;
    try { detail = JSON.parse(raw).detail || raw; } catch { /* text error */ }
    const error = new Error(detail || `接口请求失败：${response.status}`) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return response.json() as Promise<T>;
}

export const consoleApi = {
  getAlgorithmCapabilities: () => request<Record<string, unknown>>("/algorithm/capabilities"),
  getAlgorithmSettings: () => request<AlgorithmSettings>("/algorithm/settings"),
  listProjects: () => request<{ projects: Project[] }>("/projects"),
  createProject: (name: string) => request<Project>("/projects", { method: "POST", body: JSON.stringify({ name }) }),
  updateProject: (id: string, name: string) => request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteProject: (projectId: string) => request<{ id: string; deleted: boolean }>(`/projects/${projectId}`, { method: "DELETE" }),
  deleteProject: (projectId: string) => request<{ id: string; deleted: boolean }>(`/projects/${projectId}`, { method: "DELETE" }),
  setProjectMemberEnabled: (projectId: string, userId: string, enabled: boolean) => request<{ ok: boolean }>(`/projects/${projectId}/members/${userId}`, { method: "PUT", body: JSON.stringify({ enabled }) }),
  login: async (email: string, password: string) => {
    const result = await request<AuthResponse>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    csrfToken = result.csrf_token;
    return result;
  },
  me: async () => {
    const result = await request<AuthResponse>("/auth/me");
    csrfToken = result.csrf_token;
    return result;
  },
  logout: async () => { const result = await request<{ ok: boolean }>("/auth/logout", { method: "POST" }); csrfToken = ""; return result; },
  changePassword: async (currentPassword: string, newPassword: string) => {
    const result = await request<{ ok: boolean; login_required: boolean }>("/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
    csrfToken = "";
    return result;
  },
  listUsers: () => request<SystemUser[]>("/users"),
  createUser: (payload: UserCreatePayload) => request<{ user: SystemUser; temporary_password: string }>("/users", { method: "POST", body: JSON.stringify(payload) }),
  updateUser: (id: string, payload: Partial<Pick<SystemUser, "name" | "role" | "work_area" | "enabled">>) => request<SystemUser>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  resetUserPassword: (id: string) => request<{ temporary_password: string }>(`/users/${id}/reset-password`, { method: "POST" }),
  health: () => request<HealthResponse>("/health"),
  uploadJobs: (payload: FormData, projectId?: string) => request<UploadJobsResponse>("/jobs/upload", { method: "POST", body: payload, headers: projectId ? { "X-Project-ID": projectId } : undefined }),
  getJob: (jobId: string, projectId?: string) => request<JobState>(`/jobs/${jobId}`, { headers: projectId ? { "X-Project-ID": projectId } : undefined }),
  getJobResult: (jobId: string, projectId?: string) => request<AnalysisResult>(`/jobs/${jobId}/result`, { headers: projectId ? { "X-Project-ID": projectId } : undefined }),
  dispatchJobEmail: (jobId: string, payload: EmailDispatchPayload, projectId?: string) => request<EmailDispatchResponse>(`/jobs/${jobId}/notifications/email`, { method: "POST", body: JSON.stringify(payload), headers: projectId ? { "X-Project-ID": projectId } : undefined }),
  startStream: (payload: StreamStartPayload) => request<StreamState>("/streams/start", { method: "POST", body: JSON.stringify(payload) }),
  stopStream: (streamId: string) => request<StreamState>(`/streams/${streamId}/stop`, { method: "POST" }),
  getStreamEvents: (streamId: string) => request<StreamEvent[]>(`/streams/${streamId}/events`),
  createVideoSource: (payload: VideoSourcePayload) => request<VideoSourceView>("/video-sources", { method: "POST", body: JSON.stringify(payload) }),
  listVideoSources: () => request<VideoSourceView[]>("/video-sources"),
  testVideoSource: (sourceId: string) => request<{ ok: boolean; source: VideoSourceView; requires_transcode: boolean }>(`/video-sources/${sourceId}/test`, { method: "POST" }),
  listHikvisionProfiles: () => request<HikvisionProfile[]>("/hikvision/nvr-profiles"),
  configureHikvisionProfile: (payload: HikvisionProfileInput) => request<HikvisionProfile>("/hikvision/nvr-profiles", { method: "POST", body: JSON.stringify(payload) }),
  testHikvisionProfile: (id: string) => request<{ ok: boolean; message: string }>(`/hikvision/nvr-profiles/${id}/test`, { method: "POST" }),
  syncHikvisionProfile: (id: string) => request<{ profile_id: string; channels: HikvisionChannel[] }>(`/hikvision/nvr-profiles/${id}/sync`, { method: "POST" }),
  listHikvisionChannels: () => request<HikvisionChannel[]>("/hikvision/channels"),
  updateHikvisionChannel: (id: string, payload: Partial<HikvisionChannel>) => request<HikvisionChannel>(`/hikvision/channels/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  startStreamSession: (payload: StreamSessionStartPayload) => request<StreamSession>("/stream-sessions", { method: "POST", body: JSON.stringify(payload) }),
  stopStreamSession: (sessionId: string) => request<StreamSession>(`/stream-sessions/${sessionId}/stop`, { method: "POST" }),
  listStreamSessions: () => request<StreamSession[]>("/stream-sessions"),
  listMonitorCameras: ({ workArea = "", limit = 4, offset = 0 }: { workArea?: string; limit?: number; offset?: number }) => {
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (workArea) query.set("work_area", workArea);
    return request<MonitorCameraPage>(`/monitor/cameras?${query}`);
  },
  createMonitorPlaybackTicket: (cameraIds: string[], purpose = "monitor-wall") => request<MonitorPlaybackTicket>("/monitor/playback-tickets", {
    method: "POST",
    body: JSON.stringify({ camera_ids: cameraIds, purpose }),
  }),
  releaseMonitorPlaybackTicket: (ticketId: string) => request<{ ticket_id: string; released: boolean }>(`/monitor/playback-tickets/${ticketId}`, { method: "DELETE" }),
  monitorSnapshotUrl: (cameraId: string, nonce = Date.now()) => `${API_ORIGIN}/api/v1/monitor/cameras/${encodeURIComponent(cameraId)}/snapshot?_=${nonce}`,
  getOverview: (projectId: string) => request(`/projects/${projectId}/overview`),
  getHazards: (query = "") => request<BackendHazard[]>(`/hazards${query}`),
  getHazardGroups: () => request<BackendHazardGroup[]>("/hazard-groups"),
  getHazardGroup: (id: string) => request<BackendHazardGroup>(`/hazard-groups/${id}`),
  verifyGroupHazard: (groupId: string, hazardId: string, payload: HazardVerificationPayload) => request<{ group: BackendHazardGroup; hazard: BackendHazard }>(`/hazard-groups/${groupId}/hazards/${hazardId}/verify`, { method: "POST", body: JSON.stringify(payload) }),
  submitGroupRectification: (groupId: string, hazardId: string, payload: RectificationSubmitPayload) => request<{ group: BackendHazardGroup; hazard: BackendHazard }>(`/hazard-groups/${groupId}/hazards/${hazardId}/rectifications`, { method: "POST", body: JSON.stringify(payload) }),
  reviewGroupHazard: (groupId: string, hazardId: string, payload: HazardReviewPayload) => request<{ group: BackendHazardGroup; hazard: BackendHazard }>(`/hazard-groups/${groupId}/hazards/${hazardId}/reviews`, { method: "POST", body: JSON.stringify(payload) }),
  confirmGroupMajor: (groupId: string, hazardId: string, payload: HazardReviewPayload) => request<{ group: BackendHazardGroup; hazard: BackendHazard }>(`/hazard-groups/${groupId}/hazards/${hazardId}/major-confirmation`, { method: "POST", body: JSON.stringify(payload) }),
  getNotifications: (markRead = false) => request<{ unread_count: number; items: BackendNotification[] }>(`/notifications${markRead ? "?mark_read=true" : ""}`, markRead ? { headers: { "X-CSRF-Token": csrfToken } } : undefined),
  getHazard: (hazardId: string) => request(`/hazards/${hazardId}`),
  verifyHazard: (hazardId: string, payload: unknown) => request<BackendHazard>(`/hazards/${hazardId}/verify`, { method: "POST", body: JSON.stringify(payload) }),
  markFalsePositive: (hazardId: string, payload: unknown) => request<BackendHazard>(`/hazards/${hazardId}/mark-false-positive`, { method: "POST", body: JSON.stringify(payload) }),
  undoFalsePositive: (hazardId: string) => request<BackendHazard>(`/hazards/${hazardId}/undo-false-positive`, { method: "POST" }),
  submitRectification: (hazardId: string, payload: RectificationSubmitPayload) => request<BackendHazard>(`/hazards/${hazardId}/rectifications`, { method: "POST", body: JSON.stringify(payload) }),
  uploadRectificationEvidence: (hazardId: string, payload: FormData) => request<{ url: string }>(`/hazards/${hazardId}/rectification-evidence`, { method: "POST", body: payload }),
  reviewHazard: (hazardId: string, payload: HazardReviewPayload) => request<BackendHazard>(`/hazards/${hazardId}/reviews`, { method: "POST", body: JSON.stringify(payload) }),
  confirmMajorHazard: (hazardId: string, payload: HazardReviewPayload) => request<BackendHazard>(`/hazards/${hazardId}/major-confirmation`, { method: "POST", body: JSON.stringify(payload) }),
  getOrders: () => request("/orders"),
  getPeople: () => request("/people"),
  createPerson: (payload: unknown) => request("/people", { method: "POST", body: JSON.stringify(payload) }),
  updatePerson: (personId: string, payload: unknown) => request(`/people/${personId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  getDevices: () => request("/devices"),
  testDevice: (deviceId: string) => request(`/devices/${deviceId}/test`, { method: "POST" }),
  uploadInspection: (payload: FormData) => request("/inspections/uploads", { method: "POST", body: payload }),
  retryNotification: (notificationId: string) => request(`/notifications/${notificationId}/retry`, { method: "POST" }),
  createExport: (type: string, payload: unknown) => request(`/exports/${type}`, { method: "POST", body: JSON.stringify(payload) }),
};

export type UserRole = "system_admin" | "safety_officer" | "work_area_manager" | "project_manager" | "safety_director";
export type Project = { id: string; name: string };
export type SystemUser = { id: string; name: string; email: string; role: UserRole; role_name: string; work_area: string; enabled: boolean; must_change_password: boolean; project_member?: boolean };
export type AuthResponse = { user: SystemUser; csrf_token: string };
export type UserCreatePayload = { name: string; email: string; role: UserRole; work_area: string };

export type Finding = {
  label_id: string;
  category: string;
  name: string;
  final_status: "confirmed_hazard" | "review_required";
  evidence: string[];
  source_frame_ids: string[];
  occurrence_count: number;
  severity: "general" | "major";
  severity_name: "一般隐患" | "重大隐患";
  severity_reason: string;
  severity_source: "model" | "realtime_rule" | "catalog_rule";
  severity_rule_version: string;
};

export type AnalysisFrame = {
  frame_id: string;
  artifact_url?: string | null;
  annotated_url?: string | null;
};

export type AnalysisResult = {
  job_id: string;
  source_type: "image" | "video" | "camera";
  created_at: string;
  frames: AnalysisFrame[];
  findings: Finding[];
  no_clear_hazard: boolean;
  warnings: string[];
};

export type JobProgress = { stage: string; completed: number; total: number; elapsed_sec: number; estimated_remaining_sec?: number | null };
export type JobState = { id: string; status: "queued" | "running" | "completed" | "failed"; error?: string | null; progress?: JobProgress | null };
export type AlgorithmSettings = { realtime_fps: number; inspection_interval_sec: number; test_stream_interval_sec: number };
export type UploadJobsResponse = { jobs: Array<{ job_id: string; filename: string; status_url: string }> };
export type HealthResponse = { ok: boolean; api_key_configured: boolean; email_notification: { smtp_configured: boolean; recipients: Array<{ role: string; role_name: string; email: string }> } };
export type EmailRecord = { role: string; recipient: string; email: string; status: "sent" | "failed"; error?: string };
export type EmailDispatchPayload = { work_area: string; finding_label_id?: string };
export type EmailDispatchResponse = { job_id: string; records: EmailRecord[]; dispatches: Array<{ severity: "general" | "major"; severity_name: "一般隐患" | "重大隐患"; deadline: string; findings: Finding[]; records: EmailRecord[] }> };
export type RectificationSubmitPayload = { description: string; submitted_by?: string; after_image?: string | null };
export type HazardReviewPayload = { passed: boolean; reviewer?: string; comment?: string };
export type HazardVerificationPayload = { passed: boolean; version: number; reason?: string; description_correct?: boolean; corrected_name?: string; corrected_evidence?: string; corrected_severity?: "general" | "major" };
export type BackendRectification = { id: string; hazard_id: string; description: string; submitted_by: string; after_image?: string | null; submitted_at: string; review_result?: "通过" | "退回" | null; reviewer?: string | null; review_comment?: string | null };
export type BackendAuditEvent = { id: string; action: string; actor_name: string; before_value: Record<string, unknown>; after_value: Record<string, unknown>; created_at: string };
export type BackendHazard = { id: string; group_id?: string | null; group_order_id?: string | null; job_id?: string | null; label_id: string; name: string; original_name?: string | null; severity: "general" | "major"; original_severity?: "general" | "major" | null; severity_name?: "一般隐患" | "重大隐患"; work_area: string; status: string; evidence: string; original_evidence?: string | null; before_image?: string | null; created_at: string; updated_at: string; version?: number; rectifications?: BackendRectification[]; audit_events?: BackendAuditEvent[] };
export type BackendNotification = { id: string; title: string; body: string; read: boolean; created_at: string; delivery_status: string; delivery_records: EmailRecord[]; notification_type?: "initial" | "correction" | "rectification" };
export type BackendHazardGroup = { id: string; order_id: string; job_id?: string | null; work_area: string; point: string; before_image?: string | null; annotated_image?: string | null; created_at: string; updated_at: string; status: string; hazards: BackendHazard[]; notifications: BackendNotification[] };
export type StreamStartPayload = { camera_id: string; source_url: string; inference_fps: number; work_area: string; auto_email: boolean };
export type StreamState = { stream_id: string; camera_id: string; status: string; work_area: string; auto_email: boolean; processed_frames: number; emitted_events: number; last_error?: string | null };
export type VideoSourcePayload = { id: string; name: string; source_type: "rtsp" | "hikvision"; source_url: string; work_area: string; enabled?: boolean; risk_point?: string; ai_enabled?: boolean };
export type VideoSourceView = { id: string; name: string; source_type: string; source_url: string; work_area: string; enabled: boolean; risk_point?: string; ai_enabled?: boolean };
export type HikvisionProfileInput = { id?: string; name: string; host: string; port: number; username: string; password: string };
export type HikvisionProfile = { id: string; name: string; host: string; port: number; username: string; configured: boolean };
export type HikvisionChannel = { id: string; profile_id: string; channel_no: number; name: string; nvr_name: string; online: boolean; work_area: string; risk_point: string; enabled: boolean; ai_enabled: boolean };
export type StreamSessionStartPayload = { source_id: string; inference_fps: number; auto_email: boolean; analysis_mode?: "realtime" | "inspection" | "test"; inspection_interval_sec?: number };
export type StreamSession = {
  session_id: string;
  source_id: string;
  camera_id?: string;
  inference_stream_id: string;
  status: "starting" | "running" | "stopping" | "stopped" | "failed";
  phase?: "connecting_stream" | "waiting_first_frame" | "first_frame_ready" | "inferencing" | "inspection" | "running" | "algorithm_error" | "vlm_error" | string;
  started_at: number;
  last_error?: string | null;
  playback_urls: { webrtc: string; hls: string };
  analysis_mode?: "realtime" | "inspection" | "test";
  processed_frames?: number;
  emitted_events?: number;
  actual_fps?: number;
  frames_submitted?: number;
  frames_skipped?: number;
  vlm_submitted?: number;
  vlm_skipped?: number;
  last_frame_at?: number | null;
  last_inference_at?: number | null;
  last_event_at?: number | null;
  orders_created?: number;
  workflow_submitted?: number;
  workflow_completed?: number;
  workflow_failed?: number;
  last_workflow_error?: string | null;
  model_name?: string;
  connection_status?: string;
  last_error_code?: string | null;
};
export type StreamEvent = { stream_id: string; camera_id: string; timestamp: number; label_id: string; name: string; category: string; status: "confirmed_hazard" | "review_required"; severity: "general" | "major"; work_area: string; evidence: string; snapshot_url: string; annotated_url?: string | null; notification_records?: Array<{ role: string; recipient: string; email: string; status: "sent" | "failed" }> };
export type MonitorCameraStatus = "online" | "reconnecting" | "offline" | "unauthorized" | "codec_unsupported";
export type MonitorCamera = { id: string; name: string; work_area: string; status: MonitorCameraStatus; enabled: boolean };
export type MonitorCameraPage = { items: MonitorCamera[]; total: number; limit: number; offset: number; work_areas: string[] };
export type MonitorPlaybackCamera = MonitorCamera & { webrtc: string; hls: string };
export type MonitorPlaybackTicket = { ticket_id: string; expires_in: number; purpose: string; cameras: MonitorPlaybackCamera[] };

export function absoluteApiUrl(path?: string | null) {
  if (!path) return "";
  if (/^https?:\/\//.test(path)) return path;
  return `${API_ORIGIN}${path}`;
}

export { API_BASE, API_ORIGIN };
