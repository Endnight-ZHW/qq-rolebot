<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../lib/api";
import { useDraft } from "../lib/drafts";
import SaveBar from "./SaveBar.vue";

const config = ref<any>(null),
  busy = ref(false),
  failed = ref(false),
  message = ref("");
const key = ref(""),
  clearKey = ref(false),
  file = ref<File | null>(null),
  preview = ref("");
const account = ref<any>(null),
  result = ref<any>(null);
const draft = useDraft(
  () => ({ key: key.value, clearKey: clearKey.value }),
  save,
  (value) => {
    key.value = value.key;
    clearKey.value = value.clearKey;
  },
);
async function run(fn: () => Promise<void>) {
  busy.value = true;
  failed.value = false;
  message.value = "";
  try {
    await fn();
    return true;
  } catch (e: any) {
    failed.value = true;
    message.value = e.message;
    return false;
  } finally {
    busy.value = false;
  }
}
async function load() {
  await run(async () => {
    config.value = await api("/image-search/config");
    account.value = config.value.account;
    result.value = config.value.last_test;
    draft.accept();
  });
}
async function save() {
  return run(async () => {
    config.value = await api("/image-search/config", "PUT", {
      api_key: clearKey.value ? "" : key.value,
      clear_key: clearKey.value,
      base_revision: config.value.revision,
    });
    key.value = "";
    clearKey.value = false;
    account.value = config.value.account;
    result.value = config.value.last_test;
    draft.accept();
    message.value = "搜图密钥已保存";
  });
}
async function checkAccount() {
  await run(async () => {
    account.value = await api("/image-search/account", "POST");
    message.value = account.value.free_eligible
      ? "已确认免费套餐，可进行图片验证"
      : "未确认可用的免费套餐，图片验证将停止";
  });
}
function choose(event: Event) {
  if (preview.value) URL.revokeObjectURL(preview.value);
  preview.value = "";
  file.value = (event.target as HTMLInputElement).files?.[0] || null;
  if (file.value && file.value.size > 8 * 1024 * 1024) {
    file.value = null;
    (event.target as HTMLInputElement).value = "";
    failed.value = true;
    message.value = "图片不能超过 8 MiB";
    return;
  }
  if (file.value) preview.value = URL.createObjectURL(file.value);
}
async function testImage() {
  if (!file.value) return;
  const body = new FormData();
  body.append("file", file.value);
  await run(async () => {
    result.value = await api("/image-search/test", "POST", body);
    account.value =
      result.value.account_after ||
      result.value.account_before ||
      account.value;
    failed.value = result.value.status === "error";
    message.value =
      result.value.error ||
      (result.value.cached
        ? "已复用这张图片的验证结果，未重复查询"
        : result.value.status === "results_found"
          ? "已取得候选来源，请核对结果"
          : "未取得可核对的来源");
  });
}
const date = (ts: number) => new Date(ts * 1000).toLocaleString("zh-CN");
onMounted(load);
onUnmounted(() => {
  if (preview.value) URL.revokeObjectURL(preview.value);
});
</script>

