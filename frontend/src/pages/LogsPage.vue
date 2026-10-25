<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, query } from "../lib/api";
import { date, format, label } from "../lib/format";
import { nameOf } from "../lib/state";
import { usePoll } from "../lib/poll";
const kinds: Record<string, string> = {
  tool: "工具执行",
  decision: "聊天决策",
  message_filter: "消息过滤",
  error: "异常",
  connection: "连接状态",
  tool_policy: "工具授权变更",
  qq_permissions: "QQ 权限变更",
  group: "会话配置",
  task_run: "任务执行",
  task_updated: "任务变更",
  settings: "系统设置",
  models: "模型配置",
  persona: "角色变更",
  forget: "遗忘操作",
};
import RecordFilters from "../components/RecordFilters.vue";
import Pagination from "../components/Pagination.vue";
const props = defineProps<{ scope: string }>(),
  route = useRoute(),
  router = useRouter(),
  rows = ref<any[]>([]),
  error = ref(""),
  busy = ref(false),
  updated = ref(0);
let gen = 0;
const offset = computed(() => Math.max(0, Number(route.query.page) || 0) * 50),
  kind = computed(() => String(route.query.kind || ""));
async function load() {
  const ticket = ++gen;
  busy.value = true;
  try {
    const result = await api(
      "/audit?" +
        query({
          scope: props.scope,
          kind: kind.value,
          start: route.query.start,
          end: route.query.end,
          offset: offset.value,
          limit: 50,
          status: route.query.status,
        }),
    );
    if (gen === ticket) {
      rows.value = result;
      error.value = "";
      updated.value = Date.now() / 1000;
    }
  } catch (e: any) {
    if (gen === ticket) error.value = e.message;
  } finally {
    if (gen === ticket) busy.value = false;
  }
}
watch(
  () => [
    props.scope,
    kind.value,
    route.query.start,
    route.query.end,
    route.query.status,
    offset.value,
  ],
  load,
  { immediate: true },
);
usePoll(load, 15000, false);
onUnmounted(() => gen++);
</script>
<template>
  <RecordFilters :busy="busy" @refresh="load"
    ><select
      :value="kind"
      aria-label="日志类型"
      @change="
        router.replace({
          query: {
            ...route.query,
            kind: ($event.target as HTMLSelectElement).value || undefined,
            page: undefined,
          },
        })
      "
    >
      <option value="">全部类型</option>
      <option v-for="(title, id) in kinds" :key="id" :value="id">
        {{ title }}
      </option>
      <option v-if="kind && !kinds[kind]" :value="kind">{{ kind }}</option>
    </select></RecordFilters
  >
  <p class="muted" v-if="updated">
    更新于 {{ date(updated) }} · 页面可见时自动刷新
  </p>
  <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  <article class="panel">
    <details v-for="r in rows" :key="r.id" class="log-row">
      <summary>
        <span class="tag">{{ kinds[r.kind] || label(r.kind) }}</span
        ><strong>{{ nameOf(r.scope) }}</strong
        ><time>{{ date(r.ts) }}</time>
      </summary>
      <pre>{{ format(r.data) }}</pre>
    </details>
    <p v-if="!rows.length" class="empty compact">
      {{ busy ? "正在读取日志…" : "当前筛选下暂无运行记录。" }}
    </p>
    <Pagination
      :offset="offset"
      :size="50"
      :count="rows.length"
      :busy="busy"
      @change="
        router.replace({
          query: { ...route.query, page: $event / 50 || undefined },
        })
      "
    />
  </article>
</template>
