<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import * as echarts from "echarts/core";
import { LineChart, BarChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  AriaComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
echarts.use([
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  AriaComponent,
  SVGRenderer,
]);
const props = defineProps<{
  title: string;
  subtitle?: string;
  option: any;
  columns: string[];
  rows: (string | number | null)[][];
  summary: string;
  empty?: boolean;
}>();
const isEmpty = computed(
  () =>
    props.empty ||
    !props.rows.length ||
    props.rows.every((r) => r.slice(1).every((v) => v === null)),
);
const emit = defineEmits<{
  select: [{ name: string; seriesName: string; dataIndex: number }];
}>();
const el = ref<HTMLElement>();
let chart: echarts.ECharts | undefined, observer: ResizeObserver;
function render() {
  chart?.setOption(
    {
      color: ["#248677", "#83bdb2", "#c67657", "#e0ba73"],
      textStyle: {
        fontFamily: "system-ui, Microsoft YaHei, sans-serif",
        color: "#73817f",
      },
      aria: { enabled: true },
      animation: false,
      ...props.option,
    },
    true,
  );
}
watch(() => props.option, render, { deep: true });
onMounted(() => {
  chart = echarts.init(el.value, undefined, { renderer: "svg" });
  render();
  chart.on("click", (p: any) =>
    emit("select", {
      name: p.name,
      seriesName: p.seriesName,
      dataIndex: p.dataIndex,
    }),
  );
  observer = new ResizeObserver(() => chart?.resize());
  observer.observe(el.value!);
});
onUnmounted(() => {
  observer?.disconnect();
  chart?.dispose();
});
</script>
<template>
  <article class="panel chart-panel">
    <div class="section-heading">
      <div>
        <h3>{{ title }}</h3>
        <small v-if="subtitle" class="muted">{{ subtitle }}</small>
      </div>
      <slot name="action" />
    </div>
    <p class="sr-only">{{ summary }}</p>
    <div class="chart-stage">
      <div
        ref="el"
        class="chart-canvas"
        role="img"
        :aria-label="summary"
        :class="{ 'chart-unavailable': isEmpty }"
      ></div>
      <div v-if="isEmpty" class="chart-empty">
        当前范围暂无可用记录<small>产生数据后，这里会展示实际趋势</small>
      </div>
    </div>
    <details class="chart-data">
      <summary>查看数据表</summary>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th v-for="c in columns" :key="c">{{ c }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in rows" :key="i">
              <td v-for="(v, j) in row" :key="j" :data-label="columns[j]">
                {{ v === null ? "未采集 / 不可用" : v }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!rows.length" class="muted">当前范围暂无记录。</p>
      </div>
    </details>
  </article>
</template>
