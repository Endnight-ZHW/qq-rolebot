<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../lib/api";
import { core, loadCore } from "../lib/state";
import ModelManager from "../ModelManager.vue";
import PromptManager from "../PromptManager.vue";
import ImageSearchSettings from "../components/ImageSearchSettings.vue";
const route = useRoute(),
  router = useRouter(),
  view = computed(() =>
    route.params.view === "prompts"
      ? "prompts"
      : route.params.view === "image-search"
        ? "image-search"
        : "models",
  ),
  scope = computed(() => String(route.query.scope || ""));
function navigate(target: string) {
  if (target === "personas") router.push("/personas");
  else if (scope.value)
    router.push(
      "/groups/" +
        encodeURIComponent(scope.value) +
        "/" +
        (target === "qq-permissions" ? "access?view=members" : "settings"),
    );
  else router.push("/groups");
}
function changeScope(event: Event) {
  const input = event.target as HTMLSelectElement,
    next = input.value;
  input.value = scope.value;
  router.push({ query: { scope: next || undefined } });
}
</script>
<template>
  <nav class="section-tabs" aria-label="模型配置分类">
    <RouterLink
      to="/configuration/models"
      :class="{ active: view === 'models' }"
      >模型与供应商</RouterLink
    ><RouterLink
      to="/configuration/prompts"
      :class="{ active: view === 'prompts' }"
      >提示词管理</RouterLink
    ><RouterLink
      to="/configuration/image-search"
      :class="{ active: view === 'image-search' }"
      >搜图服务</RouterLink
    >
  </nav>
  <ModelManager
    v-if="view === 'models'"
    :api="api"
    @saved="loadCore().catch(() => {})"
  /><ImageSearchSettings v-else-if="view === 'image-search'" /><template v-else
    ><div class="toolbar">
      <span class="muted">群覆盖目标</span
      ><select :value="scope" aria-label="提示词覆盖会话" @change="changeScope">
        <option value="">全局模板</option>
        <option v-for="g in core.groups" :key="g.id" :value="g.id">
          {{ g.name }}
        </option></select
      ><span class="muted">选择会话后，可编辑该会话的专用覆盖</span>
    </div>
    <PromptManager :key="scope" :scope="scope" :api="api" @navigate="navigate"
  /></template>
</template>
