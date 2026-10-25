<script setup lang="ts">
import SettingSwitch from "./components/SettingSwitch.vue";
import { onMounted, ref, computed } from "vue";
import { useRoute, useRouter } from "vue-router";
const route = useRoute(),
  router = useRouter();
import { useDraft } from "./lib/drafts";
import NumberField from "./components/NumberField.vue";
import SaveBar from "./components/SaveBar.vue";

const props = defineProps<{
  api: (path: string, method?: string, body?: any) => Promise<any>;
}>();
const emit = defineEmits<{ saved: [] }>();
const config = ref<any>(null),
  busy = ref(false),
  message = ref(""),
  failed = ref(false);
const globalBudget = ref<number>(0);
const effectiveBudget = computed(() =>
  Math.min(
    globalBudget.value,
    ...["chat", "decision"].map((p) => {
      const route = config.value?.routes[p];
      const model = config.value?.models.find(
        (m: any) => m.id === route?.model,
      );
      return (model?.context_window || 0) - (route?.max_tokens || 0) - 3000;
    }),
  ),
);
const keys = ref<Record<string, string>>({}),
  clearKeys = ref<string[]>([]);
const discovered = ref<Record<string, any[]>>({});
const panel = computed(() =>
  ["routes", "providers", "models"].includes(String(route.query.panel))
    ? String(route.query.panel)
    : "routes",
);
const providerId = computed(
  () =>
    config.value?.providers.find((p: any) => p.id === route.query.provider)
      ?.id || config.value?.providers[0]?.id,
);
const modelId = computed(
  () =>
    config.value?.models.find((m: any) => m.id === route.query.model)?.id ||
    config.value?.models[0]?.id,
);
const tasks = [
  { id: "chat", name: "聊天", hint: "角色日常回复" },
  { id: "decision", name: "决策", hint: "接话判断与工具调用" },
  { id: "memory", name: "记忆整理", hint: "摘要与长期记忆" },
  { id: "creation", name: "作品制作", hint: "后台图表、动画与三维制作，独立高思考路线" },
];

const draft = useDraft(
  () => ({
    config: config.value,
    keys: keys.value,
    clearKeys: clearKeys.value,
  }),
  save,
  (v) => {
    config.value = v.config;
    keys.value = v.keys;
    clearKeys.value = v.clearKeys;
  },
);
async function run(fn: () => Promise<void>, success = "") {
  busy.value = true;
  message.value = "";
  failed.value = false;
  try {
    await fn();
    if (success) message.value = success;
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
    const [models, settings] = await Promise.all([
      props.api("/models"),
      props.api("/settings"),
    ]);
    config.value = models;
    globalBudget.value = settings.context_tokens;
    draft.accept();
  });
}
onMounted(load);
async function save() {
  return await run(async () => {
    const { key_configured, ...body } = config.value;
    body.keys = Object.fromEntries(
      Object.entries(keys.value).filter(
        ([id, key]) => key.trim() && !clearKeys.value.includes(id),
      ),
    );
    body.clear_keys = clearKeys.value;
    config.value = await props.api("/models", "PUT", body);
    keys.value = {};
    clearKeys.value = [];
    discovered.value = {};
    draft.accept();
    emit("saved");
  }, "模型配置已保存，下次请求生效");
}
function newId(prefix: string) {
  return prefix + "-" + crypto.randomUUID().slice(0, 8);
}
function addProvider() {
  const id = newId("provider");
  router.replace({
    query: { ...route.query, panel: "providers", provider: id },
  });
  config.value.providers.push({
    id,
    name: "新供应商",
    base_url: "https://api.deepseek.com/anthropic",
    protocol: "anthropic-messages",
    enabled: true,
  });
}
function protocolChange(p: any) {
  if (
    [
      "https://api.deepseek.com",
      "https://api.deepseek.com/v1",
      "https://api.deepseek.com/anthropic",
      "https://api.deepseek.com/anthropic/v1",
    ].includes(p.base_url)
  )
    p.base_url =
      p.protocol === "anthropic-messages"
        ? "https://api.deepseek.com/anthropic"
        : "https://api.deepseek.com";
}
function addModel(provider?: string, modelId = "", name = "") {
  const id = newId("model");
  router.replace({ query: { ...route.query, panel: "models", model: id } });
  config.value.models.push({
    id,
    provider_id: provider || config.value.providers[0].id,
    model_id: modelId,
    name: name || modelId || "新模型",
    description: "请核对供应商的容量与图片能力",
    enabled: true,
    context_window: 64000,
    max_tokens: 4096,
    input_modalities: ["text"],
  });
}
function removeProvider(id: string) {
  config.value.providers = config.value.providers.filter(
    (p: any) => p.id !== id,
  );
  delete keys.value[id];
  clearKeys.value = clearKeys.value.filter((x) => x !== id);
}
async function discover(id: string) {
  await run(async () => {
    const r = await props.api("/models/discover/" + id, "POST");
    discovered.value[id] = r.models;
    message.value = r.models.length
      ? "已读取接口模型目录，点击名称加入配置草稿"
      : "接口未列出模型，可以手动添加";
  });
}
async function test(id: string) {
  await run(async () => {
    const r = await props.api("/models/test/" + id, "POST");
    message.value =
      "连接测试成功：" +
      (r.reply || "接口已返回（正文为空，可检查思考与输出上限）");
  });
}
</script>

