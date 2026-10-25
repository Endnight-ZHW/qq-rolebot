<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, session } from "../lib/api";
import { loadCore } from "../lib/state";
const route = useRoute(),
  router = useRouter(),
  form = reactive({ username: "admin", password: "", remember_me: false }),
  busy = ref(false),
  error = ref("");
async function signIn() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const r = await api("/auth/login", "POST", form);
    session.csrf = r.csrf;
    session.authenticated = true;
    form.password = "";
    await loadCore();
    const next = String(route.query.next || "/overview");
    await router.replace(
      next.startsWith("/") &&
        !next.startsWith("//") &&
        !next.startsWith("/login")
        ? next
        : "/overview",
    );
  } catch (e: any) {
    error.value = e.status === 401 ? "密码不正确，请再试一次。" : e.message;
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <main class="login-page">
    <section class="login-story">
      <a class="brand"
        ><span class="brand-mark">Q</span
        ><strong>机器人<span>QQ 机器人工作台</span></strong></a
      >
      <div>
        <span class="eyebrow">A LITTLE PRESENCE, A LONG MEMORY</span>
        <h1>让陪伴有记忆。<br />让管理更从容。</h1>
        <p>群聊、角色与每一次恰到好处的回应。<br />在这里，一切井然有序。</p>
        <div class="login-orbit" aria-hidden="true">
          <span>Q</span><i></i><b>✦</b>
        </div>
      </div>
      <small>角色有性格 · 记忆有来源 · 操作有边界</small>
    </section>
    <form class="login-card" @submit.prevent="signIn">
      <span class="eyebrow">WELCOME BACK</span>
      <h2>打开工作台</h2>
      <p class="muted">输入管理员密码，查看机器人的运行状态。</p>
      <input type="hidden" name="username" autocomplete="username" value="admin" />
      <label>密码<input
          v-model="form.password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
      /></label>
      <label class="login-remember">
        <input v-model="form.remember_me" type="checkbox" name="remember_me" />
        <span>记住登录 30 天</span>
      </label>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <button class="primary full" :disabled="busy">
        {{ busy ? "正在登录…" : "登录工作台 →" }}</button
      ><small class="muted">未勾选时，登录状态保留 12 小时。</small>
    </form>
  </main>
</template>
