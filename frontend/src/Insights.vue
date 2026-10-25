<script setup lang="ts">
import { ref, watch, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import RecordFilters from "./components/RecordFilters.vue";
import Pagination from "./components/Pagination.vue";
import Overlay from "./components/Overlay.vue";
import SaveBar from "./components/SaveBar.vue";
import { useDraft, allowLeave } from "./lib/drafts";
const route = useRoute(),
  router = useRouter();
import MemoryDetails from "./MemoryDetails.vue";
const props = defineProps<{
  view: string;
  scope: string;
  persona: string;
  api: (path: string, method?: string, body?: any) => Promise<any>;
}>();
const emit = defineEmits<{ openCall: [string]; openCallTrace: [string] }>();
const profiles = ref<any[]>([]),
  styles = ref<any[]>([]),
  traces = ref<any[]>([]),
  jobs = ref<any[]>([]);
const selectedTrace = ref<any>(null),
  styleVersions = ref<Record<string, any[]>>({}),
  aliasInputs = ref<Record<string, string>>({});
const query = ref(""),
  search = ref<any>(null),
  replay = ref<any>(null),
  message = ref(""),
  failed = ref(false),
  busy = ref(false);
const mode = ref("profiles");
let generation = 0,
  detailGeneration = 0;
const traceStatus = ref(String(route.query.status || "")),
  traceKind = ref(String(route.query.kind || ""));
const draft = useDraft(
  () => ({ aliases: aliasInputs.value, styles: styles.value }),
  saveAll,
  (v) => {
    aliasInputs.value = v.aliases;
    styles.value = v.styles;
  },
);
async function saveAll() {
  return await run(async () => {
    for (const p of profiles.value)
      if (p.remember)
        await props.api(
          `/profiles/${props.scope}/${p.user_id}/aliases`,
          "PUT",
          {
            aliases: (aliasInputs.value[p.user_id] || "")
              .split(/[,，\n]/)
              .map((s) => s.trim())
              .filter(Boolean),
          },
        );
    for (let i = 0; i < styles.value.length; i++) {
      const s = styles.value[i];
      styles.value[i] = await props.api(
        `/styles/${s.id}?${scopeQuery()}`,
        "PUT",
        { meaning: s.meaning, usage: s.usage, enabled: s.enabled },
      );
    }
    draft.accept();
  }, "成员与学习修改已保存");
}
onUnmounted(() => {
  generation++;
  detailGeneration++;
});
function traceFilters() {
  router.replace({
    query: {
      ...route.query,
      status: traceStatus.value || undefined,
      kind: traceKind.value || undefined,
      page: undefined,
      trace: undefined,
    },
  });
}
function navigateTrace(id: string) {
  router.push({ query: { ...route.query, trace: id } });
}
async function refresh() {
  if (await allowLeave()) await load();
}
const labels: Record<string, string> = {
  sent: "已发送",
  complete: "完成",
  silent: "保持沉默",
  pending: "待处理",
  running: "处理中",
  retry: "等待重试",
  failed: "失败",
  error: "错误",
  cancelled: "已取消",
  partial: "部分发送后停止",
  partial_unknown: "部分发送，后续结果未知",
  partial_failed: "部分发送，后续失败",
  unknown: "发送结果未知",
  interrupted: "发送已中断",
  rate_limited: "达到发送上限",
  delivery: "发送分段计划",
  delivery_wait: "段间停顿",
  delivery_end: "发送完成情况",
  segment_review: "插话复核",
  segments_merged: "按额度合并分段",
  stale: "已过时",
  duplicate: "重复已抑制",
  context_limit: "内容超预算",
  extract: "记忆与表达整理",
  summary: "摘要压缩",
  chat: "聊天",
  decision: "决策",
  memory: "记忆",
  creation: "作品制作",
  "creation.code": "编写作品",
  "creation.review": "检查作品",
  context: "上下文组装",
  context_after_memory: "更新记忆后的上下文",
  context_reply: "回复阶段上下文",
  "participation.sample": "旁听概率抽取",
  memory_input: "增量来源与辅助上下文",
  summary_items: "摘要条目与执行证据",
  tool: "工具执行",
  tool_source_review: "工具请求来源核对",
  tool_reply_corrected: "执行结果表述校正",
  review: "发送前复核",
  memory_result: "整理结果",
  memory_job: "关联记忆任务",
  suppressed: "取消发送",
  images: "图片观察",
  image_download_error: "图片下载诊断",
  image_error: "图片识别诊断",
  image_retry_error: "原图重取诊断",
};
const label = (value: string) => labels[value] || value;
const date = (ts: number) =>
  ts
    ? new Date(ts * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
    : "—";
const format = (value: any) => JSON.stringify(value, null, 2);
const scopeQuery = () => "scope=" + encodeURIComponent(props.scope);
async function run(fn: () => Promise<void>, success = "") {
  busy.value = true;
  failed.value = false;
  message.value = "";
  try {
    await fn();
    if (success) message.value = success;
    return true;
  } catch (e: any) {
    failed.value = true;
    message.value = e.message;
    return false;
  } finally {
    busy.value = false;
  }
}
async function load() {
  const ticket = ++generation;
  if (!props.scope && props.view === "profiles") return;
  await run(async () => {
    if (props.view === "profiles") {
      const result = await Promise.all([
        props.api(`/profiles?${scopeQuery()}&persona=${props.persona}`),
        props.api(`/styles?${scopeQuery()}`),
      ]);
      if (ticket !== generation) return;
      [profiles.value, styles.value] = result;
      aliasInputs.value = Object.fromEntries(
        profiles.value.map((p) => [p.user_id, (p.aliases || []).join("，")]),
      );
      draft.accept();
    } else if (props.view === "traces") {
      const q = new URLSearchParams({
        scope: props.scope,
        limit: "40",
        offset: String(Math.max(0, Number(route.query.page) || 0) * 40),
      });
      for (const key of ["start", "end", "status", "kind"])
        if (route.query[key]) q.set(key, String(route.query[key]));
      const result = await props.api("/traces?" + q);
      if (ticket === generation) traces.value = result;
    } else if (props.scope) {
      const result = await props.api(`/memory/jobs?${scopeQuery()}`);
      if (ticket === generation) jobs.value = result;
    }
  });
}
async function selectTrace(id: string) {
  const selectedScope = props.scope;
  await run(async () => {
    const ticket = ++detailGeneration;
    const result = await props.api("/traces/" + id);
    if (
      props.scope === selectedScope &&
      ticket === detailGeneration &&
      route.query.trace === id
    )
      selectedTrace.value = result;
  });
}
async function retryJob(id: string) {
  await run(async () => {
    await props.api("/memory/jobs/" + id + "/retry", "POST");
    jobs.value = await props.api(`/memory/jobs?${scopeQuery()}`);
  }, "已重新排入整理队列");
}
watch(
  () => [props.view, props.scope, props.persona],
  () => {
    selectedTrace.value = null;
    profiles.value = [];
    styles.value = [];
    traces.value = [];
    jobs.value = [];
    search.value = null;
    styleVersions.value = {};
    void load();
  },
  { immediate: true },
);
async function editAliases(p: any) {
  await run(async () => {
    await props.api(`/profiles/${props.scope}/${p.user_id}/aliases`, "PUT", {
      aliases: aliasInputs.value[p.user_id]
        .split(/[,，\n]/)
        .map((s) => s.trim())
        .filter(Boolean),
    });
  }, "成员称呼已保存");
  if (!failed.value)
    draft.acceptPart((previous) => ({
      ...previous,
      aliases: {
        ...previous.aliases,
        [p.user_id]: aliasInputs.value[p.user_id],
      },
    }));
}
async function saveStyle(s: any) {
  await run(async () => {
    const saved = await props.api(`/styles/${s.id}?${scopeQuery()}`, "PUT", {
      meaning: s.meaning,
      usage: s.usage,
      enabled: s.enabled,
    });
    const index = styles.value.findIndex((x) => x.id === s.id);
    if (index >= 0) styles.value[index] = saved;
    draft.acceptPart((previous) => ({
      ...previous,
      styles: previous.styles.map((x: any) => (x.id === s.id ? saved : x)),
    }));
  }, "学习记录已保存");
}
async function history(s: any) {
  await run(async () => {
    styleVersions.value[s.id] = await props.api(
      `/styles/${s.id}/versions?${scopeQuery()}`,
    );
  });
}
async function restoreStyle(s: any, v: any) {
  if (!(await allowLeave())) return;
  await run(async () => {
    await props.api(
      `/styles/${s.id}/restore/${v.version}?${scopeQuery()}`,
      "POST",
    );
  }, "已恢复为新版本");
  await load();
}
watch(
  () => [
    route.query.start,
    route.query.end,
    route.query.status,
    route.query.kind,
    route.query.page,
  ],
  () => {
    if (props.view === "traces") {
      traceStatus.value = String(route.query.status || "");
      traceKind.value = String(route.query.kind || "");
      void load();
    }
  },
);
watch(
  () => route.query.trace,
  (id) => {
    if (props.view === "traces") {
      selectedTrace.value = null;
      detailGeneration++;
      if (id) void selectTrace(String(id));
    }
  },
  { immediate: true },
);
</script>
<template>
  <div class="page-intro">
    <p>
      {{
        view === "profiles"
          ? "查看她记住了谁、学到了什么，以及这些内容来自哪里。"
          : view === "traces"
            ? "从触发消息到最终发送，查看每一步的依据和结果。"
            : "用同一批记录检查召回结果，再用固定样例验证关键行为。"
      }}
    </p>
    <button type="button" class="secondary" :disabled="busy" @click="refresh">
      刷新记录
    </button>
  </div>
  <p
    v-if="message"
    role="status"
    class="insight-feedback"
    :class="{ 'error-text': failed }"
  >
    {{ message }}
  </p>
  <article v-if="!scope && view === 'profiles'" class="panel empty-state">
    请先在上方选择一个群或会话。
  </article>
  <template v-else-if="view === 'profiles'">
    <div class="view-switch">
      <button
        :class="mode === 'profiles' ? 'primary' : 'secondary'"
        @click="mode = 'profiles'"
      >
        成员档案</button
      ><button
        :class="mode === 'styles' ? 'primary' : 'secondary'"
        @click="mode = 'styles'"
      >
        群风格学习
      </button>
    </div>
    <template v-if="mode === 'profiles'">
      <article class="panel" v-for="p in profiles" :key="p.user_id">
        <div class="section-heading">
          <h3>
            {{ p.name || p.user_id }}
            <small class="muted">QQ {{ p.user_id }}</small>
          </h3>
          <span class="tag">{{ p.remember ? "正在积累" : "已停止记忆" }}</span>
        </div>
        <form class="alias-form" @submit.prevent="editAliases(p)">
          <label
            >称呼与别名<input
              v-model="aliasInputs[p.user_id]"
              :disabled="!p.remember"
              placeholder="用逗号分隔，始终以 QQ 号绑定身份" /></label
          ><button class="secondary" :disabled="busy || !p.remember">
            保存称呼
          </button>
        </form>
        <div class="fact-card" v-for="m in p.facts" :key="m.id">
          <p>{{ m.text }}</p>
          <MemoryDetails
            :memory="m"
            :api="api"
            @changed="load"
            @open-call="emit('openCall', $event)"
          />
        </div>
        <p v-if="!p.facts.length" class="muted">
          暂无已确认的偏好、称呼或相处经历。
        </p>
        <details v-if="p.uncertain.length">
          <summary>待核实内容 · {{ p.uncertain.length }}</summary>
          <div class="fact-card" v-for="m in p.uncertain" :key="m.id">
            <p>{{ m.text }}</p>
            <MemoryDetails
              :memory="m"
              :api="api"
              @changed="load"
              @open-call="emit('openCall', $event)"
            />
          </div>
        </details>
      </article>
      <article class="panel empty-state" v-if="!profiles.length">
        还没有成员档案，接入后的群聊会逐渐积累。
      </article>
    </template>
    <template v-else>
      <p class="muted">
        自动启用需要至少 5
        条有效样例、覆盖两个对话片段。你可以人工修正或启用；学习内容不会改写核心人设与工具授权。
      </p>
      <form
        class="panel"
        v-for="s in styles"
        :key="s.id"
        @submit.prevent="saveStyle(s)"
      >
        <div class="section-heading">
          <h3>{{ s.expression }}</h3>
          <span class="tag"
            >{{ s.enabled ? "正在使用" : "未启用" }} · v{{ s.version }}</span
          >
        </div>
        <div class="form-grid">
          <label>表达含义<input v-model="s.meaning" maxlength="500" /></label
          ><label>使用场景<input v-model="s.usage" maxlength="500" /></label>
        </div>
        <div class="form-footer">
          <small
            >{{ s.source_ids.length }} 条样例 · {{ s.fragments }} 个片段 ·
            {{ s.eligible ? "达到自动启用条件" : "仍在积累样本" }}</small
          ><label class="switch-label"
            ><input type="checkbox" v-model="s.enabled" />应用这条表达</label
          ><button class="primary" :disabled="busy">保存学习记录</button>
        </div>
        <details>
          <summary>查看来源样例</summary>
          <blockquote v-for="e in s.examples" :key="e.source_id">
            {{ e.quote }}<small>QQ {{ e.actor }} · {{ e.source_id }}</small>
          </blockquote>
        </details>
        <button
          class="quiet"
          type="button"
          @click="history(s)"
          :disabled="busy"
        >
          查看学习版本
        </button>
        <div
          class="version-item"
          v-for="v in styleVersions[s.id] || []"
          :key="v.version"
        >
          <div>
            v{{ v.version }} · {{ date(v.updated_at) }}
            <p>{{ v.meaning }} / {{ v.usage }}</p>
          </div>
          <button
            class="secondary"
            type="button"
            @click="restoreStyle(s, v)"
            :disabled="busy || v.version === s.version"
          >
            恢复学习版本
          </button>
        </div>
      </form>
      <article class="panel empty-state" v-if="!styles.length">
        尚未形成表达候选。学习开启后会从有来源的群聊样例中积累。
      </article>
    </template>
  </template>
  <template v-else-if="view === 'traces'">
    <RecordFilters :busy="busy" @refresh="load"
      ><select
        v-model="traceStatus"
        aria-label="追踪状态"
        @change="traceFilters"
      >
        <option value="">全部状态</option>
        <option
          v-for="s in [
            'sent',
            'complete',
            'silent',
            'error',
            'running',
            'unknown',
            'interrupted',
          ]"
          :key="s"
          :value="s"
        >
          {{ label(s) }}
        </option></select
      ><select v-model="traceKind" aria-label="追踪类型" @change="traceFilters">
        <option value="">全部类型</option>
        <option value="chat">聊天</option>
        <option value="extract">记忆整理</option>
        <option value="scheduled_task">定时任务</option>
        <option value="diagnostic">诊断</option>
      </select></RecordFilters
    >
    <div class="trace-layout">
      <article class="panel trace-list">
        <button
          v-for="t in traces"
          :key="t.id"
          class="trace-item"
          :class="{ selected: selectedTrace?.id === t.id }"
          @click="navigateTrace(t.id)"
        >
          <strong>{{ label(t.kind) }} · {{ label(t.status) }}</strong
          ><small>{{ date(t.ts) }} · {{ t.duration_ms || 0 }} ms</small
          ><small
            >{{ t.models.length }} 次调用 ·
            {{
              t.redacted ? "内容已隐藏" : t.source_ids.length + " 条触发消息"
            }}</small
          >
        </button>
        <p v-if="!traces.length" class="muted">暂无请求记录。</p>
        <Pagination
          :offset="Math.max(0, Number(route.query.page) || 0) * 40"
          :size="40"
          :count="traces.length"
          :busy="busy"
          @change="
            router.replace({
              query: {
                ...route.query,
                page: $event / 40 || undefined,
                trace: undefined,
              },
            })
          "
        />
      </article>
      <Overlay
        v-if="selectedTrace"
        title="请求过程"
        drawer
        @close="router.replace({ query: { ...route.query, trace: undefined } })"
        ><article class="trace-detail">
          <div class="section-heading">
            <h3>请求过程</h3>
            <span class="tag">{{ label(selectedTrace.status) }}</span>
          </div>
          <p class="muted trace-id">{{ selectedTrace.id }}</p>
          <button
            class="secondary"
            @click="emit('openCallTrace', selectedTrace.id)"
          >
            查看本轮完整模型输入与返回
          </button>
          <button
            v-if="selectedTrace.parent_id"
            class="quiet"
            @click="navigateTrace(selectedTrace.parent_id)"
          >
            返回关联聊天请求
          </button>
          <p v-if="selectedTrace.redacted" class="muted">
            {{
              selectedTrace.notice || "本轮包含停止记忆的成员，只保留运行指标。"
            }}
          </p>
          <div
            class="usage-call"
            v-for="(m, i) in selectedTrace.models"
            :key="i"
          >
            <strong>{{ label(m.purpose) }} · {{ m.model }}</strong>
            <p>
              输入 {{ m.input_tokens }} / 缓存 {{ m.cached_tokens }} / 输出
              {{ m.output_tokens }} token
            </p>
            <small
              >请求估算 {{ m.estimated_tokens ?? "—" }} · {{ m.duration_ms }} ms
              · {{ m.currency }} {{ Number(m.cost).toFixed(6) }}</small
            >
          </div>
          <ol class="timeline">
            <li v-for="(step, i) in selectedTrace.stages" :key="i">
              <strong>{{ label(step.name) }}</strong
              ><small>{{ date(step.ts) }}</small
              ><button
                v-if="step.name === 'memory_job' && step.data.trace_id"
                class="quiet"
                @click="navigateTrace(step.data.trace_id)"
              >
                查看记忆模型调用</button
              ><template
                v-if="['context', 'context_after_memory'].includes(step.name)"
                ><div class="section-meters">
                  <div
                    v-for="section in step.data.sections || []"
                    :key="section.name"
                  >
                    <span>{{ section.name }}</span
                    ><strong>{{ section.estimated_tokens }}</strong>
                  </div>
                </div>
                <p class="muted">
                  估算 {{ step.data.estimated_tokens }} / 预算
                  {{ step.data.budget }} token
                  <template v-if="step.data.history_loaded != null">
                    · 读取 {{ step.data.history_loaded }} 条近期消息
                    <span v-if="step.data.history_limited"
                      >（已达预算或条数上限，可用历史工具补查）</span
                    >
                  </template>
                </p></template
              >
              <details>
                <summary>查看依据与结果</summary>
                <pre>{{ format(step.data) }}</pre>
              </details>
            </li>
          </ol>
        </article></Overlay
      >
      <article v-else class="panel empty-state">
        选择一次请求，查看上下文、接话判断与工具结果。
      </article>
    </div>
  </template>
  <template v-else-if="view === 'diagnostics'">
    <form
      class="panel"
      @submit.prevent="
        run(async () => {
          search = await api('/memory/search', 'POST', {
            scope,
            persona,
            query,
          });
        })
      "
    >
      <div class="section-heading">
        <h3>记忆检索测试</h3>
        <span class="tag">读取当前会话</span>
      </div>
      <label
        >测试问题<input
          v-model="query"
          required
          placeholder="例如：成员甲喜欢什么水果？"
          maxlength="1000" /></label
      ><button class="primary" :disabled="busy || !scope">查看检索结果</button>
      <p v-if="!scope" class="muted">选择会话后可检索该会话记忆。</p>
    </form>
    <div v-if="search">
      <article class="panel">
        <h3>当前排序</h3>
        <div class="fact-card" v-for="(m, i) in search.results" :key="m.id">
          <strong>#{{ Number(i) + 1 }}</strong>
          <p>{{ m.text }}</p>
          <small class="muted" v-if="m.retrieval"
            >综合分 {{ m.retrieval.score }} · 词覆盖
            {{ m.retrieval.lexical_coverage }} ·
            {{ m.retrieval.subject_match ? "关联当前成员" : "内容匹配" }}</small
          >
        </div>
        <p v-if="!search.results.length" class="muted">没有匹配结果。</p>
      </article>
    </div>
    <article class="panel">
      <div class="section-heading">
        <h3>固定样例回放</h3>
        <span class="tag">隔离数据 · 无模型调用</span>
      </div>
      <p class="muted">
        检查引用还原、工具归属、记忆排序、隔离与学习门槛。结果是确定性行为检查，不是语言质量评分。
      </p>
      <button
        class="secondary"
        :disabled="busy"
        @click="
          run(async () => {
            replay = await api('/evaluation/replay', 'POST');
          })
        "
      >
        运行固定样例</button
      ><template v-if="replay"
        ><p class="replay-score">
          通过 {{ replay.passed }} / {{ replay.total }} 项
        </p>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>样例</th>
                <th>结果</th>
                <th>预期</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in replay.cases" :key="c.name">
                <td>{{ c.name }}</td>
                <td>{{ c.passed ? "通过" : "未通过" }}</td>
                <td>{{ c.expected }}</td>
              </tr>
            </tbody>
          </table>
        </div></template
      >
    </article>
    <article class="panel">
      <div class="section-heading">
        <h3>后台整理任务</h3>
        <small class="muted">失败重试会请求记忆模型</small>
      </div>
      <div class="job-row" v-for="j in jobs" :key="j.id">
        <div>
          <strong>{{ label(j.kind) }} · {{ label(j.status) }}</strong
          ><small
            >{{ date(j.ts) }} · {{ j.source_ids.length }} 条来源 · 尝试
            {{ j.attempts }} 次</small
          ><small v-if="j.error">{{ j.error }}</small>
        </div>
        <button
          v-if="['failed', 'retry'].includes(j.status)"
          class="secondary"
          :disabled="busy"
          @click="retryJob(j.id)"
        >
          重新整理
        </button>
      </div>
      <p v-if="!jobs.length" class="muted">暂无后台任务。</p>
    </article>
  </template>
  <SaveBar
    v-if="view === 'profiles'"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="failed ? message : ''"
    @save="saveAll"
    @reset="draft.reset"
  />
</template>
<style scoped>
.insight-feedback {
  background: #eaf1de;
  padding: 14px 20px;
  border-radius: 12px;
}
.view-switch {
  display: flex;
  gap: 10px;
  margin-bottom: 20px;
}
.alias-form {
  display: flex;
  align-items: end;
  gap: 16px;
  margin: 18px 0;
}
.alias-form label {
  flex: 1;
  margin: 0;
}
.fact-card {
  padding: 16px 0;
  border-top: 1px solid var(--line);
  overflow-wrap: anywhere;
}
.fact-card p {
  margin: 8px 0;
}
blockquote {
  margin: 12px 0;
  padding: 12px 16px;
  background: #f4f7f3;
  border-left: 3px solid #b9d3b8;
  overflow-wrap: anywhere;
}
small {
  display: block;
  color: var(--muted);
}
.version-item,
.job-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 0;
  border-top: 1px solid var(--line);
}
.trace-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.trace-list {
  padding: 10px;
  max-height: 80vh;
  overflow: auto;
}
.trace-item {
  display: block;
  text-align: left;
  width: 100%;
  padding: 15px 12px;
  background: transparent;
  border-radius: 12px;
  border-bottom: 1px solid var(--line);
}
.trace-item.selected {
  background: #eaf1de;
}
.trace-id {
  overflow-wrap: anywhere;
}
.usage-call {
  padding: 12px;
  background: #f4f7f3;
  border-radius: 12px;
  margin-bottom: 10px;
}
.usage-call p {
  margin: 6px 0;
}
.timeline {
  padding-left: 24px;
}
.timeline li {
  padding: 0 0 20px 12px;
}
.timeline summary,
summary {
  cursor: pointer;
  color: var(--teal);
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 480px;
  overflow: auto;
  font-size: 12px;
  background: #f5f7f4;
  padding: 14px;
  border-radius: 12px;
}
.section-meters {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 12px 0;
}
.section-meters div {
  display: flex;
  justify-content: space-between;
  padding: 8px;
  background: #f4f7f3;
  border-radius: 8px;
  gap: 8px;
}
.replay-score {
  font-size: 20px;
  color: var(--teal);
  margin-top: 18px;
}
.table-wrap {
  overflow: auto;
}
td {
  overflow-wrap: anywhere;
}
form.panel > button {
  margin-top: 12px;
}
@media (max-width: 1050px) {
  .trace-layout {
    grid-template-columns: 1fr;
  }
  .trace-list {
    max-height: 320px;
  }
}
@media (max-width: 600px) {
  .alias-form {
    display: block;
  }
  .alias-form button {
    margin-top: 12px;
  }
  .version-item,
  .job-row {
    flex-wrap: wrap;
  }
  .section-meters {
    grid-template-columns: 1fr;
  }
  .section-heading {
    flex-wrap: wrap;
  }
  .form-footer {
    gap: 14px;
  }
}
</style>
