import { computed } from "vue";
import { useRoute } from "vue-router";
import { runtimeKeys } from "./config";
import { core } from "./state";
export const groupFields = [
  ...runtimeKeys,
  "name",
  "enabled",
  "paused",
  "management_paused",
  "persona",
  "guidance",
  "cooldown",
  "proactive_hourly",
  "listening_probability",
  "learning_enabled",
  "followup_seconds",
  "segmented_reply",
  "segment_threshold",
  "segment_max_parts",
  "segment_interval_min",
  "segment_interval_max",
];
export const groupPayload = (g: any) =>
  Object.fromEntries(
    groupFields.filter((k) => g[k] !== undefined).map((k) => [k, g[k]]),
  );
export function useGroup() {
  const route = useRoute();
  const scope = computed(() => String(route.params.scope || ""));
  const group = computed(() => core.groups.find((g) => g.id === scope.value));
  return { scope, group };
}
