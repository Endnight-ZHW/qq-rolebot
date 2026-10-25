<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
import { useRoute } from "vue-router";
import { api, query } from "../lib/api";
import { number, money, label } from "../lib/format";
import { nameOf } from "../lib/state";
import RecordFilters from "../components/RecordFilters.vue";
import ChartPanel from "../components/ChartPanel.vue";
import { usePoll } from "../lib/poll";
const props = defineProps<{ scope: string }>(),
  route = useRoute(),
  data = ref<any>({ rows: [], daily: [] }),
  error = ref(""),
  busy = ref(false);
let gen = 0;
async function load() {
  const ticket = ++gen;
  busy.value = true;
  try {
    const r = await api(
      "/usage?" +
        query({
          scope: props.scope,
          start: route.query.start,
          end: route.query.end,
        }),
    );
    if (gen === ticket) {
      data.value = r;
      error.value = "";
    }
  } catch (e: any) {
    if (ticket === gen) error.value = e.message;
  } finally {
    if (ticket === gen) busy.value = false;
  }
}
watch(() => [props.scope, route.query.start, route.query.end], load, {
  immediate: true,
});
usePoll(load, 30000, false);
onUnmounted(() => gen++);
const totals = computed(() =>
  data.value.rows.reduce(
    (a: any, r: any) => ({
      calls: a.calls + r.calls,
      input: a.input + r.input_tokens,
      cached: a.cached + r.cached_tokens,
      output: a.output + r.output_tokens,
    }),
    { calls: 0, input: 0, cached: 0, output: 0 },
  ),
);
const currencies = computed(
  () => [...new Set(data.value.daily.map((d: any) => d.currency))] as string[],
);
const days = computed(() => {
  if (!data.value.daily.length) return [];
  const present = data.value.daily.map((d: any) => d.day).sort();
  const first = route.query.start
      ? new Date(Number(route.query.start) * 1000 + 28800000)
          .toISOString()
          .slice(0, 10)
      : present[0],
    last = route.query.end
      ? new Date(Number(route.query.end) * 1000 - 1 + 28800000)
          .toISOString()
          .slice(0, 10)
      : present.at(-1);
  const dates = [];
  let t = Date.parse(first + "T00:00:00Z");
  while (t <= Date.parse(last + "T00:00:00Z") && dates.length < 367) {
    dates.push(new Date(t).toISOString().slice(0, 10));
    t += 86400000;
  }
  return dates;
});
const option = computed(() => ({
  tooltip: { trigger: "axis" },
  legend: { bottom: 0 },
  grid: { left: 64, right: 24, top: 16, bottom: 60 },
  xAxis: {
    type: "category",
    data: days.value,
    axisLabel: { hideOverlap: true },
  },
  yAxis: { type: "value", splitLine: { lineStyle: { color: "#edf1ef" } } },
  series: currencies.value.map((currency) => ({
    name: currency,
    type: "bar",
    barMaxWidth: 24,
    data: days.value.map(
      (day) =>
        data.value.daily.find(
          (r: any) => r.day === day && r.currency === currency,
        )?.cost || 0,
    ),
  })),
}));
</script>
<template>
  <RecordFilters :busy="busy" @refresh="load" />
  <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  <p class="muted">{{ data.note }} · 不同币种分别统计，未进行汇率换算。</p>
  <div class="metric-grid">
    <article class="metric-card">
      <span>计费用量记录</span><strong>{{ number(totals.calls) }}</strong
      ><small>含联网工具，与请求尝试数口径不同</small>
    </article>
    <article class="metric-card">
      <span>输入 token</span><strong>{{ number(totals.input) }}</strong
      ><small>已包含缓存输入</small>
    </article>
    <article class="metric-card">
      <span>缓存命中 token</span><strong>{{ number(totals.cached) }}</strong
      ><small>输入 token 的子集</small>
    </article>
    <article class="metric-card">
      <span>输出 token</span><strong>{{ number(totals.output) }}</strong
      ><small>依据供应商接口实际回传</small>
    </article>
  </div>
  <ChartPanel
    title="每日估算费用"
    subtitle="北京时间 · 按币种分别展示"
    :option="option"
    :columns="['日期', ...currencies]"
    :rows="
      days.map((day) => [
        day,
        ...currencies.map((c) =>
          money(
            data.daily.find((r: any) => r.day === day && r.currency === c)
              ?.cost || 0,
          ),
        ),
      ])
    "
    summary="每日费用按币种分别展示，不汇总不同币种。"
  />
  <article class="panel">
    <h3>阶段用量与缓存</h3>
    <p class="muted">使用供应商返回的真实用量。未知字段显示 —；压缩也计入成本。命中率仅统计有缓存字段的请求。</p>
    <div class="table-wrap"><table><thead><tr>
      <th>阶段</th><th>调用数</th><th>输入</th><th>命中</th><th>未命中</th><th>命中率</th><th>输出</th><th>缺失指标</th><th>估算费用</th>
    </tr></thead><tbody><tr v-for="(r,i) in data.by_stage || []" :key="i">
      <td>{{ label(r.stage) }}</td><td>{{ r.calls }}</td><td>{{ r.input_tokens == null ? '—' : number(r.input_tokens) }}</td>
      <td>{{ r.cached_tokens == null ? '—' : number(r.cached_tokens) }}</td><td>{{ r.cache_miss_tokens == null ? '—' : number(r.cache_miss_tokens) }}</td>
      <td>{{ r.cache_hit_ratio == null ? '—' : (r.cache_hit_ratio * 100).toFixed(1) + '%' }}</td>
      <td>{{ r.output_tokens == null ? '—' : number(r.output_tokens) }}</td><td>{{ r.unknown_calls }} 次</td><td>{{ r.currency }} {{ money(r.cost) }}</td>
    </tr></tbody></table><p v-if="!data.by_stage?.length" class="empty compact">暂无阶段指标。</p></div>
  </article>
  <article class="panel">
    <h3>按会话、模型与用途统计</h3>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>会话</th>
            <th>模型 / 工具</th>
            <th>用途</th>
            <th>记录数</th>
            <th>输出 token</th>
            <th>估算费用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in data.rows" :key="i">
            <td data-label="会话">{{ nameOf(r.scope) }}</td>
            <td data-label="模型">{{ r.model }}</td>
            <td data-label="用途">{{ label(r.purpose) }}</td>
            <td data-label="记录数">{{ r.calls }}</td>
            <td data-label="输出 token">{{ number(r.output_tokens) }}</td>
            <td data-label="估算费用">{{ r.currency }} {{ money(r.cost) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!data.rows.length" class="empty compact">
        当前范围暂无计费用量。
      </p>
    </div>
  </article>
</template>
