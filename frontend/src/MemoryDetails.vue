<script setup lang="ts">
import { computed, ref } from "vue";
const props = defineProps<{
  memory: any;
  api: (path: string, method?: string, body?: any) => Promise<any>;
}>();
const emit = defineEmits<{ changed: []; openCall: [string] }>();
const versions = ref<any[]>([]),
  error = ref(""),
  busy = ref(false);
const labels: Record<string, string> = {
  active: "当前有效",
  conflict: "存在冲突",
  unverified: "尚未核实",
  archived: "已归档",
  self_report: "主体本人表达",
  third_party: "他人转述",
  inference: "推测",
  story: "角色剧情",
  manual: "管理员确认",
  legacy: "历史记录",
};
const claims: Record<string, string> = {
  fact: "事实记录",
  preference: "偏好陈述",
  wish: "愿望表达",
  expression: "感受表达",
  event: "相处经历",
  story: "角色剧情",
  unknown: "含义待核实",
};
const temporal: Record<string, string> = {
  ongoing: "持续信息",
  episode: "当时的表达或经历",
  unknown: "持续性未确认",
};
const reasons: Record<string, string> = {
  administrator_semantic_correction: "管理员核对语义与归属",
  previous: "修正前记录",
  extracted: "自动提取",
  initial: "初始记录",
  administrator: "管理员编辑",
  restore: "恢复版本",
  self_correction: "本人纠正",
  merge_sources: "合并来源",
  status: "状态调整",
  resolve: "确认冲突",
  conflict: "发现冲突",
};
const meaning = computed(() => props.memory.interpretation || {});
const people = (values: any[] = []) =>
  values.map((p) => `${p.name}（QQ ${p.qq}）`).join("、");
const date = (ts: number) =>
  new Date(ts * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
async function load(event: Event) {
  if (!(event.target as HTMLDetailsElement).open) return;
  try {
    versions.value = await props.api(
      `/memories/${props.memory.id}/versions?scope=${encodeURIComponent(props.memory.scope)}`,
    );
  } catch (e: any) {
    error.value = e.message;
  }
}
async function change(action: string, body?: any) {
  busy.value = true;
  error.value = "";
  try {
    await props.api(
      `/memories/${props.memory.id}/${action}?scope=${encodeURIComponent(props.memory.scope)}`,
      action === "state" ? "PUT" : "POST",
      body,
    );
    versions.value = await props.api(
      `/memories/${props.memory.id}/versions?scope=${encodeURIComponent(props.memory.scope)}`,
    );
    emit("changed");
  } catch (e: any) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="memory-meta">
    <span class="tag">{{ labels[memory.data.status || "active"] }}</span
    ><span class="muted"
      >v{{ memory.data.version || 1 }} ·
      {{ labels[memory.data.evidence_kind || "legacy"] }}</span
    >
  </div>
  <div class="memory-identity">
    <p>
      主体：{{
        people(meaning.subjects) ||
        memory.data.subject_ids?.join("、") ||
        "群内常识"
      }}
    </p>
    <p v-if="meaning.objects?.length">对象：{{ people(meaning.objects) }}</p>
    <p>
      {{ claims[memory.data.claim_kind || "unknown"] }} ·
      {{ temporal[memory.data.temporal_scope || "unknown"] }}
    </p>
    <p v-if="memory.data.occurred_at">
      发生时间：{{ date(memory.data.occurred_at)
      }}<span v-if="memory.data.last_observed_at > memory.data.occurred_at"
        >；最近表达：{{ date(memory.data.last_observed_at) }}</span
      >
    </p>
    <p
      v-if="['wish', 'expression'].includes(memory.data.claim_kind)"
      class="muted"
    >
      仅记录该成员当时的表达，不代表对方同意、关系成立或长期承诺。
    </p>
  </div>
  <details class="memory-details" @toggle="load">
    <summary>来源与版本</summary>
    <p v-if="error" class="error-text">{{ error }}</p>
    <p class="muted">
      陈述者：{{
        people(meaning.declarants) ||
        memory.data.declarant_ids?.join("、") ||
        "未记录"
      }}
    </p>
    <p
      class="muted"
      v-if="
        memory.data.confidence != null && memory.data.evidence_kind !== 'manual'
      "
    >
      模型提取分数：{{
        memory.data.confidence
      }}（未校准，不代表事实正确率或双方认可程度）
    </p>
    <blockquote v-for="(e, i) in memory.data.evidence || []" :key="i">
      {{ e.quote }}<small>来源 {{ e.source_id }} · 陈述者 {{ e.actor }}</small>
    </blockquote>
    <p v-if="!memory.data.evidence?.length" class="muted">
      {{ memory.data.source_ids?.join("、") || "没有原始消息摘录" }}
    </p>
    <div class="memory-actions">
      <button
        class="secondary"
        :disabled="busy"
        @click="change('resolve')"
        v-if="['conflict', 'unverified'].includes(memory.data.status)"
      >
        确认这条记忆</button
      ><button
        class="quiet"
        :disabled="busy"
        @click="
          change('state', {
            status: memory.data.status === 'archived' ? 'active' : 'archived',
          })
        "
      >
        {{ memory.data.status === "archived" ? "恢复使用" : "归档" }}
      </button>
    </div>
    <div class="version-item" v-for="v in versions" :key="v.version">
      <div>
        <strong>版本 {{ v.version }}</strong
        ><small>{{ date(v.ts) }} · {{ reasons[v.reason] || v.reason }}</small>
        <p>{{ v.text }}</p>
        <small
          >{{ v.persona ? "角色记忆：" + v.persona : "同群共享 · 跨角色" }} ·
          {{ claims[v.data?.claim_kind || "unknown"] }}</small
        >
        <button
          v-if="v.data?.formation?.call_id"
          class="link"
          @click="emit('openCall', v.data.formation.call_id)"
        >
          查看形成这条记忆的模型调用
        </button>
      </div>
      <button
        class="quiet"
        :disabled="busy || v.version === (memory.data.version || 1)"
        @click="change('restore/' + v.version)"
      >
        恢复此版本
      </button>
    </div>
  </details>
</template>
<style scoped>
.memory-identity {
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.memory-identity p {
  margin: 4px 0;
}
.memory-meta,
.memory-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin: 10px 0;
}
.memory-details {
  margin-top: 12px;
  border-top: 1px solid var(--line);
  padding-top: 10px;
}
.memory-details summary {
  cursor: pointer;
  color: var(--teal);
}
blockquote {
  margin: 12px 0;
  padding: 10px 14px;
  background: #f4f7f3;
  border-left: 3px solid #b9d3b8;
  overflow-wrap: anywhere;
}
small {
  display: block;
  color: var(--muted);
}
.version-item {
  display: flex;
  justify-content: space-between;
  gap: 15px;
  border-top: 1px solid var(--line);
  padding: 14px 0;
}
.version-item p {
  margin-top: 8px;
  overflow-wrap: anywhere;
}
.version-item button {
  flex-shrink: 0;
  align-self: start;
}
</style>
