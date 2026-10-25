import { configuration } from "./config";
import { core } from "./state";
const labels: Record<string, string> = {
  tools: "工具",
  permissions: "权限",
  members: "成员",
  roles: "权限组",
  name: "名称",
  enabled: "启用状态",
  user_enabled: "用户请求",
  autonomous_enabled: "自主使用",
  request_access: "授权模式与范围",
  member: "普通成员",
  admin: "管理员",
  allow: "允许的能力",
  deny: "明确禁止",
  inherit_qq_admin: "继承 QQ 管理指令",
  autonomous_rules: "自主群管规则",
  member_tools: "成员默认工具",
  protected_ids: "保护名单",
  tools_disabled: "禁止工具",
  muted: "暂停回应",
  expires_at: "授权有效期",
  interaction_enabled: "互动扩展",
  paused: "暂停会话",
  management_paused: "管理工具停用",
  segmented_reply: "分段回复",
  learning_enabled: "自动学习",
  persona: "角色",
  guidance: "群说明",
  nickname: "机器人称呼",
  private_enabled: "私聊互动",
  admin_qq: "管理员 QQ",
  adminQQ: "管理员 QQ",
  prices: "估算费率",
  currency: "币种",
  key: "密钥",
  keys: "密钥",
  clearKey: "清除密钥",
  clearKeys: "清除密钥",
  description: "简介",
  prompt: "角色设定",
  aliases: "别名",
  relationships: "人物关系",
  body: "提示词内容",
};
export function changeLabel(path: string) {
  return path
    .split(".")
    .filter((p) => !["config", "form"].includes(p))
    .map(
      (p) =>
        core.tools.find((t) => t.name === p)?.title ||
        labels[p] ||
        Object.values(configuration.fields).find((f) => f.key === p)?.label ||
        p,
    )
    .join(" / ");
}
