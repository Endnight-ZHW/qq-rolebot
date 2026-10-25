<script setup lang="ts">
import SettingSwitch from "../components/SettingSwitch.vue";
import { ref, computed, onMounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, query } from "../lib/api";
import { useGroup } from "../lib/groups";
import { notify, useAction } from "../lib/state";
import { clone } from "../lib/format";
import { useDraft, allowLeave, leaveBusy } from "../lib/drafts";
import { confirmAction } from "../lib/confirm";
import Overlay from "../components/Overlay.vue";
import InteractionSettings from "../components/InteractionSettings.vue";
import StickerLibrarySettings from "../components/StickerLibrarySettings.vue";
import StatusBadge from "../components/StatusBadge.vue";
const { scope } = useGroup(),
  route = useRoute(),
  router = useRouter(),
  rows = ref<any[]>([]),
  search = ref(String(route.query.q || "")),
  state = ref(String(route.query.state || "")),
  editor = ref<any>(null),
  loading = ref(true),
  { busy, error, action } = useAction();
const draft = useDraft(
  () => editor.value,
  save,
  (v) => (editor.value = v),
);
const filtered = computed(() =>
  rows.value.filter(
    (s) =>
      (s.name + " " + s.tags + " " + s.usage).includes(search.value) &&
      (!state.value ||
        (state.value === "enabled"
          ? s.enabled !== false
          : s.enabled === false)),
  ),
);
async function load() {
  await action(async () => {
    rows.value = await api("/stickers?" + query({ scope: scope.value }));
  }, "");
  loading.value = false;
  if (route.query.sticker) select(String(route.query.sticker));
}
function filters() {
  router.replace({
    query: {
      ...route.query,
      q: search.value || undefined,
      state: state.value || undefined,
    },
  });
}
function select(id: string) {
  const s = rows.value.find((x) => x.id === id);
  editor.value = s ? { enabled: true, usage: "", tags: "", ...clone(s) } : null;
  draft.accept();
}
async function edit(s: any) {
  await router.push({ query: { ...route.query, sticker: s.id } });
}
watch(
  () => route.query.sticker,
  (id) => select(String(id || "")),
);
async function close() {
  if (!(await allowLeave())) return;
  editor.value = null;
  draft.accept();
  await router.replace({ query: { ...route.query, sticker: undefined } });
}
async function save() {
  if (!editor.value?.name?.trim()) {
    error.value = "请填写表情名称";
    return false;
  }
  return await action(async () => {
    const s = editor.value;
    await api("/stickers/" + s.id, "PUT", {
      name: s.name,
      tags: s.tags,
      usage: s.usage,
      enabled: s.enabled,
    });
    editor.value = null;
    draft.accept();
    rows.value = await api("/stickers?" + query({ scope: scope.value }));
    if (!leaveBusy.value)
      await router.replace({ query: { ...route.query, sticker: undefined } });
  }, "表情已保存");
}
async function remove(s: any) {
  if (
    !(await confirmAction(
      "删除表情",
      "将从共享表情库删除「" +
        s.name +
        "」及保存的图片文件，所有群将无法再使用。",
      "删除表情",
    ))
  )
    return;
  await action(async () => {
    await api("/stickers/" + s.id, "DELETE");
    editor.value = null;
    draft.accept();
    rows.value = await api("/stickers?" + query({ scope: scope.value }));
    await router.replace({ query: { ...route.query, sticker: undefined } });
  }, "表情已删除");
}
async function upload(event: Event) {
  const el = event.target as HTMLInputElement,
    file = el.files?.[0];
  if (!file) return;
  await action(async () => {
    const body = new FormData();
    body.append("file", file);
    const result = await api<any>(
      "/stickers?" + query({ scope: scope.value, name: file.name }),
      "POST",
      body,
    );
    rows.value = await api("/stickers?" + query({ scope: scope.value }));
    notify(
      result.duplicate
        ? result.enabled === false
          ? "表情已在共享库中，仍保持禁用"
          : "已复用共享库中的相同表情"
        : "图片已加入共享表情库",
    );
  }, "");
  el.value = "";
}
onMounted(load);
</script>
<template>
  <InteractionSettings :scope="scope" stickers />
  <StickerLibrarySettings />
  <div class="toolbar">
    <input
      v-model="search"
      aria-label="搜索表情"
      placeholder="搜索名称、标签或使用场景"
      @input="filters"
    /><select v-model="state" aria-label="表情状态" @change="filters">
      <option value="">全部表情</option>
      <option value="enabled">允许使用</option>
      <option value="disabled">已禁用</option></select
    ><span class="muted result-count">{{ filtered.length }} 张</span
    ><button class="secondary" :disabled="busy" @click="load">刷新</button
    ><label class="button primary upload-button"
      >＋ 上传表情<input
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        :disabled="busy"
        @change="upload"
    /></label>
  </div>
  <p v-if="error && !editor" class="error-text" role="alert">{{ error }}</p>
  <div v-if="loading" class="skeleton panel"></div>
  <div v-else class="sticker-grid">
    <article v-for="s in filtered" :key="s.id" class="panel sticker-card">
      <button
        class="sticker-preview"
        :aria-label="'编辑表情 ' + s.name"
        @click="edit(s)"
      >
        <img
          :src="'/api/v1/stickers/' + s.id + '/image'"
          :alt="s.name"
          loading="lazy"
        />
      </button>
      <div class="section-heading">
        <strong>{{ s.name }}</strong
        ><StatusBadge
          :status="s.enabled !== false ? 'enabled' : 'disabled'"
          :text="s.enabled !== false ? '允许使用' : '已禁用'"
        />
      </div>
      <p class="muted">{{ s.tags || "暂无标签" }}</p>
      <small
        >{{ s.animated ? "动画 · " : "" }}所有群共享 ·
        {{ s.bytes ? Math.ceil(s.bytes / 1024) + " KiB" : "" }}</small
      >
      <div class="card-actions">
        <button class="link" @click="edit(s)">编辑详情 →</button
        ><button class="quiet danger" :disabled="busy" @click="remove(s)">
          删除
        </button>
      </div>
    </article>
  </div>
  <div v-if="!loading && !filtered.length" class="empty panel">
    <h3>表情库还是空的</h3>
    <p>上传喜欢的表情，或开启自动筛选收藏。GIF 动画会完整保留。</p>
  </div>
  <Overlay v-if="editor" title="表情详情" drawer :busy="busy" @close="close"
    ><img
      class="sticker-detail-image"
      :src="'/api/v1/stickers/' + editor.id + '/image'"
      :alt="editor.name"
    />
    <form id="sticker-form" @submit.prevent="save">
      <label>名称<input v-model="editor.name" maxlength="80" required /></label
      ><label>标签<input v-model="editor.tags" maxlength="500" /></label
      ><label
        >使用场景<textarea
          v-model="editor.usage"
          maxlength="500"
          rows="3"
        /></label
      ><SettingSwitch
        v-model="editor.enabled"
        label="允许聊天使用"
        :disabled="busy"
      />
      <p v-if="editor.description">{{ editor.description }}</p>
      <details v-if="editor.source_ids?.length">
        <summary>收藏来源</summary>
        <p v-for="id in editor.source_ids" :key="id" class="identifiers">
          {{ id }}
        </p>
      </details>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    </form>
    <template #footer
      ><button class="secondary" :disabled="busy" @click="close">取消</button
      ><button class="primary" form="sticker-form" :disabled="busy">
        保存
      </button></template
    ></Overlay
  >
</template>
