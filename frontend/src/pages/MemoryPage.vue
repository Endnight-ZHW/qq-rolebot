<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, query } from "../lib/api";
import { useGroup } from "../lib/groups";
import { core, useAction } from "../lib/state";
import { date, splitIds, download, label } from "../lib/format";
import { useDraft, allowLeave } from "../lib/drafts";
import { confirmAction } from "../lib/confirm";
import MemoryDetails from "../MemoryDetails.vue";
import Overlay from "../components/Overlay.vue";
import StatusBadge from "../components/StatusBadge.vue";
const { scope } = useGroup(),
  route = useRoute(),
  router = useRouter(),
  memories = ref<any[]>([]),
  learning = ref<any>(null),
  loading = ref(true),
  editor = ref<any>(null),
  subjects = ref(""),
  search = ref(String(route.query.q || "")),
  status = ref(String(route.query.status || "")),
  { busy, error, action } = useAction();
const selected = computed(() =>
  memories.value.find((m) => m.id === route.query.memory),
);
const filtered = computed(() =>
  memories.value.filter(
    (m) =>
      (m.text + " " + (m.data.subject_ids || []).join(" ")).includes(
        search.value,
      ) &&
      (!status.value || (m.data.status || "active") === status.value),
  ),
);
const draft = useDraft(
  () => ({ editor: editor.value, subjects: subjects.value }),
  save,
  (v) => {
    editor.value = v.editor;
    subjects.value = v.subjects;
  },
);
async function load() {
  await action(async () => {
    memories.value = await api("/memories?" + query({ scope: scope.value }));
    learning.value = await api("/groups/" + scope.value + "/learning-status");
  }, "");
  loading.value = false;
}
function filters() {
  router.replace({
    query: {
      ...route.query,
      q: search.value || undefined,
      status: status.value || undefined,
    },
  });
}
async function edit(m?: any) {
  if (!(await allowLeave())) return;
  editor.value = m
    ? {
        id: m.id,
        scope: m.scope,
        persona: m.persona,
        text: m.text,
        pinned: !!m.data.pinned,
      }
    : { scope: scope.value, persona: "", text: "", pinned: false };
  subjects.value = (m?.data.subject_ids || []).join(", ");
  draft.accept();
}
async function close() {
  if (!(await allowLeave())) return;
  editor.value = null;
  subjects.value = "";
  draft.accept();
}
async function save() {
  if (!editor.value?.text?.trim()) {
    error.value = "请填写记忆内容";
    return false;
  }
  const ok = await action(async () => {
    const { id, ...body } = editor.value;
    await api("/memories" + (id ? "/" + id : ""), id ? "PUT" : "POST", {
      ...body,
      subject_ids: splitIds(subjects.value),
    });
    editor.value = null;
    subjects.value = "";
    draft.accept();
    memories.value = await api("/memories?" + query({ scope: scope.value }));
  }, "记忆已保存");
  return ok;
}
async function forget(m: any) {
  if (
    !(await confirmAction(
      "遗忘这条记忆",
      "将删除「" +
        m.text.slice(0, 60) +
        "」及关联摘要，并排除原始来源。此操作不可撤销。",
      "遗忘记忆",
    ))
  )
    return;
  const ok = await action(async () => {
    await api(
      "/memories/" + m.id + "?" + query({ scope: scope.value }),
      "DELETE",
    );
    memories.value = await api("/memories?" + query({ scope: scope.value }));
  }, "记忆已遗忘");
  if (ok)
    await router.replace({ query: { ...route.query, memory: undefined } });
}
function openCall(id: string) {
  router.push({
    path: "/observe/calls",
    query: { scope: scope.value, call: id },
  });
}
onMounted(load);
</script>
<template>
  <article v-if="learning?.mode === 'participation'" class="panel">
    <div class="section-heading">
      <h3>参与会话后的记忆整理</h3>
      <button class="secondary" :disabled="busy" @click="load">
        刷新整理状态
      </button>
    </div>
    <p>
      待整理 {{ learning.pending_messages }} 条消息 ·
      {{ learning.pending_sessions }} 段参与会话
    </p>
    <p v-if="learning.failed_sessions" class="error-text">
      {{
        learning.failed_sessions
      }}
      段整理已达到重试上限，请查看请求追踪；未反复调用模型。
    </p>
    <p v-if="learning.ready_sessions" class="muted">
      有 {{ learning.ready_sessions }} 段已到整理时间，等待后台按预算处理。
    </p>
    <p v-else-if="learning.next_due_at" class="muted">
      预计整理时间：{{
        date(learning.next_due_at)
      }}。机器人再次回复后可能顺延，持续参与仍受最长间隔限制。
    </p>
    <p v-else class="muted">
      当前没有待整理的参与会话。沉默期间的新群消息仅保留为可查询历史。
    </p>
  </article>
  <div class="toolbar">
    <input
      v-model="search"
      aria-label="搜索记忆"
      placeholder="搜索内容或相关 QQ"
      @input="filters"
    /><select v-model="status" aria-label="记忆状态" @change="filters">
      <option value="">全部状态</option>
      <option value="active">有效</option>
      <option value="unverified">待核实</option>
      <option value="archived">已归档</option></select
    ><span class="muted result-count">{{ filtered.length }} 条记忆</span
    ><button class="secondary" @click="download('记忆档案.json', memories)">
      导出</button
    ><button class="primary" @click="edit()">＋ 添加记忆</button>
  </div>
  <p v-if="error && !editor" role="alert" class="error-text">
    {{ error }} <button class="link" @click="load">重试</button>
  </p>
  <div v-if="loading" class="skeleton panel"></div>
  <div v-else class="memory-grid">
    <article v-for="m in filtered" :key="m.id" class="memory-card">
      <div class="section-heading">
        <StatusBadge :status="m.data.status || 'active'" /><small class="muted"
          >{{ m.data.pinned ? "已固定 · " : "" }}v{{
            m.data.version || 1
          }}</small
        >
      </div>
      <p class="memory-text">{{ m.text }}</p>
      <div class="memory-summary">
        <span>{{
          m.persona
            ? core.personas.find((p) => p.id === m.persona)?.name || m.persona
            : "同群共享"
        }}</span
        ><span
          >相关成员 · {{ m.data.subject_ids?.join("、") || "群内常识" }}</span
        ><small
          >{{ date(m.ts) }} ·
          {{
            m.data.evidence_kind === "manual" ? "管理员整理" : "自动整理"
          }}</small
        >
      </div>
      <div class="card-actions">
        <RouterLink
          class="link"
          :to="{ query: { ...route.query, memory: m.id } }"
          >来源与版本 →</RouterLink
        ><button class="quiet" @click="edit(m)">编辑</button
        ><button class="quiet danger" @click="forget(m)">遗忘</button>
      </div>
    </article>
  </div>
  <div v-if="!loading && !filtered.length" class="empty panel">
    <h3>{{ memories.length ? "没有匹配的记忆" : "故事从这里开始" }}</h3>
    <p>重要的偏好与共同经历会出现在这里，也可以手动添加。</p>
  </div>
  <Overlay
    v-if="selected && !editor"
    title="记忆详情"
    drawer
    @close="router.replace({ query: { ...route.query, memory: undefined } })"
    ><p class="memory-text">{{ selected.text }}</p>
    <MemoryDetails
      :memory="selected"
      :api="api"
      @changed="load"
      @open-call="openCall"
    /><template #footer
      ><button class="secondary" @click="edit(selected)">编辑记忆</button
      ><button class="danger-button" @click="forget(selected)">
        遗忘记忆
      </button></template
    ></Overlay
  >
  <Overlay
    v-if="editor"
    :title="editor.id ? '修正记忆' : '新增记忆'"
    drawer
    :busy="busy"
    @close="close"
    ><form id="memory-form" @submit.prevent="save">
      <label
        >归属角色<select v-model="editor.persona">
          <option value="">同群共享资料</option>
          <option v-for="p in core.personas" :key="p.id" :value="p.id">
            {{ p.name }}的经历
          </option>
        </select></label
      ><label
        >记忆内容<textarea
          v-model="editor.text"
          required
          rows="8"
          maxlength="4000"
        /></label
      ><label
        >相关成员 QQ<input
          v-model="subjects"
          placeholder="多个号码用逗号分隔" /></label
      ><label class="switch-label"
        ><input
          v-model="editor.pinned"
          type="checkbox"
        />固定此记忆，优先加入上下文</label
      >
      <p class="muted">修正后会清理与旧来源关联的摘要。</p>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    </form>
    <template #footer
      ><button class="secondary" :disabled="busy" @click="close">取消</button
      ><button class="primary" form="memory-form" :disabled="busy">
        保存记忆
      </button></template
    ></Overlay
  >
</template>
