<script setup lang="ts">
defineProps<{ offset: number; size: number; count: number; busy?: boolean }>();
defineEmits<{ change: [number] }>();
</script>
<template>
  <div class="pagination">
    <span class="muted"
      >第 {{ Math.floor(offset / size) + 1 }} 页 · 本页 {{ count }} 条</span
    >
    <div class="button-row">
      <button
        class="secondary"
        :disabled="!offset || busy"
        @click="$emit('change', Math.max(0, offset - size))"
      >
        上一页</button
      ><button
        class="secondary"
        :disabled="count < size || busy"
        @click="$emit('change', offset + size)"
      >
        下一页
      </button>
    </div>
  </div>
</template>
