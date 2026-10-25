<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../lib/api";
import { useGroup, groupPayload } from "../lib/groups";
import { core, loadCore, useAction } from "../lib/state";
import DashboardPage from "./DashboardPage.vue";
import { confirmAction } from "../lib/confirm";
import { useDraft } from "../lib/drafts";
import SettingSwitch from "../components/SettingSwitch.vue";
import SaveBar from "../components/SaveBar.vue";
const { group, scope } = useGroup(),
  { busy, error, action } = useAction(),
  enabled = ref(!!group.value?.enabled);
const draft = useDraft(
  () => enabled.value,
  saveEnabled,
  (value) => (enabled.value = value),
);
draft.accept();
const savedState = computed(() =>
  !group.value?.enabled
    ? "未启用"
    : group.value?.paused
      ? "已启用 · 已暂停"
      : "已启用",
);
async function saveEnabled() {
  return await action(
    async () => {
      await api("/groups/" + scope.value, "PATCH", {
        expected_revision: group.value?.revision,
        enabled: enabled.value,
      });
      await loadCore();
      draft.accept();
    },
    enabled.value ? "当前会话已启用" : "当前会话已关闭",
  );
}
async function pause() {
  await action(
    async () => {
      await api("/groups/" + scope.value, "PATCH", {
        expected_revision: group.value?.revision,
        paused: !group.value?.paused,
      });
      await loadCore();
    },
    group.value?.paused ? "已恢复当前会话" : "当前会话已暂停",
  );
}
async function emergency() {
  if (
    !(await confirmAction(
      "停用本群管理工具",
      "将立即停用当前群的管理工具，聊天设置与其他群不受影响。",
      "停用管理工具",
    ))
  )
    return;
  await action(async () => {
    await api("/groups/" + scope.value, "PATCH", {
      expected_revision: group.value?.revision,
      management_paused: true,
    });
    await loadCore();
  }, "本群管理工具已停用");
}
</script>
<template>
  <div class="panel summary-intro">
    <div>
      <h3>
        {{ core.personas.find((p) => p.id === group?.persona)?.name || "机器人" }}
        · 当前角色
      </h3>
      <p class="muted">
        {{
          group?.guidance || "还没有填写群说明，可以在聊天设置中补充相处偏好。"
        }}
      </p>
    </div>
    <div class="summary-controls">
      <div class="group-enable-panel">
        <SettingSwitch
          v-model="enabled"
          :saved-value="!!group?.enabled"
          :label="scope.split(':')[1] === 'g' ? '群聊启用' : '私聊启用'"
          on-text="已启用"
          off-text="未启用"
          :disabled="busy"
        />
        <div class="enable-save-row">
          <span class="saved-switch-state">当前生效：{{ savedState }}</span>
          <span v-if="draft.dirty.value" class="pending-change">{{
            enabled ? "待保存：将启用此会话" : "待保存：将关闭此会话"
          }}</span>
        </div>
      </div>
      <div class="button-row">
        <RouterLink
          class="secondary"
          :to="'/tasks?scope=' + encodeURIComponent(scope)"
          >查看本群任务</RouterLink
        ><button class="secondary" :disabled="busy" @click="pause">
          {{ group?.paused ? "恢复会话" : "暂停会话" }}</button
        ><button
          v-if="scope.split(':')[1] === 'g' && !group?.management_paused"
          class="danger-button"
          :disabled="busy"
          @click="emergency"
        >
          停用管理工具
        </button>
      </div>
    </div>
  </div>
  <DashboardPage :fixed-scope="scope" compact />
  <SaveBar
    :changes="draft.changes.value"
    :dirty="draft.dirty.value"
    :busy="busy"
    :error="error"
    @save="saveEnabled"
    @reset="draft.reset"
  />
</template>
<style scoped>
.summary-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px 24px;
  justify-content: flex-end;
  max-width: 100%;
}
.group-enable-panel {
  min-width: 208px;
}
.enable-save-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 11px;
  line-height: 1.6;
}
.saved-switch-state {
  color: #64786a;
}
.pending-change {
  color: #94600f;
}
@media (max-width: 767px) {
  .summary-controls,
  .group-enable-panel {
    width: 100%;
  }
}
</style>
