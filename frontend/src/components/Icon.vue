<script setup lang="ts">
defineProps<{ name: string }>();
const paths: Record<string, string> = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  groups: "M3 4h18v12H8l-5 4z M7 8h10 M7 12h6",
  personas: "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M4 21v-3a8 6 0 0 1 16 0v3",
  tasks: "M9 5H4v16h16V5h-5 M9 3h6v4H9z M8 12l2 2 5-5 M8 18h8",
  observe: "M3 12h4l3-8 4 16 3-8h4",
  configuration: "M4 5h16 M4 12h16 M4 19h16 M8 2v6 M16 9v6 M10 16v6",
  settings:
    "M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  arrow: "M5 12h14 M14 7l5 5-5 5",
  refresh: "M20 7v5h-5 M20 12a8 8 0 1 0-2 6",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6",
  close: "M5 5l14 14 M5 19L19 5",
};
</script>
<template>
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || paths.overview" />
  </svg>
</template>
