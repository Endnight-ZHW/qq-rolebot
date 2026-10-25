<script setup lang="ts">
import { computed } from "vue";
import { label } from "../lib/format";
const props = defineProps<{ status: string; text?: string }>();
const tone = computed(() =>
  ["success", "sent", "complete", "active", "online", "enabled"].includes(
    props.status,
  )
    ? "good"
    : [
          "error",
          "failed",
          "http_error",
          "network_error",
          "invalid_response",
        ].includes(props.status)
      ? "bad"
      : [
            "unknown",
            "missed",
            "interrupted",
            "unverified",
            "offline",
            "not_sent",
          ].includes(props.status)
        ? "warn"
        : ["running", "sending", "pending", "retry"].includes(props.status)
          ? "info"
          : "neutral",
);
</script>
<template>
  <span class="status-badge" :class="tone"
    ><i aria-hidden="true"></i>{{ text || label(status) }}</span
  >
</template>