<template>
  <div class="page-intro">
    <p>
      用户明确要求搜图时，机器人可用 Google Lens
      检索选中的聊天图片，并核对候选来源。
    </p>
    <span class="tag">仅使用免费额度 · 每轮最多一次搜索</span>
  </div>
  <p class="muted">
    在会话工具设置中管理“以图搜图”。仅响应用户请求，沿用联网搜索成员权限；同群、同角色的相同图片可在
    24 小时内复用结果。普通看图不自动搜图。
  </p>
  <p
    v-if="message"
    role="status"
    :class="['search-feedback', { 'error-text': failed }]"
  >
    {{ message }}
  </p>
  <button
    v-if="failed && !config"
    class="secondary"
    :disabled="busy"
    @click="load"
  >
    重新加载搜图配置
  </button>
  <form v-if="config" class="panel" @submit.prevent="save">
    <div class="section-heading">
      <h3>SerpApi 连接</h3>
      <span class="tag">{{
        config.key_configured ? "密钥已配置" : "尚未配置密钥"
      }}</span>
    </div>
    <p class="muted">
      密钥仅保存在服务器，不回显。保存密钥不会发起搜索，也不会修改聊天模型配置。
    </p>
    <label
      >SerpApi API 密钥
      <input
        v-model="key"
        type="password"
        autocomplete="new-password"
        :disabled="busy || clearKey"
        :placeholder="
          config.key_configured
            ? '留空保留已保存的密钥'
            : '填写 SerpApi API 密钥'
        "
      />
    </label>
    <label class="clear-choice"
      ><input
        v-model="clearKey"
        type="checkbox"
        :disabled="busy || !config.key_configured"
      />清除已保存的 SerpApi 密钥</label
    >
    <p class="muted">
      <a
        href="https://serpapi.com/manage-api-key"
        target="_blank"
        rel="noopener noreferrer"
        >在 SerpApi 账户中取得密钥</a
      >
    </p>
  </form>
  <SaveBar
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    @save="save"
    @reset="draft.reset"
  />
  <section v-if="config" class="panel">
    <div class="section-heading">
      <h3>免费额度</h3>
      <button
        class="secondary"
        :disabled="busy || !config.key_configured || draft.dirty.value"
        @click="checkAccount"
      >
        检查免费额度
      </button>
    </div>
    <p class="muted">
      账户查询不消耗搜索次数。每次搜图前会核对套餐和余额；付费套餐、额外付费点数或信息不完整时停止查询。
    </p>
    <dl v-if="account" class="quota-grid">
      <div>
        <dt>套餐</dt>
        <dd>{{ account.plan_name || "未知" }}</dd>
      </div>
      <div>
        <dt>本月剩余查询</dt>
        <dd>{{ account.plan_searches_left ?? "未知" }}</dd>
      </div>
      <div>
        <dt>本月已用查询</dt>
        <dd>{{ account.this_month_usage ?? "未知" }}</dd>
      </div>
      <div>
        <dt>免费套餐核验</dt>
        <dd>{{ account.free_eligible ? "已确认" : "未确认" }}</dd>
      </div>
    </dl>
    <small v-if="account" class="muted"
      >检查时间：{{ date(account.checked_at) }}</small
    >
  </section>
  <section v-if="config" class="panel">
    <div class="section-heading">
      <h3>独立图片验证</h3>
      <span class="tag">不发送 QQ 消息</span>
    </div>
    <p>
      选择图片后点击验证，将这张图片提交给 SerpApi / Google
      Lens。单次只发起一次搜索，不额外请求 AI 概览；同一次响应有概览则直接展示。
    </p>
    <label
      >选择测试图片<input
        type="file"
        accept="image/jpeg,image/png,image/webp,image/gif"
        :disabled="busy"
        @change="choose"
    /></label>
    <p class="muted">
      支持 JPEG、PNG、WebP、GIF，原文件最多 8
      MiB。动图使用首帧，后台会按接口限制压缩。
    </p>
    <img
      v-if="preview"
      :src="preview"
      alt="本次待验证的图片"
      class="test-preview"
    />
    <button
      class="primary"
      :disabled="busy || !file || !config.key_configured || draft.dirty.value"
      @click="testImage"
    >
      {{ busy ? "正在处理…" : "使用免费额度验证图片" }}
    </button>
    <p v-if="draft.dirty.value" class="muted">
      请先保存密钥修改，再检查额度或验证图片。
    </p>
  </section>
  <section v-if="result" class="panel search-result">
    <div class="section-heading">
      <h3>最近一次验证</h3>
      <span class="tag">{{
        result.status === "results_found"
          ? "取得候选，待核对"
          : result.status === "error"
            ? "验证未完成"
            : "暂无匹配来源"
      }}</span>
    </div>
    <p class="muted">
      {{ date(result.ts) }} · 本次搜索请求
      {{ result.search_requests_this_run }} 次{{
        result.cached ? " · 缓存结果" : ""
      }}
    </p>
    <p v-if="result.error" class="error-text">{{ result.error }}</p>
    <p>{{ result.notice }}</p>
    <ul v-if="result.matches?.length" class="source-links">
      <li v-for="match in result.matches" :key="match.url">
        <a :href="match.url" target="_blank" rel="noopener noreferrer">{{
          match.title || match.source || "查看来源"
        }}</a>
        <small class="muted"
          >{{ match.source }} ·
          {{
            match.kind === "exact_matches"
              ? "完全匹配候选"
              : match.kind === "visual_matches"
                ? "外观匹配候选"
                : "网页结果"
          }}</small
        >
      </li>
    </ul>
    <h4>Google AI 概览</h4>
    <p v-if="result.overview?.text" class="overview-text">
      {{ result.overview.text }}
    </p>
    <p v-else class="muted">
      {{
        result.overview_notice || "本次没有取得 AI 概览，可继续核对上方来源。"
      }}
    </p>
    <ul v-if="result.overview?.references?.length" class="source-links">
      <li v-for="item in result.overview.references" :key="item.url">
        <a :href="item.url" target="_blank" rel="noopener noreferrer">{{
          item.title || "概览引用来源"
        }}</a>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.panel {
  margin-bottom: 20px;
}
.search-feedback {
  background: #eaf1de;
  padding: 14px 20px;
  border-radius: 12px;
  overflow-wrap: anywhere;
}
.clear-choice {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
}
.clear-choice input {
  width: auto;
}
.quota-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.quota-grid dt {
  color: var(--muted);
  font-size: 13px;
}
.quota-grid dd {
  margin: 6px 0;
  font-weight: 600;
}
.test-preview {
  display: block;
  max-width: 100%;
  max-height: 240px;
  object-fit: contain;
  margin: 16px 0;
  border-radius: 10px;
}
.source-links {
  padding-left: 20px;
}
.source-links li {
  margin: 12px 0;
  overflow-wrap: anywhere;
}
.source-links small {
  display: block;
  margin-top: 4px;
}
.overview-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .quota-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .section-heading {
    flex-wrap: wrap;
    gap: 12px;
  }
}
</style>
