import { onMounted, onUnmounted } from "vue";
export function usePoll(
  fn: () => Promise<unknown>,
  interval: number,
  immediate = true,
) {
  let timer: ReturnType<typeof setTimeout>,
    active = true,
    running = false;
  async function tick() {
    clearTimeout(timer);
    if (!active || document.hidden || running) return;
    running = true;
    try {
      await fn();
    } catch {
      /* Request owners display stale/error state. */
    } finally {
      running = false;
      if (active && !document.hidden) timer = setTimeout(tick, interval);
    }
  }
  function visibility() {
    clearTimeout(timer);
    if (!document.hidden) void tick();
  }
  onMounted(() => {
    document.addEventListener("visibilitychange", visibility);
    if (immediate) void tick();
    else timer = setTimeout(tick, interval);
  });
  onUnmounted(() => {
    active = false;
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", visibility);
  });
  return tick;
}
