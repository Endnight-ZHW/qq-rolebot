<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { core } from "../lib/state";
import { useGroup } from "../lib/groups";
import StatusBadge from "../components/StatusBadge.vue";
const route = useRoute(),
  router = useRouter(),
  { scope, group } = useGroup(),
  search = ref("");
const section = computed(() => String(route.path.split("/").at(-1)));
const tabs = computed(() => [
  { id: "summary", name: "概况" },
  { id: "settings", name: "聊天设置" },
  { id: "members", name: "成员与学习" },
  { id: "memory", name: "记忆与上下文" },
  { id: "stickers", name: "表情" },
  { id: "access", name: "权限中心" },
]);
const selected = computed(() =>
  section.value === "context" ? "memory" : section.value,
);
const options = computed(() =>
  core.groups.filter(
    (g) => g.id === scope.value || (g.name + g.id).includes(search.value),
  ),
);
function target(id: string) {
  return "/groups/" + encodeURIComponent(scope.value) + "/" + id;
}
function switchGroup(e: Event) {
  const el = e.target as HTMLSelectElement,
    next = el.value;
  el.value = scope.value;
  router.push("/groups/" + encodeURIComponent(next) + "/" + section.value);
}
</script>
<template>
  <template v-if="group"
    ><div class="group-banner">
      <RouterLink class="quiet back-link" to="/groups">← 所有会话</RouterLink
      ><span class="avatar">{{ group.name.slice(0, 1) }}</span>
      <div class="group-title">
        <h2>{{ group.name }}</h2>
        <p>
          {{ scope.split(":")[1] === "p" ? "私聊" : "QQ群" }} ·
          {{ scope.split(":")[2] }}
          <span
            >当前角色 ·
            {{ core.personas.find((p) => p.id === group.persona)?.name }}</span
          >
        </p>
      </div>
      <StatusBadge
        :status="
          group.paused ? 'paused' : group.enabled ? 'enabled' : 'disabled'
        "
        :text="group.paused ? '已暂停' : group.enabled ? '已启用' : '未启用'"
      />
      <div class="group-switcher">
        <input
          v-model="search"
          aria-label="搜索切换会话"
          placeholder="搜索会话"
        /><select :value="scope" aria-label="切换会话" @change="switchGroup">
          <option v-for="g in options" :key="g.id" :value="g.id">
            {{ g.name }} · {{ g.id.split(":")[2] }}
          </option>
        </select>
      </div>
    </div>
    <nav class="section-tabs" aria-label="群工作区导航">
      <RouterLink
        v-for="t in tabs"
        :key="t.id"
        :to="target(t.id)"
        :class="{ active: selected === t.id }"
        >{{ t.name }}</RouterLink
      >
    </nav>
    <nav v-if="selected === 'memory'" class="sub-tabs" aria-label="群功能分类">
      <template v-if="selected === 'memory'"
        ><RouterLink
          :to="target('memory')"
          :class="{ active: section === 'memory' }"
          >记忆档案</RouterLink
        ><RouterLink
          :to="target('context')"
          :class="{ active: section === 'context' }"
          >上下文与预算</RouterLink
        ></template
      >
    </nav>
    <RouterView :key="scope"
  /></template>
  <div v-else class="empty panel">
    <h2>未找到这个会话</h2>
    <p>会话可能尚未添加，请从列表重新选择。</p>
    <RouterLink to="/groups" class="primary">返回群列表</RouterLink>
  </div>
</template>
