<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, query } from "../lib/api";
import { useAction } from "../lib/state";
const props = defineProps<{ persona: string }>();
const rows = ref<any[]>([]);
const { busy, error, action } = useAction();
const base = () => `/personas/${props.persona}/images`;
async function load() { rows.value = await api(base()); }
async function upload(event: Event, row?: any) {
  const input = event.target as HTMLInputElement, file = input.files?.[0];
  if (!file) return;
  await action(async () => {
    const form = new FormData(); form.append("file", file);
    await api(row ? `${base()}/${row.id}/file` : `${base()}?${query({name: file.name})}`, row ? "PUT" : "POST", form);
    await load();
  }, row ? "角色参考图已替换" : "角色参考图已上传");
  input.value = "";
}
async function toggle(row: any) {
  await action(async () => { await api(`${base()}/${row.id}`, "PUT", { enabled: !row.enabled }); await load(); }, "参考图状态已更新");
}
onMounted(() => action(load, ""));
</script>
<template>
  <details>
    <summary>角色参考图片（{{ rows.length }}）</summary>
    <p class="muted">由你上传角色设定图，同一角色在各群共用。群聊图片不会自动加入这里。模型按需选取少量参考图比较，相似画风不代表确定身份。</p>
    <label class="button secondary">
      ＋ 上传角色参考图
      <input type="file" accept="image/png,image/jpeg,image/gif,image/webp" aria-label="上传角色参考图" hidden :disabled="busy" @change="upload($event)" />
    </label>
    <p class="muted">支持 JPEG、PNG、WebP、GIF，单张最多 8 MiB；动图保存首帧。参考图独立保存，直到你替换或删除。</p>
    <div v-for="row in rows" :key="row.id" class="reference-row">
      <img v-if="row.image_available" :src="`/api/v1${base()}/${row.id}/file?v=${row.revision}`" alt="后台上传的角色参考图" loading="lazy" />
      <div>
        <p>{{ row.label }}</p>
        <p v-if="!row.image_available" class="muted">图片文件不可读取，请替换图片。</p>
        <p class="muted">{{ row.enabled ? '允许模型使用' : '已停用' }}</p>
        <div class="button-row">
          <label class="button secondary">替换图片<input type="file" accept="image/png,image/jpeg,image/gif,image/webp" :aria-label="'替换参考图 ' + row.label" hidden :disabled="busy" @change="upload($event, row)" /></label>
          <button type="button" class="quiet" :disabled="busy" @click="toggle(row)">{{ row.enabled ? '停用参考图' : '启用参考图' }}</button>
          <button type="button" class="quiet" :disabled="busy" @click="action(async () => { await api(`${base()}/${row.id}`, 'DELETE'); await load(); }, '参考图已删除')">删除参考图</button>
        </div>
      </div>
    </div>
    <p v-if="!rows.length" class="muted">尚未上传角色参考图。</p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  </details>
</template>
<style scoped>
.reference-row { display: flex; gap: 12px; margin: 14px 0; align-items: flex-start; }
.reference-row img { width: 100px; height: 100px; object-fit: contain; flex-shrink: 0; }
.reference-row > div { min-width: 0; overflow-wrap: anywhere; }
@media (max-width: 600px) { .reference-row { flex-direction: column; } }
</style>
