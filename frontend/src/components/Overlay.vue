<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from "vue";
import Icon from "./Icon.vue";
import { lockOverlay, unlockOverlay } from "../lib/overlay";
const props = defineProps<{
  title: string;
  drawer?: boolean;
  busy?: boolean;
}>();
const emit = defineEmits<{ close: [] }>();
const panel = ref<HTMLElement>();
const previous = document.activeElement as HTMLElement;
function key(e: KeyboardEvent) {
  if (e.key === "Escape") {
    e.preventDefault();
    if (!props.busy) emit("close");
  }
  if (e.key === "Tab") {
    const nodes = [
      ...panel.value!.querySelectorAll<HTMLElement>(
        'button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]',
      ),
    ].filter((el) => el.offsetParent !== null);
    if (!nodes.length) {
      e.preventDefault();
      return;
    }
    if (
      e.shiftKey &&
      (document.activeElement === nodes[0] ||
        document.activeElement === panel.value)
    ) {
      e.preventDefault();
      nodes.at(-1)!.focus();
    } else if (!e.shiftKey && document.activeElement === nodes.at(-1)) {
      e.preventDefault();
      nodes[0].focus();
    }
  }
}
onMounted(async () => {
  lockOverlay();
  await nextTick();
  panel.value?.focus();
});
onUnmounted(() => {
  unlockOverlay();
  if (previous?.isConnected) previous.focus();
});
</script>
<template>
  <Teleport to="body"
    ><div
      class="overlay-backdrop"
      :class="{ drawer }"
      @click.self="!busy && emit('close')"
    >
      <section
        ref="panel"
        class="overlay-panel"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
        @keydown="key"
      >
        <header>
          <h2>{{ title }}</h2>
          <button
            class="icon-button"
            aria-label="关闭"
            :disabled="busy"
            @click="emit('close')"
          >
            <Icon name="close" />
          </button>
        </header>
        <div class="overlay-body"><slot /></div>
        <footer v-if="$slots.footer"><slot name="footer" /></footer>
      </section></div
  ></Teleport>
</template>
