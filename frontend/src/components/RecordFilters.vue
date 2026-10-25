<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { date } from "../lib/format";
defineProps<{ busy?: boolean }>();
defineEmits<{ refresh: [] }>();
const route = useRoute(),
  router = useRouter();
const range = computed(() =>
  route.query.start && route.query.end
    ? date(Number(route.query.start)) + " — " + date(Number(route.query.end))
    : "全部留存记录",
);
function change(e: Event) {
  const value = Number((e.target as HTMLSelectElement).value),
    end = Date.now() / 1000;
  router.replace({
    query: {
      ...route.query,
      start: value ? end - value * 86400 : undefined,
      end: value ? end : undefined,
      page: undefined,
    },
  });
}
</script>
<template>
  <div class="record-filters">
    <label class="sr-only" for="record-range">记录时间范围</label
    ><select
      id="record-range"
      :value="route.query.start ? 'custom' : ''"
      @change="change"
    >
      <option value="">全部留存记录</option>
      <option v-if="route.query.start" value="custom">所选时间范围</option>
      <option value="1">最近 24 小时</option>
      <option value="7">最近 7 天</option>
      <option value="30">最近 30 天</option></select
    ><span class="muted range-description">{{ range }}</span
    ><slot /><button
      class="secondary"
      :disabled="busy"
      @click="$emit('refresh')"
    >
      刷新记录
    </button>
  </div>
</template>
