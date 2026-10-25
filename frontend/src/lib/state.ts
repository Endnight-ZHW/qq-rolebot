import { reactive, ref } from "vue";
import { api } from "./api";
import { loadSchema } from "./config";
import type { Group, Persona, Health } from "./types";
export const core = reactive({
  groups: [] as Group[],
  personas: [] as Persona[],
  tools: [] as any[],
  health: {} as Health,
  updated: 0,
  error: "",
});
export const notice = ref<{ text: string; error: boolean } | null>(null);
let timer: ReturnType<typeof setTimeout>;
export function notify(text: string, error = false) {
  notice.value = { text, error };
  clearTimeout(timer);
  timer = setTimeout(() => (notice.value = null), 5500);
}
export async function loadCore() {
  try {
    const [groups, personas, tools, health] = await Promise.all([
      api<Group[]>("/groups"),
      api<Persona[]>("/personas"),
      api("/tools"),
      api<Health>("/status"),
      loadSchema(),
    ]);
    Object.assign(core, {
      groups,
      personas,
      tools,
      health,
      updated: Date.now(),
      error: "",
    });
  } catch (e: any) {
    core.error = e.message;
    throw e;
  }
}
export async function refreshHealth() {
  core.health = await api("/status");
  core.updated = Date.now();
  core.error = "";
}
export function nameOf(scope: string) {
  return (
    core.groups.find((g) => g.id === scope)?.name || scope || "系统 / 诊断"
  );
}
export function useAction() {
  const busy = ref(false),
    error = ref("");
  async function action(fn: () => Promise<unknown>, message = "已保存") {
    if (busy.value) return false;
    busy.value = true;
    error.value = "";
    try {
      await fn();
      if (message) notify(message);
      return true;
    } catch (e: any) {
      error.value = e.message;
      notify(e.message, true);
      return false;
    } finally {
      busy.value = false;
    }
  }
  return { busy, error, action };
}
