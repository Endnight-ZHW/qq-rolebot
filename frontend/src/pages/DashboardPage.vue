<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, query } from "../lib/api";
import { core, nameOf } from "../lib/state";
import { usePoll } from "../lib/poll";
import { date, number, money, duration, label } from "../lib/format";
import type { Dashboard } from "../lib/types";
import ChartPanel from "../components/ChartPanel.vue";
import StatusBadge from "../components/StatusBadge.vue";
import Icon from "../components/Icon.vue";
const props = defineProps<{ fixedScope?: string; compact?: boolean }>(),
  route = useRoute(),
  router = useRouter(),
  data = ref<Dashboard>(),
  loading = ref(false),
  error = ref("");
let controller: AbortController | undefined;
const scope = computed(
    () => props.fixedScope || String(route.query.scope || ""),
  ),
  range = computed(() =>
    ["24h", "7d", "30d"].includes(String(route.query.range))
      ? String(route.query.range)
      : "24h",
  );
const available = computed(() =>
  data.value?.series.some((s) => s.calls !== null),
);
const health = computed(() => core.health),
  uptime = computed(() => {
    const hours = Math.floor((health.value.uptime || 0) / 3600);
    return hours >= 24
      ? Math.floor(hours / 24) + " 天 " + (hours % 24) + " 小时"
      : hours +
          " 小时 " +
          Math.floor(((health.value.uptime || 0) % 3600) / 60) +
          " 分钟";
  });
