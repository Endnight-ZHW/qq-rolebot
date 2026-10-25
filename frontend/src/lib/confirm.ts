import { ref } from "vue";
export const confirmation = ref<{
  title: string;
  message: string;
  confirmLabel: string;
} | null>(null);
let resolve: ((v: boolean) => void) | undefined;
export function confirmAction(
  title: string,
  message: string,
  confirmLabel = "确认操作",
): Promise<boolean> {
  if (resolve) return Promise.resolve(false);
  confirmation.value = { title, message, confirmLabel };
  return new Promise((r) => (resolve = r));
}
export function answer(value: boolean) {
  confirmation.value = null;
  const done = resolve;
  resolve = undefined;
  done?.(value);
}
