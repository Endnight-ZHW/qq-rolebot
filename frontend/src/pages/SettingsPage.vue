<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { useRoute } from "vue-router";
import { changeLabel } from "../lib/change-labels";
import { api, session } from "../lib/api";
import { useAction } from "../lib/state";
import { splitIds, clone } from "../lib/format";
import { useDraft, changesBetween } from "../lib/drafts";
import { configuration } from "../lib/config";
import SaveBar from "../components/SaveBar.vue";
import SettingSwitch from "../components/SettingSwitch.vue";
import NumberField from "../components/NumberField.vue";
const route = useRoute(),
  section = computed(() => String(route.query.section || "general"));
const config = ref<any>(null),
  saved = ref<any>(null),
  adminQQ = ref(""),
  search = ref(""),
  conflict = ref<any>(null),
  formEl = ref<HTMLFormElement>();
const password = ref({ current_password: "", new_password: "" }),
  { busy, error, action } = useAction();
const sections = [
  ["general", "基础信息"],
  ["context", "上下文与记忆"],
  ["images", "图片预算"],
  ["retention", "数据保留"],
  ["pricing", "费用"],
  ["security", "安全与备份"],
];
const fields: Record<string, string[]> = {
  context: [
    "context_tokens",
    "gate_max_messages",
    "gate_context_budget",
    "working_initial_messages",
    "working_max_messages",
    "working_context_budget",
    "learning_idle_seconds",
    "learning_max_seconds",
  ],
  images: ["image_max_count", "image_input_budget", "image_request_mib"],
  retention: [
    "retention_days",
    "media_hours",
    "model_log_days",
    "model_log_image_hours",
  ],
};
const prices: Record<string, string> = {
  input: "输入 / 百万 token",
  cached: "缓存输入 / 百万 token",
  output: "输出 / 百万 token",
  search: "每次搜索",
  fetch: "每次网页读取",
};
const shown = computed(() =>
  (search.value
    ? Object.values(fields).flat()
    : fields[section.value] || []
  ).filter(
    (k) =>
      !search.value ||
      (configuration.fields["global." + k]?.label + k).includes(search.value),
  ),
);
const draft = useDraft(
  () => ({ config: config.value, adminQQ: adminQQ.value }),
  save,
  (v) => {
    config.value = v.config;
    adminQQ.value = v.adminQQ;
  },
);
const passDraft = useDraft(
  () => password.value,
  changePassword,
  (v) => (password.value = v),
);
passDraft.accept();
const conflictRows = computed(() =>
  conflict.value ? changesBetween(saved.value, conflict.value) : [],
);
function accept(value: any) {
  config.value = value;
  saved.value = clone(value);
  adminQQ.value = value.admin_qq.join(", ");
  draft.accept();
}
async function load() {
  await action(async () => {
    accept(await api("/settings"));
  }, "");
}
async function save() {
  if (!formEl.value?.reportValidity()) return false;
  const ids = splitIds(adminQQ.value);
  if (!ids.every((x) => /^\d{5,20}$/.test(x))) {
    error.value = "管理员 QQ 应为 5 至 20 位数字";
    return false;
  }
  return action(async () => {
    const values = { ...config.value, admin_qq: ids };
    const body = Object.fromEntries(
      Object.entries(values).filter(
        ([k, v]) =>
          !["initialized", "api_key_configured", "revision"].includes(k) &&
          JSON.stringify(v) !== JSON.stringify(saved.value[k]),
      ),
    );
    try {
      accept(
        await api(
          "/settings?expected_revision=" + (saved.value.revision || 0),
          "PUT",
          body,
        ),
      );
      conflict.value = null;
    } catch (e: any) {
      if (e.status === 409) conflict.value = await api("/settings");
      throw e;
    }
  }, "系统设置已保存");
}
async function changePassword() {
  if (
    !password.value.current_password ||
    password.value.new_password.length < 8
  ) {
    error.value = "请填写当前密码，新密码至少 8 位";
    return false;
  }
  return action(async () => {
    await api("/auth/password", "PUT", password.value);
    password.value = { current_password: "", new_password: "" };
    passDraft.accept();
    session.authenticated = false;
    session.csrf = "";
  }, "密码已更新，请重新登录");
}
onMounted(load);
</script>
<template>
  <div class="workspace-toolbar">
    <span class="scope-chip">全局配置 · 所有会话</span
    ><span class="muted">参数建议与边界由服务器统一提供</span>
  </div>
  <p v-if="error" role="alert" class="error-banner">
    {{ error }}
    <button v-if="!config" class="secondary" @click="load">重试</button>
  </p>
  <div v-if="conflict" class="conflict-panel">
    <strong>服务器设置已变化，当前草稿已保留</strong>
    <p v-for="c in conflictRows" :key="c.path">
      {{ changeLabel(c.path) }}：{{ c.before }} → {{ c.after }}
    </p>
    <button
      class="secondary"
      @click="
        accept(conflict);
        conflict = null;
      "
    >
      放弃草稿并载入服务器版本
    </button>
  </div>
  <div v-if="config" class="settings-layout">
    <aside class="settings-navigation">
      <input
        v-model="search"
        aria-label="搜索系统设置"
        placeholder="搜索参数"
      /><RouterLink
        v-for="[id, label] in sections"
        :key="id"
        :class="{ active: section === id && !search }"
        :to="{ query: { section: id } }"
        @click="search = ''"
        >{{ label }}</RouterLink
      >
    </aside>
    <form
      v-if="section !== 'security' || search"
      ref="formEl"
      class="panel editor-panel"
      @submit.prevent="save"
    >
      <h3>
        {{ search ? "参数搜索" : sections.find((s) => s[0] === section)?.[1] }}
      </h3>
      <template v-if="section === 'general' && !search"
        ><label
          >机器人称呼<input
            v-model="config.nickname"
            maxlength="100"
            required /></label
        ><label
          >管理员 QQ 号<input
            v-model="adminQQ"
            placeholder="多个 QQ 号用逗号分隔"
            required
        /></label>
        <p class="field-hint">
          用于管理指令及群管理保护。QQ 授权在会话的权限中心配置。
        </p>
        <SettingSwitch
          v-model="config.private_enabled"
          :saved-value="saved.private_enabled"
          label="允许私聊互动"
          description="私聊与群聊记忆隔离；各会话仍有独立开关。"
        /><RouterLink class="link" to="/configuration/models"
          >配置模型与服务 →</RouterLink
        ></template
      >
      <div class="form-grid" v-if="shown.length">
        <NumberField
          v-for="key in shown"
          :key="key"
          :field="'global.' + key"
          v-model="config[key]"
        />
      </div>
      <p class="field-hint" v-if="section === 'context' && !search">
        预算是保守估算，实际请求受所用模型的上下文和输出容量约束。初始消息数量不能超过工作消息上限；等待时间不能超过整理间隔。
      </p>
      <p class="field-hint" v-if="section === 'images' && !search">
        包含角色参考图。图片数量、视觉预算和请求体共同限制输入，实际 token
        以供应商用量为准。单文件解码上限为 8 MiB。
      </p>
      <p class="field-hint" v-if="section === 'retention' && !search">
        缩短期限会清理已过期数据；延长期限无法恢复已经清理的记录和图片。所有保留设置一次保存。
      </p>
      <template v-if="section === 'pricing' && !search"
        ><p class="muted">
          估算费率仅用于统计，历史记录保留记账时的币种与金额。
        </p>
        <label
          >币种<select v-model="config.currency">
            <option>USD</option>
            <option>CNY</option>
          </select></label
        >
        <div class="form-grid">
          <label v-for="(title, key) in prices" :key="key"
            >{{ title
            }}<input
              v-model.number="config.prices[key]"
              type="number"
              min="0"
              max="10000"
              step="0.000001"
              required
          /></label></div
      ></template>
      <p v-if="search && !shown.length" class="empty">没有匹配的参数</p>
    </form>
    <div v-else class="settings-stack">
      <form class="panel editor-panel" @submit.prevent="changePassword">
        <h3>修改后台密码</h3>
        <label
          >当前密码<input
            v-model="password.current_password"
            type="password"
            autocomplete="current-password"
            required /></label
        ><label
          >新密码<input
            v-model="password.new_password"
            type="password"
            minlength="8"
            autocomplete="new-password"
            required
        /></label>
        <p class="field-hint">更新后所有后台会话需要重新登录。</p>
        <button class="primary" :disabled="busy">更新密码</button>
      </form>
      <article class="panel editor-panel">
        <h3>备份与恢复</h3>
        <p>自动快照保留最近 7 份。恢复方式见项目运维文档。</p>
        <button
          class="secondary"
          :disabled="busy"
          @click="
            action(() => api('/backup', 'POST'), '数据库备份已保存到服务器')
          "
        >
          立即备份数据库</button
        ><small>数据时区：Asia/Shanghai · 模型密钥仅在服务器保存</small>
      </article>
    </div>
  </div>
  <SaveBar
    :dirty="draft.dirty.value"
    :changes="draft.changes.value"
    :busy="busy"
    :error="error"
    @save="save"
    @reset="draft.reset"
  />
</template>
