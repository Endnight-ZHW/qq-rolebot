<script setup lang="ts">
import { confirmation, answer } from "../lib/confirm";
import Overlay from "./Overlay.vue";
</script>
<template>
  <Overlay
    v-if="confirmation"
    :title="confirmation.title"
    @close="answer(false)"
    ><p>{{ confirmation.message }}</p>
    <template #footer
      ><button class="secondary" @click="answer(false)">取消</button
      ><button class="danger-button" @click="answer(true)">
        {{ confirmation.confirmLabel }}
      </button></template
    ></Overlay
  >
</template>
