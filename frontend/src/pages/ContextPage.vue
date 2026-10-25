<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api, query } from "../lib/api";
import { useGroup } from "../lib/groups";
import { core, useAction } from "../lib/state";
import { date, format, number } from "../lib/format";
import { confirmAction } from "../lib/confirm";
const { scope, group } = useGroup(),
  data = ref<any>({}),
  loading = ref(true),
  { busy, error, action } = useAction(),
  role = computed(
    () =>
      core.personas.find((p) => p.id === group.value?.persona)?.name || "机器人",
  );
async function load() {
  await action(async () => {
    data.value = await api(
      "/context?" +
        query({ scope: scope.value, persona: group.value?.persona }),
    );
  }, "");
  loading.value = false;
}
async function reset() {
  if (
    !(await confirmAction(
      "重置当前上下文",
      "将清除「" +
        role.value +
        "」在当前会话的工作上下文和预览，长期记忆会保留。",
      "重置上下文",
    ))
  )
    return;
  await action(async () => {
    await api("/context/reset", "POST", {
      scope: scope.value,
      persona: group.value?.persona,
    });
    data.value = await api(
      "/context?" +
        query({ scope: scope.value, persona: group.value?.persona }),
    );
  }, "上下文已重置，长期记忆保留");
}
onMounted(load);
</script>
<template>
  <div class="page-intro">
    <p>{{ role }}最近一轮使用的上下文与摘要</p>
    <div class="button-row">
      <RouterLink class="secondary" to="/settings?section=context"
        >调整全局预算</RouterLink
      >
      <button class="secondary" :disabled="busy" @click="load">刷新</button
      ><button class="danger-button" :disabled="busy" @click="reset">
        重置当前上下文
      </button>
    </div>
  </div>
  <p v-if="error" role="alert" class="error-text">{{ error }}</p>
  <div v-if="loading" class="skeleton panel"></div>
  <template v-else
    ><article class="panel">
      <div class="section-heading">
        <h3>上下文预算</h3>
        <span class="tag">{{ date(data.preview?.ts) }}</span>
      </div>
      <template v-if="data.preview && !data.preview.notice"
        ><div class="budget-total">
          <strong>{{ number(data.preview.estimated_tokens) }}</strong
          ><span>/ {{ number(data.preview.budget) }} 估算 token</span>
        </div>
        <meter
          :value="data.preview.estimated_tokens"
          :max="data.preview.budget || 1"
          min="0"
          aria-label="上下文预算占用"
        ></meter>
        <div class="budget-sections">
          <div v-for="s in data.preview.sections || []" :key="s.name">
            <span>{{ s.name }}</span
            ><strong>{{ number(s.estimated_tokens) }}</strong
            ><meter
              :value="s.estimated_tokens"
              :max="data.preview.budget || 1"
              :aria-label="s.name + '占用'"
            />
          </div>
        </div>
        <p class="muted">
          {{ data.preview.stage === "gate" ? "旁听判定" : "回复阶段" }} · 召回
          {{ data.preview.memory_ids?.length || 0 }} 条记忆
        </p></template
      >
      <p v-else class="empty compact">
        {{
          data.preview?.notice || "尚无模型请求，产生对话后会显示实际上下文。"
        }}
      </p>
    </article>
    <article v-if="data.preview && !data.preview.notice" class="panel">
      <h3>最近消息与召回资料</h3>
      <div
        v-for="(m, i) in data.preview.context?.消息"
        :key="i"
        class="transcript"
      >
        <span class="tag"
          >{{ m.speaker || m.qq }} ·
          {{ m.role === "assistant" ? "机器人" : m.qq }}</span
        >
        <p>{{ m.text }}</p>
      </div>
      <details>
        <summary>组装资料</summary>
        <pre>{{ format(data.preview.context) }}</pre>
      </details>
      <details>
        <summary>记忆来源与去重明细</summary>
        <pre>{{ format(data.preview.memory_sources || []) }}</pre>
      </details>
    </article></template
  >
</template>
