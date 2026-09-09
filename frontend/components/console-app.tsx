"use client";

import {
  BadgeCheck,
  Bell,
  Building2,
  Camera,
  ChartNoAxesCombined,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  ClipboardCheck,
  ClipboardList,
  Clock3,
  Cctv,
  Download,
  Eye,
  FileDown,
  FileText,
  ImagePlus,
  LayoutDashboard,
  MapPin,
  Menu,
  MessageSquareText,
  Play,
  Radio,
  RefreshCcw,
  ScanLine,
  Search,
  Send,
  ShieldCheck,
  Smartphone,
  Upload,
  UserRoundPlus,
  Users,
  Video,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { exportHazardLedger, exportRectificationOrder, exportRectificationReply } from "@/lib/exports";
import { absoluteApiUrl, consoleApi } from "@/lib/api";
import type { AlgorithmSettings, AnalysisResult, BackendHazard, BackendHazardGroup, EmailDispatchResponse, Finding, HikvisionChannel, HikvisionProfile, StreamEvent as BackendStreamEvent, StreamSession, SystemUser, VideoSourceView } from "@/lib/api";
import { DEFAULT_PROJECT, getSelectedProjectId, onProjectChange, selectProject } from "@/lib/project-selection";
import type { Project } from "@/lib/project-selection";
import { DEMO_WORK_AREA, initialDevices, initialHazards, initialPeople, PROJECT_NAME } from "@/lib/mock-data";
import type { Hazard, HazardLevel, HazardStatus, PageKey, Person, PersonRole, TimelineEvent } from "@/lib/types";
import LiveMonitorPage from "@/components/live-monitor-page";
import ManagedStreamPlayer from "@/components/managed-stream-player";

const navItems: Array<{ key: PageKey; label: string; hint: string; icon: typeof LayoutDashboard }> = [
  { key: "overview", label: "项目总览", hint: "安全态势与闭环指标", icon: LayoutDashboard },
  { key: "monitor", label: "实时监控", hint: "主画面监看与 AI 控制", icon: Video },
  { key: "live", label: "AI 实时发现", hint: "实时研判结果与测试", icon: ScanLine },
  { key: "dispatch", label: "隐患分发中心", hint: "整改单与消息下发", icon: Send },
  { key: "ledger", label: "隐患台账", hint: "全过程隐患记录", icon: ClipboardList },
  { key: "rectification", label: "整改闭环", hint: "整改、复核与闭合", icon: ClipboardCheck },
  { key: "people", label: "人员管理", hint: "账号、工区与角色", icon: Users },
  { key: "devices", label: "设备监控", hint: "摄像头与巡检终端", icon: Cctv },
  { key: "reports", label: "统计与导出", hint: "台账与闭环材料", icon: ChartNoAxesCombined },
];

const statusOrder: HazardStatus[] = ["待核实", "待整改", "整改中", "待复核", "复核退回", "待重大确认", "已闭合", "误报/已作废"];

function nowText() {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date()).replaceAll("/", "-");
}

function maskPhone(phone: string) {
  return phone.replace(/(\d{3})\d{4}(\d{4})/, "$1****$2");
}

function event(title: string, detail: string, actor: string, tone: TimelineEvent["tone"] = "blue"): TimelineEvent {
  return { id: `T-${Date.now()}-${Math.random()}`, title, detail, actor, tone, time: nowText() };
}

function routePeople(level: HazardLevel, people: Person[]) {
  const roles: PersonRole[] = level === "重大隐患" ? ["项目经理", "安全总监"] : ["安全总监", "安全员", "工区负责人"];
  return people.filter((person) => person.status === "启用" && person.notificationEnabled && roles.includes(person.role));
}

function backendHazardToView(item: BackendHazard, group?: BackendHazardGroup): Hazard {
  const level: HazardLevel = item.severity === "major" ? "重大隐患" : "一般隐患";
  return {
    id: item.id,
    groupId: item.group_id || group?.id || item.id,
    orderId: item.group_order_id || group?.order_id || `ZGD-${item.id.replace(/^HZ-/, "")}`,
    inspectionId: group?.id || item.group_id || item.job_id || item.id,
    title: item.name,
    category: item.label_id,
    level,
    status: item.status as HazardStatus,
    project: PROJECT_NAME,
    workArea: item.work_area,
    point: "未配置点位",
    source: item.job_id ? "本地上传" : "摄像头",
    device: item.job_id ?? "实时视频",
    detectedAt: item.created_at,
    deadline: level === "重大隐患" ? "24小时内" : "3天内",
    evidence: item.evidence,
    originalTitle: item.original_name || item.name,
    originalEvidence: item.original_evidence || item.evidence,
    originalLevel: (item.original_severity || item.severity) === "major" ? "重大隐患" : "一般隐患",
    version: item.version || 1,
    beforeImage: absoluteApiUrl(group?.before_image || item.before_image) || "/hazard-edge.jpg",
    owner: level === "重大隐患" ? "项目经理" : "工区负责人",
    verifier: level === "重大隐患" ? "安全总监" : "安全员",
    repeats: 1,
    isOverdue: false,
    recipients: (group?.notifications ?? []).flatMap((notification) => (notification.delivery_records ?? []).map((record, index) => ({
      personId: `${notification.id}-${record.role}-${index}`, name: record.recipient || record.role,
      role: ({ safety_officer: "安全员", work_area_manager: "工区负责人", project_manager: "项目经理", safety_director: "安全总监" } as Record<string, PersonRole>)[record.role] || "安全员",
      phone: "", status: record.status === "sent" ? "真实已发送" as const : "发送失败" as const, sentAt: notification.created_at || item.created_at,
    }))),
    rectifications: (item.rectifications ?? []).map((round) => ({ id: round.id, description: round.description, submittedBy: round.submitted_by, submittedAt: round.submitted_at, afterImage: absoluteApiUrl(round.after_image) || undefined, reviewResult: round.review_result ?? undefined, reviewComment: round.review_comment ?? undefined })),
    timeline: [
      { id: `T-${item.id}`, title: "后端已生成隐患记录", detail: `${item.name}已进入后端台账。`, time: item.created_at, actor: "闭环业务系统", tone: level === "重大隐患" ? "red" : "orange" },
      ...(item.audit_events ?? []).map((audit) => ({ id: audit.id, title: ({ verify: "人工核实有效", mark_false_positive: "人工核实为误报", submit_rectification: "提交整改", review_passed: "复核通过", review_rejected: "复核退回", major_confirmed: "项目经理确认闭合" } as Record<string, string>)[audit.action] || audit.action, detail: JSON.stringify(audit.after_value), time: audit.created_at, actor: audit.actor_name, tone: audit.action.includes("rejected") || audit.action.includes("false") ? "red" as const : "green" as const })),
    ],
  };
}

type ViewHazardGroup = { id: string; orderId: string; beforeImage: string; workArea: string; detectedAt: string; status: HazardStatus; hazards: Hazard[] };

function groupHazards(hazards: Hazard[]): ViewHazardGroup[] {
  const groups = new Map<string, ViewHazardGroup>();
  for (const hazard of hazards) {
    const group = groups.get(hazard.groupId);
    if (group) { group.hazards.push(hazard); continue; }
    groups.set(hazard.groupId, { id: hazard.groupId, orderId: hazard.orderId, beforeImage: hazard.beforeImage, workArea: hazard.workArea, detectedAt: hazard.detectedAt, status: hazard.status, hazards: [hazard] });
  }
  return [...groups.values()].map((group) => {
    const statuses = new Set(group.hazards.map((item) => item.status));
    const status: HazardStatus = group.hazards.every((item) => ["已闭合", "误报/已作废"].includes(item.status)) ? "已闭合" : statuses.has("待核实") ? "待核实" : statuses.has("待复核") || statuses.has("待重大确认") ? "待复核" : "整改中";
    return { ...group, status };
  });
}

