import { reactive } from "vue";
import { api } from "./api";
export interface FieldSpec {
  key: string;
  scope: string;
  label: string;
  type: "integer" | "number";
  default: number;
  minimum: number;
  maximum: number;
  recommended_max: number;
  unit: string;
  hard_reason: string;
  dependencies: string[];
}
export const configuration = reactive({
  fields: {} as Record<string, FieldSpec>,
});
let pending: Promise<void> | undefined;
export function loadSchema() {
  if (Object.keys(configuration.fields).length) return Promise.resolve();
  return (pending ||= api("/config-schema")
    .then((r) => {
      configuration.fields = r.fields;
    })
    .finally(() => {
      pending = undefined;
    }));
}
export const runtimeKeys = [
  "send_per_minute",
  "send_wait_seconds",
  "reply_timeout_seconds",
  "forward_reply_timeout_seconds",
  "tool_max_rounds",
  "tool_max_calls",
];
