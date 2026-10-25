<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import RecordFilters from "./components/RecordFilters.vue";
import Overlay from "./components/Overlay.vue";
import PayloadView from "./components/PayloadView.vue";
const route = useRoute(),
  router = useRouter();
const props = defineProps<{
  scope: string;
  api: (path: string, method?: string, body?: any) => Promise<any>;
  initialId?: string;
  initialTrace?: string;
}>();
const calls = ref<any[]>([]),
  selected = ref<any>(null),
  trace = ref<any>(null),
  related = ref<any[]>([]),
  purpose = ref(String(route.query.purpose || "")),
  status = ref(String(route.query.status || "")),
  offset = ref(Math.max(0, Number(route.query.page) || 0) * 40),
  busy = ref(false),
  error = ref(""),
  note = ref(""),
  mode = ref("request"),
  days = ref(7),
  hours = ref(24),
  requestCopy = ref(""),
  mocks = ref("{}"),
  rounds = ref(1),
  traceFilter = ref(""),
  lookup = ref("");
const labels: Record<string, string> = {
  chat: "聊天",
  decision: "决策",
  memory: "记忆",
  creation: "作品制作",
  "creation.code": "编写作品",
  "creation.review": "检查作品",
  replay: "副本试跑",
  search: "联网搜索",
  fetch: "网页读取",
  diagnostic: "诊断",
  failure: "全部失败",
  success: "成功",
  running: "处理中",
  http_error: "接口错误",
  network_error: "连接异常",
  invalid_response: "返回格式异常",
  not_sent: "未发送",
  error: "错误",
  cancelled: "已取消",
  interrupted: "进程中断",
  memory_candidate: "模型记忆候选",
  memory_candidate_result: "候选校验结果",
  memory_normalized: "语义与归属校验",
  memory_write: "记忆写入与版本",
  memory_result: "整理结果",
  memory_job: "关联记忆任务",
  onebot_request: "实际 QQ 接口请求",
  onebot_response: "QQ 接口返回",
  tool_proposed: "工具提议",
  tool_validated: "工具参数与权限校验",
  tool: "工具执行结果",
  reply_raw: "模型原始正文",
  reply_prepared: "发送前正文",
  sent: "实际发送",
  replay_tools: "模拟工具结果",
  context: "上下文组成",
  context_after_memory: "更新记忆后的上下文",
  command: "管理指令（未调用模型）",
};
const label = (s: string) => labels[s] || s;
const format = (x: any) => JSON.stringify(x, null, 2);
const date = (ts: number) =>
  new Date(ts * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
const reasoning = computed(() => {
  const r = selected.value?.response;
  if (r?.choices)
    return r.choices
      .map((c: any) => c.message?.reasoning_content)
      .filter((x: any) => x != null)
      .join("\n\n");
  return (
    r?.content
      ?.filter((b: any) => b.type === "thinking")
      .map((b: any) => b.thinking || "")
      .join("\n\n") || ""
  );
});
const output = computed(() => {
  const r = selected.value?.response;
  if (r?.choices)
    return r.choices.map((c: any) => c.message?.content || "").join("\n\n");
  return (
    r?.content
      ?.filter((b: any) => b.type === "text")
      .map((b: any) => b.text)
      .join("\n\n") || ""
  );
});
const imageList = computed(() => selected.value?.images || []);
let generation = 0,
  detailGeneration = 0;
onUnmounted(() => {
  generation++;
  detailGeneration++;
});
function navigateCall(id: string) {
  router.push({ query: { ...route.query, call: id } });
}
function filters() {
  router.replace({
    query: {
      ...route.query,
      purpose: purpose.value || undefined,
      status: status.value || undefined,
      page: undefined,
      call: undefined,
    },
  });
}
function pageBy(delta: number) {
  router.replace({
    query: {
      ...route.query,
      page: Math.max(0, offset.value / 40 + delta) || undefined,
      call: undefined,
    },
  });
}
async function run(fn: () => Promise<void>) {
  busy.value = true;
  error.value = "";
  note.value = "";
  try {
    await fn();
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function load() {
  const ticket = ++generation;
  await run(async () => {
    const q = new URLSearchParams({
      limit: "40",
      offset: String(offset.value),
    });
    if (props.scope) q.set("scope", props.scope);
    for (const key of ["start", "end", "model"])
      if (route.query[key]) q.set(key, String(route.query[key]));
    if (purpose.value) q.set("purpose", purpose.value);
    if (status.value) q.set("status", status.value);
    if (traceFilter.value) q.set("trace_id", traceFilter.value);
    const rows = await props.api("/model-calls?" + q);
    if (ticket === generation) calls.value = rows;
    const settings = await props.api("/model-calls/settings");
    days.value = settings.days;
    hours.value = settings.image_hours;
  });
}
async function choose(id: string) {
  const ticket = generation,
    detail = ++detailGeneration;
  const result = await props.api("/model-calls/" + encodeURIComponent(id));
  if (
    ticket !== generation ||
    detail !== detailGeneration ||
    route.query.call !== id
  )
    return;
  selected.value = result;
  requestCopy.value = format(result.request ?? {});
  mode.value = "request";
  related.value = [];
  trace.value = null;
  if (result.trace_id) {
    const [t, r] = await Promise.all([
      props.api("/traces/" + result.trace_id).catch(() => null),
      props.api(
        "/model-calls?trace_id=" +
          encodeURIComponent(result.trace_id) +
          "&limit=100",
      ),
    ]);
    if (selected.value?.id === id) {
      trace.value = t;
      related.value = r.reverse();
    }
  }
}
async function openTrace(id: string) {
  await run(async () => {
    const rows = await props.api(
      "/model-calls?trace_id=" + encodeURIComponent(id) + "&limit=100",
    );
    if (rows.length) navigateCall(rows[rows.length - 1].id);
    else note.value = "该关联过程没有模型调用，或完整记录已过期";
  });
}
async function lookupCall() {
  navigateCall(lookup.value.trim());
}
async function replay() {
  await run(async () => {
    const r = await props.api(
      "/model-calls/" + selected.value.id + "/replay",
      "POST",
      {
        request: JSON.parse(requestCopy.value),
        mock_results: JSON.parse(mocks.value),
        rounds: rounds.value,
      },
    );
    if (r.call_ids.length)
      await router.push({
        query: {
          scope: props.scope || undefined,
          trace: r.trace_id,
          call: r.call_ids[r.call_ids.length - 1],
        },
      });
    note.value = "副本试跑完成：真实工具未执行，QQ 消息发送 0 条。";
  });
}
watch(
  () => [
    props.scope,
    route.query.start,
    route.query.end,
    route.query.status,
    route.query.purpose,
    route.query.page,
    route.query.trace,
    route.query.model,
  ],
  async () => {
    purpose.value = String(route.query.purpose || "");
    status.value = String(route.query.status || "");
    offset.value = Math.max(0, Number(route.query.page) || 0) * 40;
    traceFilter.value = String(route.query.trace || "");
    await load();
    if (route.query.call) await run(() => choose(String(route.query.call)));
  },
  { immediate: true },
);
watch(
  () => route.query.call,
  (id) => {
    selected.value = null;
    detailGeneration++;
    if (id) void run(() => choose(String(id)));
  },
  { immediate: true },
);
</script>
<template>
  <p class="page-intro">
    查看每次真正提交的请求、接口返回的思考内容和工具结果。完整日志
    {{ days }} 天；实际图片 {{ hours }} 小时。仅从功能上线后采集。
  </p>
  <RecordFilters :busy="busy" @refresh="load" />
  <div class="panel controls">
    <label
      >任务<select v-model="purpose" aria-label="任务" @change="filters">
        <option value="">全部</option>
        <option
          v-for="v in [
            'chat',
            'decision',
            'memory',
            'creation',
            'replay',
            'search',
            'fetch',
            'diagnostic',
          ]"
          :value="v"
        >
          {{ label(v) }}
        </option>
      </select></label
    ><label
      >状态<select v-model="status" aria-label="状态" @change="filters">
        <option value="">全部</option>
        <option
          v-for="v in [
            'failure',
            'success',
            'running',
            'http_error',
            'network_error',
            'invalid_response',
            'not_sent',
            'error',
            'cancelled',
            'interrupted',
          ]"
          :value="v"
        >
          {{ label(v) }}
        </option>
      </select></label
    ><button class="secondary" @click="load" :disabled="busy">
      刷新调用记录</button
    ><button
      v-if="traceFilter"
      class="quiet"
      @click="
        router.replace({
          query: { ...route.query, trace: undefined, call: undefined },
        })
      "
    >
      取消请求追踪筛选
    </button>
  </div>
  <details class="panel">
    <summary>按编号查找</summary>
    <div class="controls">
      <label>模型调用编号<input v-model="lookup" /></label
      ><button
        class="secondary"
        @click="lookupCall"
        :disabled="busy || !lookup.trim()"
      >
        查找调用
      </button>
    </div>
  </details>
  <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  <p v-if="note" class="success-text" role="status">{{ note }}</p>
  <div class="calls-layout">
    <aside class="panel calls-list">
      <button
        v-for="c in calls"
        :key="c.id"
        class="call-item"
        :class="{ selected: selected?.id === c.id }"
        @click="navigateCall(c.id)"
      >
        <strong>{{ label(c.purpose) }} · {{ label(c.status) }}</strong
        ><small>{{ date(c.ts) }} · 第 {{ c.attempt }} 次尝试</small
        ><small>{{ c.model || c.resource }}</small
        ><small v-if="c.redacted">内容已清理，仅保留指标</small>
      </button>
      <p v-if="!calls.length" class="muted">
        当前筛选下没有记录。历史未采集的完整返回无法补回。
      </p>
      <div class="controls">
        <button class="quiet" :disabled="!offset || busy" @click="pageBy(-1)">
          上一页</button
        ><button
          class="quiet"
          :disabled="calls.length < 40 || busy"
          @click="pageBy(1)"
        >
          下一页
        </button>
      </div>
    </aside>
    <Overlay
      v-if="selected"
      title="模型调用详情"
      drawer
      @close="router.replace({ query: { ...route.query, call: undefined } })"
      ><article class="call-detail">
        <div class="section-heading">
          <h3>
            {{ label(selected.purpose) }} ·
            {{ selected.model || selected.resource }}
          </h3>
          <span class="tag">{{ label(selected.status) }}</span>
        </div>
        <p class="muted identifiers">
          {{ selected.id }} · {{ selected.protocol }} ·
          {{ selected.duration_ms ?? "—" }} ms
        </p>
        <p v-if="selected.redacted" class="muted">{{ selected.notice }}</p>
        <p class="muted">
          阶段：{{ label(selected.stage || selected.purpose) }}
        </p>
        <p v-if="selected.usage" class="muted">
          实际输入 {{ selected.usage.input_tokens ?? "—" }} · 缓存命中
          {{ selected.usage.cached_tokens ?? "—" }} · 未命中
          {{ selected.usage.cache_miss_tokens ?? "—" }} · 输出
          {{ selected.usage.output_tokens ?? "—" }} <br />输入保守估算
          {{ selected.usage.estimated_input ?? "—" }}（{{
            selected.usage.estimate_source ===
            "conservative_characters_plus_image_allowance"
              ? "文字字符＋图片预留"
              : "字符口径"
          }}，非计费用量）
          <template v-if="selected.input?.image_count">
            <br />图片 {{ selected.input.image_count }} 张 · 视觉保守估算
            {{ selected.input.visual_estimate }} · 请求体
            {{ (selected.input.request_bytes / 1024 / 1024).toFixed(2) }} MiB
          </template>
        </p>
        <p v-if="selected.redactions?.length" class="muted">
          凭据已脱敏
          {{ selected.redactions.length }}
          处，详见导出中的标记。鉴权请求头不记录。
        </p>
        <div class="controls">
          <a
            class="link"
            :href="'/api/v1/model-calls/' + selected.id + '/export'"
            >导出完整记录 JSON</a
          ><a
            v-if="imageList.length"
            class="link"
            :href="
              '/api/v1/model-calls/' +
              selected.id +
              '/export?include_images=true'
            "
            >导出含实际图片的 JSON</a
          >
        </div>
        <div class="controls tabs">
          <button
            v-for="t in [
              ['request', '实际输入'],
              ['response', '模型输出'],
              ['reasoning', '接口思考内容'],
              ['prompts', '提示词版本'],
              ['process', '工具与记忆过程'],
              ['replay', '复制试跑'],
            ]"
            :class="mode === t[0] ? 'primary' : 'secondary'"
            @click="mode = t[0]"
          >
            {{ t[1] }}
          </button>
        </div>
        <template v-if="mode === 'request'"
          ><p class="muted">
            协议转换完成后提交的请求体。图片以附件引用展示；正文按原始顺序完整保存。
          </p>
          <PayloadView
            :value="selected.request ?? { notice: selected.notice }"
          />
          <details>
            <summary>原始请求 JSON</summary>
            <pre>{{
              format(selected.request ?? { notice: selected.notice })
            }}</pre>
          </details>
          <div
            v-for="img in imageList"
            :key="img.__record_image__"
            class="image-record"
          >
            <img v-if="img.url" :src="img.url" alt="当时送入模型的图片" />
            <p>
              {{
                img.status === "available"
                  ? "图片可查看"
                  : "实际图片已过期或不可用"
              }}
              · {{ img.bytes }} 字节
            </p>
            <small class="identifiers">SHA-256 {{ img.sha256 }}</small>
          </div>
          <details v-if="selected.input">
            <summary>进入协议适配前的消息</summary>
            <pre>{{ format(selected.input) }}</pre>
          </details></template
        >
        <template v-else-if="mode === 'response'"
          ><strong>返回正文</strong>
          <pre>{{
            output || "没有返回正文；可检查工具调用、错误或结束原因。"
          }}</pre>
          <details>
            <summary>原始接口返回 JSON</summary>
            <pre>{{ format(selected.response) }}</pre>
          </details>
          <pre v-if="selected.error">{{ format(selected.error) }}</pre>
        </template>
        <template v-else-if="mode === 'reasoning'"
          ><p class="muted">
            仅展示供应商接口实际返回的思考字段。此内容不会发送到 QQ。
          </p>
          <pre>{{
            reasoning ||
            "本次未返回可读思考内容，可能未开启、接口未提供，或记录已清理。"
          }}</pre>
        </template>
        <template v-else-if="mode === 'prompts'"
          ><div v-for="p in selected.prompts || []" :key="p.key + p.sha256">
            <strong
              >{{ p.key }} · v{{ p.version }} ·
              {{ p.scope ? "群覆盖" : "全局或内置" }}</strong
            >
            <pre>{{ p.body }}</pre>
            <small class="identifiers">{{ p.sha256 }}</small>
          </div>
          <p v-if="!selected.prompts?.length" class="muted">
            此请求没有可关联的模板，或记录已清理；实际输入仍以请求体为准。
          </p></template
        >
        <template v-else-if="mode === 'process'"
          ><p class="muted">
            按时间关联模型调用、工具执行、记忆候选及保存结果。没有模型调用的操作不会伪造模型返回。
          </p>
          <button
            v-if="trace?.parent_id"
            class="secondary"
            @click="openTrace(trace.parent_id)"
          >
            返回关联聊天调用
          </button>
          <div class="controls">
            <button
              v-for="c in related"
              :key="c.id"
              class="quiet"
              @click="navigateCall(c.id)"
            >
              {{ label(c.purpose) }} · {{ label(c.status) }} · {{ date(c.ts) }}
            </button>
          </div>
          <details v-for="(s, i) in trace?.stages || []" :key="i">
            <summary>{{ label(s.name) }} · {{ date(s.ts) }}</summary>
            <button
              v-if="s.name === 'memory_job' && s.data.trace_id"
              class="secondary"
              @click="openTrace(s.data.trace_id)"
            >
              查看关联记忆模型调用
            </button>
            <pre>{{ format(s.data) }}</pre>
          </details>
          <p v-if="!trace" class="muted">
            关联过程记录未采集或已过期。
          </p></template
        >
        <template v-else-if="mode === 'replay'"
          ><p>
            修改副本后试跑模型；所有工具仅返回模拟结果，原始记录保留，业务记忆与
            QQ 群不会改变。试跑会产生模型用量。
          </p>
          <label
            >请求副本 JSON<textarea
              v-model="requestCopy"
              rows="16"
              spellcheck="false"
            /></label
          ><label
            >模拟工具结果（按工具名的 JSON 对象）<textarea
              v-model="mocks"
              rows="4"
              spellcheck="false"
            /></label
          ><label
            >最多模型轮数<input
              type="number"
              min="1"
              max="4"
              v-model.number="rounds" /></label
          ><button
            class="primary"
            @click="replay"
            :disabled="
              busy ||
              selected.redacted ||
              !selected.request ||
              !['chat', 'messages'].includes(selected.resource)
            "
          >
            {{ busy ? "试跑中…" : "仅调用模型试跑副本" }}
          </button></template
        >
      </article></Overlay
    >
  </div>
