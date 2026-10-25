<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../lib/api";
import { useAction } from "../lib/state";
import { useDraft } from "../lib/drafts";
import NumberField from "./NumberField.vue";
import SaveBar from "./SaveBar.vue";

const form = ref({ sticker_limit: 100, sticker_max_mib: 5 });
const ready = ref(false);
const { busy, error, action } = useAction();
const draft = useDraft(
  () => form.value,
  save,
  (value) => (form.value = value),
);

async function load() {
  await action(async () => {
    form.value = await api("/sticker-library");
    draft.accept();
    ready.value = true;
  }, "");
}
async function save() {
  if (!ready.value) return false;
  return await action(async () => {
    form.value = await api("/sticker-library", "PUT", form.value);
    draft.accept();
  }, "共享表情库容量已保存，对所有群生效");
}
onMounted(load);
</script>
<template>
  <article class="panel shared-sticker-settings">
    <h3>共享表情库</h3>
    <p class="muted">
      所有群共用以下表情。上传、编辑、禁用和删除会对所有群生效；各群的互动与自动收藏开关独立控制。
    </p>
    <form v-if="ready" @submit.prevent="save">
      <details>
        <summary>共享表情库容量设置</summary>
        <div class="form-grid">
          <NumberField
            field="library.sticker_limit"
            v-model="form.sticker_limit"
            :disabled="busy"
          />
          <NumberField
            field="library.sticker_max_mib"
            v-model="form.sticker_max_mib"
            :disabled="busy"
          />
        </div>
        <div class="form-footer">
          <small>调低容量不会删除已有表情；达到上限后暂停新增收藏。</small>
        </div>
      </details>
    </form>
    <p v-else-if="!error" class="muted">正在读取共享容量…</p>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <button
      v-if="!ready && error"
      class="secondary"
      :disabled="busy"
      @click="load"
    >
      重新加载共享容量
    </button>
  </article>
  <SaveBar
    inline
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="error"
    @save="save"
    @reset="draft.reset"
  />
</template>
<style scoped>
.shared-sticker-settings .form-footer {
  margin-top: 16px;
}
</style>
