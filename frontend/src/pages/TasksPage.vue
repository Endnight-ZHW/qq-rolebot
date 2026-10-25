<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, query } from "../lib/api";
import { core, nameOf, useAction } from "../lib/state";
import { date, splitIds } from "../lib/format";
import { useDraft, allowLeave } from "../lib/drafts";
import { confirmAction } from "../lib/confirm";
import Overlay from "../components/Overlay.vue";
import StatusBadge from "../components/StatusBadge.vue";
import InteractionSettings from "../components/InteractionSettings.vue";
import CreationsPanel from "../components/CreationsPanel.vue";
import ArtLibraryPanel from "../components/ArtLibraryPanel.vue";
const route = useRoute(),
  router = useRouter(),
  scope = computed(() => String(route.query.scope || "")),
  status = ref(String(route.query.status || "")),
  search = ref(String(route.query.q || "")),
  rows = ref<any[]>([]),
  runs = ref<any[]>([]),
  loading = ref(true),
  editor = ref<any>(null),
  { busy, error, action } = useAction();
let generation = 0,
  runGeneration = 0;
const filtered = computed(() =>
  rows.value
    .filter(
      (r) =>
        (!status.value || r.status === status.value) &&
        (!search.value ||
          (r.text + " " + nameOf(r.scope)).includes(search.value)),
    )
    .sort((a, b) => a.due - b.due),
);
const selected = computed(() =>
  rows.value.find((r) => r.id === route.query.task),
);
const draft = useDraft(
  () => editor.value,
  save,
  (v) => (editor.value = v),
);
async function load() {
  const ticket = ++generation;
  loading.value = true;
  try {
    const result = await api("/tasks?" + query({ scope: scope.value }));
    if (ticket === generation) {
      rows.value = result;
      error.value = "";
    }
  } catch (e: any) {
    if (ticket === generation) error.value = e.message;
  } finally {
    if (ticket === generation) loading.value = false;
  }
}
watch(
  scope,
  () => {
    rows.value = [];
    void load();
  },
  { immediate: true },
);
watch(
  () => route.query.task,
  async (id) => {
    const ticket = ++runGeneration;
    runs.value = [];
    if (id)
      try {
        const result = await api(
          "/tasks/" + encodeURIComponent(String(id)) + "/runs",
        );
        if (ticket === runGeneration) runs.value = result;
      } catch (e: any) {
        if (ticket === runGeneration) error.value = e.message;
      }
  },
  { immediate: true },
);
onUnmounted(() => {
  generation++;
  runGeneration++;
});
function changeScope(event: Event) {
  const input = event.target as HTMLSelectElement,
    next = input.value;
  input.value = scope.value;
  filter({ scope: next || undefined });
}
function filter(values: any = {}) {
  router.replace({
    query: {
      ...route.query,
      status: status.value || undefined,
      q: search.value || undefined,
      task: undefined,
      ...values,
    },
  });
}
async function open(item?: any) {
  if (!(await allowLeave())) return;
  editor.value = item
    ? {
        id: item.id,
        scope: item.scope,
        text: item.text,
        timing: "keep",
        seconds: 60,
        daily_time: "20:00",
        interval: item.interval_seconds,
      }
    : {
        scope:
          scope.value ||
          core.groups.find((g) => g.enabled && g.interaction_enabled)?.id ||
          core.groups[0]?.id ||
          "",
        text: "",
        mode: "message",
        timing: "once",
        seconds: 60,
        daily_time: "20:00",
        mentions: "",
      };
  draft.accept();
}
async function close() {
  if (!(await allowLeave())) return;
  editor.value = null;
  draft.accept();
}
const preview = computed(() => {
  const e = editor.value;
  if (!e) return "";
  if (e.timing === "keep") return "保持现有时间与重复规则";
  if (e.timing === "daily")
    return "每天 " + e.daily_time + "（北京时间），从下一个时刻开始";
  return (
    "预计 " +
    date(Date.now() / 1000 + Number(e.seconds)) +
    (e.timing === "interval"
      ? "，之后每 " + e.seconds + " 秒一次"
      : e.interval
        ? "，保留原固定间隔重复规则"
        : "")
  );
});
async function save() {
  const e = editor.value;
  if (!e?.text?.trim() || !e.scope) {
    error.value = "请选择会话并填写任务内容";
    return false;
  }
  return await action(
    async () => {
      if (e.id) {
        const body: any = { id: e.id, action: "edit", text: e.text };
        if (e.timing === "daily") body.daily_time = e.daily_time;
        else if (e.timing !== "keep") body.delay_seconds = Number(e.seconds);
        await api("/tasks/" + e.id, "PUT", body);
      } else {
        const body: any = {
          scope: e.scope,
          text: e.text,
          mode: e.mode,
          mentions: splitIds(e.mentions),
        };
        if (e.timing === "daily") body.daily_time = e.daily_time;
        else {
          body.delay_seconds = Number(e.seconds);
          if (e.timing === "interval")
            body.interval_seconds = Number(e.seconds);
        }
        await api("/tasks", "POST", body);
      }
      editor.value = null;
      draft.accept();
      await load();
    },
    e.id ? "任务已更新" : "任务已创建",
  );
}
async function change(r: any, actionName: string) {
  if (
    actionName === "cancel" &&
    !(await confirmAction(
      "取消任务",
      "将取消「" + r.text.slice(0, 80) + "」后续执行，已送出的消息不会撤回。",
      "取消任务",
    ))
  )
    return;
  await action(
    async () => {
      if (r.version === 2)
        await api("/tasks/" + r.id, "PUT", { id: r.id, action: actionName });
      else await api("/reminders/" + r.id, "DELETE");
      await load();
    },
    actionName === "pause"
      ? "任务已暂停"
      : actionName === "resume"
        ? "任务已恢复"
        : "任务已取消",
  );
}
</script>
<template>
  <nav class="section-tabs" aria-label="任务中心分类">
    <RouterLink
      :to="{ path: '/tasks', query: { scope: route.query.scope } }"
      :class="{ active: !['creations','assets'].includes(String(route.query.view || '')) }"
      >定时任务</RouterLink
    >
    <RouterLink
      :to="{
        path: '/tasks',
        query: { view: 'creations', scope: route.query.scope },
      }"
      :class="{ active: route.query.view === 'creations' }"
      >作品制作</RouterLink
    >
    <RouterLink :to="{path:'/tasks',query:{view:'assets'}}" :class="{active:route.query.view==='assets'}">美术素材</RouterLink>
  </nav>
  <ArtLibraryPanel v-if="route.query.view === 'assets'" />
  <CreationsPanel v-else-if="route.query.view === 'creations'" />
  <template v-else>
    <div class="toolbar">
      <select :value="scope" aria-label="任务会话范围" @change="changeScope">
        <option value="">全部会话</option>
        <option v-for="g in core.groups" :key="g.id" :value="g.id">
          {{ g.name }}
        </option></select
      ><input
        v-model="search"
        aria-label="搜索任务"
        placeholder="搜索任务内容"
        @input="filter()"
      /><select v-model="status" aria-label="任务状态" @change="filter()">
        <option value="">全部状态</option>
        <option
          v-for="s in [
            'pending',
            'paused',
            'running',
            'sent',
            'failed',
            'unknown',
            'missed',
            'cancelled',
          ]"
          :key="s"
          :value="s"
        >
          {{
            (
              {
                pending: "待执行",
                paused: "已暂停",
                running: "执行中",
                sent: "已送达",
                failed: "失败",
                unknown: "结果未知",
                missed: "已错过",
                cancelled: "已取消",
              } as any
            )[s]
          }}
        </option></select
      ><button class="secondary" :disabled="loading" @click="load">刷新</button
      ><button class="primary" :disabled="!core.groups.length" @click="open()">
        ＋ 创建任务
      </button>
    </div>
    <InteractionSettings
      v-if="scope && core.groups.some((g) => g.id === scope)"
      :key="scope"
      :scope="scope"
    />
    <p v-if="error && !editor" class="error-text" role="alert">{{ error }}</p>
    <div v-if="loading" class="skeleton panel"></div>
    <div v-else class="task-list">
      <article v-for="r in filtered" :key="r.id" class="panel task-card">
        <div class="section-heading">
          <span class="tag">{{ nameOf(r.scope) }}</span
          ><StatusBadge :status="r.status" />
        </div>
        <h3>{{ r.text }}</h3>
        <p class="muted">
          {{ date(r.due) }} ·
          {{
            r.daily_time
              ? "每天 " + r.daily_time
              : r.interval_seconds
                ? "每 " + r.interval_seconds + " 秒"
                : "一次"
          }}
          · {{ r.mode === "model" ? "查询 / 生成" : "直接发送" }}
        </p>
        <p v-if="r.mentions?.length" class="muted">
          @ {{ r.mentions.join("、") }}
        </p>
        <div class="card-actions">
          <RouterLink
            class="link"
            :to="{ query: { ...route.query, task: r.id } }"
            >执行记录 →</RouterLink
          ><template v-if="r.version === 2"
            ><button
              v-if="['pending', 'paused'].includes(r.status)"
              class="quiet"
              :disabled="busy"
              @click="open(r)"
            >
              编辑</button
            ><button
              v-if="r.status === 'pending'"
              class="quiet"
              :disabled="busy"
              @click="change(r, 'pause')"
            >
              暂停</button
            ><button
              v-if="r.status === 'paused'"
              class="quiet"
              :disabled="busy"
              @click="change(r, 'resume')"
            >
              恢复</button
            ><button
              v-if="['pending', 'paused', 'running'].includes(r.status)"
              class="quiet danger"
              :disabled="busy"
              @click="change(r, 'cancel')"
            >
              取消
            </button></template
          ><button
            v-else-if="r.status === 'pending'"
            class="quiet danger"
            @click="change(r, 'cancel')"
          >
            取消
          </button>
        </div>
      </article>
    </div>
    <div v-if="!loading && !filtered.length" class="empty panel">
      <h3>这里还没有任务</h3>
      <p>安排一次提醒，或定时查询并发送摘要。</p>
      <button class="primary" :disabled="!core.groups.length" @click="open()">
        创建任务
      </button>
    </div>
    <Overlay
      v-if="selected && !editor"
      title="任务执行记录"
      drawer
      @close="router.replace({ query: { ...route.query, task: undefined } })"
      ><h3>{{ selected.text }}</h3>
      <p>{{ nameOf(selected.scope) }} · 创建者 {{ selected.creator }}</p>
      <StatusBadge :status="selected.status" />
      <div v-if="!runs.length" class="empty compact">尚无执行记录</div>
      <article v-for="run in runs" :key="run.id" class="run-card">
        <div class="section-heading">
          <strong>{{ date(run.started_at) }}</strong
          ><StatusBadge :status="run.status" />
        </div>
        <p v-if="run.error" class="error-text">{{ run.error }}</p>
        <p v-if="run.message_id" class="muted">QQ 消息 {{ run.message_id }}</p>
        <RouterLink
          v-if="run.trace_id"
          class="link"
          :to="{
            path: '/observe/traces',
            query: { scope: selected.scope, trace: run.trace_id },
          }"
          >查看关联请求 →</RouterLink
        >
      </article></Overlay
    >
    <Overlay
      v-if="editor"
      :title="editor.id ? '编辑任务' : '创建任务'"
      drawer
      :busy="busy"
      @close="close"
      ><form id="task-form" @submit.prevent="save">
        <label
          >执行会话<select
            v-model="editor.scope"
            required
            :disabled="!!editor.id"
          >
            <option v-for="g in core.groups" :key="g.id" :value="g.id">
              {{ g.name }}
            </option>
          </select></label
        >
        <p
          v-if="
            !core.groups.find((g) => g.id === editor.scope)?.interaction_enabled
          "
          class="notice-banner"
        >
          请先关闭此窗口，选择该会话并开启“本群互动扩展”。
        </p>
        <label
          >任务内容<textarea
            v-model="editor.text"
            required
            maxlength="2000"
            rows="5"
            placeholder="例如：提醒喝水，或搜索今日游戏更新并整理摘要"
          /></label
        ><label v-if="!editor.id"
          >任务方式<select v-model="editor.mode">
            <option value="message">直接发送</option>
            <option value="model">到时查询 / 生成</option>
          </select></label
        >
        <div class="form-grid">
          <label
            >执行时间<select v-model="editor.timing">
              <option v-if="editor.id" value="keep">保持原时间</option>
              <option value="once">从现在起</option>
              <option value="daily">每天时刻</option>
              <option v-if="!editor.id" value="interval">固定间隔</option>
            </select></label
          ><label v-if="editor.timing === 'daily'"
            >北京时间<input
              v-model="editor.daily_time"
              type="time"
              required /></label
          ><label v-else-if="editor.timing !== 'keep'"
            >{{ editor.timing === "interval" ? "间隔秒数" : "多少秒后执行"
            }}<input
              v-model.number="editor.seconds"
              type="number"
              :min="editor.timing === 'interval' ? 60 : 5"
              max="31536000"
              required
          /></label>
        </div>
        <label v-if="!editor.id"
          >@ QQ（可选，最多三位）<input
            v-model="editor.mentions"
            placeholder="用逗号分隔 QQ 号"
        /></label>
        <div class="schedule-preview">
          <strong>执行时间预览</strong>
          <p>{{ preview }}</p>
        </div>
        <p class="field-hint">
          查询任务可读取资料、生成内容和发送消息；未知结果不会自动重试。实际完成时间受网络和模型耗时影响。
        </p>
        <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      </form>
      <template #footer
        ><button class="secondary" :disabled="busy" @click="close">取消</button
        ><button
          class="primary"
          form="task-form"
          :disabled="
            busy ||
            !core.groups.find((g) => g.id === editor.scope)?.interaction_enabled
          "
        >
          {{ editor.id ? "保存任务" : "创建任务" }}
        </button></template
      ></Overlay
    >
  </template>
</template>