</template>
<style scoped>
.controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
}
.controls label {
  min-width: 120px;
}
.calls-layout {
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr);
  gap: 22px;
  margin-top: 22px;
}
.calls-list {
  max-height: 85vh;
  overflow: auto;
}
.call-item {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--line);
  padding: 14px 8px;
  color: var(--ink);
}
.call-item small {
  display: block;
  color: var(--muted);
  margin-top: 6px;
}
.call-item.selected {
  background: #edf2dc;
  border-radius: 8px;
}
.call-detail {
  min-width: 0;
}
.identifiers {
  overflow-wrap: anywhere;
}
.call-detail pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 750px;
  overflow: auto;
  background: #f4f7f3;
  padding: 16px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
}
.call-detail textarea {
  width: 100%;
  font: inherit;
  line-height: 1.6;
}
.call-detail label {
  display: block;
  margin: 16px 0;
}
.image-record img {
  max-width: 100%;
  max-height: 420px;
  object-fit: contain;
}
.image-record {
  margin: 20px 0;
}
details {
  margin: 12px 0;
}
summary {
  color: var(--teal);
  cursor: pointer;
}
@media (max-width: 900px) {
  .calls-layout {
    grid-template-columns: 1fr;
  }
  .calls-list {
    max-height: 310px;
  }
  .tabs button {
    font-size: 12px;
  }
}
</style>
