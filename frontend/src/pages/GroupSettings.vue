<script setup lang="ts">
import { computed, ref } from "vue";
import { changeLabel } from "../lib/change-labels";
import { api } from "../lib/api";
import { core, useAction } from "../lib/state";
import { clone } from "../lib/format";
import { useGroup, groupPayload } from "../lib/groups";
import { useDraft, changesBetween } from "../lib/drafts";
import { configuration, runtimeKeys } from "../lib/config";
import SaveBar from "../components/SaveBar.vue";
import SettingSwitch from "../components/SettingSwitch.vue";
import NumberField from "../components/NumberField.vue";
const { group, scope } = useGroup(),
  { busy, error, action } = useAction();
const form = ref<any>(clone(group.value)),
  saved = ref<any>(clone(group.value)),
  conflict = ref<any>(null),
  search = ref(""),
  section = ref("basic"),
  formEl = ref<HTMLFormElement>();
const draft = useDraft(
  () => form.value,
  save,
  (v) => (form.value = v),
);
draft.accept();
const sections = [
  ["basic", "基本配置"],
  ["rhythm", "接话节奏"],
  ["segments", "分段回复"],
  ["advanced", "运行额度"],
];
const fields: Record<string, string[]> = {
  rhythm: [
    "listening_probability",
    "cooldown",
    "proactive_hourly",
    "followup_seconds",
  ],
  segments: [
    "segment_threshold",
    "segment_max_parts",
    "segment_interval_min",
    "segment_interval_max",
  ],
  advanced: runtimeKeys,
};
const visibleFields = computed(() =>
  (search.value
    ? Object.values(fields).flat()
    : fields[section.value] || []
  ).filter(
    (k) =>
      (k !== "listening_probability" || scope.value.includes(":g:")) &&
      (!search.value ||
        (configuration.fields["group." + k]?.label + k).includes(search.value)),
  ),
);
const conflictRows = computed(() =>
  conflict.value
    ? changesBetween(groupPayload(saved.value), groupPayload(conflict.value))
    : [],
);
async function save() {
  if (!formEl.value?.reportValidity()) return false;
  return action(async () => {
    const patch = Object.fromEntries(
      Object.entries(groupPayload(form.value)).filter(
        ([key, value]) =>
          JSON.stringify(value) !== JSON.stringify(saved.value[key]),
      ),
    );
    try {
      const result = await api("/groups/" + scope.value, "PATCH", {
        ...patch,
        expected_revision: saved.value.revision,
      });
      form.value = clone(result);
      saved.value = clone(result);
      if (group.value) Object.assign(group.value, result);
      draft.accept();
      conflict.value = null;
    } catch (e: any) {
      if (e.status === 409)
        conflict.value = (await api("/groups")).find(
          (g: any) => g.id === scope.value,
        );
      throw e;
    }
  }, "聊天设置已保存");
}
async function control(key: string) {
  await action(async () => {
    const result = await api("/groups/" + scope.value, "PATCH", {
      expected_revision: saved.value.revision,
      [key]: !saved.value[key],
    });
    if (group.value) Object.assign(group.value, result);
    for (const k of [key, "revision"]) {
      form.value[k] = result[k];
      saved.value[k] = result[k];
    }
    draft.acceptPart((v) => ({
      ...v,
      [key]: result[key],
      revision: result.revision,
    }));
  }, "运行控制已更新，其他修改保留在草稿中");
}
function useServer() {
  if (group.value) Object.assign(group.value, conflict.value);
  form.value = clone(conflict.value);
  saved.value = clone(conflict.value);
  conflict.value = null;
  draft.accept();
}
</script>
<template>
  <div class="workspace-toolbar">
    <span class="scope-chip">仅当前会话 · {{ group?.name }}</span>
    <div class="button-row">
      <button class="secondary" :disabled="busy" @click="control('paused')">
        {{ saved.paused ? "恢复会话" : "暂停会话" }}</button
      ><button
        v-if="scope.includes(':g:')"
        class="danger-button"
        :disabled="busy"
        @click="control('management_paused')"
      >
        {{ saved.management_paused ? "恢复管理工具" : "紧急停用管理工具" }}
      </button>
    </div>
  </div>
  <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
  <div v-if="conflict" class="conflict-panel">
    <strong>服务器配置已变化，当前草稿已保留</strong>
    <p v-for="c in conflictRows" :key="c.path">
      {{ changeLabel(c.path) }}：{{ c.before }} → {{ c.after }}
    </p>
    <button class="secondary" @click="useServer">
      放弃草稿并载入服务器版本
    </button>
  </div>
  <div class="settings-layout">
    <aside class="settings-navigation">
      <input
        v-model="search"
        aria-label="搜索聊天设置"
        placeholder="搜索参数"
      /><button
        v-for="[id, label] in sections"
        :key="id"
        :class="{ active: section === id && !search }"
        @click="
          section = id;
          search = '';
        "
      >
        {{ label }}
      </button>
    </aside>
    <form ref="formEl" class="panel editor-panel" @submit.prevent="save">
      <template v-if="section === 'basic' && !search"
        ><h3>基本配置</h3>
        <div class="form-grid">
          <label
            >显示名称<input
              v-model="form.name"
              maxlength="80"
              required /></label
          ><label
            >当前角色<select v-model="form.persona">
              <option v-for="p in core.personas" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select></label
          ><label class="wide"
            >群说明与互动偏好<textarea
              v-model="form.guidance"
              rows="5"
              maxlength="6000"
              placeholder="聊什么、怎样参与，以及大家之间的约定…"
            />
          </label>
        </div>
        <SettingSwitch
          v-model="form.enabled"
          :saved-value="saved.enabled"
          label="启用当前会话"
        /><SettingSwitch
          v-model="form.learning_enabled"
          :saved-value="saved.learning_enabled"
          label="自动学习群梗与表达"
        />
        <p class="field-hint">切换角色会保留原角色的记忆。</p></template
      >
      <template v-else
        ><h3>
          {{
            search ? "参数搜索" : sections.find((s) => s[0] === section)?.[1]
          }}
        </h3>
        <template v-if="section === 'segments' && !search"
          ><SettingSwitch
            v-model="form.segmented_reply"
            :saved-value="saved.segmented_reply"
            label="自然分段发送"
            description="较长回复按内容边界拆分，代码块和结构化内容保持完整。"
          />
          <p v-if="!form.segmented_reply" class="notice">
            分段当前关闭。下方参数仍可编辑和保存，开启后生效。
          </p></template
        >
        <div class="form-grid">
          <NumberField
            v-for="key in visibleFields"
            :key="key"
            :field="'group.' + key"
            v-model="form[key]"
          />
        </div>
        <p v-if="!visibleFields.length" class="empty">没有匹配的配置项</p>
        <p class="field-hint" v-if="section === 'rhythm'">
          旁听按普通聊天批次抽取；@、回复机器人及昵称唤醒不受影响。跟聊仍需语义相关。
        </p>
        <p class="field-hint" v-if="section === 'segments'">
          每分钟最多
          {{ form.send_per_minute }}
          条消息。发送额度不足时合并分段，或在配置时限内等待。
        </p>
        <p class="field-hint" v-if="section === 'advanced'">
          聊天与定时任务共用工具额度。交互最后一轮收束回答，达到额度后不继续执行工具。
        </p></template
      >
    </form>
  </div>
  <SaveBar
    :dirty="draft.dirty.value"
    :changes="draft.changes.value"
    :busy="busy"
    :error="error"
    @save="save"
    @reset="draft.reset"
  />
</template>