async function load() {
  controller?.abort();
  const request = new AbortController();
  controller = request;
  loading.value = true;
  error.value = "";
  const end = Date.now() / 1000,
    start =
      end -
      ({ "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 }[range.value] ||
        86400);
  try {
    const result = await api<Dashboard>(
      "/dashboard?" + query({ scope: scope.value, start, end }),
      "GET",
      undefined,
      request.signal,
    );
    if (controller === request) data.value = result;
  } catch (e: any) {
    if (e.name !== "AbortError" && controller === request)
      error.value = e.message;
  } finally {
    if (controller === request) loading.value = false;
  }
}
watch(
  [scope, range],
  () => {
    data.value = undefined;
    void load();
  },
  { immediate: true },
);
usePoll(load, 30000, false);
onUnmounted(() => controller?.abort());
function change(values: any) {
  router.replace({ query: { ...route.query, ...values } });
}
function calls(status = "", purpose = "", index?: number) {
  const r = data.value?.range;
  let start = r?.start,
    end = r?.end;
  if (index !== undefined && data.value) {
    start = Math.max(r!.start, data.value.series[index].ts);
    end = Math.min(
      r!.end,
      data.value.series[index].ts + (r!.bucket === "hour" ? 3600 : 86400),
    );
  }
  router.push({
    path: "/observe/calls",
    query: {
      scope: scope.value || undefined,
      start,
      end,
      status: status || undefined,
      purpose: purpose || undefined,
    },
  });
}
const line = computed(() => ({
  tooltip: {
    trigger: "axis",
    valueFormatter: (v: any) => (v == null ? "不可用" : v + " 次"),
  },
  legend: { bottom: 0, icon: "roundRect", itemWidth: 12, itemHeight: 4 },
  grid: { left: 42, right: 18, top: 18, bottom: 58 },
  xAxis: {
    type: "category",
    boundaryGap: false,
    data: data.value?.series.map((s) => s.label) || [],
    axisLine: { lineStyle: { color: "#e4eae8" } },
    axisTick: { show: false },
    axisLabel: { fontSize: 11, hideOverlap: true },
  },
  yAxis: {
    type: "value",
    minInterval: 1,
    splitLine: { lineStyle: { color: "#edf1ef", type: "dashed" } },
  },
  series: [
    {
      name: "成功",
      type: "line",
      smooth: 0.25,
      symbol: "circle",
      symbolSize: 5,
      showSymbol: true,
      lineStyle: { width: 2 },
      areaStyle: { color: "#248677", opacity: 0.08 },
      data: data.value?.series.map((s) => s.success) || [],
    },
    {
      name: "失败",
      type: "line",
      smooth: 0.25,
      symbol: "circle",
      symbolSize: 5,
      showSymbol: true,
      itemStyle: { color: "#c67657" },
      lineStyle: { width: 2 },
      data: data.value?.series.map((s) => s.failed) || [],
    },
  ],
}));
const donut = computed(() => ({
  tooltip: { trigger: "item" },
  legend: { bottom: 0 },
  series: [
    {
      type: "pie",
      stillShowZeroSum: false,
      radius: ["55%", "73%"],
      center: ["50%", "44%"],
      label: { show: false },
      data: [
        {
          name: "成功",
          value: data.value?.metrics.success || 0,
          itemStyle: { color: "#248677" },
        },
        {
          name: "失败",
          value: data.value?.metrics.failed || 0,
          itemStyle: { color: "#c67657" },
        },
        {
          name: "其他状态",
          value: Object.values(data.value?.metrics.excluded || {}).reduce(
            (a, b) => a + b,
            0,
          ),
          itemStyle: { color: "#b7c6c2" },
        },
      ],
    },
  ],
}));
const purposeChart = computed(() => ({
  tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
  legend: { bottom: 0, itemWidth: 10, itemHeight: 10 },
  grid: { left: 80, right: 18, top: 8, bottom: 50 },
  xAxis: {
    type: "value",
    splitLine: { lineStyle: { color: "#edf1ef", type: "dashed" } },
  },
  yAxis: {
    type: "category",
    data: data.value?.purposes.map((p) => label(p.purpose)) || [],
    axisLine: { show: false },
    axisTick: { show: false },
  },
  series: [
    {
      name: "非缓存输入",
      type: "bar",
      stack: "tokens",
      barMaxWidth: 16,
      data:
        data.value?.purposes.map((p) =>
          Math.max(0, p.input_tokens - p.cached_tokens),
        ) || [],
    },
    {
      name: "缓存输入",
      type: "bar",
      stack: "tokens",
      data: data.value?.purposes.map((p) => p.cached_tokens) || [],
    },
    {
      name: "输出",
      type: "bar",
      stack: "tokens",
      data: data.value?.purposes.map((p) => p.output_tokens) || [],
    },
  ],
}));
function alertTarget(a: any) {
  return a.type === "call"
    ? {
        path: "/observe/calls",
        query: { scope: a.scope || undefined, call: a.id },
      }
    : a.type === "task"
      ? { path: "/tasks", query: { scope: a.scope, task: a.id } }
      : { path: "/observe/diagnostics", query: { scope: a.scope } };
}
</script>
<template>
  <div v-if="!compact && !health.initialized" class="setup-banner">
    <div>
      <strong>完成连接，开始第一段陪伴</strong>
      <p>配置模型与管理员，然后启用一个熟悉的群。</p>
    </div>
    <div class="button-row">
      <RouterLink class="secondary" to="/configuration/models"
        >配置模型</RouterLink
      ><RouterLink class="primary" to="/settings">设置管理员 →</RouterLink>
    </div>
  </div>
  <div class="health-strip">
    <span class="health-caption">全局运行状态</span
    ><StatusBadge
      :status="health.onebot_connected ? 'online' : 'offline'"
      :text="health.onebot_connected ? 'NapCat 已连接' : 'NapCat 未连接'"
    /><span
      ><i class="mini-dot" :class="{ good: health.model_configured }"></i
      >{{ health.model_configured ? "模型已配置" : "模型未配置" }}</span
    ><span
      >运行 <strong>{{ uptime }}</strong></span
    ><span
      >队列 <strong>{{ health.queue_depth || 0 }}</strong></span
    >
  </div>
  <div class="toolbar dashboard-toolbar">
    <div class="range-switch" aria-label="统计时间范围">
      <button
        v-for="r in [
          { id: '24h', name: '24 小时' },
          { id: '7d', name: '7 天' },
          { id: '30d', name: '30 天' },
        ]"
        :key="r.id"
        :class="{ active: range === r.id }"
        @click="change({ range: r.id })"
      >
        {{ r.name }}
      </button>
    </div>
    <select
      v-if="!fixedScope"
      :value="scope"
      aria-label="统计会话范围"
      @change="
        change({
          scope: ($event.target as HTMLSelectElement).value || undefined,
        })
      "
    >
      <option value="">全部会话与系统</option>
      <option v-for="g in core.groups" :key="g.id" :value="g.id">
        {{ g.name }}
      </option></select
    ><span class="muted updated-at">{{
      data ? "更新于 " + date(data.generated_at) : "读取运行数据…"
    }}</span
    ><button
      class="icon-button"
      aria-label="刷新统计"
      :disabled="loading"
      @click="load"
    >
      <Icon name="refresh" />
    </button>
  </div>
  <div v-if="error" class="error-banner" role="alert">
    {{ error }} <span v-if="data">正在展示上次成功读取的数据。</span
    ><button class="link" @click="load">重试</button>
  </div>
  <div
    v-if="!data && loading"
    class="dashboard-skeleton"
    aria-label="正在加载统计"
  >
    <div v-for="i in 4" :key="i" class="skeleton"></div>
  </div>
  <template v-if="data"
    ><div class="metric-grid">
      <button class="metric-card" @click="calls()">
        <span>接口调用 <Icon name="observe" /></span
        ><strong
          >{{ available ? number(data.metrics.calls) : "—"
          }}<small>次</small></strong
        ><small>按实际请求尝试计数，包含重试</small></button
      ><button class="metric-card" @click="calls('failure')">
        <span>调用成功率 <Icon name="arrow" /></span
        ><strong>{{
          data.metrics.success_rate === null
            ? "—"
            : (data.metrics.success_rate * 100).toFixed(1) + "%"
        }}</strong
        ><small
          ><i class="mini-dot good"></i
          >{{
            available
              ? data.metrics.success +
                " 成功 · " +
                data.metrics.failed +
                " 失败"
              : "暂无可用样本"
          }}</small
        ></button
      ><button class="metric-card" @click="calls()">
        <span>P95 请求耗时 <Icon name="observe" /></span
        ><strong>{{ duration(data.metrics.p95_ms) }}</strong
        ><small>已结束且结果明确的请求</small></button
      ><RouterLink
        class="metric-card"
        :to="{
          path: '/observe/usage',
          query: {
            scope: scope || undefined,
            start: data.range.start,
            end: data.range.end,
          },
        }"
        ><span>估算费用 <Icon name="arrow" /></span
        ><strong v-if="!data.metrics.costs.length">—</strong
        ><strong
          v-for="c in data.metrics.costs"
          :key="c.currency"
          class="metric-money"
          ><small>{{ c.currency }}</small> {{ money(c.cost) }}</strong
        ><small>按配置费率估算，以供应商账单为准</small></RouterLink
      >
    </div>
    <p v-if="data.range.partial" class="coverage-note">
      统计基于当前留存记录，可用记录起点
      {{
        date(data.range.coverage_start)
      }}。较早时段标为不可用；未采集或已清理的记录无法补回。费用使用独立用量账本。
    </p>
    <div class="dashboard-main">
      <ChartPanel
        title="调用趋势"
        :subtitle="
          data.range.bucket === 'hour'
            ? '每小时请求结果 · 点击数据点查看记录'
            : '每日请求结果 · 北京时间'
        "
        :option="line"
        :columns="['时间', '成功', '失败']"
        :rows="data.series.map((s) => [s.label, s.success, s.failed])"
        :summary="
          '共 ' +
          data.metrics.success +
          ' 次成功，' +
          data.metrics.failed +
          ' 次失败。'
        "
        @select="
          calls(
            $event.seriesName === '失败' ? 'failure' : 'success',
            '',
            $event.dataIndex,
          )
        "
      />
      <article class="panel attention-panel">
        <div class="section-heading">
          <h3>需要关注</h3>
          <span class="count-badge">{{
            data.alerts.length +
            (!health.onebot_connected ? 1 : 0) +
            (!health.model_configured ? 1 : 0)
          }}</span>
        </div>
        <div v-if="!health.onebot_connected" class="attention-item">
          <span class="alert-dot"></span>
          <div>
            <strong>NapCat 尚未连接</strong
            ><small>请检查 QQ 登录和 OneBot 连接状态</small>
          </div>
        </div>
        <RouterLink
          v-if="!health.model_configured"
          class="attention-item"
          to="/configuration/models"
          ><span class="alert-dot"></span>
          <div>
            <strong>模型尚未配置</strong
            ><small>完善供应商密钥与任务路由 →</small>
          </div></RouterLink
        ><RouterLink
          v-for="a in data.alerts"
          :key="a.type + a.id"
          :to="alertTarget(a)"
          class="attention-item"
          ><span class="alert-dot"></span>
          <div>
            <strong>{{ a.title }}</strong
            ><small>{{ nameOf(a.scope) }} · {{ date(a.ts) }}</small>
          </div>
          <StatusBadge :status="a.status"
        /></RouterLink>
        <div
          v-if="
            !data.alerts.length &&
            health.onebot_connected &&
            health.model_configured
          "
          class="empty compact"
        >
          <span class="good-check">✓</span><strong>目前一切平稳</strong>
          <p>当前范围没有需要关注的异常。</p>
        </div>
        <RouterLink
          class="link attention-more"
          :to="{
            path: '/observe/calls',
            query: {
              scope: scope || undefined,
              start: data.range.start,
              end: data.range.end,
            },
          }"
          >查看调用记录 →</RouterLink
        >
      </article>
    </div>
    <div class="dashboard-secondary">
      <ChartPanel
        title="调用结果分布"
        subtitle="取消、中断、未发送和处理中单独计数"
        :empty="data.metrics.calls === 0"
        :option="donut"
        :columns="['结果', '次数']"
        :rows="[
          ['成功', data.metrics.success],
          ['失败', data.metrics.failed],
          ...Object.entries(data.metrics.excluded).map(([k, v]) => [
            label(k),
            v,
          ]),
        ]"
        summary="调用结果分布，下方数据表提供全部状态。"
        @select="
          calls(
            $event.name === '失败'
              ? 'failure'
              : $event.name === '成功'
                ? 'success'
                : '',
          )
        "
      /><ChartPanel
        title="各用途 token 消耗"
        subtitle="缓存输入是总输入的一部分，不重复累计"
        :option="purposeChart"
        :columns="['用途', '输入', '其中缓存', '输出']"
        :rows="
          data.purposes.map((p) => [
            label(p.purpose),
            p.input_tokens,
            p.cached_tokens,
            p.output_tokens,
          ])
        "
        summary="按用途展示输入、缓存与输出 token。"
        @select="calls('', data.purposes[$event.dataIndex]?.purpose)"
      />
    </div>
    <div class="dashboard-bottom">
      <article v-if="!fixedScope" class="panel group-status-panel">
        <div class="section-heading">
          <h3>
            群运行概况 <small class="muted">{{ data.groups.length }} 个</small>
          </h3>
          <RouterLink class="link" to="/groups">管理会话 →</RouterLink>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>群 / 会话</th>
                <th>状态</th>
                <th>调用</th>
                <th>失败</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="g in data.groups" :key="g.id">
                <td data-label="会话">
                  <strong>{{ g.name }}</strong
                  ><small class="table-sub"
                    >{{ g.id.split(":")[1] === "p" ? "私聊" : "QQ群" }} ·
                    {{ g.id.split(":")[2] }}</small
                  >
                </td>
                <td data-label="状态">
                  <StatusBadge
                    :status="
                      g.paused ? 'paused' : g.enabled ? 'enabled' : 'disabled'
                    "
                    :text="
                      g.paused ? '已暂停' : g.enabled ? '已启用' : '未启用'
                    "
                  />
                </td>
                <td data-label="调用">{{ available ? g.calls : "—" }}</td>
                <td data-label="失败" :class="{ danger: g.failures }">
                  {{ available ? g.failures : "—" }}
                </td>
                <td>
                  <RouterLink
                    class="link"
                    :to="'/groups/' + encodeURIComponent(g.id) + '/summary'"
                    >进入工作区 →</RouterLink
                  >
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="!data.groups.length" class="empty compact">
            暂无接入会话，添加一个群开始使用。
          </div>
        </div>
      </article>
      <article class="panel upcoming-panel">
        <div class="section-heading">
          <h3>接下来要做</h3>
          <RouterLink
            class="link"
            :to="{ path: '/tasks', query: { scope: scope || undefined } }"
            >任务中心 →</RouterLink
          >
        </div>
        <RouterLink
          v-for="t in data.tasks"
          :key="t.id"
          :to="{ path: '/tasks', query: { scope: t.scope, task: t.id } }"
          class="upcoming-row"
          ><span class="task-time">{{
            new Date(t.due * 1000).toLocaleTimeString("zh-CN", {
              timeZone: "Asia/Shanghai",
              hour: "2-digit",
              minute: "2-digit",
            })
          }}</span>
          <div>
            <strong>{{ t.text }}</strong
            ><small>{{ date(t.due) }} · {{ nameOf(t.scope) }}</small>
          </div>
          <StatusBadge :status="t.status"
        /></RouterLink>
        <div v-if="!data.tasks.length" class="empty compact">
          <Icon name="tasks" /><strong>没有待执行任务</strong>
          <p>需要提醒或定时查询时，来这里安排。</p>
        </div>
      </article>
    </div></template
  >
</template>
