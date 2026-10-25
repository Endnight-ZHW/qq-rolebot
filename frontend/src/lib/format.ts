export const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));
export const date = (ts?: number) =>
  ts
    ? new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        dateStyle: "short",
        timeStyle: "medium",
      }).format(ts * 1000)
    : "—";
export const number = (n?: number | null) =>
  n == null
    ? "—"
    : new Intl.NumberFormat("zh-CN", {
        maximumFractionDigits: 1,
        notation: n >= 100000 ? "compact" : "standard",
      }).format(n);
export const money = (n: number) =>
  n.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
export const duration = (ms?: number | null) =>
  ms == null
    ? "—"
    : ms >= 1000
      ? (ms / 1000).toFixed(2) + " s"
      : Math.round(ms) + " ms";
export const splitIds = (s: string) => [
  ...new Set(s.split(/[,，\s]+/).filter(Boolean)),
];
export const format = (v: unknown) => JSON.stringify(v, null, 2);
export const labels: Record<string, string> = {
  "participation.gate": "轻量参与判定",
  "participation.sample": "旁听概率抽取",
  "conversation.agent": "统一回复与工具",
  compaction: "历史压缩",
  "legacy.unknown": "历史记录（阶段未知）",
  "decision.main": "旧版完整决策",
  "reply.main": "旧版回复生成",
  extract: "事实记忆提取",
  summary: "旧版摘要",
  "task.execute": "定时生成任务",
  success: "成功",
  running: "处理中",
  pending: "待执行",
  paused: "已暂停",
  sent: "已送达",
  sending: "发送中",
  complete: "完成",
  silent: "保持沉默",
  error: "失败",
  failed: "失败",
  http_error: "接口错误",
  network_error: "网络异常",
  invalid_response: "返回格式异常",
  not_sent: "未发送",
  cancelled: "已取消",
  interrupted: "已中断",
  unknown: "结果未知",
  missed: "已错过",
  retry: "等待重试",
  approved: "已通过",
  rejected: "已拒绝",
  chat: "聊天",
  decision: "接话决策",
  memory: "记忆整理",
  creation: "作品制作",
  "creation.code": "编写作品",
  "creation.review": "检查作品",
  search: "联网搜索",
  fetch: "网页读取",
  replay: "副本试跑",
  diagnostic: "诊断",
  active: "有效",
  archived: "已归档",
  unverified: "待核实",
};
export const label = (s: string) => labels[s] || s;
export function download(name: string, data: unknown) {
  const url = URL.createObjectURL(
    new Blob([format(data)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
