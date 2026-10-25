<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { useRouter } from "vue-router";
import { api, query } from "../lib/api";
import { useGroup } from "../lib/groups";
import { useAction } from "../lib/state";
import { confirmAction } from "../lib/confirm";
import Insights from "../Insights.vue";
import StatusBadge from "../components/StatusBadge.vue";
const { scope, group } = useGroup(),
  router = useRouter(),
  members = ref<any[]>([]),
  requests = ref<any[]>([]),
  search = ref(""),
  epoch = ref(0),
  { busy, error, action } = useAction();
const filtered = computed(() =>
  members.value.filter((m) =>
    (m.name + " " + m.user_id).includes(search.value),
  ),
);
async function load() {
  await action(async () => {
    [members.value, requests.value] = await Promise.all([
      api("/members?" + query({ scope: scope.value })),
      api("/requests?" + query({ scope: scope.value })),
    ]);
  }, "");
}
async function remember(m: any) {
  if (
    m.remember &&
    !(await confirmAction(
      "停止记忆并遗忘成员",
      "将清理「" +
        m.name +
        "（" +
        m.user_id +
        "）」已有记忆与关联记录。恢复记忆后不会自动恢复已清理的资料。",
      "停止并遗忘",
    ))
  )
    return;
  await action(
    async () => {
      await api("/members/" + scope.value + "/" + m.user_id, "PUT", {
        remember: !m.remember,
      });
      members.value = await api("/members?" + query({ scope: scope.value }));
      epoch.value++;
    },
    m.remember ? "已停止记忆并清理资料" : "已恢复后续记忆",
  );
}
onMounted(load);
</script>
<template>
  <article class="panel">
    <div class="section-heading">
      <h3>成员记忆管理</h3>
      <input
        v-model="search"
        aria-label="搜索成员"
        placeholder="搜索昵称或 QQ 号"
      />
    </div>
    <div v-for="m in filtered" :key="m.user_id" class="member-row">
      <span class="avatar">{{ (m.name || m.user_id).slice(0, 1) }}</span>
      <div>
        <strong>{{ m.name || m.user_id }}</strong
        ><small>{{ m.user_id }}</small>
      </div>
      <StatusBadge
        :status="m.remember ? 'enabled' : 'paused'"
        :text="m.remember ? '正常记忆' : '停止记忆'"
      /><button class="quiet" :disabled="busy" @click="remember(m)">
        {{ m.remember ? "停止并遗忘" : "恢复记忆" }}
      </button>
    </div>
    <p v-if="!filtered.length" class="empty compact">
      暂无匹配成员。群聊记录会逐渐积累成员资料。
    </p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  </article>
  <Insights
    :key="epoch"
    view="profiles"
    :scope="scope"
    :persona="group?.persona || 'default'"
    :api="api"
    @open-call="
      router.push({ path: '/observe/calls', query: { scope, call: $event } })
    "
  />
  <article v-if="scope.includes(':g:')" class="panel">
    <div class="section-heading">
      <h3>入群申请记录</h3>
      <span class="tag">{{ requests.length }} 条</span>
    </div>
    <div v-for="r in requests" :key="r.id" class="list-row">
      <div>
        <strong>{{ r.user_id }}</strong>
        <p>{{ r.comment || "未填写说明" }}</p>
      </div>
      <StatusBadge :status="r.status" />
    </div>
    <p v-if="!requests.length" class="empty compact">
      暂无申请记录。启用申请处理工具后，可在这里查看处理结果。
    </p>
  </article>
</template>
