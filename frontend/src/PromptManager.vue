<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
const route = useRoute(),
  router = useRouter();
import { useDraft, allowLeave } from "./lib/drafts";
import { confirmAction } from "./lib/confirm";
import SaveBar from "./components/SaveBar.vue";
const props = defineProps<{
  scope: string;
  api: (path: string, method?: string, body?: any) => Promise<any>;
}>();
const emit = defineEmits<{ navigate: [string] }>();
const entries = ref<any[]>([]),
  selected = ref(String(route.query.template || "")),
  groupOverride = ref(route.query.override === "1" && !!props.scope),
  filter = ref(""),
  body = ref(""),
  preview = ref(""),
  variables = ref<Record<string, string>>({}),
  versions = ref<any[]>([]),
  comparison = ref(""),
  message = ref(""),
  failed = ref(false),
  busy = ref(false);
const editScope = computed(() => (groupOverride.value ? props.scope : ""));
const entry = computed(() =>
  entries.value.find((x) => x.key === selected.value),
);
const filtered = computed(() =>
  entries.value.filter((x) =>
    (x.title + x.key + x.category)
      .toLowerCase()
      .includes(filter.value.toLowerCase()),
  ),
);
const draft = useDraft(
  () => body.value,
  save,
  (v) => (body.value = v),
);
async function selectEntry(key: string) {
  await router.replace({ query: { ...route.query, template: key } });
}
async function toggleOverride(e: Event) {
  const input = e.target as HTMLInputElement,
    desired = input.checked;
  input.checked = groupOverride.value;
  if (await allowLeave())
    await router.replace({
      query: { ...route.query, override: desired ? "1" : undefined },
    });
}
let generation = 0;
onUnmounted(() => generation++);
const json = (v: any) => JSON.stringify(v, null, 2);
async function run(fn: () => Promise<void>) {
  busy.value = true;
  message.value = "";
  failed.value = false;
  try {
    await fn();
    return true;
  } catch (e: any) {
    message.value = e.message;
    failed.value = true;
    return false;
  } finally {
    busy.value = false;
  }
}
async function load() {
  const id = ++generation;
  await run(async () => {
    const result = await props.api(
      "/prompts?scope=" + encodeURIComponent(editScope.value),
    );
    if (id !== generation) return;
    entries.value = result;
    await choose(
      selected.value && result.some((x: any) => x.key === selected.value)
        ? selected.value
        : result[0]?.key,
    );
  });
}
async function choose(key: string) {
  selected.value = key;
  const e = entries.value.find((x) => x.key === key);
  if (!e) return;
  body.value = e.draft?.body ?? e.effective.body;
  draft.accept();
  variables.value = Object.fromEntries(
    e.variables.map((v: string) => [v, `示例 ${v}`]),
  );
  preview.value = "";
  comparison.value = e.effective.body;
  const scope = editScope.value;
  const data = await props.api(
    `/prompts/${key}/versions?scope=${encodeURIComponent(scope)}`,
  );
  if (selected.value === key && editScope.value === scope)
    versions.value = data;
}
async function save() {
  return await run(async () => {
    await props.api(`/prompts/${selected.value}/draft`, "PUT", {
      scope: editScope.value,
      body: body.value,
      base_version: entry.value.local_version,
    });
    draft.accept();
    message.value = "草稿已保存，当前运行版本未变化";
  });
}
async function publish() {
  await run(async () => {
    const key = selected.value,
      scope = editScope.value,
      version = entry.value.local_version;
    const draft = await props.api(`/prompts/${key}/draft`, "PUT", {
      scope,
      body: body.value,
      base_version: version,
    });
    await props.api(`/prompts/${key}/publish`, "POST", {
      scope,
      base_version: version,
      draft_hash: draft.sha256,
    });
    if (scope !== editScope.value || key !== selected.value) return;
    entries.value = await props.api(
      "/prompts?scope=" + encodeURIComponent(editScope.value),
    );
    await choose(key);
    message.value = "已发布，从下一轮开始生效";
  });
}
async function render() {
  await run(async () => {
    const r = await props.api(`/prompts/${selected.value}/preview`, "POST", {
      body: body.value,
      variables: variables.value,
    });
    preview.value = r.text;
    message.value = `预览 ${r.characters} 字；未调用模型`;
  });
}
async function restore(version: number) {
  if (
    !(await confirmAction(
      "恢复提示词版本",
      "将把所选历史版本发布为新的运行版本，替换当前编辑内容。",
      "恢复版本",
    ))
  )
    return;
  await run(async () => {
    const key = selected.value;
    await props.api(`/prompts/${key}/restore/${version}`, "POST", {
      scope: editScope.value,
      base_version: entry.value.local_version,
    });
    entries.value = await props.api(
      "/prompts?scope=" + encodeURIComponent(editScope.value),
    );
    await choose(key);
    message.value = "已生成恢复版本，从下一轮生效";
  });
}
watch(
  () => [props.scope, groupOverride.value],
  () => void load(),
  { immediate: true },
);
watch(
  () => route.query.override,
  (value) => {
    groupOverride.value = value === "1" && !!props.scope;
  },
);
watch(
  () => route.query.template,
  (key) => {
    if (key && entries.value.some((e) => e.key === key))
      void run(() => choose(String(key)));
  },
);
</script>
<template>
  <p class="page-intro">
    管理全部服务提示模板。每轮固定使用开始时的版本；历史调用保留当时的提示词。
  </p>
  <div class="panel controls">
    <label class="switch-label"
      ><input
        type="checkbox"
        :checked="groupOverride"
        @change="toggleOverride"
        :disabled="!scope || busy"
      />编辑当前群覆盖</label
    >
    <span class="muted">{{
      groupOverride
        ? "只影响当前群；未覆盖模板继承全局"
        : "全局模板；群覆盖优先"
    }}</span>
    <button class="quiet" @click="emit('navigate', 'personas')">
      编辑角色卡提示词
    </button>
    <button class="quiet" @click="emit('navigate', 'groups')">
      编辑群说明
    </button>
    <button class="quiet" @click="emit('navigate', 'qq-permissions')">
      编辑自主管理规则
    </button>
  </div>
  <p
    v-if="message"
    :class="failed ? 'error-text' : 'success-text'"
    role="status"
  >
    {{ message }}
  </p>
  <div class="prompt-layout">
    <aside class="panel prompt-list">
      <label
        >搜索模板<input
          v-model="filter"
          placeholder="记忆、接话、工具…" /></label
      ><button
        v-for="e in filtered"
        :key="e.key"
        class="template-item"
        :class="{ selected: selected === e.key }"
        @click="selectEntry(e.key)"
        :disabled="busy"
      >
        <strong>{{ e.title }}</strong
        ><small
          >{{ e.category }} ·
          {{ e.effective.builtin ? "内置" : `v${e.effective.version}` }}</small
        >
      </button>
    </aside>
    <article class="panel editor" v-if="entry">
      <div class="section-heading">
        <h3>{{ entry.title }}</h3>
        <span class="tag"
          >{{
            entry.effective.scope
              ? "当前群"
              : entry.effective.builtin
                ? "内置默认"
                : "全局发布"
          }}
          · v{{ entry.effective.version }}</span
        >
      </div>
      <small class="muted">{{ entry.key }}</small>
      <p class="muted">
        {{ entry.usage
        }}<template v-if="groupOverride">
          ·
          {{
            entry.active_for_scope ? "当前群已选用" : "当前群未选用"
          }}</template
        >
      </p>
      <label
        >提示词草稿<textarea
          v-model="body"
          rows="16"
          maxlength="64000"
          spellcheck="false"
        />
      </label>
      <p class="muted">
        {{ body.length }} / 64000 字。变量只做文字替换，不执行代码；必需变量：{{
          entry.variables.join("、") || "无"
        }}。
      </p>
      <div class="controls">
        <button class="secondary" @click="render" :disabled="busy">
          渲染预览</button
        ><button class="primary" @click="publish" :disabled="busy">
          发布到{{ groupOverride ? "当前群" : "全局" }}
        </button>
      </div>
      <div class="form-grid" v-if="entry.variables.length">
        <label v-for="v in entry.variables" :key="v"
          >预览变量 {{ v }}<textarea v-model="variables[v]" rows="2" />
        </label>
      </div>
      <pre v-if="preview" class="payload">{{ preview }}</pre>
      <details>
        <summary>比较已发布内容与草稿</summary>
        <div class="comparison">
          <div>
            <strong>已发布内容</strong>
            <pre>{{ entry.effective.body }}</pre>
          </div>
          <div>
            <strong>当前草稿</strong>
            <pre>{{ body }}</pre>
          </div>
        </div>
      </details>
      <details>
        <summary>历史版本与恢复</summary>
        <div class="version-row" v-for="v in versions" :key="v.version">
          <span
            >v{{ v.version }} ·
            {{
              v.version === 0
                ? "内置默认"
                : new Date(v.ts * 1000).toLocaleString("zh-CN", {
                    timeZone: "Asia/Shanghai",
                  })
            }}</span
          ><button class="quiet" @click="comparison = v.body">查看此版本</button
          ><button
            class="secondary"
            @click="restore(v.version)"
            :disabled="busy"
          >
            恢复为新版本
          </button>
        </div>
        <pre>{{ comparison }}</pre>
      </details>
      <p class="muted">
        提示词改变模型收到的文字；实际 QQ
        身份、工具授权、参数类型及执行限制仍由服务端校验。
      </p>
    </article>
  </div>
  <SaveBar
    save-label="保存草稿"
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="failed ? message : ''"
    @save="save"
    @reset="draft.reset"
  />
</template>
<style scoped>
.controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}
.prompt-layout {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 22px;
}
.prompt-list {
  max-height: 80vh;
  overflow: auto;
}
.template-item {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  padding: 12px;
  border: 0;
  border-bottom: 1px solid var(--line);
  color: var(--ink);
}
.template-item small {
  display: block;
  color: var(--muted);
  margin-top: 5px;
}
.template-item.selected {
  background: #edf2dc;
  border-radius: 9px;
}
.editor {
  min-width: 0;
}
.editor label {
  display: block;
  margin-top: 18px;
}
.editor textarea {
  width: 100%;
  font: inherit;
  line-height: 1.6;
}
.comparison {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.comparison > div {
  min-width: 0;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 650px;
  overflow: auto;
  font-size: 13px;
  line-height: 1.6;
  background: #f4f7f3;
  padding: 16px;
  border-radius: 8px;
}
details {
  margin-top: 22px;
}
summary {
  cursor: pointer;
  color: var(--teal);
}
.version-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 12px 0;
}
@media (max-width: 900px) {
  .prompt-layout {
    grid-template-columns: 1fr;
  }
  .prompt-list {
    max-height: 260px;
  }
  .comparison {
    grid-template-columns: 1fr;
  }
}
</style>
