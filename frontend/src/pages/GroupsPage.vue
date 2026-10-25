<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { api } from "../lib/api";
import { core, loadCore, useAction } from "../lib/state";
import StatusBadge from "../components/StatusBadge.vue";
import Overlay from "../components/Overlay.vue";
import Icon from "../components/Icon.vue";
const router = useRouter(),
  route = useRoute(),
  search = ref(String(route.query.q || "")),
  state = ref(String(route.query.state || "")),
  adding = ref(false),
  manual = ref(""),
  discovered = ref<any[]>([]),
  { busy, error, action } = useAction();
const filtered = computed(() =>
  core.groups.filter(
    (g) =>
      (g.name + g.id).toLowerCase().includes(search.value.toLowerCase()) &&
      (!state.value ||
        (state.value === "enabled"
          ? g.enabled && !g.paused
          : state.value === "paused"
            ? g.paused
            : !g.enabled)),
  ),
);
function filters() {
  router.replace({
    query: { q: search.value || undefined, state: state.value || undefined },
  });
}
async function discover() {
  await action(async () => {
    discovered.value = await api("/groups/discover");
  }, "已读取 QQ 群列表");
}
async function add(id: string, name: string) {
  if (core.groups.some((g) => g.id === id)) {
    adding.value = false;
    await router.push("/groups/" + encodeURIComponent(id) + "/summary");
    return;
  }
  const ok = await action(async () => {
    await api("/groups/" + id, "PUT", {
      name,
      enabled: false,
      paused: false,
      management_paused: false,
      persona: core.personas[0]?.id || "default",
      guidance: "",
      cooldown: 60,
      proactive_hourly: 10,
    });
    await loadCore();
  }, "会话已添加，开启前请检查聊天设置");
  if (ok) {
    adding.value = false;
    await router.push("/groups/" + encodeURIComponent(id) + "/settings");
  }
}
</script>
<template>
  <div class="toolbar">
    <div class="search-field">
      <Icon name="search" /><input
        v-model="search"
        aria-label="搜索群或会话"
        placeholder="搜索群名称、群号或 QQ 号"
        @input="filters"
      />
    </div>
    <select v-model="state" aria-label="会话状态" @change="filters">
      <option value="">全部状态</option>
      <option value="enabled">已启用</option>
      <option value="paused">已暂停</option>
      <option value="disabled">未启用</option></select
    ><span class="muted result-count">{{ filtered.length }} 个会话</span
    ><button class="primary" @click="adding = true">＋ 添加会话</button>
  </div>
  <div class="group-grid">
    <RouterLink
      v-for="g in filtered"
      :key="g.id"
      :to="'/groups/' + encodeURIComponent(g.id) + '/summary'"
      class="panel group-card"
      ><div class="section-heading">
        <span class="avatar">{{ g.name.slice(0, 1) }}</span
        ><StatusBadge
          :status="g.paused ? 'paused' : g.enabled ? 'enabled' : 'disabled'"
          :text="g.paused ? '已暂停' : g.enabled ? '已启用' : '未启用'"
        />
      </div>
      <h2>{{ g.name }}</h2>
      <p class="muted">
        {{ g.id.split(":")[1] === "p" ? "私聊" : "QQ群" }} ·
        {{ g.id.split(":")[2] }}
      </p>
      <div class="group-card-bottom">
        <span
          >当前角色
          <strong>{{
            core.personas.find((p) => p.id === g.persona)?.name || g.persona
          }}</strong></span
        ><Icon name="arrow" /></div
    ></RouterLink>
  </div>
  <div v-if="!filtered.length" class="empty panel">
    <Icon name="groups" />
    <h3>{{ core.groups.length ? "没有匹配的会话" : "从第一个群开始" }}</h3>
    <p>
      {{
        core.groups.length
          ? "换个关键词或清除筛选试试。"
          : "连接 NapCat 后，添加机器人已经加入的 QQ 群。"
      }}
    </p>
    <button
      v-if="core.groups.length"
      class="secondary"
      @click="
        search = '';
        state = '';
        filters();
      "
    >
      清除筛选</button
    ><button v-else class="primary" @click="adding = true">添加会话</button>
  </div>
  <Overlay v-if="adding" title="添加会话" :busy="busy" @close="adding = false"
    ><p class="muted">从机器人已加入的 QQ 群中选择，或手动填写群号。</p>
    <p v-if="!core.health.onebot_connected" class="notice-banner">
      NapCat 尚未连接，连接后可读取和添加群。
    </p>
    <button
      class="secondary"
      :disabled="busy || !core.health.onebot_connected"
      @click="discover"
    >
      从 QQ 读取群列表
    </button>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <div class="stack">
      <button
        v-for="g in discovered"
        :key="g.id"
        class="list-row"
        :disabled="busy"
        @click="add(g.id, g.name)"
      >
        {{ g.name }}<span class="muted">{{ g.id.split(":")[2] }}</span
        ><span>添加 →</span>
      </button>
    </div>
    <form @submit.prevent="add(core.health.self_id + ':g:' + manual, manual)">
      <label
        >手动添加群号<input
          v-model="manual"
          inputmode="numeric"
          pattern="[0-9]{5,20}"
          required /></label
      ><button class="primary" :disabled="busy || !core.health.self_id">
        添加会话
      </button>
    </form></Overlay
  >
</template>
