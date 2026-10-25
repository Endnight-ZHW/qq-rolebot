<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { changeLabel } from "../lib/change-labels";
import { api } from "../lib/api";
import type { AccessSnapshot } from "../lib/types";
import { useGroup } from "../lib/groups";
import { core, useAction } from "../lib/state";
import { clone, splitIds } from "../lib/format";
import { useDraft, changesBetween } from "../lib/drafts";
import SaveBar from "../components/SaveBar.vue";
import SettingSwitch from "../components/SettingSwitch.vue";
import NumberField from "../components/NumberField.vue";
import Overlay from "../components/Overlay.vue";
const { group, scope } = useGroup(),
  route = useRoute(),
  router = useRouter();
const { busy, error, action } = useAction();
const form = ref<any>(null),
  saved = ref<any>(null),
  members = ref<any[]>([]),
  caps = ref<any[]>([]);
const search = ref(""),
  category = ref(""),
  status = ref(""),
  memberSearch = ref(""),
  selectedUser = ref(""),
  newUser = ref("");
const selectedRole = ref(""),
  roleKey = ref(""),
  roleName = ref(""),
  diagnosticQQ = ref(""),
  effective = ref<any>(null);
const conflicts = ref<any>(null),
  formEl = ref<HTMLFormElement>();
const isGroup = computed(() => scope.value.includes(":g:"));
const views = computed(() =>
  isGroup.value
    ? [
        ["tools", "工具授权"],
        ["members", "成员授权"],
        ["roles", "权限组"],
        ["rules", "群默认规则"],
        ["diagnostics", "权限诊断"],
      ]
    : [["tools", "工具授权"]],
);
const view = computed(() => {
  const requested = String(route.query.view || "tools");
  return views.value.some(([id]) => id === requested) ? requested : "tools";
});
const selectedTool = computed(() =>
  core.tools.find(
    (t) => t.name === route.query.tool && (!t.group_only || isGroup.value),
  ),
);
const policy = computed(
  () => selectedTool.value && form.value?.tools[selectedTool.value.name],
);
const draft = useDraft(
  () => form.value,
  save,
  (v) => {
    form.value = v;
  },
);
const tools = computed(() =>
  core.tools.filter(
    (t) =>
      (!t.group_only || isGroup.value) &&
      (!search.value || (t.title + t.name).includes(search.value)) &&
      (!category.value || t.category === category.value) &&
      (!status.value ||
        !!form.value?.tools[t.name].enabled === (status.value === "enabled")),
  ),
);
const categories = computed(() => [
  ...new Set(core.tools.map((t) => t.category)),
]);
const userOptions = computed(() =>
  [
    ...new Set([
      ...members.value.map((m) => m.user_id),
      ...Object.keys(form.value?.permissions.members || {}),
    ]),
  ]
    .map((id) => ({
      id,
      name: members.value.find((m) => m.user_id === id)?.name || id,
    }))
    .filter((u) => (u.name + u.id).includes(memberSearch.value)),
);
const member = computed(
  () => form.value?.permissions.members[selectedUser.value],
);
const role = computed(() => form.value?.permissions.roles[selectedRole.value]);
const blankMember = () => ({
  roles: [],
  allow: [],
  deny: [],
  tools_disabled: false,
  muted: false,
  expires_at: null,
  max_duration: null,
  protected_ids: [],
});
const scopeLabel = (value: string) =>
  (({ disabled: "禁止使用", self: "仅限自己", all: "允许使用" }) as any)[
    value
  ] || "账号授权";
const userName = (id: string) =>
  members.value.find((m) => m.user_id === id)?.name || id;
