import {
  computed,
  onUnmounted,
  ref,
  shallowReactive,
  type ComputedRef,
} from "vue";
export type Change = { path: string; before: unknown; after: unknown };
export function changesBetween(before: any, after: any, path = ""): Change[] {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  if (
    before &&
    after &&
    typeof before === "object" &&
    typeof after === "object" &&
    !Array.isArray(before) &&
    !Array.isArray(after)
  )
    return [
      ...new Set([...Object.keys(before), ...Object.keys(after)]),
    ].flatMap((k) =>
      changesBetween(before[k], after[k], path ? path + "." + k : k),
    );
  const secret =
    /password|(?:^|\.)(?:keys?|api_key|secret|token)(?:\.|$)/i.test(path);
  return [
    {
      path,
      before: secret ? "已隐藏" : before,
      after: secret ? "已隐藏" : after,
    },
  ];
}
type Draft = {
  dirty: ComputedRef<boolean>;
  save: () => Promise<any>;
  reset: () => void;
};
const drafts = shallowReactive(new Set<Draft>());
export const hasUnsaved = computed(() =>
  [...drafts].some((x) => x.dirty.value),
);
export const leaveDialog = ref(false),
  leaveBusy = ref(false),
  leaveError = ref("");
let resolver: ((value: boolean) => void) | undefined;
export function useDraft(
  get: () => unknown,
  save: () => Promise<any>,
  restore?: (value: any) => void,
) {
  const baseline = ref<string | null>(null);
  const dirty = computed(
    () => baseline.value !== null && JSON.stringify(get()) !== baseline.value,
  );
  const changes = computed(() =>
    baseline.value === null
      ? []
      : changesBetween(JSON.parse(baseline.value), get()),
  );
  const accept = () => (baseline.value = JSON.stringify(get()));
  const acceptPart = (update: (previous: any) => any) => {
    if (baseline.value !== null)
      baseline.value = JSON.stringify(update(JSON.parse(baseline.value)));
  };
  const reset = () => {
    if (baseline.value !== null && restore) restore(JSON.parse(baseline.value));
    else accept();
  };
  const draft = { dirty, save, reset };
  drafts.add(draft);
  onUnmounted(() => drafts.delete(draft));
  return { dirty, changes, accept, acceptPart, reset };
}
export function allowLeave(): Promise<boolean> {
  if (!hasUnsaved.value) return Promise.resolve(true);
  if (resolver) return Promise.resolve(false);
  leaveError.value = "";
  leaveDialog.value = true;
  return new Promise((resolve) => (resolver = resolve));
}
export async function settleLeave(choice: "save" | "discard" | "stay") {
  if (leaveBusy.value) return;
  if (choice === "save") {
    leaveBusy.value = true;
    try {
      for (const d of [...drafts])
        if (d.dirty.value) {
          const ok = await d.save();
          if (ok === false || d.dirty.value) {
            leaveError.value = "尚有修改未保存，请检查表单后继续。";
            return;
          }
        }
    } catch (e: any) {
      leaveError.value = e.message;
      return;
    } finally {
      leaveBusy.value = false;
    }
  }
  if (choice === "discard")
    for (const d of drafts) if (d.dirty.value) d.reset();
  leaveDialog.value = false;
  const done = resolver;
  resolver = undefined;
  done?.(choice !== "stay");
}
