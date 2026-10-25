<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../lib/api";
import { core, useAction } from "../lib/state";
import { clone, splitIds } from "../lib/format";
import { useDraft, allowLeave, leaveBusy } from "../lib/drafts";
import { confirmAction } from "../lib/confirm";
import SaveBar from "../components/SaveBar.vue";
import PersonaImages from "../components/PersonaImages.vue";
const personas = computed(() => core.personas),
  selectedPersona = ref(""),
  personaForm = ref<any>({}),
  aliasText = ref(""),
  relationships = ref<any[]>([]),
  versions = ref<any[]>([]);
const { busy, error, action } = useAction();
const route = useRoute(),
  router = useRouter();
const draft = useDraft(
  () => ({
    form: personaForm.value,
    aliases: aliasText.value,
    relationships: relationships.value,
  }),
  () => savePersona(),
  (v) => {
    personaForm.value = v.form;
    aliasText.value = v.aliases;
    relationships.value = v.relationships;
  },
);
async function choose(id: string) {
  await router.push({ query: { ...route.query, persona: id } });
}
function setPersona(id: string) {
  selectedPersona.value = id;
  const p = personas.value.find((x) => x.id === id);
  if (p) {
    personaForm.value = clone(p);
    aliasText.value = (p.aliases || []).join(", ");
    relationships.value = Object.entries(p.relationships || {}).map(
      ([qq, label]) => ({ qq, label }),
    );
    draft.accept();
  }
}
async function newPersona() {
  if (!(await allowLeave())) return;
  selectedPersona.value = "";
  personaForm.value = {
    name: "新角色",
    description: "",
    prompt: "",
    aliases: [],
  };
  aliasText.value = "";
  relationships.value = [];
  versions.value = [];
  draft.accept();
  await router.replace({ query: { ...route.query, persona: undefined } });
}
function personaPayload() {
  return {
    name: personaForm.value.name,
    description: personaForm.value.description,
    prompt: personaForm.value.prompt,
    aliases: splitIds(aliasText.value),
    relationships: Object.fromEntries(
      relationships.value
        .filter((x) => x.qq && x.label)
        .map((x) => [x.qq, x.label]),
    ),
  };
}
async function savePersona(copy = false) {
  if (!personaForm.value.name?.trim()) {
    error.value = "请填写角色名字；角色设定可以稍后填写";
    return false;
  }
  return await action(async () => {
    const id = copy ? "" : selectedPersona.value;
    const p = await api(
      "/personas" + (id ? "/" + id : ""),
      id ? "PUT" : "POST",
      personaPayload(),
    );
    core.personas = await api("/personas");
    setPersona(p.id);
    if (!leaveBusy.value)
      await router.replace({ query: { ...route.query, persona: p.id } });
    versions.value = [];
  });
}
async function importPersona(event: Event) {
  if (!(await allowLeave())) {
    (event.target as HTMLInputElement).value = "";
    return;
  }
  await action(async () => {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    const p = JSON.parse(await file.text());
    const body = Object.fromEntries(
      ["name", "description", "prompt", "aliases", "relationships"]
        .filter((k) => k in p)
        .map((k) => [k, p[k]]),
    );
    const result = await api("/personas", "POST", body);
    core.personas = await api("/personas");
    setPersona(result.id);
    await router.replace({ query: { ...route.query, persona: result.id } });
  }, "角色已导入");
  (event.target as HTMLInputElement).value = "";
}
function download(name: string, data: any) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function restorePersona(version: number) {
  if (
    !(await confirmAction(
      "恢复角色版本",
      "将以所选版本创建新的角色版本，当前未保存的内容会被替换。",
      "恢复版本",
    ))
  )
    return;
  await action(async () => {
    await api(`/personas/${selectedPersona.value}/restore/${version}`, "POST");
    core.personas = await api("/personas");
    setPersona(selectedPersona.value);
    versions.value = await api(`/personas/${selectedPersona.value}/versions`);
  }, "已恢复为一个新版本");
}

