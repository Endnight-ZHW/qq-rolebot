<script setup lang="ts">
import { ref } from "vue";
import Overlay from "./Overlay.vue";
import { changeLabel } from "../lib/change-labels";
import type { Change } from "../lib/drafts";
defineProps<{
  dirty: boolean;
  busy?: boolean;
  error?: string;
  saveLabel?: string;
  inline?: boolean;
  changes?: Change[];
}>();
defineEmits<{ save: []; reset: [] }>();
const show = ref(false);
const value = (v: unknown) =>
  v === undefined || v === null
    ? "未设置"
    : typeof v === "boolean"
      ? v
        ? "开启"
        : "关闭"
      : typeof v === "object"
        ? JSON.stringify(v)
        : String(v);
</script>
<template>
  <div
    v-if="dirty"
    class="save-bar"
    :class="{ inline }"
    role="region"
    aria-label="未保存修改"
  >
    <div>
      <strong>{{
        changes ? changes.length + " 项待保存修改" : "有未保存的修改"
      }}</strong
      ><small v-if="error" class="error-text" role="alert">{{ error }}</small
      ><small v-else>保存后应用到后续请求</small>
    </div>
    <div class="button-row">
      <button v-if="changes?.length" class="quiet" @click="show = true">
        查看变更
      </button>
      <button class="secondary" :disabled="busy" @click="$emit('reset')">
        放弃修改</button
      ><button class="primary" :disabled="busy" @click="$emit('save')">
        {{ busy ? "正在保存…" : saveLabel || "保存修改" }}
      </button>
    </div>
  </div>
  <Overlay v-if="show && dirty" title="待保存的变更" @close="show = false">
    <div v-for="change in changes" :key="change.path" class="change-row">
      <strong>{{ changeLabel(change.path) }}</strong
      ><span>{{ value(change.before) }} → {{ value(change.after) }}</span>
    </div>
  </Overlay>
</template>
