<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../lib/api";
import { core, useAction, nameOf } from "../lib/state";
import { date, money } from "../lib/format";
import { useDraft } from "../lib/drafts";
import { confirmAction } from "../lib/confirm";
import SaveBar from "./SaveBar.vue";
import SettingSwitch from "./SettingSwitch.vue";
import Overlay from "./Overlay.vue";
const route = useRoute(),
  router = useRouter(),
  { busy, error, action } = useAction();
const rows = ref<any[]>([]),
  config = ref<any>(null),
  usage = ref<any>({}),
  renderer = ref<any>({});
const detail = ref<any>(null),
  showSettings = ref(false),
  filter = ref(""),
  scope = ref(String(route.query.scope || ""));
const active = new Set([
  "queued",
  "generating",
  "rendering",
  "checking",
  "ready",
  "sending",
]);
const labels: Record<string, string> = {
  queued: "排队中",
  generating: "编写作品",
  rendering: "渲染中",
  checking: "检查画面",
  ready: "待发送",
  sending: "发送中",
  sent: "已发送",
  failed: "失败",
  unknown: "发送结果未知",
  cancelled: "已取消",
  interrupted: "已中断",
};
const fields = [
  ["context_tokens", "制作上下文总预算", 4096, 1000000],
  ["retention_days", "未收藏作品保存天数", 1, 30],
  ["storage_mib", "作品总配额（MiB）", 192, 10240],
  ["favorites_mib", "收藏配额（MiB）", 0, 4096],
  ["queue_limit", "等待队列容量", 1, 8],
  ["queue_seconds", "最长排队时间（秒）", 30, 900],
  ["run_seconds", "最长制作时间（秒）", 30, 900],
  ["request_hourly", "每群每小时请求次数", 1, 120],
  ["proactive_hourly", "每群每小时主动制作次数", 0, 12],
  ["proactive_cooldown", "主动制作间隔（秒）", 60, 86400],
] as const;
const draft = useDraft(
  () => config.value,
  save,
  (v) => (config.value = v),
);
const visible = computed(() =>
  rows.value.filter((r) => !filter.value || r.status === filter.value),
);
const mib = (n?: number) => ((n || 0) / 1048576).toFixed(1);
const url = (a: any) =>
  "/api/v1/creations/" + detail.value.id + "/artifacts/" + a.id;
let timer: ReturnType<typeof setInterval> | undefined,
  disposed = false;
