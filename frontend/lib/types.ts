export type PageKey =
  | "overview"
  | "live"
  | "monitor"
  | "dispatch"
  | "ledger"
  | "rectification"
  | "people"
  | "devices"
  | "reports";

export type HazardLevel = "一般隐患" | "重大隐患";
export type HazardStatus =
  | "待核实"
  | "待整改"
  | "整改中"
  | "待复核"
  | "复核退回"
  | "待重大确认"
  | "已闭合"
  | "已误报"
  | "误报/已作废";

export type PersonRole = "安全员" | "工区负责人" | "项目经理" | "安全总监";
export type NotificationStatus = "模拟已发送" | "真实已发送" | "待发送" | "发送失败";

export type TimelineEvent = {
  id: string;
  title: string;
  detail: string;
  time: string;
  actor: string;
  tone?: "blue" | "orange" | "green" | "red";
};

export type Person = {
  id: string;
  name: string;
  role: PersonRole;
  phone: string;
  gender: "男" | "女";
  idCard: string;
  company: string;
  workArea: string;
  duty: string;
  status: "启用" | "停用";
  notificationEnabled: boolean;
};

export type Recipient = {
  personId: string;
  name: string;
  role: PersonRole;
  phone: string;
  status: NotificationStatus;
  sentAt: string;
};

export type RectificationRound = {
  id: string;
  description: string;
  submittedBy: string;
  submittedAt: string;
  afterImage?: string;
  reviewResult?: "待复核" | "通过" | "退回";
  reviewComment?: string;
};

export type Hazard = {
  id: string;
  groupId: string;
  orderId: string;
  inspectionId: string;
  title: string;
  category: string;
  level: HazardLevel;
  status: HazardStatus;
  project: string;
  workArea: string;
  point: string;
  source: "摄像头" | "无人机" | "机器狗" | "本地上传" | "电脑摄像头";
  device: string;
  detectedAt: string;
  deadline: string;
  evidence: string;
  originalTitle?: string;
  originalEvidence?: string;
  originalLevel?: HazardLevel;
  version: number;
  beforeImage: string;
  annotatedImage?: string;
  owner: string;
  verifier: string;
  repeats: number;
  isOverdue: boolean;
  recipients: Recipient[];
  rectifications: RectificationRound[];
  timeline: TimelineEvent[];
};

export type Device = {
  id: string;
  name: string;
  type: "摄像头" | "无人机" | "机器狗" | "模拟RTSP";
  workArea: string;
  point: string;
  status: "在线" | "离线" | "任务中";
  aiEnabled: boolean;
  lastSeen: string;
  streamMode: string;
};