export default function ConsoleApp({ currentUser, onLogout }: { currentUser?: SystemUser; onLogout?: () => void } = {}) {
  const [page, setPage] = useState<PageKey>("overview");
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [hazards, setHazards] = useState<Hazard[]>(initialHazards);
  const [people, setPeople] = useState<Person[]>(initialPeople);
  const [selectedHazardId, setSelectedHazardId] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const [personDialog, setPersonDialog] = useState(false);
  const [unreadNotifications, setUnreadNotifications] = useState(0);
  const [dataSource, setDataSource] = useState<"checking" | "backend" | "demo">("checking");
  const [monitorVisited, setMonitorVisited] = useState(false);
  const [projects, setProjects] = useState<Project[]>([DEFAULT_PROJECT]);
  const [selectedProjectId, setSelectedProjectId] = useState(DEFAULT_PROJECT.id);
  const [projectDraft, setProjectDraft] = useState("");
  const [editingProject, setEditingProject] = useState(false);

  const selectedProject = projects.find((item) => item.id === selectedProjectId) ?? DEFAULT_PROJECT;

  useEffect(() => { if (page === "monitor") setMonitorVisited(true); }, [page]);

  useEffect(() => {
    setSelectedProjectId(getSelectedProjectId());
    return onProjectChange((project) => setSelectedProjectId(project.id));
  }, []);

  useEffect(() => {
    if (typeof consoleApi.listProjects !== "function") return;
    void consoleApi.listProjects().then(({ projects: items }) => {
      const available = items.length ? items : [DEFAULT_PROJECT];
      setProjects(available);
      const linkedProject = new URLSearchParams(window.location.search).get("project_id");
      const saved = getSelectedProjectId();
      const preferred = available.find((item) => item.id === linkedProject) ?? available.find((item) => item.id === saved) ?? available.find((item) => item.name === DEFAULT_PROJECT.name) ?? available[0];
      if (preferred) { setSelectedProjectId(preferred.id); selectProject(preferred.id); }
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    let disposed = false;
    if (typeof consoleApi.getHazards !== "function" || typeof consoleApi.getNotifications !== "function") { setDataSource("demo"); return; }
    void Promise.all([consoleApi.getHazardGroups(), consoleApi.getNotifications()]).then(([groups, notifications]) => {
      if (disposed) return;
      setHazards(groups.flatMap((group) => group.hazards.map((item) => backendHazardToView(item, group))));
      setUnreadNotifications(notifications.unread_count);
      setDataSource("backend");
      const requestedGroup = new URLSearchParams(window.location.search).get("group");
      const linked = requestedGroup ? groups.find((group) => group.id === requestedGroup) : undefined;
      if (linked?.hazards[0]) { setPage("dispatch"); setSelectedHazardId(linked.hazards[0].id); }
    }).catch(() => {
      if (!disposed) { setDataSource("demo"); setUnreadNotifications(0); }
    });
    return () => { disposed = true; };
  }, [selectedProjectId]);

  function changeProject(id: string) {
    if (id === selectedProjectId) return;
    setHazards([]); setPeople([]); setSelectedHazardId(null); setUnreadNotifications(0);
    setSelectedProjectId(id); selectProject(id);
    flash("已切换项目，正在加载该项目的数据；运行中的 AI 不受影响");
  }

  async function createProject() {
    const name = projectDraft.trim();
    if (!name) return;
    try { const project = await consoleApi.createProject(name); setProjects((items) => [...items, project]); setProjectDraft(""); changeProject(project.id); }
    catch (error) { flash(error instanceof Error ? error.message : "项目创建失败"); }
  }

  async function renameProject() {
    const name = projectDraft.trim();
    if (!name) return;
    try { const project = await consoleApi.updateProject(selectedProjectId, name); setProjects((items) => items.map((item) => item.id === project.id ? project : item)); setProjectDraft(""); setEditingProject(false); flash("项目名称已更新"); }
    catch (error) { flash(error instanceof Error ? error.message : "项目名称更新失败"); }
  }
  async function openDispatch() {
    setPage("dispatch"); setMobileNav(false);
    try { await consoleApi.getNotifications(true); setUnreadNotifications(0); } catch { /* offline demo remains usable */ }
  }

  const selectedHazard = hazards.find((hazard) => hazard.id === selectedHazardId) ?? null;
  const activeNav = navItems.find((item) => item.key === page) ?? navItems[0];

  function flash(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(""), 2600);
  }

  function mutateHazard(id: string, update: (hazard: Hazard) => Hazard) {
    setHazards((items) => items.map((hazard) => (hazard.id === id ? update(hazard) : hazard)));
  }

  function replaceGroup(group: BackendHazardGroup) {
    setHazards((items) => {
      const remaining = items.filter((item) => item.groupId !== group.id);
      return [...group.hazards.map((item) => backendHazardToView(item, group)), ...remaining];
    });
  }

  async function verifyHazard(id: string, values: { exists: boolean; descriptionCorrect: boolean; name: string; evidence: string; level: HazardLevel; reason: string }) {
    const current = hazards.find((item) => item.id === id);
    try {
      if (current?.groupId && typeof consoleApi.verifyGroupHazard === "function") {
        const response = await consoleApi.verifyGroupHazard(current.groupId, id, {
          passed: values.exists, version: current.version, reason: values.reason,
          description_correct: values.descriptionCorrect,
          corrected_name: values.exists && !values.descriptionCorrect ? values.name : undefined,
          corrected_evidence: values.exists && !values.descriptionCorrect ? values.evidence : undefined,
          corrected_severity: values.exists && !values.descriptionCorrect ? (values.level === "重大隐患" ? "major" : "general") : undefined,
        });
        replaceGroup(response.group); setSelectedHazardId(response.hazard.id);
        flash(values.exists ? "核实完成，整改单已进入正式整改" : "已标记误报，原始记录仍保留"); return;
      }
      const saved = values.exists ? await consoleApi.verifyHazard(id, { verified: true, verifier: currentUser?.name || "安全员" }) : await consoleApi.markFalsePositive(id, { reason: values.reason || "人工核实为误报" });
      setHazards((items) => items.map((item) => item.id === id ? backendHazardToView(saved as BackendHazard) : item)); return;
    } catch (reason) { if (dataSource === "backend") { flash(reason instanceof Error ? reason.message : "核实提交失败"); return; } }
    mutateHazard(id, (hazard) => ({
      ...hazard,
      status: values.exists ? "待整改" : "误报/已作废",
      title: values.descriptionCorrect ? hazard.title : values.name,
      evidence: values.descriptionCorrect ? hazard.evidence : values.evidence,
      level: values.descriptionCorrect ? hazard.level : values.level,
      version: hazard.version + 1,
      timeline: [...hazard.timeline, event(values.exists ? "人工核实有效" : "标记为误报", values.exists ? "隐患描述已经核实，正式进入整改环节。" : values.reason, currentUser?.name || "安全员", values.exists ? "green" : "red")],
    }));
    flash(values.exists ? "核实完成，整改单已进入正式整改" : "已标记误报");
  }

  async function markFalsePositive(id: string) {
    try { const saved = await consoleApi.markFalsePositive(id, { reason: "人工核实为误报" }); setHazards((items) => items.map((item) => item.id === id ? backendHazardToView(saved as BackendHazard) : item)); flash("已标记误报，原始记录仍保留"); return; } catch { if (dataSource === "backend") { flash("后端暂未提供误报接口，未修改真实台账"); return; } }
    mutateHazard(id, (hazard) => ({
      ...hazard,
      status: "误报/已作废",
      timeline: [...hazard.timeline, event("标记为误报", "人工核实后判定隐患不成立，整改单已作废并保留记录。", "张安全", "red")],
    }));
    flash("已标记误报，原始识别和下发记录仍保留");
  }

  async function submitRectification(id: string, description: string, afterImage?: string) {
    const current = hazards.find((item) => item.id === id);
    try {
      let evidenceUrl = afterImage ?? null;
      if (afterImage?.startsWith("data:")) {
        const blob = await (await fetch(afterImage)).blob();
        const evidence = new FormData();
        evidence.append("file", blob, "rectification.jpg");
        evidenceUrl = (await consoleApi.uploadRectificationEvidence(id, evidence)).url;
      }
      if (current?.groupId) {
        const response = await consoleApi.submitGroupRectification(current.groupId, id, { description, after_image: evidenceUrl });
        replaceGroup(response.group); flash("整改结果已提交，等待复核"); return;
      }
      const saved = await consoleApi.submitRectification(id, { description, submitted_by: currentUser?.name || "整改责任人", after_image: evidenceUrl });
      setHazards((items) => items.map((item) => item.id === id ? backendHazardToView(saved) : item)); flash("整改结果已提交，等待复核"); return;
    } catch { if (dataSource === "backend") { flash("整改提交失败，未修改真实台账"); return; } }
    mutateHazard(id, (hazard) => ({
      ...hazard,
      status: "待复核",
      rectifications: [
        ...hazard.rectifications,
        {
          id: `R-${Date.now()}`,
          description,
          submittedBy: "李工区",
          submittedAt: nowText(),
          afterImage,
          reviewResult: "待复核",
        },
      ],
      timeline: [...hazard.timeline, event("提交整改结果", "整改人员已上传整改后照片和文字说明。", "李工区", "blue")],
    }));
    flash("整改结果已提交，等待复核");
  }

  async function reviewHazard(id: string, passed: boolean) {
    const current = hazards.find((item) => item.id === id);
    const reviewer = currentUser?.name || (current?.level === "重大隐患" ? "安全总监" : "安全员");
    try {
      if (current?.groupId) { const response = await consoleApi.reviewGroupHazard(current.groupId, id, { passed, reviewer, comment: passed ? "现场整改符合要求。" : "请补充整改证据。" }); replaceGroup(response.group); flash(passed ? "复核完成" : "已退回整改"); return; }
      const saved = await consoleApi.reviewHazard(id, { passed, reviewer, comment: passed ? "现场整改符合要求。" : "请补充整改证据。" }); setHazards((items) => items.map((item) => item.id === id ? backendHazardToView(saved) : item)); flash(passed ? "复核完成" : "已退回整改"); return;
    } catch (error) { if (dataSource === "backend") { flash(error instanceof Error ? error.message : "后端复核失败，未修改真实台账"); return; } }
    mutateHazard(id, (hazard) => {
      const rounds = [...hazard.rectifications];
      const latest = rounds.at(-1);
      if (latest) {
        rounds[rounds.length - 1] = {
          ...latest,
          reviewResult: passed ? "通过" : "退回",
          reviewComment: passed ? "现场整改符合要求。" : "整改照片证据不足，请补充完整防护和整体环境照片。",
        };
      }
      const nextStatus: HazardStatus = passed ? (hazard.level === "重大隐患" ? "待重大确认" : "已闭合") : "复核退回";
      return {
        ...hazard,
        status: nextStatus,
        rectifications: rounds,
        timeline: [
          ...hazard.timeline,
          event(
            passed ? "复核通过" : "复核退回",
            passed
              ? hazard.level === "重大隐患"
                ? "安全总监复核通过，等待项目经理最终确认。"
                : "安全员复核通过，隐患已闭合。"
              : "整改结果未达到要求，已退回下一轮整改。",
            hazard.level === "重大隐患" ? "赵总监" : "张安全",
            passed ? "green" : "red",
          ),
        ],
      };
    });
    flash(passed ? "复核完成" : "已退回整改并保留本轮记录");
  }

  async function confirmMajor(id: string) {
    const current = hazards.find((item) => item.id === id);
    try {
      if (current?.groupId) { const response = await consoleApi.confirmGroupMajor(current.groupId, id, { passed: true, reviewer: currentUser?.name || "项目经理" }); replaceGroup(response.group); flash("重大隐患已完成最终确认并闭合"); return; }
      const saved = await consoleApi.confirmMajorHazard(id, { passed: true, reviewer: currentUser?.name || "项目经理" }); setHazards((items) => items.map((item) => item.id === id ? backendHazardToView(saved) : item)); flash("重大隐患已完成最终确认并闭合"); return;
    } catch (error) { if (dataSource === "backend") { flash(error instanceof Error ? error.message : "重大隐患确认失败，未修改真实台账"); return; } }
    mutateHazard(id, (hazard) => ({
      ...hazard,
      status: "已闭合",
      timeline: [...hazard.timeline, event("重大隐患确认闭合", "项目经理已完成最终确认，台账状态更新为已闭合。", "王经理", "green")],
    }));
    flash("重大隐患已完成最终确认并闭合");
  }

  function addPerson(person: Omit<Person, "id" | "idCard" | "gender" | "company" | "duty" | "status" | "notificationEnabled">) {
    setPeople((items) => [
      ...items,
      {
        ...person,
        id: `P-${String(items.length + 1).padStart(3, "0")}`,
        idCard: "500***********999",
        gender: "男",
        company: person.workArea,
        duty: "演示新增人员",
        status: "启用",
        notificationEnabled: true,
      },
    ]);
    setPersonDialog(false);
    flash("人员已加入当前工区的演示通讯录");
  }

  return (
    <main className="console-shell">
      <aside className={`side-nav ${collapsed ? "is-collapsed" : ""} ${mobileNav ? "mobile-open" : ""}`}>
        <div className="brand-lockup">
          <img src="/logo.png" alt="智筑云Logo" />
          {!collapsed && <div><strong>智筑云 AI</strong><span>事故隐患智能识别系统</span></div>}
          <button className="mobile-close" onClick={() => setMobileNav(false)} aria-label="关闭导航"><X size={18} /></button>
        </div>

        <div className="project-chip project-selector">
          <Building2 size={17} />
          {!collapsed && <span><small>当前项目</small><select aria-label="切换项目" value={selectedProjectId} onChange={(event) => changeProject(event.target.value)}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select><button onClick={() => { setProjectDraft(selectedProject.name); setEditingProject(true); }}>编辑</button></span>}
        </div>

        <nav aria-label="主导航">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.key} className={page === item.key ? "active" : ""} onClick={() => { if (item.key === "dispatch") void openDispatch(); else { setPage(item.key); setMobileNav(false); } }}>
                <span className="nav-icon"><Icon size={19} /></span>
                {!collapsed && <span className="nav-copy"><b>{item.label}</b><small>{item.hint}</small></span>}
                {!collapsed && item.key === "dispatch" && unreadNotifications > 0 && <em>{unreadNotifications}</em>}
              </button>
            );
          })}
        </nav>

        <div className="nav-footer">
          {!collapsed && <div className="system-health"><span><i />业务控制台已启动</span><small>真实识别已联调 · 内置演示台账</small></div>}
          <button onClick={() => setCollapsed((value) => !value)}><ChevronLeft size={17} />{!collapsed && "收起导航"}</button>
        </div>
      </aside>

      <section className={`main-workspace ${collapsed ? "nav-collapsed" : ""}`}>
        <header className="top-header">
          <div className="page-identity">
            <button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="打开导航"><Menu size={21} /></button>
            <div><span>智筑云AI · 施工安全数字工作台</span><h1>{activeNav.label}</h1><p>{activeNav.hint}</p></div>
          </div>
          <div className="header-actions">
            <div className="mode-pill"><i />前后端联调运行中</div>
            <button className="notification-button" aria-label="通知" onClick={openDispatch}><Bell size={19} />{unreadNotifications > 0 && <em>{unreadNotifications}</em>}</button>
            <button className="account-card" onClick={onLogout} title={onLogout ? "点击退出登录" : undefined}><span>{(currentUser?.name || "安").slice(0, 1)}</span><div><b>{currentUser?.name || "安全管理员"}</b><small>{currentUser ? `${currentUser.role_name} · ${currentUser.work_area}` : "示范工区"}</small></div></button>
          </div>
        </header>

        <div className="workspace-content">
          {page === "overview" && <OverviewPage hazards={hazards} openHazard={setSelectedHazardId} navigate={setPage} />}
          {page === "live" && <LivePage projectId={selectedProjectId} hazards={hazards} people={people} setHazards={setHazards} openHazard={setSelectedHazardId} flash={flash} navigate={setPage} />}
          {monitorVisited && <div className="persistent-monitor-page" hidden={page !== "monitor"}><LiveMonitorPage key={selectedProjectId} devices={initialDevices} /></div>}
          {page === "dispatch" && <DispatchPage hazards={hazards} openHazard={setSelectedHazardId} flash={flash} />}
          {page === "ledger" && <LedgerPage hazards={hazards} openHazard={setSelectedHazardId} flash={flash} />}
          {page === "rectification" && <RectificationPage hazards={hazards} currentUser={currentUser} openHazard={setSelectedHazardId} submitRectification={submitRectification} reviewHazard={reviewHazard} confirmMajor={confirmMajor} />}
          {page === "people" && (currentUser ? <AccountManagementPage currentUser={currentUser} flash={flash}/> : <PeoplePage people={people} setPeople={setPeople} openAdd={() => setPersonDialog(true)} flash={flash} />)}
          {page === "devices" && <DevicesPage flash={flash} navigate={setPage} />}
          {page === "reports" && <ReportsPage hazards={hazards} openHazard={setSelectedHazardId} flash={flash} />}
        </div>
      </section>

      {selectedHazard && (
        <HazardDrawer
          hazard={selectedHazard}
          close={() => setSelectedHazardId(null)}
          verify={(values) => verifyHazard(selectedHazard.id, values)}
          markFalse={() => markFalsePositive(selectedHazard.id)}
          review={(passed) => reviewHazard(selectedHazard.id, passed)}
          confirmMajor={() => confirmMajor(selectedHazard.id)}
          flash={flash}
        />
      )}
      {personDialog && <PersonDialog close={() => setPersonDialog(false)} submit={addPerson} />}
      {editingProject && <div className="modal-backdrop"><div className="person-dialog project-dialog"><header><div><b>编辑当前项目</b><p>项目切换后，页面数据会按项目重新加载。</p></div><button onClick={() => setEditingProject(false)}><X size={19}/></button></header><div className="dialog-form"><label><span>项目名称</span><input autoFocus value={projectDraft} onChange={(event) => setProjectDraft(event.target.value)} /></label><label><span>新增项目</span><input placeholder="输入名称后点击新增" value={projectDraft} onChange={(event) => setProjectDraft(event.target.value)} /></label></div><footer><button className="secondary-action" onClick={createProject}>新增项目</button><button className="primary-action" onClick={renameProject}>保存名称</button></footer></div></div>}
      {toast && <div className="toast-message"><CheckCircle2 size={19} />{toast}</div>}
    </main>
  );
}