async function load() {
  if (disposed) return;
  const values = await api<any[]>(
    "/creations" +
      (scope.value ? "?scope=" + encodeURIComponent(scope.value) : ""),
  );
  if (!disposed) rows.value = values;
  if (detail.value && active.has(detail.value.status)) {
    const id = detail.value.id,
      value = await api<any>("/creations/" + id);
    if (!disposed && detail.value?.id === id) detail.value = value;
  }
}
async function loadSettings() {
  const r = await api<any>("/creations/settings");
  config.value = r.config;
  usage.value = r.usage;
  renderer.value = r.renderer;
  draft.accept();
}
async function save() {
  if (!config.value) return false;
  return action(async () => {
    config.value = await api(
      "/creations/settings?expected_revision=" + config.value.revision,
      "PUT",
      config.value,
    );
    draft.accept();
  }, "制作设置已保存");
}
async function open(id: string) {
  await action(async () => {
    detail.value = await api("/creations/" + id);
    router.replace({ query: { ...route.query, creation: id } });
  }, "");
}
function close() {
  detail.value = null;
  router.replace({ query: { ...route.query, creation: undefined } });
}
async function cancel(row: any) {
  await action(async () => {
    await api("/creations/" + row.id + "/cancel", "POST", {});
    await load();
    if (detail.value?.id === row.id)
      detail.value = await api("/creations/" + row.id);
  }, "已取消制作");
}
async function favorite(row: any) {
  await action(async () => {
    detail.value = await api("/creations/" + row.id + "/favorite", "PUT", {
      favorite: !row.favorite,
    });
    await load();
  }, "收藏状态已更新");
}
async function remove(row: any) {
  if (
    !(await confirmAction(
      "清理作品",
      "将删除源文件和产物，正在制作的任务也会取消；已经发到 QQ 的消息不会被撤回。",
      "清理",
    ))
  )
    return;
  await action(async () => {
    await api("/creations/" + row.id, "DELETE");
    close();
    await load();
  }, "作品已清理");
}
onMounted(async () => {
  await action(async () => {
    await Promise.all([load(), loadSettings()]);
    if (route.query.creation) await open(String(route.query.creation));
  }, "");
  if (!disposed)
    timer = setInterval(() => {
      if (!document.hidden) load().catch((e) => (error.value = e.message));
    }, 3000);
});
onUnmounted(() => {
  disposed = true;
  if (timer) clearInterval(timer);
});
</script>
<template>
  <div class="toolbar">
    <select v-model="scope" aria-label="作品会话范围" @change="load">
      <option value="">全部会话</option>
      <option v-for="g in core.groups" :key="g.id" :value="g.id">
        {{ g.name }}
      </option>
    </select>
    <select v-model="filter" aria-label="作品状态">
      <option value="">全部状态</option>
      <option v-for="(label, key) in labels" :key="key" :value="key">
        {{ label }}
      </option>
    </select>
    <button class="secondary" @click="load">刷新作品</button>
    <button class="secondary" @click="showSettings = !showSettings">
      {{ showSettings ? "收起制作设置" : "制作设置" }}
    </button>
  </div>
  <p v-if="error" role="alert" class="error">{{ error }}</p>
  <section v-if="showSettings && config" class="panel creation-settings">
    <h3>制作环境与额度</h3>
    <p>
      {{ renderer.ready ? "渲染服务已连接" : "渲染服务未连接" }} · 全局同时制作
      1 个 · 每任务最高 2 GiB / 2 核 CPU
    </p>
    <p>
      已使用 {{ mib(usage.stored_bytes) }} MiB；任务预留
      {{ mib(usage.reserved_bytes) }} MiB；收藏
      {{
        mib(usage.favorite_bytes)
      }}
      MiB。临时帧结束即清理，不自动删除未过期作品。
    </p>
    <form id="creation-settings" @submit.prevent="save">
      <SettingSwitch
        v-model="config.enabled"
        label="启用作品制作"
        description="还需在各群权限中心开启制作工具，并配置普通成员、管理员和自主使用权限。"
      />
      <div class="creation-fields">
        <label v-for="[key, label, min, max] in fields" :key="key"
          >{{ label
          }}<input
            v-model.number="config[key]"
            type="number"
            :min="min"
            :max="max"
            step="1"
            required
        /></label>
      </div>
      <button class="primary" :disabled="busy" type="submit">
        保存制作设置
      </button>
    </form>
    <SaveBar
      :dirty="draft.dirty.value"
      :changes="draft.changes.value"
      :busy="busy"
      :error="error"
      @save="save"
      @reset="draft.reset"
    />
  </section>
  <p v-if="!visible.length" class="muted">
    当前没有作品。群聊中的制作请求会在这里显示真实进度。
  </p>
  <div class="creation-list">
    <article v-for="row in visible" :key="row.id" class="panel creation-row">
      <div>
        <strong>{{ row.title }}</strong>
        <p class="muted">
          {{ nameOf(row.scope) }} · {{ date(row.created_at) }} ·
          {{ row.format.toUpperCase() }}
        </p>
        <p>
          {{ labels[row.status] || row.status
          }}<span v-if="row.favorite"> · 已收藏</span
          ><span v-if="row.private"> · 临时任务</span>
        </p>
        <p v-if="row.error">{{ row.error }}</p>
        <p v-if="row.status === 'queued' && row.waiting_reason" class="muted">{{ row.waiting_reason }}</p>
        <p v-if="row.cleanup_reason" class="muted">{{ row.cleanup_reason }}</p>
      </div>
      <div class="creation-actions">
        <button class="secondary" @click="open(row.id)">查看作品</button
        ><button
          v-if="active.has(row.status)"
          class="secondary"
          :disabled="busy"
          @click="cancel(row)"
        >
          取消制作
        </button>
      </div>
    </article>
  </div>
  <Overlay v-if="detail" title="作品制作详情" @close="close">
    <h3>{{ detail.title }}</h3>
    <p>
      {{ labels[detail.status] || detail.status }} ·
      {{ detail.model_calls }} 次制作模型调用 · 估算
      {{ money(detail.cost || 0) }} {{ detail.currency }}
    </p>
    <p v-if="detail.metrics">
      渲染 {{ detail.metrics.duration_seconds }} 秒，内存峰值
      {{ mib(detail.metrics.peak_memory_bytes) }} MiB
    </p>
    <p v-if="detail.error" role="alert">{{ detail.error }}</p>
    <p v-if="detail.cancel_source" class="muted">取消来源：{{ ({ requester: '请求者', administrator: '后台管理员', system: '系统' } as Record<string, string>)[detail.cancel_source] || detail.cancel_source }}</p>
    <p v-if="detail.waiting_reason" class="muted">等待原因：{{ detail.waiting_reason }}</p>
    <p v-if="detail.call_counts" class="muted">
      <span v-if="detail.call_counts['creation.plan']">蓝图 {{ detail.call_counts['creation.plan'] }} 次，</span>
      编写与素材查询 {{ detail.call_counts['creation.code'] || 0 }} 次，画面检查
      {{ detail.call_counts['creation.review'] || 0 }} 次，已修补 {{ detail.repairs || 0 }} 轮。
    </p>
    <details v-if="detail.source?.design || detail.source?.reviews?.length">
      <summary>构图方案与逐轮检查</summary>
      <pre class="creation-source">{{ JSON.stringify({ design: detail.source.design, reviews: detail.source.reviews }, null, 2) }}</pre>
    </details>
    <div
      v-for="a in detail.artifacts.filter((a: any) => a.primary)"
      :key="a.id"
      class="creation-preview"
    >
      <video
        v-if="a.mime === 'video/mp4'"
        :src="url(a)"
        controls
        muted
        playsinline
        preload="metadata"
      ></video>
      <img v-else :src="url(a)" :alt="detail.title" loading="lazy" />
      <p>
        {{ a.width }} × {{ a.height }} · {{ mib(a.bytes) }} MiB ·
        <a :href="url(a) + '?download=true'">下载产物</a>
      </p>
    </div>
    <details v-if="detail.source">
      <summary>制作资料与源码（仅文本）</summary>
      <pre class="creation-source">{{
        JSON.stringify(detail.source, null, 2)
      }}</pre>
      <a :href="'/api/v1/creations/' + detail.id + '/source'">下载源文件</a>
    </details>
    <p class="muted">
      来源：{{
        detail.source_ids?.join("、") || "内容已清理或未保存"
      }}。费用按后台费率估算。
    </p>
    <template #footer>
      <button
        v-if="
          detail.status === 'sent' && !detail.private && !detail.cleanup_reason
        "
        class="secondary"
        :disabled="busy"
        @click="favorite(detail)"
      >
        {{ detail.favorite ? "取消收藏" : "收藏作品" }}
      </button>
      <button
        v-if="active.has(detail.status)"
        class="secondary"
        :disabled="busy"
        @click="cancel(detail)"
      >
        取消制作
      </button>
      <button class="secondary" :disabled="busy" @click="remove(detail)">
        清理作品</button
      ><button class="primary" @click="close">关闭</button>
    </template>
  </Overlay>
</template>
<style scoped>
.creation-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin: 20px 0;
}
.creation-fields label {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.creation-list {
  display: grid;
  gap: 14px;
}
.creation-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 20px;
}
.creation-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.creation-preview {
  margin: 16px 0;
}
.creation-preview img,
.creation-preview video {
  max-width: 100%;
  max-height: 65vh;
  display: block;
  object-fit: contain;
}
.creation-source {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  max-height: 400px;
  overflow: auto;
}
.creation-settings {
  padding: 20px;
  margin-bottom: 20px;
}
@media (max-width: 600px) {
  .creation-row {
    flex-direction: column;
  }
  .creation-fields {
    grid-template-columns: 1fr;
  }
}
</style>