setPersona(
  core.personas.find((p) => p.id === route.query.persona)?.id ||
    core.personas[0]?.id,
);
watch(
  () => route.query.persona,
  (id) => {
    if (id && core.personas.some((p) => p.id === id)) setPersona(String(id));
  },
);
</script>
<template>
  <div class="page-intro">
    <p>性格和口吻是一张角色卡，经历则由记忆慢慢填满。</p>
    <div class="button-row">
      <label class="button secondary"
        >导入角色<input
          type="file"
          accept=".json"
          hidden
          @change="importPersona" /></label
      ><button class="primary" @click="newPersona">＋ 新建角色</button>
    </div>
  </div>
  <div class="persona-layout">
    <div class="persona-list">
      <button
        v-for="p in personas"
        :key="p.id"
        :class="{ chosen: selectedPersona === p.id }"
        @click="
          choose(p.id);
          versions = [];
        "
      >
        <span class="avatar">{{ p.name.slice(0, 1) }}</span>
        <div>
          <strong>{{ p.name }}</strong
          ><small>{{ p.description || "还没有简介" }}</small>
        </div>
        <span class="tag">v{{ p.version }}</span>
      </button>
    </div>
    <form class="panel form-grid" @submit.prevent="savePersona()">
      <div class="section-heading wide">
        <h3>{{ selectedPersona ? "编辑角色" : "新角色" }}</h3>
        <span class="tag">{{ selectedPersona || "尚未保存" }}</span>
      </div>
      <label
        >角色名字<input
          v-model="personaForm.name"
          required
          maxlength="80" /></label
      ><label
        >唤醒称呼<input v-model="aliasText" placeholder="机器人，助手" /></label
      ><label class="wide"
        >一句话简介<input v-model="personaForm.description" /></label
      ><label class="wide"
        >角色设定<textarea
          v-model="personaForm.prompt"
          rows="12"
          required
          placeholder="写下她的性格、说话方式、背景与相处习惯……"
        ></textarea>
      </label>
      <div class="wide">
        <div class="section-heading">
          <h4>人物关系</h4>
          <button
            type="button"
            class="link"
            @click="relationships.push({ qq: '', label: '' })"
          >
            ＋ 添加绑定
          </button>
        </div>
        <small class="muted">使用 QQ 号识别人，关系称呼不授予管理权限。</small>
        <div v-for="(r, i) in relationships" class="relationship-row">
          <input v-model="r.qq" placeholder="QQ 号" inputmode="numeric" /><input
            v-model="r.label"
            placeholder="由你填写的关系备注"
          /><button
            type="button"
            class="quiet"
            @click="relationships.splice(i, 1)"
          >
            移除
          </button>
        </div>
      </div>
      <div class="form-footer wide">
        <div class="button-row">
          <button
            v-if="selectedPersona"
            type="button"
            class="secondary"
            @click="savePersona(true)"
          >
            另存为新角色</button
          ><button
            type="button"
            class="quiet"
            @click="download(personaForm.name + '.json', personaPayload())"
          >
            导出
          </button>
        </div>
      </div>
      <div v-if="selectedPersona" class="wide">
        <PersonaImages :key="selectedPersona" :persona="selectedPersona" />
        <button
          type="button"
          class="link"
          @click="
            action(async () => {
              versions = await api(
                '/personas/' + selectedPersona + '/versions',
              );
            }, '已读取历史版本')
          "
        >
          查看历史版本
        </button>
        <div v-for="v in versions" class="version-row">
          <span>v{{ v.version }} · {{ v.name }}</span
          ><button
            type="button"
            class="quiet"
            @click="restorePersona(v.version)"
          >
            恢复此版本
          </button>
        </div>
      </div>
    </form>
  </div>
  <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  <SaveBar
    save-label="保存角色"
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="error"
    @save="savePersona()"
    @reset="draft.reset"
  />
</template>