function OverviewPage({ hazards, openHazard, navigate }: { hazards: Hazard[]; openHazard: (id: string) => void; navigate: (page: PageKey) => void }) {
  const groups = groupHazards(hazards);
  const active = hazards.filter((hazard) => !["已闭合", "误报/已作废"].includes(hazard.status));
  const closed = hazards.filter((hazard) => hazard.status === "已闭合").length;
  const metrics: Array<{ label: string; value: string | number; hint: string; icon: LucideIcon; tone: string }> = [
    { label: "今日AI发现", value: hazards.length, hint: "已自动生成整改单", icon: ScanLine, tone: "blue" },
    { label: "待核实", value: hazards.filter((item) => item.status === "待核实").length, hint: "等待安全管理人员确认", icon: CircleAlert, tone: "orange" },
    { label: "整改处理中", value: hazards.filter((item) => ["待整改", "整改中", "复核退回"].includes(item.status)).length, hint: "含退回整改任务", icon: RefreshCcw, tone: "violet" },
    { label: "待复核", value: hazards.filter((item) => ["待复核", "待重大确认"].includes(item.status)).length, hint: "请及时完成线上复核", icon: ClipboardCheck, tone: "cyan" },
    { label: "重大未闭合", value: active.filter((item) => item.level === "重大隐患").length, hint: "项目经理重点关注", icon: ShieldCheck, tone: "red" },
    { label: "闭合率", value: `${Math.round((closed / Math.max(hazards.length, 1)) * 100)}%`, hint: `已闭合 ${closed} 项`, icon: BadgeCheck, tone: "green" },
  ];
  const workflowSteps: Array<[string, number, LucideIcon, string]> = [
    ["AI发现", hazards.length, ScanLine, "blue"],
    ["待核实", hazards.filter((h) => h.status === "待核实").length, Eye, "orange"],
    ["整改中", hazards.filter((h) => ["待整改", "整改中", "复核退回"].includes(h.status)).length, RefreshCcw, "violet"],
    ["待复核", hazards.filter((h) => ["待复核", "待重大确认"].includes(h.status)).length, ClipboardCheck, "cyan"],
    ["已闭合", closed, CircleCheck, "green"],
  ];

  return <div className="page-stack">
    <section className="hero-banner">
      <div className="hero-copy"><span className="section-kicker">施工安全演示环境</span><h2>今天最重要的，是让每一条隐患真正闭合</h2><p>AI识别结果会进入建单、分发、整改和复核流程。请在部署后按实际工区配置人员与权限。</p><div className="hero-actions"><button className="primary-action" onClick={() => navigate("live")}><Play size={17} />进入AI识别</button><button className="secondary-action" onClick={() => navigate("dispatch")}><Send size={17} />查看分发任务</button></div></div>
      <div className="closure-orbit"><div><strong>{active.length}</strong><span>未闭合隐患</span></div><i className="orbit-one"/><i className="orbit-two"/><i className="orbit-three"/></div>
    </section>

    <section className="metric-grid">
      {metrics.map((metric) => { const Icon = metric.icon; return <article key={metric.label} className="metric-card"><span className={`metric-symbol ${metric.tone}`}><Icon size={20}/></span><div><p>{metric.label}</p><strong>{metric.value}</strong><small>{metric.hint}</small></div></article>; })}
    </section>

    <section className="overview-layout">
      <article className="surface workflow-board">
        <SectionTitle kicker="闭环进度" title="从发现到闭合" action={<button onClick={() => navigate("rectification")}>进入整改中心 <ChevronRight size={15}/></button>} />
        <div className="workflow-steps">
          {workflowSteps.map(([label, value, Icon, tone], index) => <div className="workflow-step" key={label}><span className={`step-icon ${tone}`}><Icon size={19}/></span><p><b>{value}</b><small>{label}</small></p>{index < 4 && <i className="step-line"/>}</div>)}
        </div>
        <div className="routing-note"><MessageSquareText size={18}/><div><b>当前自动分发规则</b><p>一般隐患 → 安全总监、安全员、所属工区负责人；重大隐患 → 项目经理、安全总监。</p></div><span>规则已启用</span></div>
      </article>

      <article className="surface latest-panel">
        <SectionTitle kicker="实时更新" title="最新隐患" action={<button onClick={() => navigate("ledger")}>查看全部</button>} />
        <div className="latest-list">
          {groups.slice(0, 4).map((group) => <button key={group.id} onClick={() => openHazard(group.hazards[0].id)}><img src={group.beforeImage} alt="隐患证据"/><span><strong>{group.hazards.length}项同图隐患</strong><small>{group.hazards.map((item) => item.title).join("、")}</small><small><MapPin size={12}/>{group.workArea} · <Clock3 size={12}/>{group.detectedAt}</small></span><div><LevelBadge level={group.hazards.some((item) => item.level === "重大隐患") ? "重大隐患" : "一般隐患"}/><StatusBadge status={group.status}/></div></button>)}
        </div>
      </article>
    </section>
  </div>;
}