const savedMember = computed(
  () => saved.value?.permissions.members[selectedUser.value] || blankMember(),
);
const moderationHint = computed(() => {
  const mute = form.value?.tools.member_mute;
  const unmute = form.value?.tools.member_unmute;
  if (!mute || !unmute) return "";
  if (
    mute.enabled &&
    mute.user_enabled &&
    (!unmute.enabled || !unmute.user_enabled)
  )
    return "禁言已允许用户请求，但解禁入口已关闭。需要解禁时只能等待到期或由有权限的人在 QQ 中处理。";
  if (
    mute.enabled &&
    mute.user_enabled &&
    mute.request_access &&
    !unmute.request_access
  )
    return "禁言按身份授权，解禁仍沿用账号授权；能请求禁言的管理员未必能请求解禁。请分别核对两项权限。";
  if (
    mute.enabled &&
    mute.user_enabled &&
    mute.request_access &&
    unmute.request_access
  ) {
    const rank: Record<string, number> = { disabled: 0, self: 1, all: 2 };
    const restricted = ["member", "admin"].filter(
      (a) => rank[mute.request_access[a]] > rank[unmute.request_access[a]],
    );
    if (restricted.length)
      return `${restricted.map((a) => (a === "admin" ? "管理员" : "普通成员")).join("、")}的解禁范围小于禁言范围，请确认是否符合预期。`;
  }
  return "禁言与解禁分别授权；开启禁言不会自动开启解禁。仅自己禁言后可能无法在群里发出解禁请求。";
});
const conflictRows = computed(() =>
  conflicts.value
    ? changesBetween(saved.value, conflicts.value).filter(
        (x) => !x.path.endsWith("revision"),
      )
    : [],
);
async function load() {
  await action(async () => {
    const [data, catalog, users] = await Promise.all([
      api<AccessSnapshot>("/groups/" + scope.value + "/access"),
      api("/permissions/catalog"),
      api("/members?scope=" + encodeURIComponent(scope.value)),
    ]);
    form.value = data;
    saved.value = clone(data);
    caps.value = catalog;
    members.value = users;
    draft.accept();
    const id = String(route.query.user || "");
    if (id) {
      selectedUser.value = id;
      diagnosticQQ.value = id;
    }
  }, "");
}
async function save() {
  if (!form.value || !formEl.value?.reportValidity()) return false;
  const inputs = document.querySelectorAll<HTMLInputElement>(
    ".overlay-panel input,.overlay-panel select,.overlay-panel textarea",
  );
  for (const input of inputs) if (!input.reportValidity()) return false;
  return action(async () => {
    try {
      const result = await api<AccessSnapshot>(
        "/groups/" + scope.value + "/access",
        "PUT",
        form.value,
      );
      form.value = result;
      saved.value = clone(result);
      draft.accept();
      conflicts.value = null;
      effective.value = null;
      if (group.value) {
        group.value.revision = result.group_revision;
        group.value.tools = clone(result.tools);
        group.value.interaction_enabled = result.interaction_enabled;
      }
    } catch (e: any) {
      if (e.status === 409)
        conflicts.value = await api("/groups/" + scope.value + "/access");
      throw e;
    }
  }, "当前会话权限已保存");
}
async function useServer() {
  await action(async () => {
    const groups = await api("/groups");
    core.groups = groups;
    form.value = clone(conflicts.value);
    saved.value = clone(conflicts.value);
    conflicts.value = null;
    draft.accept();
    effective.value = null;
  }, "已载入服务器版本");
}
function openTool(name: string) {
  router.replace({ query: { ...route.query, tool: name } });
}
function closeTool() {
  const query = { ...route.query };
  delete query.tool;
  router.replace({ query });
}
function setMode(name: string, value: string) {
  form.value.tools[name].request_access =
    value === "profile" ? { member: "disabled", admin: "all" } : null;
}
function preset(name: string, value: string) {
  Object.assign(form.value.tools[name], {
    enabled: true,
    user_enabled: true,
    request_access: { member: value, admin: "all" },
  });
}
function blockers(name: string) {
  const p = form.value.tools[name],
    t = core.tools.find((t) => t.name === name),
    reasons = [];
  if (!group.value?.enabled) reasons.push("会话未启用");
  if (group.value?.paused) reasons.push("会话已暂停");
  if (group.value?.management_paused && t.mutation)
    reasons.push("管理工具已停用");
  if (t.requires_interaction && !form.value.interaction_enabled)
    reasons.push("互动扩展未开启");
  if (t.service_configured === false) reasons.push("服务尚未配置");
  if (!p.enabled) reasons.push("工具已关闭");
  if (!p.user_enabled) reasons.push("用户请求关闭");
  return reasons.join(" · ") || "按授权规则检查";
}
async function control(key: string, value: boolean) {
  await action(async () => {
    const result = await api("/groups/" + scope.value, "PATCH", {
      expected_revision: form.value.group_revision,
      [key]: value,
    });
    if (group.value) Object.assign(group.value, result);
    form.value.group_revision = result.revision;
    saved.value.group_revision = result.revision;
    draft.acceptPart((v) => ({ ...v, group_revision: result.revision }));
    effective.value = null;
  }, "运行控制已更新，其他草稿尚未保存");
}
function chooseUser(id: string) {
  selectedUser.value = id;
  diagnosticQQ.value = id;
}
function addMember(id = newUser.value) {
  if (!/^\d{5,20}$/.test(id)) {
    error.value = "请填写真实 QQ 号（5 至 20 位数字）";
    return;
  }
  form.value.permissions.members[id] ||= blankMember();
  selectedUser.value = id;
  newUser.value = "";
  error.value = "";
}
function capValue(id: string) {
  return member.value.deny.includes(id)
    ? "deny"
    : member.value.allow.includes(id)
      ? "allow"
      : "inherit";
}
function setCap(id: string, value: string) {
  member.value.allow = member.value.allow.filter((x: string) => x !== id);
  member.value.deny = member.value.deny.filter((x: string) => x !== id);
  if (value !== "inherit") member.value[value].push(id);
}
function createRole() {
  if (
    !/^[a-z][a-z0-9_-]{0,39}$/.test(roleKey.value) ||
    ["owner", "member", "staff"].includes(roleKey.value) ||
    !roleName.value.trim()
  ) {
    error.value = "填写权限组名称和小写字母开头的有效标识";
    return;
  }
  if (form.value.permissions.roles[roleKey.value]) {
    error.value = "权限组标识已存在";
    return;
  }
  form.value.permissions.roles[roleKey.value] = {
    name: roleName.value.trim(),
    allow: [],
    max_duration: null,
    protected_ids: [],
  };
  selectedRole.value = roleKey.value;
  roleKey.value = "";
  roleName.value = "";
  error.value = "";
}
function roleMembers(id: string) {
  return Object.keys(form.value.permissions.members).filter((k) =>
    form.value.permissions.members[k].roles.includes(id),
  );
}
function expiryValue(ts: number) {
  return ts ? new Date(ts * 1000 + 8 * 3600000).toISOString().slice(0, 16) : "";
}
function setExpiry(e: Event) {
  const v = (e.target as HTMLInputElement).value;
  member.value.expires_at = v ? new Date(v + "+08:00").getTime() / 1000 : null;
}
async function diagnose() {
  await action(async () => {
    effective.value = await api(
      "/permissions/" + scope.value + "/effective/" + diagnosticQQ.value,
    );
  }, "");
}
onMounted(load);
</script>
<template>
  <div v-if="!form" class="panel">
    <p v-if="!error">正在读取权限配置…</p>
    <p v-else role="alert">
      {{ error }} <button class="secondary" @click="load">重新加载</button>
    </p>
  </div>
  <template v-else>
    <div class="workspace-toolbar">
      <div>
        <span class="scope-chip"
          >当前{{ isGroup ? "群" : "私聊" }} · {{ group?.name }}</span
        ><span class="muted">权限修改保存后生效</span>
      </div>
      <div class="button-row">
        <button
          class="secondary"
          :disabled="busy"
          @click="control('paused', !group?.paused)"
        >
          {{ group?.paused ? "恢复会话" : "暂停会话" }}</button
        ><button
          v-if="isGroup"
          class="danger-button"
          :disabled="busy"
          @click="control('management_paused', !group?.management_paused)"
        >
          {{ group?.management_paused ? "恢复管理工具" : "紧急停用管理工具" }}
        </button>
      </div>
    </div>
    <nav class="section-tabs" aria-label="权限中心分类">
      <RouterLink
        v-for="[id, label] in views"
        :key="id"
        :to="{ query: { view: id } }"
        :class="{ active: view === id }"
        >{{ label }}</RouterLink
      >
    </nav>
    <div v-if="conflicts" class="conflict-panel" role="alert">
      <strong>保存冲突 · 服务器配置已变化</strong>
      <p>
        当前草稿已保留。以下为服务器相对原版本的变更，可先查看草稿变更，再载入服务器版本重新编辑。
      </p>
      <ul>
        <li v-for="c in conflictRows" :key="c.path">
          {{ changeLabel(c.path) }}：{{ c.before }} → {{ c.after }}
        </li>
      </ul>
      <button type="button" class="secondary" @click="useServer">
        放弃草稿并载入服务器版本
      </button>
    </div>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <form ref="formEl" @submit.prevent="save">
      <template v-if="view === 'tools'">
        <p
          v-if="isGroup"
          class="field-hint"
          role="note"
          aria-label="禁言与解禁配置提示"
        >
          {{ moderationHint }} 此提示依据当前草稿，不会自动修改任何开关或授权。
        </p>
        <div class="list-toolbar">
          <input
            v-model="search"
            aria-label="搜索工具"
            placeholder="搜索工具名称或标识"
          /><select v-model="category" aria-label="工具分类">
            <option value="">全部分类</option>
            <option v-for="c in categories" :key="c">{{ c }}</option></select
          ><select v-model="status" aria-label="工具状态">
            <option value="">全部状态</option>
            <option value="enabled">已启用</option>
            <option value="disabled">已关闭</option></select
          ><span class="muted">{{ tools.length }} 个工具</span>
        </div>
        <div class="panel access-table">
          <div class="access-table-head">
            <span>工具 / 当前限制</span><span>启用</span><span>普通成员</span
            ><span>管理员</span><span>自主使用</span><span>每小时</span
            ><span></span>
          </div>
          <div v-for="tool in tools" :key="tool.name" class="access-row">
            <div>
              <button
                type="button"
                class="tool-title"
                @click="openTool(tool.name)"
              >
                {{ tool.title }}</button
              ><small>{{ blockers(tool.name) }}</small>
            </div>
            <SettingSwitch
              v-model="form.tools[tool.name].enabled"
              :label="'启用' + tool.title"
              :saved-value="saved.tools[tool.name].enabled"
              compact
              :disabled="busy"
            /><span data-label="普通成员">{{
              scopeLabel(form.tools[tool.name].request_access?.member)
            }}</span
            ><span data-label="管理员">{{
              scopeLabel(form.tools[tool.name].request_access?.admin)
            }}</span
            ><span data-label="自主使用">{{
              form.tools[tool.name].autonomous_enabled ? "允许" : "关闭"
            }}</span
            ><span data-label="每小时"
              >{{ form.tools[tool.name].max_per_hour }} 次</span
            ><button
              type="button"
              class="secondary"
              :aria-label="'配置' + tool.title"
              @click="openTool(tool.name)"
            >
              配置
            </button>
          </div>
          <div v-if="!tools.length" class="empty">没有符合筛选条件的工具</div>
        </div>
      </template>
      <template v-else-if="view === 'members'">
        <div class="master-detail">
          <aside class="panel object-list">
            <input
              v-model="memberSearch"
              aria-label="搜索成员"
              placeholder="搜索昵称或 QQ"
            /><button
              type="button"
              v-for="u in userOptions"
              :key="u.id"
              :class="{ active: selectedUser === u.id }"
              @click="chooseUser(u.id)"
            >
              <strong>{{ u.name }}</strong
              ><small
                >{{ u.id }} ·
                {{
                  form.permissions.members[u.id] ? "已配置" : "默认继承"
                }}</small
              >
            </button>
            <div class="section-divider"></div>
            <label
              >添加 QQ 授权<input
                v-model="newUser"
                inputmode="numeric"
                maxlength="20" /></label
            ><button type="button" class="secondary" @click="addMember()">
              添加到草稿
            </button>
          </aside>
          <section class="panel editor-panel">
            <template v-if="selectedUser"
              ><div class="section-heading">
                <h3>{{ userName(selectedUser) }} · {{ selectedUser }}</h3>
                <button
                  v-if="member"
                  type="button"
                  class="quiet"
                  @click="delete form.permissions.members[selectedUser]"
                >
                  恢复默认继承
                </button>
              </div>
              <template v-if="member"
                ><div class="form-grid">
                  <label
                    >授权有效期（北京时间，留空永久）<input
                      type="datetime-local"
                      :value="expiryValue(member.expires_at)"
                      @change="setExpiry" /></label
                  ><NumberField
                    field="tool.max_duration"
                    label="账号最长禁言时间"
                    v-model="member.max_duration"
                    nullable
                  /><label class="wide"
                    >该账号不能管理的 QQ<input
                      :value="member.protected_ids.join(', ')"
                      @change="
                        member.protected_ids = splitIds(
                          ($event.target as HTMLInputElement).value,
                        )
                      "
                  /></label>
                </div>
                <div class="setting-row">
                  <SettingSwitch
                    v-model="member.tools_disabled"
                    :saved-value="savedMember.tools_disabled"
                    label="禁止该账号使用工具"
                    tone="warning"
                  /><SettingSwitch
                    v-model="member.muted"
                    :saved-value="savedMember.muted"
                    label="暂停回应该账号"
                    tone="warning"
                  />
                </div>
                <h4>分配权限组</h4>
                <div class="check-options">
                  <label
                    ><input
                      v-model="member.roles"
                      type="checkbox"
                      value="staff"
                    />本群协管（管理指令）</label
                  ><label v-for="(r, id) in form.permissions.roles" :key="id"
                    ><input
                      v-model="member.roles"
                      type="checkbox"
                      :value="id"
                    />{{ r.name }}</label
                  >
                </div>
                <p class="field-hint">
                  自定义权限组不代表 QQ
                  管理员。分类工具范围仍然生效，明确禁止优先。
                </p>
                <details>
                  <summary>单项权限 · {{ caps.length }} 项</summary>
                  <div class="permission-matrix">
                    <label v-for="cap in caps" :key="cap.id"
                      >{{ cap.title
                      }}<select
                        :value="capValue(cap.id)"
                        :aria-label="cap.title + '单项权限'"
                        @change="
                          setCap(
                            cap.id,
                            ($event.target as HTMLSelectElement).value,
                          )
                        "
                      >
                        <option value="inherit">默认继承</option>
                        <option value="allow">单独允许</option>
                        <option value="deny">明确禁止</option>
                      </select></label
                    >
                  </div>
                </details></template
              ><template v-else
                ><p>当前成员使用默认继承，没有单独授权。</p>
                <button
                  type="button"
                  class="secondary"
                  @click="addMember(selectedUser)"
                >
                  配置该成员
                </button></template
              ></template
            >
            <div v-else class="empty">从左侧选择成员，或输入 QQ 添加授权</div>
          </section>
        </div>
      </template>
      <template v-else-if="view === 'roles'">
        <div class="master-detail">
          <aside class="panel object-list">
            <button
              v-for="(r, id) in form.permissions.roles"
              :key="id"
              type="button"
              :class="{ active: selectedRole === id }"
              @click="selectedRole = String(id)"
            >
              <strong>{{ r.name }}</strong
              ><small
                >{{ roleMembers(String(id)).length }} 位成员 ·
                {{ r.allow.length }} 项能力</small
              >
            </button>
            <h4>新建权限组</h4>
            <label
              >权限组标识<input
                v-model="roleKey"
                placeholder="例如 helpers"
                maxlength="40" /></label
            ><label>权限组名称<input v-model="roleName" maxlength="60" /></label
            ><button type="button" class="secondary" @click="createRole">
              创建草稿
            </button>
          </aside>
          <section class="panel editor-panel">
            <template v-if="role"
              ><div class="section-heading">
                <h3>{{ role.name }}</h3>
                <button
                  type="button"
                  class="quiet"
                  :disabled="!!roleMembers(selectedRole).length"
                  @click="
                    delete form.permissions.roles[selectedRole];
                    selectedRole = '';
                  "
                >
                  删除权限组
                </button>
              </div>
              <label
                >名称<input
                  v-model="role.name"
                  required
                  maxlength="60" /></label
              ><NumberField
                field="tool.max_duration"
                label="权限组最长禁言时间"
                v-model="role.max_duration"
                nullable
              /><label
                >保护的 QQ<input
                  :value="role.protected_ids.join(', ')"
                  @change="
                    role.protected_ids = splitIds(
                      ($event.target as HTMLInputElement).value,
                    )
                  "
              /></label>
              <p>
                关联成员：{{
                  roleMembers(selectedRole).map(userName).join("、") || "暂无"
                }}；删除前需解除成员分配。
              </p>
              <div class="check-options">
                <label v-for="cap in caps" :key="cap.id"
                  ><input
                    v-model="role.allow"
                    type="checkbox"
                    :value="cap.id"
                  />{{ cap.title }}</label
                >
              </div></template
            >
            <div v-else class="empty">选择或新建权限组</div>
          </section>
        </div>
      </template>
      <section v-else-if="view === 'rules'" class="panel editor-panel">
        <h3>群默认规则</h3>
        <SettingSwitch
          v-model="form.interaction_enabled"
          :saved-value="saved.interaction_enabled"
          label="启用本群互动扩展"
          description="控制消息发送、表情及定时任务等互动能力；工具授权仍独立检查。"
        />
        <SettingSwitch
          v-model="form.permissions.inherit_qq_admin"
          :saved-value="saved.permissions.inherit_qq_admin"
          label="QQ 群主和群管继承管理指令"
          description="仅影响暂停、恢复、紧急停用等指令，不改变工具的分类权限。"
        />
        <label
          >机器人自主群管规则<textarea
            v-model="form.permissions.autonomous_rules"
            rows="4"
            maxlength="4000"
          />
        </label>
        <p class="field-hint">
          只有允许自主使用的工具才会应用这些规则；用户的请求不构成自主授权。
        </p>
        <details>
          <summary>账号授权模式下，普通成员默认可用工具</summary>
          <div class="check-options">
            <label
              v-for="cap in caps.filter((c) => c.category === '日常工具')"
              :key="cap.id"
              ><input
                v-model="form.permissions.member_tools"
                type="checkbox"
                :value="cap.id"
              />{{ cap.title }}</label
            >
          </div>
        </details>
      </section>
      <section v-else-if="view === 'diagnostics'" class="panel editor-panel">
        <h3>检查实际生效权限</h3>
        <p class="muted">
          读取已保存配置。待保存的草稿不会影响诊断；目标、额度和 QQ
          执行权限将在执行时再次检查。
        </p>
        <div class="list-toolbar">
          <input
            v-model="diagnosticQQ"
            aria-label="诊断 QQ 号"
            placeholder="输入 QQ 号"
            inputmode="numeric"
          /><button
            type="button"
            class="primary"
            :disabled="busy || !/^\d{5,20}$/.test(diagnosticQQ)"
            @click="diagnose"
          >
            检查权限
          </button>
        </div>
        <template v-if="effective"
          ><p>
            身份：{{ effective.identity_source }} ·
            {{ effective.muted ? "已暂停回应" : "正常回应" }}
          </p>
          <p
            v-if="
              effective.tools.member_mute?.available &&
              (!effective.tools.member_unmute?.available ||
                (effective.tools.member_mute.target_scope === 'all' &&
                  effective.tools.member_unmute.target_scope === 'self'))
            "
            class="field-hint"
            role="note"
            aria-label="禁言与解禁诊断提示"
          >
            该 QQ 可以请求禁言，但解禁权限或范围不足：
            {{ effective.tools.member_unmute.reason || "解禁仅限自己" }}。
            两个工具分别授权，请按需要检查解除禁言的配置。
          </p>
          <details
            v-for="tool in core.tools.filter((t) => !t.group_only || isGroup)"
            :key="tool.name"
            class="diagnostic-row"
          >
            <summary>
              <strong>{{ tool.title }}</strong
              ><span
                :class="
                  effective.tools[tool.name].available ? 'teal' : 'danger'
                "
                >{{
                  effective.tools[tool.name].available
                    ? scopeLabel(effective.tools[tool.name].target_scope)
                    : "不可使用"
                }}</span
              ><small>{{ effective.tools[tool.name].reason }}</small>
            </summary>
            <ul>
              <li
                v-for="check in effective.tools[tool.name].checks"
                :key="check.key"
              >
                {{ check.passed ? "通过" : "受限" }} · {{ check.label }}
                {{ check.reason }}
              </li>
              <li
                v-for="check in effective.tools[tool.name].execution_checks"
                :key="check"
              >
                执行时核对 · {{ check }}
              </li>
            </ul>
            <template v-if="effective.tools[tool.name].limits">
              <p v-if="tool.name === 'member_mute'" class="field-hint">
                有效最长禁言：{{
                  effective.tools[tool.name].limits.max_duration
                }}
                秒； 工具设置
                {{
                  effective.tools[tool.name].limits.duration_sources.tool
                }}
                秒， 账号／权限组
                {{
                  effective.tools[tool.name].limits.duration_sources
                    .requester ?? "未单独限制"
                }}。
              </p>
              <p class="field-hint">
                每小时工具调用上限：{{
                  effective.tools[tool.name].limits.max_per_hour
                }}
                次。
              </p>
              <div v-if="tool.mutation" class="field-hint">
                <p>
                  工具保护：{{
                    effective.tools[
                      tool.name
                    ].limits.protection_sources.tool.join("、") || "无"
                  }}； 账号／权限组保护：{{
                    effective.tools[
                      tool.name
                    ].limits.protection_sources.requester.join("、") || "无"
                  }}； 默认管理保护：{{
                    effective.tools[
                      tool.name
                    ].limits.protection_sources.default.join("、") || "无"
                  }}。
                </p>
                <p>{{ effective.tools[tool.name].limits.protection_notice }}</p>
              </div>
            </template>
          </details>
          <h4>管理指令与本人撤回</h4>
          <div
            v-for="cap in caps.filter(
              (c) =>
                c.id.startsWith('command:') ||
                c.id === 'tool:message_recall_self',
            )"
            :key="cap.id"
            class="change-row"
          >
            <strong>{{ cap.title }}</strong>
            <span v-if="cap.id.startsWith('command:')">
              {{ effective.allow.includes(cap.id) ? "允许" : "未授权" }} ·
              {{ (effective.origins[cap.id] || []).join("、") || "默认规则" }}
              {{ effective.deny.includes(cap.id) ? " · 已明确禁止" : "" }}
            </span>
            <span v-else>
              {{
                effective.tools.message_recall_self.available
                  ? "仅限本人消息"
                  : "不可使用"
              }}
              ·
              {{
                effective.tools.message_recall_self.reason ||
                effective.tools.message_recall_self.identity_source
              }}
            </span>
          </div></template
        >
      </section>
    </form>
    <SaveBar
      :dirty="draft.dirty.value"
      :changes="draft.changes.value"
      :busy="busy"
      :error="error"
      @save="save"
      @reset="draft.reset"
    />
    <Overlay
      v-if="selectedTool && policy"
      :title="selectedTool.title + ' · 工具配置'"
      drawer
      :busy="busy"
      @close="closeTool"
    >
      <div class="editor-panel">
        <p class="scope-chip">当前{{ isGroup ? "群" : "私聊" }} · 保存后生效</p>
        <SettingSwitch
          v-model="policy.enabled"
          :saved-value="saved.tools[selectedTool.name].enabled"
          :label="'启用' + selectedTool.title"
        />
        <p class="notice" v-if="!policy.enabled">
          工具当前关闭。下方配置会保留，启用后按规则生效。
        </p>
        <SettingSwitch
          v-model="policy.user_enabled"
          :saved-value="saved.tools[selectedTool.name].user_enabled"
          label="允许用户请求"
        />
        <SettingSwitch
          v-if="
            !['profile_like', 'member_poke', 'image_search'].includes(
              selectedTool.name,
            )
          "
          v-model="policy.autonomous_enabled"
          :saved-value="saved.tools[selectedTool.name].autonomous_enabled"
          label="允许机器人自主使用"
        />
        <p v-else class="field-hint">
          此能力仅响应明确的用户请求，不自主执行。
        </p>
        <template v-if="isGroup"
          ><label
            >授权模式<select
              :value="policy.request_access ? 'profile' : 'legacy'"
              @change="
                setMode(
                  selectedTool.name,
                  ($event.target as HTMLSelectElement).value,
                )
              "
            >
              <option value="legacy">沿用账号授权</option>
              <option value="profile">按身份配置</option>
            </select></label
          >
          <div v-if="policy.request_access" class="form-grid">
            <label v-for="audience in ['member', 'admin']" :key="audience"
              >{{ audience === "member" ? "普通成员" : "管理员"
              }}<select
                v-model="policy.request_access[audience]"
                :aria-label="audience === 'member' ? '普通成员' : '管理员'"
              >
                <option
                  v-for="v in selectedTool.supported_scopes"
                  :key="v"
                  :value="v"
                >
                  {{ scopeLabel(v) }}
                </option>
              </select></label
            >
          </div>
          <p class="field-hint">
            管理员指真实 QQ
            群主、群管和后台管理员。个人或权限组授权不能扩大分类范围。
          </p>
          <div class="button-row">
            <button
              class="secondary"
              @click="preset(selectedTool.name, 'disabled')"
            >
              仅管理员使用</button
            ><button
              v-if="selectedTool.supported_scopes.includes('self')"
              class="secondary"
              @click="preset(selectedTool.name, 'self')"
            >
              普通成员仅自己
            </button>
          </div></template
        >
        <p
          v-if="['member_mute', 'member_unmute'].includes(selectedTool.name)"
          class="field-hint"
          role="note"
          aria-label="禁言与解禁工具提示"
        >
          {{
            moderationHint
          }}
          修改此工具不会同步修改另一项；保存前可在权限诊断中检查实际生效结果。
        </p>
        <NumberField
          :default-value="selectedTool.defaults?.max_per_hour"
          field="tool.max_per_hour"
          v-model="policy.max_per_hour"
        />
        <NumberField
          v-if="selectedTool.name === 'member_mute'"
          field="tool.max_duration"
          v-model="policy.max_duration"
        />
        <template v-if="selectedTool.name === 'profile_like'"
          ><NumberField
            field="tool.like_max_times"
            v-model="policy.like_max_times"
          /><NumberField
            field="tool.like_daily_limit"
            v-model="policy.like_daily_limit"
          />
          <p class="field-hint">
            程序额度跨群累计，北京时间零点重置。QQ
            实际接受数量以回执为准；结果未知时不自动补发。
          </p></template
        >
        <NumberField
          v-if="selectedTool.name === 'member_poke'"
          field="tool.poke_cooldown_seconds"
          v-model="policy.poke_cooldown_seconds"
        />
        <label v-if="selectedTool.mutation"
          >额外保护的 QQ 号<input
            :value="policy.protected_ids.join(', ')"
            @change="
              policy.protected_ids = splitIds(
                ($event.target as HTMLInputElement).value,
              )
            "
        /></label>
        <SettingSwitch
          v-if="selectedTool.name === 'member_kick'"
          v-model="policy.allow_reject_rejoin"
          label="允许禁止再次入群"
        />
        <p v-if="error" role="alert" class="error-text">{{ error }}</p>
      </div>
      <template #footer
        ><span class="muted">修改已加入权限中心草稿</span
        ><button class="secondary" @click="closeTool">返回列表</button
        ><button
          class="primary"
          :disabled="busy || !draft.dirty.value"
          @click="save"
        >
          保存全部权限修改
        </button></template
      >
    </Overlay>
  </template>
</template>
