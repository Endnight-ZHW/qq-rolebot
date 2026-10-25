<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../lib/api";
import { core } from "../lib/state";
import ModelCalls from "../ModelCalls.vue";
import Insights from "../Insights.vue";
import LogsPage from "./LogsPage.vue";
import UsagePage from "./UsagePage.vue";
const route = useRoute(),
  router = useRouter(),
  view = computed(() =>
    ["traces", "calls", "logs", "usage", "diagnostics"].includes(
      String(route.params.view),
    )
      ? String(route.params.view)
      : "traces",
  ),
  scope = computed(() => String(route.query.scope || ""));
function calls(id: string, trace = false) {
  router.push({
    path: "/observe/calls",
    query: {
      scope: scope.value || undefined,
      ...(trace ? { trace: id } : { call: id }),
    },
  });
}
function scopeChange(e: Event) {
  router.push({
    query: {
      ...route.query,
      scope: (e.target as HTMLSelectElement).value || undefined,
      call: undefined,
      trace: undefined,
      page: undefined,
    },
  });
}
</script>
<template>
  <nav class="section-tabs" aria-label="观测中心分类">
    <RouterLink
      v-for="t in [
        { id: 'traces', name: '请求追踪' },
        { id: 'calls', name: '模型调用' },
        { id: 'logs', name: '运行日志' },
        { id: 'usage', name: '使用统计' },
        { id: 'diagnostics', name: '效果检查' },
      ]"
      :key="t.id"
      :to="{ path: '/observe/' + t.id, query: { scope: scope || undefined } }"
      :class="{ active: view === t.id }"
      >{{ t.name }}</RouterLink
    >
  </nav>
  <div class="toolbar observation-scope">
    <span class="muted">查看范围</span
    ><select :value="scope" aria-label="观测会话范围" @change="scopeChange">
      <option value="">全部会话与系统</option>
      <option v-for="g in core.groups" :key="g.id" :value="g.id">
        {{ g.name }}
      </option></select
    ><RouterLink
      v-if="scope"
      class="link"
      :to="'/groups/' + encodeURIComponent(scope) + '/summary'"
      >返回群工作区 →</RouterLink
    >
  </div>
  <ModelCalls
    v-if="view === 'calls'"
    :key="scope"
    :scope="scope"
    :api="api"
    :initial-id="String(route.query.call || '')"
    :initial-trace="String(route.query.trace || '')"
  /><Insights
    v-else-if="view === 'traces' || view === 'diagnostics'"
    :key="view + scope"
    :view="view"
    :scope="scope"
    :persona="core.groups.find((g) => g.id === scope)?.persona || 'default'"
    :api="api"
    @open-call="calls($event)"
    @open-call-trace="calls($event, true)"
  /><LogsPage
    v-else-if="view === 'logs'"
    :key="scope"
    :scope="scope"
  /><UsagePage v-else :key="scope" :scope="scope" />
</template>
