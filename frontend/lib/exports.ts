import type { Hazard } from "./types";

function saveBlob(content: BlobPart, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function escapeCsv(value: unknown) {
  const text = String(value ?? "").replaceAll('"', '""');
  return `"${text}"`;
}

export function exportHazardLedger(hazards: Hazard[]) {
  const headers = ["隐患编号", "整改单编号", "隐患名称", "隐患等级", "项目", "工区", "点位", "发现时间", "整改期限", "责任人", "处理状态", "是否逾期", "证据说明"];
  const rows = hazards.map((hazard) => [
    hazard.id,
    hazard.orderId,
    hazard.title,
    hazard.level,
    hazard.project,
    hazard.workArea,
    hazard.point,
    hazard.detectedAt,
    hazard.deadline,
    hazard.owner,
    hazard.status,
    hazard.isOverdue ? "是" : "否",
    hazard.evidence,
  ]);
  const csv = [headers, ...rows].map((row) => row.map(escapeCsv).join(",")).join("\r\n");
  saveBlob(`\uFEFF${csv}`, `隐患台账_${new Date().toISOString().slice(0, 10)}.csv`, "text/csv;charset=utf-8");
}

function reportDocument(hazard: Hazard, mode: "order" | "reply") {
  const latest = hazard.rectifications.at(-1);
  const title = mode === "order" ? "安全隐患整改单" : "安全隐患整改回复单";
  const rectification = mode === "reply" ? `
    <h2>整改记录</h2>
    <table>
      <tr><th>整改人员</th><td>${latest?.submittedBy ?? "尚未提交"}</td><th>整改时间</th><td>${latest?.submittedAt ?? "—"}</td></tr>
      <tr><th>整改说明</th><td colspan="3">${latest?.description ?? "尚未提交整改说明"}</td></tr>
      <tr><th>复核结果</th><td>${latest?.reviewResult ?? "待复核"}</td><th>复核意见</th><td>${latest?.reviewComment ?? "—"}</td></tr>
    </table>
    <h2>整改前后照片</h2>
    <div class="photos"><figure><img src="${hazard.beforeImage}"/><figcaption>整改前</figcaption></figure><figure><img src="${latest?.afterImage ?? hazard.beforeImage}"/><figcaption>整改后</figcaption></figure></div>` : "";

  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>${title}</title><style>
    body{font-family:"Microsoft YaHei",sans-serif;color:#243247;max-width:1000px;margin:36px auto;padding:0 28px}h1{text-align:center;font-size:28px}h2{font-size:18px;margin-top:28px;border-left:4px solid #2f6fda;padding-left:10px}table{width:100%;border-collapse:collapse}th,td{border:1px solid #cfd8e6;padding:12px;text-align:left}th{background:#eef4fc;width:16%}.photos{display:grid;grid-template-columns:1fr 1fr;gap:18px}.photos figure{margin:0}.photos img{width:100%;height:260px;object-fit:cover;border:1px solid #d7deea}.photos figcaption{text-align:center;margin-top:8px}.note{color:#738095;font-size:13px;margin-top:24px}@media print{.note{display:none}}</style></head><body>
    <h1>${title}</h1>
    <table>
      <tr><th>隐患编号</th><td>${hazard.id}</td><th>整改单编号</th><td>${hazard.orderId}</td></tr>
      <tr><th>工程名称</th><td>${hazard.project}</td><th>所属工区</th><td>${hazard.workArea}</td></tr>
      <tr><th>隐患名称</th><td>${hazard.title}</td><th>隐患等级</th><td>${hazard.level}</td></tr>
      <tr><th>风险点位</th><td>${hazard.point}</td><th>发现时间</th><td>${hazard.detectedAt}</td></tr>
      <tr><th>限期整改</th><td>${hazard.deadline}</td><th>当前状态</th><td>${hazard.status}</td></tr>
      <tr><th>隐患说明</th><td colspan="3">${hazard.evidence}</td></tr>
    </table>
    <h2>整改前照片</h2><div class="photos"><figure><img src="${hazard.beforeImage}"/><figcaption>AI识别证据图</figcaption></figure></div>
    ${rectification}
    <p class="note">当前为前端演示导出。正式系统将由业务后端生成统一PDF并保存审计编号。</p>
  </body></html>`;
}

export function exportRectificationOrder(hazard: Hazard) {
  saveBlob(reportDocument(hazard, "order"), `${hazard.orderId}_整改单.html`, "text/html;charset=utf-8");
}

export function exportRectificationReply(hazard: Hazard) {
  saveBlob(reportDocument(hazard, "reply"), `${hazard.orderId}_整改回复单.html`, "text/html;charset=utf-8");
}