<template>
  <div class="page-intro">
    <p>为聊天、决策和记忆选择模型，分别控制思考强度与输出长度。</p>
    <span class="tag">请求时读取配置</span>
  </div>
  <p
    v-if="message"
    role="status"
    class="model-feedback"
    :class="{ 'error-text': failed }"
  >
    {{ message }}
  </p>
  <button
    v-if="failed && !config"
    class="secondary"
    :disabled="busy"
    @click="load"
  >
    重新加载模型配置
  </button>
  <nav class="sub-tabs" aria-label="模型设置分类">
    <RouterLink
      v-for="t in [
        { id: 'routes', name: '用途路由' },
        { id: 'providers', name: '供应商连接' },
        { id: 'models', name: '模型目录' },
      ]"
      :key="t.id"
      :to="{ query: { ...route.query, panel: t.id } }"
      :class="{ active: panel === t.id }"
      >{{ t.name }}</RouterLink
    >
  </nav>
  <form v-if="config" @submit.prevent="save" class="model-manager" novalidate>
    <article class="panel" v-show="panel === 'routes'">
      <div class="section-heading">
        <h3>任务配置</h3>
        <span class="tag">3 个独立任务</span>
      </div>
      <p class="notice">
        聊天／判定有效输入硬预算：{{
          Math.max(0, effectiveBudget).toLocaleString()
        }}
        token，取全局预算与两条模型路由可用容量的较小值。
      </p>
      <div class="route-grid">
        <div v-for="task in tasks" :key="task.id" class="route-card">
          <h4>{{ task.name }}</h4>
          <p class="muted">{{ task.hint }}</p>
          <label
            >{{ task.name }}模型<select
              v-model="config.routes[task.id].model"
              :aria-label="task.name + '模型'"
              required
            >
              <option v-for="m in config.models" :key="m.id" :value="m.id">
                {{ m.name }} ·
                {{
                  config.providers.find((p: any) => p.id === m.provider_id)
                    ?.name || "供应商不存在"
                }}{{ m.enabled ? "" : "（已停用）" }}
              </option>
            </select></label
          >
          <label
            >{{ task.name }}思考强度<select
              v-model="config.routes[task.id].reasoning_effort"
              :aria-label="task.name + '思考强度'"
            >
              <option value="off">off · 关闭思考</option>
              <option value="low">low · 轻量思考</option>
              <option value="high">high · 深入思考</option>
              <option value="max">max · 最大强度</option>
            </select></label
          >
          <NumberField
            field="route.max_tokens"
            :label="task.name + '输出上限'"
            v-model="config.routes[task.id].max_tokens"
            :max="
              config.models.find(
                (m: any) => m.id === config.routes[task.id].model,
              )?.max_tokens
            "
          />
          <p class="field-hint">
            模型剩余输入容量：{{
              Math.max(
                0,
                (config.models.find(
                  (m: any) => m.id === config.routes[task.id].model,
                )?.context_window || 0) -
                  config.routes[task.id].max_tokens -
                  3000,
              ).toLocaleString()
            }}
            token（已预留输出与协议空间）
          </p>
          <button
            class="secondary"
            type="button"
            :disabled="busy"
            @click="test(task.id)"
          >
            测试{{ task.name }}连接
          </button>
        </div>
      </div>
      <small class="muted"
        >测试使用已保存的配置，会产生一次短模型请求。输出上限包括思考
        token，较强思考需要更多空间。容量信息是配置声明，接口测试可验证账号是否有权调用。</small
      >
    </article>
    <article class="panel" v-show="panel === 'providers'">
      <div class="section-heading">
        <h3>供应商连接</h3>
        <button type="button" class="secondary" @click="addProvider">
          ＋ 添加供应商
        </button>
      </div>
      <div class="model-editor-layout">
        <aside class="config-list" aria-label="供应商列表">
          <button
            v-for="provider in config.providers"
            :key="provider.id"
            type="button"
            :class="{ selected: providerId === provider.id }"
            @click="
              router.replace({
                query: { ...route.query, provider: provider.id },
              })
            "
          >
            <strong>{{ provider.name || "新供应商" }}</strong
            ><small
              >{{ provider.enabled ? "已启用" : "未启用" }} ·
              {{
                config.key_configured[provider.id]
                  ? "密钥已配置"
                  : "等待配置密钥"
              }}</small
            >
          </button>
        </aside>
        <div class="model-editor-content">
          <div
            v-show="p.id === providerId"
            v-for="p in config.providers"
            :key="p.id"
            class="provider-card form-grid"
          >
            <label
              >供应商名称<input v-model="p.name" required maxlength="100"
            /></label>
            <label
              >接口协议<select
                v-model="p.protocol"
                aria-label="接口协议"
                @change="protocolChange(p)"
              >
                <option value="openai-completions">
                  DeepSeek / OpenAI Chat Completions
                </option>
                <option value="anthropic-messages">
                  DeepSeek Messages · Harness
                </option>
              </select></label
            >
            <label class="wide"
              >API 地址<input type="url" v-model="p.base_url" required
            /></label>
            <label
              >API Key<input
                type="password"
                v-model="keys[p.id]"
                autocomplete="new-password"
                :disabled="clearKeys.includes(p.id)"
                :placeholder="
                  config.key_configured[p.id] ? '已配置，留空保留' : '尚未配置'
                "
            /></label>
            <div class="key-controls">
              <SettingSwitch
                v-model="p.enabled"
                label="启用供应商"
                :disabled="busy"
              /><label class="switch-label"
                ><input
                  type="checkbox"
                  :value="p.id"
                  v-model="clearKeys"
                />保存时清除密钥</label
              >
            </div>
            <div class="form-footer wide">
              <small class="muted"
                >ID：{{ p.id }} · 密钥仅写入服务器受限文件</small
              >
              <div class="button-row">
                <button
                  type="button"
                  class="secondary"
                  :disabled="busy || !config.key_configured[p.id]"
                  @click="discover(p.id)"
                >
                  读取接口模型目录</button
                ><button
                  type="button"
                  class="ghost danger"
                  :disabled="config.providers.length === 1"
                  @click="removeProvider(p.id)"
                >
                  移除供应商
                </button>
              </div>
            </div>
            <div class="wide model-discovery" v-if="discovered[p.id]?.length">
              <button
                v-for="m in discovered[p.id]"
                :key="m.id"
                type="button"
                class="secondary"
                @click="addModel(p.id, m.id, m.name)"
              >
                ＋ {{ m.name }}
              </button>
            </div>
          </div>
        </div>
      </div>
      <label
        >联网搜索 / 网页读取供应商<select v-model="config.search_provider">
          <option v-for="p in config.providers" :key="p.id" :value="p.id">
            {{ p.name }}
          </option></select
        ><small>需要供应商支持 DeepSeek Search/Fetch 接口。</small></label
      >
      <small class="muted"
        >Messages 使用 /v1/messages；自定义地址应兼容所选协议与 DeepSeek
        思考参数。供应商不支持列出模型时可手动维护下方目录。</small
      >
    </article>
    <article class="panel" v-show="panel === 'models'">
      <div class="section-heading">
        <h3>模型目录</h3>
        <button class="secondary" type="button" @click="addModel()">
          ＋ 添加模型
        </button>
      </div>
      <div class="model-editor-layout">
        <aside class="config-list" aria-label="模型列表">
          <button
            v-for="model in config.models"
            :key="model.id"
            type="button"
            :class="{ selected: modelId === model.id }"
            @click="
              router.replace({ query: { ...route.query, model: model.id } })
            "
          >
            <strong>{{ model.name || "新模型" }}</strong
            ><small
              >{{ model.enabled ? "已启用" : "未启用" }} ·
              {{ model.model_id || "未填写接口模型 ID" }}</small
            >
          </button>
        </aside>
        <div class="model-editor-content">
          <div
            v-show="m.id === modelId"
            v-for="m in config.models"
            :key="m.id"
            class="provider-card form-grid"
          >
            <label
              >显示名称<input
                v-model="m.name"
                required
                maxlength="100" /></label
            ><label
              >接口模型 ID<input v-model="m.model_id" required maxlength="150"
            /></label>
            <label
              >所属供应商<select v-model="m.provider_id" required>
                <option v-for="p in config.providers" :key="p.id" :value="p.id">
                  {{ p.name }}
                </option>
              </select></label
            ><NumberField
              field="model.context_window"
              v-model="m.context_window"
            />
            <NumberField
              field="model.max_tokens"
              v-model="m.max_tokens"
              :max="m.context_window - 1"
            />
            <div class="key-controls">
              <label class="switch-label"
                ><input
                  type="checkbox"
                  value="image"
                  v-model="m.input_modalities"
                />支持图片输入</label
              ><SettingSwitch
                v-model="m.enabled"
                label="启用模型"
                :disabled="busy"
              />
            </div>
            <label class="wide"
              >模型说明<input v-model="m.description" maxlength="500"
            /></label>
            <div class="form-footer wide">
              <small class="muted">{{
                m.input_modalities.includes("image")
                  ? "文字 + 图片"
                  : "仅文字；图片会明确降级"
              }}</small
              ><button
                class="ghost danger"
                type="button"
                :disabled="config.models.length === 1"
                @click="
                  config.models = config.models.filter(
                    (x: any) => x.id !== m.id,
                  )
                "
              >
                移除模型
              </button>
            </div>
          </div>
        </div>
      </div>
    </article>
    <div class="panel model-save">
      <small class="muted"
        >停用会阻止后续调用。移除前请先调整关联模型和任务；修改不会自动授权群管理工具。</small
      ><span class="muted">修改后在保存条统一提交</span>
    </div>
  </form>
  <SaveBar
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="failed ? message : ''"
    @save="save"
    @reset="draft.reset"
  />
</template>

<style scoped>
.route-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 18px;
}
.route-card {
  background: #f4f7f3;
  border: 1px solid var(--line);
  padding: 18px;
  border-radius: 14px;
  min-width: 0;
}
.route-card h4 {
  font-size: 16px;
}
.route-card label {
  margin-bottom: 12px;
}
.provider-card {
  border-top: 1px solid var(--line);
  padding-top: 24px;
  margin-top: 18px;
  margin-bottom: 24px;
}
.key-controls {
  display: grid;
  align-content: center;
  gap: 12px;
}
.model-save,
.button-row,
.model-discovery {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
}
.model-discovery {
  justify-content: flex-start;
}
.model-feedback {
  background: #eaf1de;
  padding: 14px 20px;
  border-radius: 12px;
  overflow-wrap: anywhere;
}
.model-save {
  position: static;
}
.model-manager input,
.model-manager select {
  min-width: 0;
}
@media (max-width: 1050px) {
  .route-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 600px) {
  .model-save small {
    display: none;
  }
  .model-save {
    padding: 14px;
  }
  .model-save button {
    width: 100%;
  }
}
</style>
