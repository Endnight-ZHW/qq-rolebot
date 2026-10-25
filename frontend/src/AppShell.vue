<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, session } from "./lib/api";
import { core, notice, loadCore, refreshHealth } from "./lib/state";
import {
  allowLeave,
  hasUnsaved,
  leaveDialog,
  leaveBusy,
  leaveError,
  settleLeave,
} from "./lib/drafts";
import { usePoll } from "./lib/poll";
import Icon from "./components/Icon.vue";
import Overlay from "./components/Overlay.vue";
import ConfirmHost from "./components/ConfirmHost.vue";
const route = useRoute(),
  router = useRouter(),
  menu = ref(false),
  bootError = ref(""),
  starting = ref(true);
const nav = [
  { id: "overview", label: "总览", path: "/overview" },
  { id: "groups", label: "会话管理", path: "/groups" },
  { id: "personas", label: "角色库", path: "/personas" },
  { id: "tasks", label: "任务中心", path: "/tasks" },
  { id: "observe", label: "观测中心", path: "/observe/traces" },
  { id: "configuration", label: "模型与服务", path: "/configuration/models" },
  { id: "settings", label: "系统设置", path: "/settings" },
];
const active = computed(() => String(route.meta.section || ""));
async function boot() {
  starting.value = true;
  bootError.value = "";
  try {
    const r = await api("/auth/me");
    session.csrf = r.csrf;
    session.authenticated = true;
    await loadCore();
  } catch (e: any) {
    if (e.status !== 401) bootError.value = e.message;
  } finally {
    session.ready = true;
    starting.value = false;
  }
  if (!session.authenticated && route.path !== "/login")
    await router.replace({ path: "/login", query: { next: route.fullPath } });
  else if (session.authenticated && route.path === "/login")
    await router.replace("/overview");
}
async function signOut() {
  if (!(await allowLeave())) return;
  try {
    await api("/auth/logout", "POST");
    session.authenticated = false;
    session.csrf = "";
    await router.replace("/login");
  } catch (e: any) {
    core.error = e.message;
  }
}
watch(
  () => route.fullPath,
  () => {
    menu.value = false;
  },
);
watch(
  () => session.authenticated,
  async (value) => {
    if (session.ready && !value && route.path !== "/login")
      await router.replace({ path: "/login", query: { next: route.fullPath } });
  },
);
usePoll(
  async () => {
    if (session.authenticated)
      try {
        await refreshHealth();
      } catch (e: any) {
        core.error = e.message;
      }
  },
  15000,
  false,
);
function unload(e: BeforeUnloadEvent) {
  if (hasUnsaved.value) {
    e.preventDefault();
    e.returnValue = "";
  }
}
onMounted(() => {
  void boot();
  window.addEventListener("beforeunload", unload);
});
onUnmounted(() => window.removeEventListener("beforeunload", unload));
</script>
<template>
  <div v-if="starting" class="loading-screen">
    <span class="brand-mark">安</span>
    <p>正在打开工作台…</p>
  </div>
  <div v-else-if="bootError" class="loading-screen">
    <h2>暂时无法加载工作台</h2>
    <p role="alert">{{ bootError }}</p>
    <button class="primary" @click="boot">重新连接</button>
  </div>
  <RouterView v-else-if="!session.authenticated" />
  <div v-else class="app-shell">
    <aside class="sidebar desktop-nav">
      <RouterLink class="brand" to="/overview"
          ><span class="brand-mark">Q</span
        ><strong>机器人<span>QQ 机器人工作台</span></strong></RouterLink
      >
      <p class="side-caption">工作空间</p>
      <nav aria-label="主导航">
        <RouterLink
          v-for="item in nav"
          :key="item.id"
          :to="item.path"
          :class="{ active: active === item.id }"
          :aria-current="active === item.id ? 'page' : undefined"
          ><Icon :name="item.id" /><span>{{ item.label }}</span
          ><span
            v-if="item.id === 'settings' && !core.health.initialized"
            class="nav-dot"
          ></span
        ></RouterLink>
      </nav>
      <div class="sidebar-bottom">
        <span
          class="connection-dot"
          :class="{ online: core.health.onebot_connected }"
        ></span>
        <div>
          <strong>{{
            core.health.onebot_connected ? "NapCat 已连接" : "NapCat 未连接"
          }}</strong
          ><small>{{ core.health.self_id || "等待机器人连接" }}</small>
        </div>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="breadcrumb">
          <button
            class="icon-button mobile-menu"
            aria-label="打开导航"
            @click="menu = true"
          >
            <Icon name="menu" /></button
          ><span class="muted">工作台</span><span class="crumb-divider">/</span
          ><strong>{{ route.meta.title }}</strong>
        </div>
        <div class="top-actions">
          <span class="timezone">北京时间 · UTC+8</span
          ><span class="admin-avatar">管</span
          ><span class="admin-name">管理员</span
          ><button class="quiet" @click="signOut">退出</button>
        </div>
      </header>
      <main id="main-content" class="page-content">
        <header v-if="!route.params.scope" class="page-heading">
          <div>
            <h1>{{ route.meta.title }}</h1>
            <p v-if="route.meta.description">{{ route.meta.description }}</p>
          </div>
        </header>
        <div v-if="core.error" class="error-banner" role="alert">
          {{ core.error }}
          <button class="link" @click="loadCore().catch(() => {})">重试</button>
        </div>
        <RouterView />
      </main>
      <footer class="app-footer">
        <span>机器人工作台</span
        ><span>所有时间均为北京时间 · 数据来自实际运行记录</span>
      </footer>
    </div>
  </div>
  <Overlay v-if="menu" title="工作空间" drawer @close="menu = false"
    ><nav class="mobile-nav" aria-label="移动导航">
      <RouterLink
        v-for="item in nav"
        :key="item.id"
        :to="item.path"
        :class="{ active: active === item.id }"
        @click="menu = false"
        ><Icon :name="item.id" />{{ item.label }}</RouterLink
      >
    </nav></Overlay
  >
  <Overlay
    v-if="leaveDialog"
    title="保存当前修改？"
    :busy="leaveBusy"
    @close="settleLeave('stay')"
    ><p>当前页面有未保存的修改。可以保存后继续，或放弃本次修改。</p>
    <p v-if="leaveError" class="error-text" role="alert">{{ leaveError }}</p>
    <template #footer
      ><button class="quiet" :disabled="leaveBusy" @click="settleLeave('stay')">
        继续编辑</button
      ><button
        class="secondary"
        :disabled="leaveBusy"
        @click="settleLeave('discard')"
      >
        放弃并离开</button
      ><button
        class="primary"
        :disabled="leaveBusy"
        @click="settleLeave('save')"
      >
        {{ leaveBusy ? "正在保存…" : "保存并离开" }}
      </button></template
    ></Overlay
  >
  <ConfirmHost />
  <div
    v-if="notice"
    class="toast"
    :class="{ error: notice.error }"
    :role="notice.error ? 'alert' : 'status'"
  >
    {{ notice.text
    }}<button aria-label="关闭通知" @click="notice = null">×</button>
  </div>
</template>
