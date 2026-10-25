<script setup lang="ts">
import { computed } from "vue";
import { format } from "../lib/format";
const props = defineProps<{ value: any }>();
function text(v: any): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return v.map(text).join("\n");
  if (v.__record_image__)
    return (
      "[图片附件 · " + (v.status === "expired" ? "已过期" : "见附件区") + "]"
    );
  if (v.type === "image" || v.type === "image_url")
    return "[图片附件 · 见附件区]";
  if (v.type === "tool_use" || v.type === "function")
    return (
      "工具请求：" + (v.name || "") + "\n" + format(v.input || v.arguments)
    );
  if (v.type === "tool_result") return "工具结果：\n" + text(v.content);
  return v.text || v.thinking || text(v.content) || format(v);
}
const messages = computed(() => {
  const v = props.value || {};
  return [
    ...(v.system ? [{ role: "system", content: v.system }] : []),
    ...(Array.isArray(v.messages) ? v.messages : []),
  ];
});
const roles: Record<string, string> = {
  system: "系统指令",
  user: "用户",
  assistant: "模型",
  tool: "工具结果",
  developer: "开发者指令",
};
</script>
<template>
  <div class="payload-readable">
    <div v-if="value?.model" class="chips">
      <span class="tag">{{ value.model }}</span
      ><span v-if="value.max_tokens" class="tag"
        >输出上限 {{ value.max_tokens }}</span
      >
    </div>
    <article v-for="(m, i) in messages" :key="i" class="message-block">
      <span class="message-role">{{ roles[m.role] || m.role }}</span>
      <p>{{ text(m.content) }}</p>
      <pre v-if="m.tool_calls?.length">{{ format(m.tool_calls) }}</pre>
    </article>
    <p v-if="!messages.length" class="prose">
      {{ text(value?.notice || value) }}
    </p>
    <details v-if="value?.tools?.length">
      <summary>可用工具 · {{ value.tools.length }} 项</summary>
      <pre>{{ format(value.tools) }}</pre>
    </details>
  </div>
</template>