function LivePage({ projectId, hazards, people, setHazards, openHazard, flash, navigate }: { projectId: string; hazards: Hazard[]; people: Person[]; setHazards: React.Dispatch<React.SetStateAction<Hazard[]>>; openHazard: (id: string) => void; flash: (message: string) => void; navigate: (page: PageKey) => void }) {
  const liveGroups = groupHazards(hazards);
  const [preview, setPreview] = useState<string>("/hazard-hoist.jpg");
  const [previewType, setPreviewType] = useState<"image" | "video">("image");
  const [filename, setFilename] = useState("尚未选择测试素材");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [analyzing, setAnalyzing] = useState(false);
  const [progressText, setProgressText] = useState("等待上传");
  const [backendState, setBackendState] = useState<"checking" | "ready" | "offline">("checking");
  const [algorithmSettings, setAlgorithmSettings] = useState<AlgorithmSettings>({ realtime_fps: 2, inspection_interval_sec: 300, test_stream_interval_sec: 3 });
  const [algorithmCapabilities, setAlgorithmCapabilities] = useState<Record<string, unknown> | null>(null);
  const [autoEmail, setAutoEmail] = useState(true);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [cameraError, setCameraError] = useState("");
  const [testOpen, setTestOpen] = useState(true);
  const [aiSessions, setAiSessions] = useState<StreamSession[]>([]);
  const [selectedAiCamera, setSelectedAiCamera] = useState("");
  const [aiTicket, setAiTicket] = useState<import("@/lib/api").MonitorPlaybackTicket | null>(null);
  const [aiEvents, setAiEvents] = useState<BackendStreamEvent[]>([]);
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const projectIdRef = useRef(projectId);

  useEffect(() => { projectIdRef.current = projectId; }, [projectId]);

  useEffect(() => {
    const defaults: AlgorithmSettings = { realtime_fps: 2, inspection_interval_sec: 300, test_stream_interval_sec: 3 };
    const settings = typeof consoleApi.getAlgorithmSettings === "function" ? consoleApi.getAlgorithmSettings() : Promise.resolve(defaults);
    const capabilities = typeof consoleApi.getAlgorithmCapabilities === "function" ? consoleApi.getAlgorithmCapabilities() : Promise.resolve(null);
    void Promise.all([consoleApi.health(), settings, capabilities]).then(([health, loadedSettings, loadedCapabilities]) => { setBackendState(health.ok && health.api_key_configured ? "ready" : "offline"); setAlgorithmSettings(loadedSettings); setAlgorithmCapabilities(loadedCapabilities); }).catch(() => setBackendState("offline"));
    return () => {
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  const selectedAiStreamId = aiSessions.find((item) => item.source_id === selectedAiCamera)?.inference_stream_id || "";

  useEffect(() => {
    let disposed = false;
    const refresh = async () => {
      try {
        const active = (await consoleApi.listStreamSessions()).filter((item) => item.status === "running");
        if (disposed) return;
        setAiSessions(active);
        setSelectedAiCamera((current) => active.some((item) => item.source_id === current) ? current : active[0]?.source_id || "");
      } catch { if (!disposed) setAiSessions([]); }
    };
    void refresh(); const timer = window.setInterval(() => void refresh(), 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    if (!selectedAiCamera) { setAiTicket(null); setAiEvents([]); return; }
    let disposed = false; let ticketId = "";
    void consoleApi.createMonitorPlaybackTicket([selectedAiCamera], "ai-discovery").then(async (ticket) => {
      if (disposed) { await consoleApi.releaseMonitorPlaybackTicket(ticket.ticket_id); return; }
      ticketId = ticket.ticket_id; setAiTicket(ticket);
    }).catch(() => { if (!disposed) setAiTicket(null); });
    return () => { disposed = true; if (ticketId) void consoleApi.releaseMonitorPlaybackTicket(ticketId); };
  }, [selectedAiCamera]);

  useEffect(() => {
    if (!selectedAiStreamId) { setAiEvents([]); return; }
    let disposed = false;
    const refresh = () => void consoleApi.getStreamEvents(selectedAiStreamId).then((items) => { if (!disposed) setAiEvents(items); }).catch(() => undefined);
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [selectedAiStreamId]);


  function chooseFiles(list: FileList | null) {
    if (!list || !list.length) return;
    const incoming: File[] = [];
    for (const file of Array.from(list)) {
      if (!file.type.startsWith("image/") && !file.type.startsWith("video/")) { flash(`跳过不支持的文件：${file.name}`); continue; }
      if (selectedFiles.some((item) => item.name === file.name && item.size === file.size)) continue;
      incoming.push(file);
    }
    if (!incoming.length) return;
    const next = [...selectedFiles, ...incoming].slice(0, 20);
    setSelectedFiles(next);
    const first = incoming[0];
    setPreview(URL.createObjectURL(first));
    setPreviewType(first.type.startsWith("video/") ? "video" : "image");
    setFilename(next.length > 1 ? `${next.length} 个素材，最新：${first.name}` : first.name);
    setProgressText(`已选择 ${next.length} 个素材，可继续追加或直接开始识别`);
  }

  function removeFile(index: number) {
    setSelectedFiles((items) => items.filter((_, position) => position !== index));
  }

  async function openCamera() {
    try {
      setCameraError("");
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      streamRef.current = stream;
      setCameraOpen(true);
      window.setTimeout(() => { if (videoRef.current) videoRef.current.srcObject = stream; }, 0);
    } catch {
      setCameraError("无法调用摄像头。请允许浏览器摄像头权限，并使用localhost或HTTPS访问页面。");
      setCameraOpen(true);
    }
  }

  function closeCamera() {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setCameraOpen(false);
  }

  function recipientsFromDispatch(severity: "general" | "major", dispatch?: EmailDispatchResponse) {
    const level: HazardLevel = severity === "major" ? "重大隐患" : "一般隐患";
    const group = dispatch?.dispatches.find((item) => item.severity === severity);
    if (!group) return routePeople(level, people).map((person) => ({ personId: person.id, name: person.name, role: person.role, phone: person.phone, status: "待发送" as const, sentAt: nowText() }));
    return group.records.map((record) => {
      const person = people.find((item) => item.name === record.recipient || item.role === record.recipient);
      return {
        personId: person?.id ?? record.role,
        name: person?.name ?? record.recipient,
        role: person?.role ?? (record.recipient as PersonRole),
        phone: person?.phone ?? "",
        status: record.status === "sent" ? "真实已发送" as const : "发送失败" as const,
        sentAt: nowText(),
      };
    });
  }

  function findingToHazard(finding: Finding, result: AnalysisResult, index: number, dispatch?: EmailDispatchResponse): Hazard {
    const sourceFrame = result.frames.find((frame) => finding.source_frame_ids.includes(frame.frame_id)) ?? result.frames[0];
    const id = `YH-${result.job_id.slice(0, 8).toUpperCase()}-${String(index + 1).padStart(2, "0")}`;
    const orderId = `ZGD-${result.job_id.slice(0, 8).toUpperCase()}-${String(index + 1).padStart(2, "0")}`;
    const level: HazardLevel = finding.severity === "major" ? "重大隐患" : "一般隐患";
    const findingDispatch = finding.final_status === "confirmed_hazard" ? dispatch : undefined;
    const recipients = recipientsFromDispatch(finding.severity, findingDispatch);
    const emailSent = recipients.filter((item) => item.status === "真实已发送").length;
    const deadline = dispatch?.dispatches.find((item) => item.severity === finding.severity)?.deadline ?? (level === "重大隐患" ? "24小时内" : "3天内");
    return {
      id,
      groupId: result.job_id,
      version: 1,
      orderId,
      inspectionId: result.job_id,
      title: finding.name,
      category: finding.category,
      level,
      status: "待核实",
      project: PROJECT_NAME,
      workArea: DEMO_WORK_AREA,
      point: "本地素材测试点",
      source: "本地上传",
      device: filename,
      detectedAt: nowText(),
      deadline,
      evidence: `${finding.evidence.join("；") || "模型未返回可见证据说明"}；等级依据：${finding.severity_reason}`,
      beforeImage: absoluteApiUrl(sourceFrame?.artifact_url) || preview,
      annotatedImage: absoluteApiUrl(sourceFrame?.annotated_url) || undefined,
      owner: level === "重大隐患" ? "王经理" : "李工区",
      verifier: level === "重大隐患" ? "赵总监" : "张安全",
      repeats: finding.occurrence_count || 1,
      isOverdue: false,
      recipients,
      rectifications: [],
      timeline: [
        event("AI真实识别到隐患", `${finding.label_id} · ${finding.final_status === "confirmed_hazard" ? "已确认" : "建议人工复核"}`, "算法识别服务", level === "重大隐患" ? "red" : "orange"),
        event("待核实整改单已生成", `${orderId} 已建立并保存原始证据。`, "闭环业务系统", "blue"),
        event(emailSent ? "邮件分发已执行" : "等待人工分发", emailSent ? `按模型自动判定的${finding.severity_name}发送成功 ${emailSent} 人。` : "当前没有已发送邮件，可在核实后继续处理。", "邮件通知服务", emailSent ? "green" : "orange"),
      ],
    };
  }

  function streamEventToHazard(item: BackendStreamEvent, index: number): Hazard {
    const streamLevel: HazardLevel = item.severity === "major" ? "重大隐患" : "一般隐患";
    const id = `YH-STREAM-${String(Math.floor(item.timestamp))}-${index}`;
    const recipients = (item.notification_records ?? []).map((record) => {
      const person = people.find((candidate) => candidate.name === record.recipient || candidate.role === record.recipient);
      return { personId: person?.id ?? record.role, name: person?.name ?? record.recipient, role: person?.role ?? (record.recipient as PersonRole), phone: person?.phone ?? "", status: record.status === "sent" ? "真实已发送" as const : "发送失败" as const, sentAt: nowText() };
    });
    return { id, groupId: `${item.stream_id}:${item.timestamp}`, version: 1, orderId: `ZGD-STREAM-${String(Math.floor(item.timestamp))}`, inspectionId: item.stream_id, title: item.name, category: item.category, level: streamLevel, status: "待核实", project: PROJECT_NAME, workArea: item.work_area, point: item.camera_id, source: "摄像头", device: item.camera_id, detectedAt: nowText(), deadline: streamLevel === "重大隐患" ? "24小时内" : "3天内", evidence: item.evidence, beforeImage: absoluteApiUrl(item.snapshot_url), annotatedImage: absoluteApiUrl(item.annotated_url) || undefined, owner: streamLevel === "重大隐患" ? "王经理" : "李工区", verifier: streamLevel === "重大隐患" ? "赵总监" : "张安全", repeats: 1, isOverdue: false, recipients, rectifications: [], timeline: [event("实时视频发现隐患", `${item.camera_id} 已生成事件。`, "视频流识别服务", streamLevel === "重大隐患" ? "red" : "orange"), event("邮件自动分发", `发送成功 ${recipients.filter((recipient) => recipient.status === "真实已发送").length} 人。`, "邮件通知服务", "green")] };
  }

  function formatJobProgress(state: import("@/lib/api").JobState) {
    const progress = state.progress;
    if (!progress) return state.status === "queued" ? "任务已排队，等待算法资源…" : "正在执行检测与核验…";
    const stageNames: Record<string, string> = { queued: "排队", extracting: "提取画面", detecting: "目标检测", verifying: "视觉核验", persisting: "建立台账" };
    const stage = stageNames[progress.stage] || progress.stage || "处理中";
    const total = Math.max(progress.total || 0, 0);
    const count = total ? `${Math.min(progress.completed, total)}/${total}` : `${progress.completed} 项`;
    const eta = progress.estimated_remaining_sec && progress.estimated_remaining_sec > 0 ? `，预计剩余约 ${Math.ceil(progress.estimated_remaining_sec)} 秒` : "";
    return `${stage}：${count}${eta}`;
  }

  async function waitForJob(jobId: string, taskProjectId: string) {
    for (let attempt = 0; attempt < 360; attempt += 1) {
      const state = await consoleApi.getJob(jobId, taskProjectId);
      if (state.status === "completed") return;
      if (state.status === "failed") throw new Error(state.error || "识别任务失败");
      setProgressText(formatJobProgress(state));
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
    throw new Error("识别任务等待超时，请稍后在任务记录中查看。 ");
  }

  async function runRecognition() {
    if (!selectedFiles.length) { flash("请先选择图片或视频"); return; }
    setAnalyzing(true);
    const taskProjectId = projectIdRef.current;
    const allCreated: Hazard[] = [];
    let failedFiles = 0;
    try {
      const form = new FormData();
      for (const file of selectedFiles) form.append("files", file);
      form.append("mode", "test");
      setProgressText(selectedFiles.length > 1 ? `正在上传 ${selectedFiles.length} 个素材…` : "正在上传素材…");
      const submitted = await consoleApi.uploadJobs(form, taskProjectId);
      const jobs = submitted.jobs;
      if (!jobs.length) throw new Error("后端未返回识别任务");
      for (let index = 0; index < jobs.length; index += 1) {
        const job = jobs[index];
        setProgressText(jobs.length > 1 ? `正在识别（${index + 1}/${jobs.length}）：${job.filename}` : "任务已提交，等待算法开始…");
        try {
          await waitForJob(job.job_id, taskProjectId);
          const result = await consoleApi.getJobResult(job.job_id, taskProjectId);
          if (result.warnings?.some((warning) => warning.includes("失败"))) {
            failedFiles += 1;
            flash(`${job.filename}：部分模型请求失败，候选已保留在任务记录中，本次不自动发送邮件`);
            continue;
          }
          if (!result.findings.length) continue;
          let dispatch: EmailDispatchResponse | undefined;
          if (autoEmail && result.findings.some((item) => item.final_status === "confirmed_hazard")) {
            setProgressText(`识别完成，正在为 ${job.filename} 发送邮件…`);
            dispatch = await consoleApi.dispatchJobEmail(job.job_id, { work_area: DEMO_WORK_AREA }, taskProjectId);
          }
          allCreated.push(...result.findings.map((finding, findingIndex) => findingToHazard(finding, result, findingIndex, dispatch)));
        } catch (jobError) {
          failedFiles += 1;
          flash(`${job.filename} 识别失败：${jobError instanceof Error ? jobError.message : "未知错误"}`);
        }
      }
      if (projectIdRef.current !== taskProjectId) { setProgressText("分析完成：结果保留在原项目中"); flash("项目已切换，识别结果未写入当前项目页面"); return; }
      if (allCreated.length) {
        setHazards((items) => [...allCreated, ...items]);
        setProgressText(`完成：${jobs.length} 个素材共生成 ${allCreated.length} 条隐患记录`);
        openHazard(allCreated[0].id);
        flash(`识别完成：${jobs.length} 个素材，生成 ${allCreated.length} 条隐患${autoEmail ? "，确认隐患已邮件分发" : ""}${failedFiles ? `（${failedFiles} 个素材部分失败）` : ""}`);
      } else {
        setProgressText(failedFiles ? `完成：${failedFiles}/${jobs.length} 个素材处理失败，其余未发现明确隐患` : "分析完成：所选素材均未发现明确隐患");
        flash(failedFiles ? "部分素材识别失败，详见提示" : "所选素材均未发现明确隐患");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "识别失败";
      setProgressText(`失败：${message}`);
      flash(message);
    } finally {
      setAnalyzing(false);
    }
  }

  const selectedAiSession = aiSessions.find((item) => item.source_id === selectedAiCamera);
  const selectedAiPlayback = aiTicket?.cameras.find((item) => item.id === selectedAiCamera);

  return <div className="live-page-grid">
    <section className="surface ai-live-console">
      <div className="section-title"><div><span>AI 运行研判台</span><h3>实时 AI 发现</h3></div><button className="secondary-action" onClick={() => navigate("monitor")}><Video size={16}/>前往实时监控</button></div>
      {selectedAiPlayback && selectedAiSession ? <div className="ai-live-content"><ManagedStreamPlayer camera={{ id: selectedAiPlayback.id, name: selectedAiPlayback.name, webrtcUrl: selectedAiPlayback.webrtc, hlsUrl: selectedAiPlayback.hls }} /><aside><label>正在研判的摄像头<select value={selectedAiCamera} onChange={(event) => setSelectedAiCamera(event.target.value)}>{aiSessions.map((item) => <option key={item.source_id} value={item.source_id}>{item.camera_id}</option>)}</select></label><p>阶段：<b>{selectedAiSession.phase || "运行中"}</b></p><p>已处理 {selectedAiSession.processed_frames || 0} 帧 · {selectedAiSession.actual_fps?.toFixed(1) || "0.0"} FPS</p><p>事件 {selectedAiSession.emitted_events || 0} · 已建单 {selectedAiSession.orders_created || 0}</p><p>{selectedAiSession.last_error || "取流、推理和闭环任务均正常时将在这里持续更新。"}</p></aside></div> : <div className="ai-live-empty"><ScanLine size={28}/><b>当前没有正在运行的摄像头 AI</b><span>请到“实时监控”选择摄像头后点击“启动 AI”。观看画面本身不会自动消耗算法资源。</span><button className="primary-action" onClick={() => navigate("monitor")}>进入实时监控</button></div>}
      {aiEvents.length > 0 && <div className="ai-event-strip">{aiEvents.slice(0, 3).map((item) => <button key={`${item.stream_id}-${item.label_id}-${item.timestamp}`} onClick={() => navigate("ledger")}><b>{item.name}</b><span>{item.severity === "major" ? "重大隐患" : "一般隐患"} · {new Date(item.timestamp * 1000).toLocaleTimeString("zh-CN")}</span></button>)}</div>}
    </section>
    <details className="surface media-lab test-lab" open={testOpen} onToggle={(event) => setTestOpen((event.target as HTMLDetailsElement).open)}>
      <summary><span><Radio size={15}/>测试识别</span><small>上传图片、视频或调用电脑摄像头进行一次性全链路测试</small></summary>
    <section>
      <div className="media-toolbar"><div><span className="live-signal"><Radio size={14}/>真实算法测试入口</span><p>上传现场图片或视频（支持一次多选，最多20个），调用 8010 后端完成真实识别与邮件分发。</p></div><div className="media-actions"><span className={`backend-pill ${backendState}`}>{backendState === "ready" ? "后端已连接" : backendState === "checking" ? "正在检查后端" : "后端未连接"}</span><label className="secondary-action file-button"><Upload size={16}/>上传图片/视频（可多选）<input type="file" accept="image/*,video/*" multiple onChange={(e) => { chooseFiles(e.target.files); e.target.value = ""; }}/></label><button className="secondary-action" onClick={openCamera}><Camera size={16}/>电脑摄像头</button></div></div>
      {selectedFiles.length > 0 && <div className="upload-file-list" aria-label="已选素材清单">{selectedFiles.map((file, index) => <span key={`${file.name}-${file.size}-${index}`} className="upload-file-chip"><b>{file.name}</b><small>{(file.size / 1024 / 1024).toFixed(1)} MB</small>{!analyzing && <button onClick={() => removeFile(index)} aria-label={`移除 ${file.name}`}>×</button>}</span>)}</div>}
      <div className="media-stage">{previewType === "video" ? <video src={preview} controls playsInline aria-label="待分析视频"/> : <img src={preview} alt="待分析素材"/>}<div className="media-overlay"><span>{filename}</span><small>{progressText}</small></div><div className="scan-frame"><i/><i/><i/><i/></div>{analyzing && <div className="analyzing-layer"><span className="scanner"/><b>正在提取画面证据并匹配隐患标签…</b><small>{progressText}</small></div>}</div>
      <div className="analysis-controls"><div><label>隐患等级</label><div className="auto-level-note"><ShieldCheck size={16}/><span>由视觉模型在“一般隐患 / 重大隐患”中自动判断；实时默认 {algorithmSettings.realtime_fps} FPS</span></div><label className="email-toggle"><input type="checkbox" checked={autoEmail} onChange={(event) => setAutoEmail(event.target.checked)}/>识别后按自动等级发送邮件</label>{algorithmCapabilities && <small className="algorithm-capability">算法能力配置已加载</small>}</div><button className="primary-action analyze-button" onClick={runRecognition} disabled={analyzing || !selectedFiles.length || backendState !== "ready"}><ScanLine size={18}/>{analyzing ? "真实分析中…" : selectedFiles.length > 1 ? `开始真实识别（${selectedFiles.length} 个素材）` : "开始真实识别"}</button></div>
      <div className="stream-reservation stream-navigation"><div><span className="section-kicker">摄像头与 AI 任务</span><h3>视频源配置和实时 AI 已集中管理</h3><p>为保护现场账号，RTSP 地址不再在识别上传页填写。请在设备监控添加视频源，在实时监控中启动、停止和查看 AI 最新发现。</p></div><div><button className="secondary-action" onClick={() => navigate("devices")}><Cctv size={16}/>管理设备</button><button className="primary-action" onClick={() => navigate("monitor")}><Video size={16}/>进入实时监控</button></div></div>
    </section>
    </details>

    <aside className="surface discovery-feed">
      <SectionTitle kicker="自动建单" title="AI最新发现" action={<span className="count-badge">{hazards.filter((h) => h.status === "待核实").length} 待核实</span>} />
      <div className="discovery-list">
        {liveGroups.slice(0, 5).map((group) => <button key={group.id} onClick={() => openHazard(group.hazards[0].id)}><img src={group.beforeImage} alt="隐患截图"/><div><span><LevelBadge level={group.hazards.some((item) => item.level === "重大隐患") ? "重大隐患" : "一般隐患"}/><small>{group.detectedAt.slice(-5)}</small></span><strong>{group.hazards.length}项同图隐患</strong><p>{group.hazards.map((item) => item.title).join("、")}</p><footer><StatusBadge status={group.status}/><em>已生成 {group.orderId}</em></footer></div></button>)}
      </div>
    </aside>

    {cameraOpen && <div className="modal-backdrop"><div className="camera-modal"><header><div><b>电脑摄像头测试</b><p>用于验证浏览器设备权限；正式持续分析仍通过后端视频流接口。</p></div><button onClick={closeCamera}><X size={19}/></button></header>{cameraError ? <div className="camera-error"><CircleAlert size={28}/><p>{cameraError}</p></div> : <video ref={videoRef} autoPlay muted playsInline/>}<footer><button className="secondary-action" onClick={closeCamera}>关闭</button><button className="primary-action" onClick={() => { closeCamera(); flash("电脑摄像头预览正常，后端取流接口已预留"); }}>确认设备可用</button></footer></div></div>}
  </div>;
}

function DispatchPage({ hazards, openHazard, flash }: { hazards: Hazard[]; openHazard: (id: string) => void; flash: (message: string) => void }) {
  const groups = useMemo(() => groupHazards(hazards), [hazards]);
  const [selected, setSelected] = useState(groups[0]?.id ?? "");
  const current = groups.find((group) => group.id === selected) ?? groups[0];
  const recipients = current ? [...new Map(current.hazards.flatMap((hazard) => hazard.recipients).map((item) => [item.personId, item])).values()] : [];
  const hasRealEmail = recipients.some((recipient) => recipient.status === "真实已发送");

  return <div className="page-stack">
    <section className="dispatch-summary">
      <article><span className="summary-icon blue"><FileText size={20}/></span><p><small>今日自动建单</small><strong>{groups.length}</strong><em>同图多隐患合并建单</em></p></article>
      <article><span className="summary-icon orange"><Bell size={20}/></span><p><small>邮件通知</small><strong>{hazards.reduce((count, h) => count + h.recipients.length, 0)}</strong><em>包含真实与待发送记录</em></p></article>
      <article><span className="summary-icon violet"><Clock3 size={20}/></span><p><small>待核实整改单</small><strong>{hazards.filter((h) => h.status === "待核实").length}</strong><em>需要安全人员处理</em></p></article>
      <article><span className="summary-icon green"><CircleCheck size={20}/></span><p><small>已闭合</small><strong>{hazards.filter((h) => h.status === "已闭合").length}</strong><em>材料可完整导出</em></p></article>
    </section>

    <section className="surface routing-rule">
      <div><span className="section-kicker">当前启用规则</span><h3>按隐患等级和所属工区自动分发</h3><p>人员来自项目通讯录；正式使用时可为每个工区配置不同负责人。</p></div>
      <div className="route-flow"><span className="level-route normal">一般隐患</span><ChevronRight size={16}/><div><b>安全总监</b><b>安全员</b><b>所属工区负责人</b></div></div>
      <div className="route-flow"><span className="level-route major">重大隐患</span><ChevronRight size={16}/><div><b>项目经理</b><b>安全总监</b></div></div>
      <button className="secondary-action" onClick={() => flash("分发规则配置将在业务后端人员权限模块中启用")}>查看规则说明</button>
    </section>

    <section className="dispatch-layout">
      <article className="surface order-list-panel">
        <SectionTitle kicker="下发记录" title="整改单" action={<button onClick={() => current?.hazards[0] && exportRectificationOrder(current.hazards[0])}><FileDown size={15}/>导出当前整改单</button>} />
        <div className="order-list">
          {groups.map((group) => { const major = group.hazards.some((item) => item.level === "重大隐患"); return <button key={group.id} className={current?.id === group.id ? "active" : ""} onClick={() => setSelected(group.id)}><span className={`order-level ${major ? "major" : "normal"}`}>{major ? "重" : "一"}</span><div><strong>{group.hazards.length}项同图隐患</strong><small>{group.orderId} · {group.workArea}</small><p>{group.hazards.map((item) => item.title).join("、")}</p></div><StatusBadge status={group.status}/></button>; })}
        </div>
      </article>

      {current && <article className="surface notification-detail">
        <SectionTitle kicker={current.orderId} title="隐患证据与接收记录" action={<button onClick={() => openHazard(current.hazards[0].id)}>查看隐患详情</button>} />
        <div className="dispatch-evidence"><img src={current.beforeImage} alt="本次隐患证据"/><div><span>同一张图片识别到 {current.hazards.length} 项隐患</span>{current.hazards.map((hazard, index) => <button key={hazard.id} onClick={() => openHazard(hazard.id)}><b>{index + 1}. {hazard.title}</b><small><LevelBadge level={hazard.level}/><StatusBadge status={hazard.status}/></small><p>{hazard.evidence}</p></button>)}</div></div>
        <div className="recipient-table"><header><span>接收人员</span><span>联系方式</span><span>发送状态</span><span>时间</span></header>{recipients.map((recipient) => <div key={recipient.personId}><span><i>{recipient.name.slice(0, 1)}</i><b>{recipient.name}<small>{recipient.role}</small></b></span><code>{recipient.phone ? maskPhone(recipient.phone) : "邮箱通知"}</code><em><Check size={12}/>{recipient.status}</em><small>{recipient.sentAt.slice(-5)}</small></div>)}</div>
        <div className="simulation-note"><CircleAlert size={17}/><p><b>{hasRealEmail ? "真实邮件分发已完成" : "这是内置演示台账"}</b><span>{hasRealEmail ? "发送结果已由8010后端返回并写入当前全过程记录。" : "请在“AI实时发现”上传素材；识别到明确隐患后可由后端真实发送邮件。"}</span></p><button onClick={() => flash(hasRealEmail ? "该任务已经完成真实邮件分发" : "请到AI实时发现页面上传图片或视频进行真实测试")}>{hasRealEmail ? "查看发送状态" : "如何真实测试"}</button></div>
      </article>}
    </section>
  </div>;
}

function LedgerPage({ hazards, openHazard, flash }: { hazards: Hazard[]; openHazard: (id: string) => void; flash: (message: string) => void }) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("全部状态");
  const [level, setLevel] = useState("全部等级");
  const filtered = useMemo(() => groupHazards(hazards).filter((group) => {
    const text = `${group.id}${group.orderId}${group.workArea}${group.hazards.map((item) => `${item.id}${item.title}${item.point}`).join("")}`.toLowerCase();
    return text.includes(search.toLowerCase()) && (status === "全部状态" || group.status === status || group.hazards.some((item) => item.status === status)) && (level === "全部等级" || group.hazards.some((item) => item.level === level));
  }), [hazards, search, status, level]);

  return <div className="page-stack">
    <section className="surface ledger-card">
      <div className="ledger-toolbar"><div><span className="section-kicker">全过程记录</span><h2>隐患台账</h2><p>同一张证据图只形成一张整改单，组内隐患分别核实、整改和闭合。</p></div><div className="toolbar-actions"><button className="secondary-action" onClick={() => { setSearch(""); setStatus("全部状态"); setLevel("全部等级"); }}><RefreshCcw size={15}/>重置</button><button className="primary-action" onClick={() => { exportHazardLedger(filtered.flatMap((item) => item.hazards)); flash("已导出当前台账CSV"); }}><Download size={16}/>导出台账</button></div></div>
      <div className="filter-row"><label className="search-control"><Search size={16}/><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索隐患编号、名称、点位或整改单"/></label><select value={status} onChange={(e) => setStatus(e.target.value)}><option>全部状态</option>{statusOrder.map((item) => <option key={item}>{item}</option>)}</select><select value={level} onChange={(e) => setLevel(e.target.value)}><option>全部等级</option><option>一般隐患</option><option>重大隐患</option></select><select><option>示范工区</option></select><span>共 {filtered.length} 条</span></div>
      <div className="data-table-wrap"><table className="data-table"><thead><tr><th>证据与整改单</th><th>组内隐患</th><th>位置</th><th>发现时间</th><th>组状态</th><th>操作</th></tr></thead><tbody>{filtered.map((group) => <tr key={group.id}><td><button className="hazard-cell" onClick={() => openHazard(group.hazards[0].id)}><img src={group.beforeImage} alt="隐患截图"/><span><strong>{group.orderId}</strong><small>{group.id}</small><small>共 {group.hazards.length} 项</small></span></button></td><td><div className="group-hazard-lines">{group.hazards.map((hazard) => <button key={hazard.id} onClick={() => openHazard(hazard.id)}><LevelBadge level={hazard.level}/><span>{hazard.title}</span><StatusBadge status={hazard.status}/></button>)}</div></td><td><b>{group.workArea}</b><small>{group.hazards[0].point}</small></td><td>{group.detectedAt}</td><td><StatusBadge status={group.status}/></td><td><button className="table-link" onClick={() => openHazard(group.hazards[0].id)}>详情 <ChevronRight size={14}/></button></td></tr>)}</tbody></table></div>
    </section>
  </div>;
}

function RectificationPage({ hazards, currentUser, openHazard, submitRectification, reviewHazard, confirmMajor }: { hazards: Hazard[]; currentUser?: SystemUser; openHazard: (id: string) => void; submitRectification: (id: string, description: string, afterImage?: string) => void; reviewHazard: (id: string, passed: boolean) => void; confirmMajor: (id: string) => void }) {
  const groups = groupHazards(hazards).filter((group) => group.hazards.some((item) => item.status !== "误报/已作废"));
  const [active, setActive] = useState(hazards.find((hazard) => ["待整改", "复核退回"].includes(hazard.status))?.id ?? hazards[0]?.id);
  const [description, setDescription] = useState("");
  const [afterImage, setAfterImage] = useState<string>("");
  const current = hazards.find((hazard) => hazard.id === active) ?? hazards[0];

  function pickAfter(file?: File) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setAfterImage(String(reader.result || ""));
    reader.readAsDataURL(file);
  }

  return <div className="rectification-layout">
    <section className="surface task-rail">
      <SectionTitle kicker="我的待办" title="整改与复核任务" action={<span className="count-badge">{hazards.filter((h) => !["已闭合", "误报/已作废"].includes(h.status)).length} 项</span>} />
      <div className="task-filter"><button className="active">全部</button><button>待整改</button><button>待复核</button><button>待确认</button></div>
      <div className="task-list grouped-tasks">{groups.map((group) => <article key={group.id} className={current?.groupId === group.id ? "active" : ""}><img src={group.beforeImage} alt="隐患组证据"/><div><strong>{group.orderId}</strong><small>{group.workArea} · {group.hazards.length}项隐患</small>{group.hazards.filter((item) => item.status !== "误报/已作废").map((hazard) => <button key={hazard.id} className={current?.id === hazard.id ? "active" : ""} onClick={() => setActive(hazard.id)}><span className="task-item-title">{hazard.title}</span><span className="task-item-badges"><LevelBadge level={hazard.level}/><StatusBadge status={hazard.status}/></span></button>)}</div></article>)}</div>
    </section>

    {current && <section className="surface rectification-workbench">
      <header className="workbench-header"><div><span className="section-kicker">{current.orderId}</span><h2>{current.title}</h2><p><MapPin size={14}/>{current.workArea} · {current.point}<span>责任人：{current.owner}</span></p></div><button className="secondary-action" onClick={() => openHazard(current.id)}><Eye size={16}/>查看完整记录</button></header>
      <div className="before-after">
        <figure><span>整改前</span><img src={current.beforeImage} alt="整改前"/><figcaption>{current.evidence}</figcaption></figure>
        <figure className={!afterImage && !current.rectifications.at(-1)?.afterImage ? "empty-photo" : ""}><span>整改后</span>{afterImage || current.rectifications.at(-1)?.afterImage ? <img src={afterImage || current.rectifications.at(-1)?.afterImage} alt="整改后"/> : <label><ImagePlus size={34}/><b>上传整改后照片</b><small>支持手机直接拍摄或从相册选择</small><input type="file" accept="image/*" capture="environment" onChange={(e) => pickAfter(e.target.files?.[0])}/></label>}<figcaption>{current.rectifications.at(-1)?.description || "等待整改人员提交说明"}</figcaption></figure>
      </div>

      {(["待整改", "整改中", "复核退回"].includes(current.status)) && (["work_area_manager", "system_admin"].includes(currentUser?.role || "")) && <div className="rectification-form"><label><span>整改说明</span><textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="请说明采取了什么整改措施、整改完成情况以及需要复核的内容…"/></label><div><p><Smartphone size={16}/>手机端可直接拍照上传，提交后自动形成整改回复单。</p><button className="primary-action" disabled={!description.trim() || !afterImage} onClick={() => { submitRectification(current.id, description, afterImage); setDescription(""); setAfterImage(""); }}><Send size={16}/>提交整改结果</button></div></div>}

      {current.status === "待复核" && (currentUser?.role === "system_admin" || currentUser?.role === (current.level === "重大隐患" ? "safety_director" : "safety_officer")) && <div className="review-panel"><div><ShieldCheck size={23}/><span><b>{current.level === "重大隐患" ? "安全总监复核" : "安全员复核"}</b><small>请对照整改前后照片及整改说明作出结论。</small></span></div><button className="danger-outline" onClick={() => reviewHazard(current.id, false)}>退回整改</button><button className="primary-action" onClick={() => reviewHazard(current.id, true)}>复核通过</button></div>}
      {current.status === "待重大确认" && ["project_manager", "system_admin"].includes(currentUser?.role || "") && <div className="review-panel major-confirm"><div><BadgeCheck size={23}/><span><b>等待项目经理最终确认</b><small>安全总监已复核通过，确认后该重大隐患正式闭合。</small></span></div><button className="primary-action" onClick={() => confirmMajor(current.id)}>确认闭合</button></div>}
      {current.status === "已闭合" && <div className="closed-banner"><CircleCheck size={23}/><div><b>该隐患已经完成闭环</b><span>整改单、整改回复单和全过程记录均可导出。</span></div><button onClick={() => exportRectificationReply(current)}><Download size={15}/>导出回复单</button></div>}
    </section>}
  </div>;
}

const accountRoleNames: Record<SystemUser["role"], string> = { system_admin: "系统管理员", safety_officer: "安全员", work_area_manager: "工区负责人", project_manager: "项目经理", safety_director: "安全总监" };

function AccountManagementPage({ currentUser, flash }: { currentUser: SystemUser; flash: (message: string) => void }) {
  const [users, setUsers] = useState<SystemUser[]>([]);
  const [draft, setDraft] = useState({ name: "", email: "", role: "safety_officer" as SystemUser["role"], work_area: "示范工区" });
  const [temporaryPassword, setTemporaryPassword] = useState("");
  const [projectMembers, setProjectMembers] = useState<Set<string>>(new Set());
  const isAdmin = currentUser.role === "system_admin";

  const refresh = () => isAdmin ? consoleApi.listUsers().then((items) => { setUsers(items); setProjectMembers(new Set(items.filter((u) => u.project_member).map((u) => u.id))); }).catch((error) => flash(error instanceof Error ? error.message : "账号列表加载失败")) : Promise.resolve();
  useEffect(() => { void refresh(); }, [isAdmin]);

  async function createAccount() {
    if (!draft.name.trim() || !draft.email.trim()) { flash("请填写姓名和邮箱"); return; }
    try {
      const result = await consoleApi.createUser(draft);
      setTemporaryPassword(result.temporary_password); setDraft({ name: "", email: "", role: "safety_officer", work_area: "示范工区" }); await refresh();
      flash("账号已创建，请安全地把一次性密码交给本人");
    } catch (error) { flash(error instanceof Error ? error.message : "账号创建失败"); }
  }

  async function toggleProjectMember(user: SystemUser) {
    if (user.role === "system_admin") { flash("系统管理员具有全部项目管理权限"); return; }
    const enabled = !projectMembers.has(user.id);
    try { await consoleApi.setProjectMemberEnabled(getSelectedProjectId(), user.id, enabled); setProjectMembers((items) => { const next = new Set(items); if (enabled) next.add(user.id); else next.delete(user.id); return next; }); flash(enabled ? "已加入当前项目" : "已移出当前项目，不影响全局账号状态"); }
    catch (error) { flash(error instanceof Error ? error.message : "项目成员状态修改失败"); }
  }

  async function resetPassword(user: SystemUser) {
    try { const result = await consoleApi.resetUserPassword(user.id); setTemporaryPassword(result.temporary_password); flash(`${user.name}的密码已重置`); }
    catch (error) { flash(error instanceof Error ? error.message : "密码重置失败"); }
  }

  if (!isAdmin) return <div className="page-stack"><section className="surface account-self-card"><span className="section-kicker">我的账号</span><h2>{currentUser.name}</h2><p>{currentUser.role_name} · {currentUser.work_area}</p><small>{currentUser.email}</small><p>人员账号、角色和工区由系统管理员统一维护。</p></section></div>;

  return <div className="page-stack">
    <section className="people-hero"><div><span className="section-kicker">账号与权限</span><h2>人员、工区与闭环职责统一配置</h2><p>新用户取得一次性密码，首次登录必须修改；停用账号会立即注销已有会话。</p></div><div><span><strong>{users.filter((item) => item.enabled).length}</strong>启用账号</span></div></section>
    <section className="surface account-create"><h3>新增账号</h3><div><input placeholder="姓名" value={draft.name} onChange={(event) => setDraft({...draft, name:event.target.value})}/><input type="email" placeholder="邮箱（也是登录账号）" value={draft.email} onChange={(event) => setDraft({...draft, email:event.target.value})}/><select value={draft.role} onChange={(event) => setDraft({...draft, role:event.target.value as SystemUser["role"]})}>{Object.entries(accountRoleNames).filter(([role]) => role !== "system_admin").map(([role,name]) => <option key={role} value={role}>{name}</option>)}</select><input placeholder="所属工区" value={draft.work_area} onChange={(event) => setDraft({...draft, work_area:event.target.value})}/><button className="primary-action" onClick={createAccount}><UserRoundPlus size={17}/>创建账号</button></div>{temporaryPassword && <div className="temporary-password"><b>一次性初始密码</b><code>{temporaryPassword}</code><span>此密码仅在本次页面显示，请交给用户后关闭。</span><button onClick={() => setTemporaryPassword("")}>我已保存</button></div>}</section>
    <section className="surface people-card"><div className="data-table-wrap"><table className="data-table"><thead><tr><th>姓名</th><th>登录邮箱</th><th>角色</th><th>工区</th><th>首次改密</th><th>全局账号</th><th>当前项目</th><th>操作</th></tr></thead><tbody>{users.map((user) => <tr key={user.id}><td><b>{user.name}</b><small>{user.id}</small></td><td>{user.email}</td><td>{user.role_name}</td><td>{user.work_area}</td><td>{user.must_change_password ? "待修改" : "已完成"}</td><td><span className={`person-status ${user.enabled ? "enabled" : "disabled"}`}>{user.enabled ? "启用" : "停用"}</span></td><td><span className={`person-status ${projectMembers.has(user.id) ? "enabled" : "disabled"}`}>{projectMembers.has(user.id) ? "已加入" : "未加入"}</span></td><td><button className="table-link" onClick={() => toggleProjectMember(user)}>{projectMembers.has(user.id) ? "移出项目" : "加入项目"}</button><button className="table-link" onClick={() => resetPassword(user)}>重置密码</button></td></tr>)}</tbody></table></div></section>
  </div>;
}

function PeoplePage({ people, setPeople, openAdd, flash }: { people: Person[]; setPeople: React.Dispatch<React.SetStateAction<Person[]>>; openAdd: () => void; flash: (message: string) => void }) {
  const [search, setSearch] = useState("");
  const filtered = people.filter((person) => `${person.name}${person.role}${person.phone}${person.workArea}`.includes(search));

  function toggle(person: Person) {
    setPeople((items) => items.map((item) => item.id === person.id ? { ...item, status: item.status === "启用" ? "停用" : "启用" } : item));
    flash(`${person.name}已${person.status === "启用" ? "停用" : "启用"}`);
  }

  return <div className="page-stack">
    <section className="people-hero"><div><span className="section-kicker">项目通讯录</span><h2>人员、工区与分发责任统一配置</h2><p>演示环境仅配置示范工区角色；正式使用时可按工区分别维护负责人。</p></div><div><span><strong>{people.filter((p) => p.status === "启用").length}</strong>启用人员</span><span><strong>1</strong>试运行工区</span><button className="primary-action" onClick={openAdd}><UserRoundPlus size={17}/>新增人员</button></div></section>
    <section className="surface people-card"><div className="people-toolbar"><label className="search-control"><Search size={16}/><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索姓名、岗位、工区或手机号"/></label><select><option>全部角色</option><option>安全员</option><option>工区负责人</option><option>项目经理</option><option>安全总监</option></select><select><option>示范工区</option><option>全项目</option></select><span>手机号和证件信息默认脱敏</span></div>
      <div className="data-table-wrap"><table className="data-table people-table"><thead><tr><th>人员</th><th>岗位与职责</th><th>所属范围</th><th>手机号</th><th>证件号码</th><th>通知</th><th>状态</th><th>操作</th></tr></thead><tbody>{filtered.map((person) => <tr key={person.id}><td><div className="person-cell"><span>{person.name.slice(0, 1)}</span><b>{person.name}<small>{person.id}</small></b></div></td><td><b>{person.role}</b><small>{person.duty}</small></td><td><b>{person.workArea}</b><small>{person.company}</small></td><td><code>{maskPhone(person.phone)}</code></td><td><code>{person.idCard}</code></td><td><span className={person.notificationEnabled ? "notify-on" : "notify-off"}>{person.notificationEnabled ? "接收通知" : "已关闭"}</span></td><td><span className={`person-status ${person.status === "启用" ? "enabled" : "disabled"}`}>{person.status}</span></td><td><button className="table-link" onClick={() => toggle(person)}>{person.status === "启用" ? "停用" : "启用"}</button></td></tr>)}</tbody></table></div>
    </section>
  </div>;
}

function DevicesPage({ flash, navigate }: { flash: (message: string) => void; navigate: (page: PageKey) => void }) {
  const [rtspDraft, setRtspDraft] = useState({ name: "", source_url: "", work_area: "", risk_point: "" });
  const [savingRtsp, setSavingRtsp] = useState(false);
  const [gatewaySources, setGatewaySources] = useState<VideoSourceView[]>([]);
  const [profiles, setProfiles] = useState<HikvisionProfile[]>([]);
  const [channels, setChannels] = useState<HikvisionChannel[]>([]);
  const [nvr, setNvr] = useState({ id: "", name: "现场NVR", host: "", port: "8000", username: "", password: "" });
  const [sessions, setSessions] = useState<StreamSession[]>([]);
  const [algorithmSettings, setAlgorithmSettings] = useState<AlgorithmSettings>({ realtime_fps: 2, inspection_interval_sec: 300, test_stream_interval_sec: 3 });
  const refreshHikvision = () => Promise.all([consoleApi.listHikvisionProfiles(), consoleApi.listHikvisionChannels()]).then(([p, c]) => { setProfiles(p); setChannels(c); });
  useEffect(() => { consoleApi.listVideoSources().then(setGatewaySources).catch(() => setGatewaySources([])); }, []);
  useEffect(() => { refreshHikvision().catch(() => undefined); }, []);
  useEffect(() => {
    let disposed = false;
    const refresh = () => { consoleApi.listStreamSessions().then((items) => { if (!disposed) setSessions(items); }).catch(() => undefined); };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  useEffect(() => { if (typeof consoleApi.getAlgorithmSettings === "function") void consoleApi.getAlgorithmSettings().then(setAlgorithmSettings).catch(() => undefined); }, []);

  async function saveNvr() {
    try { const item = await consoleApi.configureHikvisionProfile({ ...nvr, id: nvr.id || undefined, port: Number(nvr.port) }); setNvr((v) => ({ ...v, id: item.id, password: "" })); await refreshHikvision(); flash(`${item.name}已安全保存到Windows桥接机；密码不会回传网页`); }
    catch (error) { flash(error instanceof Error ? error.message : "NVR配置失败"); }
  }
  async function saveRtsp() {
    if (!rtspDraft.name.trim() || !/^rtsps?:\/\//i.test(rtspDraft.source_url.trim())) { flash("请填写摄像头名称和有效的 RTSP 地址；海康SDK地址请使用上方NVR配置"); return; }
    setSavingRtsp(true);
    try {
      await consoleApi.createVideoSource({ ...rtspDraft, source_url: rtspDraft.source_url.trim(), id: `rtsp-${crypto.randomUUID()}`, source_type: "rtsp", enabled: true, ai_enabled: false });
      setGatewaySources(await consoleApi.listVideoSources());
      setRtspDraft({ name: "", source_url: "", work_area: "", risk_point: "" });
      flash("RTSP设备已保存。请到实时监控选择画面，AI不会自动启动。");
    } catch (error) { flash(error instanceof Error ? error.message : "设备保存失败"); }
    finally { setSavingRtsp(false); }
  }
  async function sync(profile: HikvisionProfile) { try { await consoleApi.testHikvisionProfile(profile.id); const result = await consoleApi.syncHikvisionProfile(profile.id); setChannels(result.channels); flash(`已同步${result.channels.length}路海康通道`); } catch (error) { flash(error instanceof Error ? error.message : "通道同步失败"); } }
  async function updateChannel(channel: HikvisionChannel, patch: Partial<HikvisionChannel>) { try { const updated = await consoleApi.updateHikvisionChannel(channel.id, patch); setChannels((items) => items.map((item) => item.id === updated.id ? updated : item)); } catch (error) { flash(error instanceof Error ? error.message : "通道保存失败"); } }
  async function toggleAi(channel: HikvisionChannel, mode: "realtime" | "inspection" | "test" = "realtime") {
    const active = sessions.find((item) => item.source_id === channel.id && ["starting", "running"].includes(item.status));
    try {
      if (active) { await consoleApi.stopStreamSession(active.session_id); setSessions((items) => items.filter((item) => item.session_id !== active.session_id)); await updateChannel(channel, { ai_enabled: false }); flash(`${channel.name}的AI识别已停止，实时监看不受影响`); return; }
      if (!channel.enabled) { flash("请先勾选“实时监看”，再启动AI识别"); return; }
      const session = await consoleApi.startStreamSession({ source_id: channel.id, inference_fps: algorithmSettings.realtime_fps, auto_email: true, analysis_mode: mode, inspection_interval_sec: algorithmSettings.inspection_interval_sec });
      setSessions((items) => [...items.filter((item) => item.source_id !== channel.id), session]); await updateChannel(channel, { ai_enabled: true }); flash(`${channel.name}已启动${mode === "inspection" ? "每5分钟巡检" : mode === "test" ? "三秒全链路测试" : "实时AI识别"}`);
    } catch (error) { flash(error instanceof Error ? error.message : "AI识别启动失败"); }
  }

  async function testGatewaySource(source: VideoSourceView) {
    try {
      await consoleApi.testVideoSource(source.id);
      flash(`${source.name}配置检查通过；正式连通性由启动流媒体会话验证`);
    } catch (error) {
      flash(error instanceof Error ? error.message : "视频源检查失败");
    }
  }

  return <div className="page-stack">
    <section className="device-banner"><div><span className="section-kicker">统一设备入口</span><h2>摄像头、无人机、机器狗与模拟视频流</h2><p>前端只展示脱敏地址和任务控制；实时 {algorithmSettings.realtime_fps} FPS，巡检间隔 {algorithmSettings.inspection_interval_sec} 秒。</p></div><button className="primary-action" onClick={() => flash("请在下方海康 NVR 配置或后端视频源中添加设备；AI 控制在实时监控中心完成")}>＋ 新增视频源</button><button className="secondary-action device-monitor-link" onClick={() => navigate("monitor")}><Video size={16}/>前往实时监控</button></section>
    <section className="surface gateway-source-panel"><div className="section-title"><div><span>标准协议设备</span><h3>添加 RTSP 摄像头</h3></div></div><div className="people-toolbar"><input aria-label="RTSP摄像头名称" placeholder="摄像头名称" value={rtspDraft.name} onChange={(e) => setRtspDraft({ ...rtspDraft, name: e.target.value })}/><input aria-label="RTSP取流地址" type="password" autoComplete="new-password" placeholder="rtsp://设备地址/路径（含账号时不明文显示）" value={rtspDraft.source_url} onChange={(e) => setRtspDraft({ ...rtspDraft, source_url: e.target.value })}/><input aria-label="RTSP所属工区" placeholder="所属工区" value={rtspDraft.work_area} onChange={(e) => setRtspDraft({ ...rtspDraft, work_area: e.target.value })}/><input aria-label="RTSP风险点" placeholder="风险点" value={rtspDraft.risk_point} onChange={(e) => setRtspDraft({ ...rtspDraft, risk_point: e.target.value })}/><button className="secondary-action" disabled={savingRtsp} onClick={() => void saveRtsp()}>{savingRtsp ? "保存中…" : "保存RTSP设备"}</button></div></section>
    <section className="surface gateway-source-panel"><SectionTitle kicker="海康 SDK" title="海康 NVR 接入配置" action={<span>账号和密码只进入Windows桥接机</span>}/><div className="people-toolbar"><input placeholder="NVR名称" value={nvr.name} onChange={(e) => setNvr({...nvr,name:e.target.value})}/><input placeholder="映射IP或域名" value={nvr.host} onChange={(e) => setNvr({...nvr,host:e.target.value})}/><input placeholder="SDK端口" value={nvr.port} onChange={(e) => setNvr({...nvr,port:e.target.value})}/><input placeholder="只读账号" value={nvr.username} onChange={(e) => setNvr({...nvr,username:e.target.value})}/><input type="password" placeholder={nvr.id ? "留空则保留原密码" : "密码（不会显示）"} value={nvr.password} onChange={(e) => setNvr({...nvr,password:e.target.value})}/><button className="primary-action" onClick={saveNvr}>{nvr.id ? "保存编辑" : "保存并测试"}</button></div><div>{profiles.map((profile) => <article key={profile.id}><span><Cctv size={18}/></span><div><b>{profile.name}</b><small>{profile.host}:{profile.port} · {profile.username}</small><code>海康私有SDK桥接</code></div><button onClick={() => setNvr({ id: profile.id, name: profile.name, host: profile.host, port: String(profile.port), username: profile.username, password: "" })}>编辑</button><button onClick={() => sync(profile)}>同步在线通道</button></article>)}</div>{channels.length > 0 && <div className="data-table-wrap"><table className="data-table"><thead><tr><th>通道</th><th>显示名称</th><th>工区/风险点</th><th>状态</th><th>实时监看</th><th>AI识别</th></tr></thead><tbody>{channels.map((channel) => { const active = sessions.some((item) => item.source_id === channel.id && ["starting", "running"].includes(item.status)); return <tr key={channel.id}><td>{channel.nvr_name}<small>#{channel.channel_no}</small></td><td><input value={channel.name} onChange={(e) => setChannels(items => items.map(x => x.id === channel.id ? {...x,name:e.target.value} : x))} onBlur={() => updateChannel(channel,{name:channel.name})}/></td><td><input value={channel.work_area} onChange={(e) => updateChannel(channel,{work_area:e.target.value})} placeholder="工区"/><input value={channel.risk_point} onChange={(e) => updateChannel(channel,{risk_point:e.target.value})} placeholder="风险点"/></td><td>{channel.online ? "在线" : "离线"}</td><td><input type="checkbox" checked={channel.enabled} onChange={(e) => updateChannel(channel,{enabled:e.target.checked})}/></td><td><button className={active ? "danger-outline" : "secondary-action"} onClick={() => toggleAi(channel)}>{active ? "停止AI" : "启动实时AI"}</button><button className="text-action" onClick={() => toggleAi(channel,"inspection")}>巡检</button><button className="text-action" onClick={() => toggleAi(channel,"test")}>测试</button></td></tr>; })}</tbody></table></div>}</section>
    {gatewaySources.length > 0 && <section className="surface gateway-source-panel"><SectionTitle kicker="MediaMTX" title="其他后端视频源" action={<span>{gatewaySources.length} 路</span>}/><div>{gatewaySources.map((source) => <article key={source.id}><span><Cctv size={18}/></span><div><b>{source.name}</b><small>{source.id} · {source.work_area}</small><code>{source.source_url}</code></div><button onClick={() => testGatewaySource(source)}>检查配置</button></article>)}</div></section>}
    <section className="device-grid">{initialDevices.map((device) => <article className="surface device-card" key={device.id}><header><span className={`device-icon ${device.status === "离线" ? "offline" : ""}`}>{device.type === "摄像头" || device.type === "模拟RTSP" ? <Cctv size={21}/> : <Radio size={21}/>}</span><div><strong>{device.name}</strong><small>{device.id}</small></div><em className={device.status === "离线" ? "offline" : "online"}>{device.status === "离线" ? <WifiOff size={13}/> : <Wifi size={13}/>} {device.status}</em></header><dl><div><dt>所属工区</dt><dd>{device.workArea}</dd></div><div><dt>风险点位</dt><dd>{device.point}</dd></div><div><dt>接入方式</dt><dd>{device.streamMode}</dd></div><div><dt>最后数据</dt><dd>{device.lastSeen}</dd></div></dl><footer><span className={device.aiEnabled ? "ai-on" : "ai-off"}><i/>{device.aiEnabled ? "AI识别已启用" : "AI识别未启用"}</span><button onClick={() => flash(`${device.name}连接测试完成：${device.status}`)}>测试连接</button></footer></article>)}</section>
    <section className="surface gateway-note"><span><Building2 size={21}/></span><div><b>现场没有边缘算力也可以接入</b><p>现场只需低配置电脑或小主机完成取流、抽帧和加密上传，YOLO和视觉大模型继续部署在云端。若现场完全没有常驻设备或安全网络通道，云端无法直接访问私网摄像头。</p></div><button onClick={() => flash("现场视频网关部署说明已记录在V2架构文档中")}>查看部署说明</button></section>
  </div>;
}

function ReportsPage({ hazards, openHazard, flash }: { hazards: Hazard[]; openHazard: (id: string) => void; flash: (message: string) => void }) {
  const groups = groupHazards(hazards);
  const closed = groups.filter((group) => group.status === "已闭合");
  return <div className="page-stack">
    <section className="report-hero"><div><span className="section-kicker">阶段性归档</span><h2>把排查结果变成可交付的闭环材料</h2><p>同图多隐患共享整改单和证据图，回复单保留每个子项的整改与复核结论。</p></div><div className="report-actions"><button onClick={() => { exportHazardLedger(hazards); flash("隐患台账已下载"); }}><ClipboardList size={19}/><span><b>导出隐患台账</b><small>当前筛选范围 · CSV</small></span><Download size={16}/></button><button onClick={() => groups[0]?.hazards[0] && exportRectificationOrder(groups[0].hazards[0])}><FileText size={19}/><span><b>导出整改单</b><small>按证据图与整改单归组</small></span><Download size={16}/></button><button onClick={() => closed[0]?.hazards[0] && exportRectificationReply(closed[0].hazards[0])}><ClipboardCheck size={19}/><span><b>导出整改回复单</b><small>包含整改前后照片</small></span><Download size={16}/></button></div></section>
    <section className="report-grid"><article className="surface"><SectionTitle kicker="闭环质量" title="当前处理状态"/><div className="status-distribution">{statusOrder.slice(0, 7).map((status) => { const count = hazards.filter((h) => h.status === status).length; return <div key={status}><span>{status}</span><p><i style={{ width: `${Math.max(8, (count / Math.max(hazards.length, 1)) * 100)}%` }}/></p><b>{count}</b></div>; })}</div></article><article className="surface"><SectionTitle kicker="角色分工" title="闭环责任链"/><div className="role-chain"><div><span>1</span><b>AI识别服务</b><small>生成证据与待核实整改单</small></div><div><span>2</span><b>安全管理人员</b><small>核实类别、等级和有效性</small></div><div><span>3</span><b>工区负责人</b><small>上传整改后照片与说明</small></div><div><span>4</span><b>复核与确认</b><small>一般由安全员，重大由安全总监和项目经理</small></div></div></article></section>
    <section className="surface archive-list"><SectionTitle kicker="可归档记录" title="已闭合整改单" action={<span>{closed.length} 单</span>}/><div className="archive-rows">{closed.length ? closed.map((group) => <button key={group.id} onClick={() => openHazard(group.hazards[0].id)}><img src={group.beforeImage} alt="隐患"/><span><b>{group.hazards.length}项同图隐患</b><small>{group.hazards.map((item) => item.title).join("、")}</small><small>{group.orderId}</small></span><LevelBadge level={group.hazards.some((item) => item.level === "重大隐患") ? "重大隐患" : "一般隐患"}/><em>{group.hazards.reduce((sum,item) => sum + item.rectifications.length, 0)}条整改记录</em><strong>查看报告 <ChevronRight size={14}/></strong></button>) : <EmptyState text="暂无已闭合整改单"/>}</div></section>
  </div>;
}

function HazardDrawer({ hazard, close, verify, review, confirmMajor, flash }: { hazard: Hazard; close: () => void; verify: (values: { exists: boolean; descriptionCorrect: boolean; name: string; evidence: string; level: HazardLevel; reason: string }) => void; markFalse: () => void; review: (passed: boolean) => void; confirmMajor: () => void; flash: (message: string) => void }) {
  const latest = hazard.rectifications.at(-1);
  const [exists, setExists] = useState<"yes" | "no" | "">("");
  const [descriptionCorrect, setDescriptionCorrect] = useState<"yes" | "no" | "">("");
  const [name, setName] = useState(hazard.title);
  const [evidence, setEvidence] = useState(hazard.evidence);
  const [level, setLevel] = useState<HazardLevel>(hazard.level);
  const [reason, setReason] = useState("");
  useEffect(() => { setExists(""); setDescriptionCorrect(""); setName(hazard.title); setEvidence(hazard.evidence); setLevel(hazard.level); setReason(""); }, [hazard.id, hazard.title, hazard.evidence, hazard.level]);
  const canSubmit = exists === "no" ? reason.trim().length > 0 : exists === "yes" && descriptionCorrect !== "" && (descriptionCorrect === "yes" || (name.trim().length > 0 && evidence.trim().length > 0));

  return <div className="drawer-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) close(); }}><aside className="hazard-drawer">
    <header><div><span>{hazard.id}</span><h2>{hazard.title}</h2><p>{hazard.workArea} · {hazard.point}</p></div><button onClick={close}><X size={20}/></button></header>
    <div className="drawer-body"><div className="drawer-badges"><LevelBadge level={hazard.level}/><StatusBadge status={hazard.status}/><span className="source-badge">{hazard.source}</span></div>
      <figure className="drawer-evidence"><img src={hazard.beforeImage} alt="隐患证据"/><figcaption><b>AI可见证据</b><span>{hazard.evidence}</span></figcaption></figure>
      {hazard.status === "待核实" && <section className="detail-card verification-card"><h3>人工核实</h3><p className="verification-tip">请先判断隐患是否真实存在，再确认名称、描述和等级是否正确。AI原始结果会永久保留。</p>
        <div className="binary-choice"><span>1. 画面中是否存在该隐患？</span><button className={exists === "yes" ? "active" : ""} onClick={() => setExists("yes")}>存在</button><button className={exists === "no" ? "active danger" : ""} onClick={() => setExists("no")}>不存在</button></div>
        {exists === "no" && <label className="verification-field"><span>误报原因</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="请说明现场核实结论"/></label>}
        {exists === "yes" && <><div className="binary-choice"><span>2. 隐患名称、描述和等级是否正确？</span><button className={descriptionCorrect === "yes" ? "active" : ""} onClick={() => setDescriptionCorrect("yes")}>正确</button><button className={descriptionCorrect === "no" ? "active" : ""} onClick={() => setDescriptionCorrect("no")}>需要修改</button></div>
          {descriptionCorrect === "no" && <div className="verification-edit"><label><span>隐患名称</span><input value={name} onChange={(event) => setName(event.target.value)}/></label><label><span>隐患等级</span><select value={level} onChange={(event) => setLevel(event.target.value as HazardLevel)}><option>一般隐患</option><option>重大隐患</option></select></label><label className="wide"><span>可见证据描述</span><textarea value={evidence} onChange={(event) => setEvidence(event.target.value)}/></label></div>}
        </>}
        <button className="primary-action verification-submit" disabled={!canSubmit} onClick={() => verify({ exists: exists === "yes", descriptionCorrect: descriptionCorrect === "yes", name, evidence, level, reason })}>提交核实结论</button>
      </section>}
      <section className="detail-card"><h3>整改单信息</h3><div className="detail-grid"><p><span>整改单编号</span><b>{hazard.orderId}</b></p><p><span>发现时间</span><b>{hazard.detectedAt}</b></p><p><span>整改责任人</span><b>{hazard.owner}</b></p><p><span>整改期限</span><b>{hazard.deadline}</b></p><p><span>来源设备</span><b>{hazard.device}</b></p><p><span>同图隐患组</span><b>{hazard.groupId}</b></p></div></section>
      <section className="detail-card"><h3>分发记录</h3><div className="recipient-cards">{hazard.recipients.map((recipient) => <div key={recipient.personId}><span>{recipient.name.slice(0,1)}</span><p><b>{recipient.name}</b><small>{recipient.role} · {maskPhone(recipient.phone)}</small></p><em><Check size={12}/>{recipient.status}</em></div>)}</div></section>
      {latest && <section className="detail-card"><h3>最新整改回复</h3><div className="reply-summary"><img src={latest.afterImage || hazard.beforeImage} alt="整改后"/><div><b>{latest.description}</b><small>提交：{latest.submittedBy} · {latest.submittedAt}</small><span>复核：{latest.reviewResult || "待复核"}{latest.reviewComment ? ` · ${latest.reviewComment}` : ""}</span></div></div></section>}
      <section className="detail-card"><h3>全过程记录</h3><div className="timeline-list">{[...hazard.timeline].reverse().map((item) => <div key={item.id} className={item.tone ?? "blue"}><i/><div><b>{item.title}</b><p>{item.detail}</p><small>{item.time} · {item.actor}</small></div></div>)}</div></section>
    </div><footer><div><button className="text-action" onClick={() => exportRectificationOrder(hazard)}><FileDown size={15}/>整改单</button><button className="text-action" onClick={() => exportRectificationReply(hazard)}><FileDown size={15}/>回复单</button></div>{hazard.status === "待复核" && <><button className="danger-outline" onClick={() => review(false)}>退回整改</button><button className="primary-action" onClick={() => review(true)}>复核通过</button></>}{hazard.status === "待重大确认" && <button className="primary-action" onClick={confirmMajor}>项目经理确认闭合</button>}{!(["待核实", "待复核", "待重大确认"].includes(hazard.status)) && <button className="secondary-action" onClick={() => flash("完整操作记录已保留")}>记录完整</button>}</footer>
  </aside></div>;
}

function PersonDialog({ close, submit }: { close: () => void; submit: (person: Omit<Person, "id" | "idCard" | "gender" | "company" | "duty" | "status" | "notificationEnabled">) => void }) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [role, setRole] = useState<PersonRole>("工区负责人");
  const [workArea, setWorkArea] = useState(DEMO_WORK_AREA);
  return <div className="modal-backdrop"><form className="person-dialog" onSubmit={(e) => { e.preventDefault(); submit({ name, phone, role, workArea }); }}><header><div><b>新增演示人员</b><p>正式系统将由业务后端保存，并支持按多个工区配置。</p></div><button type="button" onClick={close}><X size={19}/></button></header><div className="dialog-form"><label><span>姓名</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="请输入姓名" required/></label><label><span>手机号</span><input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="请输入11位手机号" pattern="1[3-9]\d{9}" required/></label><label><span>系统角色</span><select value={role} onChange={(e) => setRole(e.target.value as PersonRole)}><option>安全员</option><option>工区负责人</option><option>项目经理</option><option>安全总监</option></select></label><label><span>所属工区</span><input value={workArea} onChange={(e) => setWorkArea(e.target.value)} required/></label></div><footer><button type="button" className="secondary-action" onClick={close}>取消</button><button type="submit" className="primary-action">保存人员</button></footer></form></div>;
}

function LevelBadge({ level }: { level: HazardLevel }) {
  return <span className={`level-badge ${level === "重大隐患" ? "major" : "normal"}`}>{level}</span>;
}

function StatusBadge({ status }: { status: HazardStatus }) {
  return <span className={`status-badge status-${status.replace("/", "-")}`}>{status}</span>;
}

function SectionTitle({ kicker, title, action }: { kicker: string; title: string; action?: React.ReactNode }) {
  return <header className="section-title"><div><span>{kicker}</span><h3>{title}</h3></div>{action && <div className="section-action">{action}</div>}</header>;
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state"><ClipboardList size={28}/><span>{text}</span></div>;
}
