<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { configuration } from "../lib/config";
const props = defineProps<{
  field: string;
  modelValue: any;
  label?: string;
  disabled?: boolean;
  nullable?: boolean;
  max?: number;
  defaultValue?: number;
}>();
const emit = defineEmits<{ "update:modelValue": [any] }>();
const unit = ref(1),
  id = useId();
const spec = computed(() => configuration.fields[props.field]);
const maximum = computed(() =>
  Math.min(spec.value?.maximum ?? Infinity, props.max ?? Infinity),
);
const warning = computed(
  () =>
    typeof props.modelValue === "number" &&
    spec.value &&
    props.modelValue > spec.value.recommended_max,
);
const seconds = computed(() => spec.value?.unit === "秒");
function input(event: Event) {
  const text = (event.target as HTMLInputElement).value;
  emit(
    "update:modelValue",
    text === ""
      ? props.nullable
        ? null
        : ""
      : Number((Number(text) * unit.value).toFixed(6)),
  );
}
</script>
<template>
  <div class="number-field" v-if="spec">
    <label :for="id"
      >{{ label || spec.label
      }}<span class="field-unit" v-if="spec.unit && !seconds">{{
        spec.unit
      }}</span></label
    >
    <div class="number-input-row">
      <input
        :id="id"
        type="number"
        :aria-label="label || spec.label"
        :aria-describedby="id + '-hint'"
        :required="!nullable"
        :value="
          modelValue == null || modelValue === '' ? '' : modelValue / unit
        "
        :min="spec.minimum / unit"
        :max="maximum / unit"
        :step="spec.type === 'integer' ? 1 / unit : 'any'"
        :disabled="disabled"
        @input="input"
      />
      <select
        v-if="seconds"
        v-model.number="unit"
        :aria-label="(label || spec.label) + '单位'"
        :disabled="disabled"
      >
        <option :value="1">秒</option>
        <option :value="60">分钟</option>
        <option :value="3600">小时</option>
      </select>
    </div>
    <small :id="id + '-hint'" :class="{ 'soft-warning': warning }">
      <template v-if="warning"
        >超过建议值 {{ spec.recommended_max }}
        {{ spec.unit }}，仍可保存。</template
      >
      <template v-else
        >默认 {{ defaultValue ?? spec.default }} {{ spec.unit
        }}<span v-if="nullable"> · 留空继承</span></template
      >
      <span v-if="spec.maximum < 2147483647"> · {{ spec.hard_reason }}</span>
    </small>
  </div>
</template>
