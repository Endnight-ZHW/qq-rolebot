<script setup lang="ts">
import { useId } from "vue";
withDefaults(
  defineProps<{
    modelValue: boolean;
    savedValue?: boolean;
    compact?: boolean;
    label: string;
    description?: string;
    onText?: string;
    offText?: string;
    tone?: "normal" | "warning" | "danger";
    disabled?: boolean;
  }>(),
  { onText: "开启", offText: "关闭", tone: "normal", disabled: false },
);
const emit = defineEmits<{ "update:modelValue": [boolean] }>();
const descriptionId = useId();
</script>
<template>
  <label
    class="setting-switch"
    :class="{ checked: modelValue, disabled, compact }"
    :data-tone="tone"
  >
    <span class="setting-switch-copy"
      ><span class="setting-switch-title">{{ label }}</span
      ><small v-if="description" :id="descriptionId">{{ description }}</small
      ><small
        v-if="savedValue !== undefined && savedValue !== modelValue"
        class="pending-change"
        >待保存 · 当前{{ savedValue ? onText : offText }}</small
      ></span
    >
    <span class="setting-switch-control"
      ><span class="setting-switch-state" aria-hidden="true">{{
        modelValue ? onText : offText
      }}</span
      ><span class="setting-switch-hitbox"
        ><input
          class="setting-switch-input"
          type="checkbox"
          role="switch"
          :checked="modelValue"
          :disabled="disabled"
          :aria-label="label"
          :aria-describedby="description ? descriptionId : undefined"
          @change="
            emit(
              'update:modelValue',
              ($event.target as HTMLInputElement).checked,
            )
          " /><span class="setting-switch-track" aria-hidden="true"
          ><span class="setting-switch-thumb"></span></span></span
    ></span>
  </label>
</template>
<style scoped>
.compact .setting-switch-title {
  position: absolute;
  clip-path: inset(50%);
  width: 1px;
  height: 1px;
  overflow: hidden;
}
.compact .setting-switch-state {
  display: none;
}
.compact .pending-change {
  font-size: 10px;
}
.setting-switch.setting-switch {
  display: flex;
  flex-direction: row;
  text-align: left;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 44px;
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  cursor: pointer;
  min-width: 0;
  --switch-active: #15745a;
  --switch-text: #166449;
}
.setting-switch[data-tone="warning"] {
  --switch-active: #a56a10;
  --switch-text: #82510a;
}
.setting-switch[data-tone="danger"] {
  --switch-active: #b44f3d;
  --switch-text: #9e4232;
}
.setting-switch-copy {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.setting-switch-title {
  font-size: 14px;
  font-weight: 600;
  color: #304d3c;
  line-height: 1.5;
}
.setting-switch-copy small {
  font-size: 12px;
  font-weight: 400;
  color: #66796b;
  line-height: 1.6;
}
.setting-switch-control {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.setting-switch-state {
  font-size: 12px;
  font-weight: 600;
  color: #56665e;
  white-space: nowrap;
}
.checked .setting-switch-state {
  color: var(--switch-text);
}
.setting-switch-hitbox {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  flex-shrink: 0;
}
.setting-switch .setting-switch-input {
  position: absolute;
  inset: 0;
  z-index: 1;
  width: 44px;
  height: 44px;
  min-height: 44px;
  max-width: none;
  margin: 0;
  padding: 0;
  border: 0;
  opacity: 0;
  cursor: pointer;
}
.setting-switch-track {
  position: relative;
  display: block;
  width: 44px;
  height: 26px;
  border: 1px solid #687a70;
  border-radius: 18px;
  background: #718378;
  transition:
    background 0.15s,
    border-color 0.15s;
}
.setting-switch-thumb {
  display: block;
  position: absolute;
  top: 3px;
  left: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 4px #20372c30;
  transition: transform 0.15s;
}
.setting-switch-input:checked + .setting-switch-track {
  background: var(--switch-active);
  border-color: var(--switch-active);
}
.setting-switch-input:checked + .setting-switch-track .setting-switch-thumb {
  transform: translateX(18px);
}
.setting-switch-input:focus-visible + .setting-switch-track {
  outline: 3px solid #296dbe;
  outline-offset: 4px;
}
.setting-switch.disabled {
  cursor: not-allowed;
  opacity: 0.65;
}
.setting-switch.disabled input {
  cursor: not-allowed;
}
@media (max-width: 767px) {
  .setting-switch.setting-switch {
    gap: 12px;
  }
  .setting-switch-control {
    gap: 8px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .setting-switch-track,
  .setting-switch-thumb {
    transition: none;
  }
}
</style>
