<script setup lang="ts">
import { computed, ref } from "vue";
import SettingSwitch from "./SettingSwitch.vue";
import { api } from "../lib/api";
import { core, loadCore, useAction } from "../lib/state";
import { useDraft } from "../lib/drafts";
import SaveBar from "./SaveBar.vue";
const props = defineProps<{ scope: string; stickers?: boolean }>();
const g = computed(() => core.groups.find((g) => g.id === props.scope));
const form = ref({
  interaction_enabled: g.value?.interaction_enabled || false,
  sticker_autocollect: g.value?.sticker_autocollect || false,
});
const revision = ref(g.value?.revision || 0);
const { busy, error, action } = useAction();
const draft = useDraft(
  () => form.value,
  save,
  (v) => (form.value = v),
);
draft.accept();
async function save() {
  return await action(async () => {
    const result = await api("/groups/" + props.scope, "PATCH", {
      ...form.value,
      expected_revision: revision.value,
    });
    revision.value = result.revision;
    draft.accept();
    await loadCore();
  }, "互动设置已保存");
}
</script>
<template>
  <article class="panel interaction-settings">
    <div class="section-heading">
      <div>
        <h3>本群互动扩展</h3>
        <p class="muted">开启后可使用定时任务与表情互动。</p>
      </div>
      <span class="tag"
        >当前生效：{{ g?.interaction_enabled ? "已开启" : "未开启" }}</span
      >
    </div>
    <form @submit.prevent="save">
      <SettingSwitch
        v-model="form.interaction_enabled"
        :saved-value="!!g?.interaction_enabled"
        label="启用本群互动扩展"
        description="修改后点击保存开关生效。"
        :disabled="busy"
      />
      <template v-if="stickers">
        <SettingSwitch
          class="autocollect-switch"
          v-model="form.sticker_autocollect"
          :saved-value="!!g?.sticker_autocollect"
          label="自动筛选收藏并使用"
          description="从本群图片中筛选表情，加入所有群共用的表情库。"
          :disabled="busy"
        />
      </template>
      <div class="form-footer">
        <small>{{
          draft.dirty.value
            ? "有未保存的开关修改"
            : "互动工具仍需满足群工具授权和 QQ 账号权限。"
        }}</small>
      </div>
    </form>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
  </article>
  <SaveBar
    inline
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="error"
    @save="save"
    @reset="draft.reset"
  />
</template>
<style scoped>
.autocollect-switch {
  margin-top: 14px;
}
.interaction-settings .section-heading {
  flex-wrap: wrap;
}
.interaction-settings .tag {
  font-size: 12px;
}
.interaction-settings .form-footer {
  margin-top: 16px;
}
</style>
