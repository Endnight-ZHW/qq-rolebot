import { createRouter, createWebHistory } from "vue-router";
import { allowLeave } from "./lib/drafts";
import { session } from "./lib/api";
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/overview" },
    {
      path: "/login",
      component: () => import("./pages/LoginPage.vue"),
      meta: { title: "登录工作台" },
    },
    {
      path: "/overview",
      component: () => import("./pages/DashboardPage.vue"),
      meta: {
        title: "总览",
        section: "overview",
        description: "运行状态、近期变化与需要关注的事",
      },
    },
    {
      path: "/groups",
      component: () => import("./pages/GroupsPage.vue"),
      meta: {
        title: "会话管理",
        section: "groups",
        description: "每个群，都有自己的角色与相处方式",
      },
    },
    {
      path: "/groups/:scope",
      component: () => import("./pages/GroupWorkspace.vue"),
      meta: { title: "会话管理", section: "groups" },
      children: [
        { path: "", redirect: (to) => ({ path: to.path + "/summary" }) },
        {
          path: "summary",
          component: () => import("./pages/GroupSummary.vue"),
          meta: { subtitle: "群概况" },
        },
        {
          path: "settings",
          component: () => import("./pages/GroupSettings.vue"),
          meta: { subtitle: "聊天设置" },
        },
        {
          path: "members",
          component: () => import("./pages/MembersPage.vue"),
          meta: { subtitle: "成员与学习" },
        },
        {
          path: "memory",
          component: () => import("./pages/MemoryPage.vue"),
          meta: { subtitle: "记忆档案" },
        },
        {
          path: "context",
          component: () => import("./pages/ContextPage.vue"),
          meta: { subtitle: "上下文" },
        },
        {
          path: "stickers",
          component: () => import("./pages/StickersPage.vue"),
          meta: { subtitle: "表情库" },
        },
        {
          path: "access",
          component: () => import("./pages/AccessPage.vue"),
          meta: { subtitle: "权限中心" },
        },
        {
          path: "tools",
          redirect: (to) => ({
            path: to.path.replace(/tools$/, "access"),
            query: { ...to.query, view: "tools" },
          }),
        },
        {
          path: "permissions",
          redirect: (to) => ({
            path: to.path.replace(/permissions$/, "access"),
            query: { ...to.query, view: "members" },
          }),
        },
      ],
    },
    {
      path: "/personas",
      component: () => import("./pages/PersonasPage.vue"),
      meta: {
        title: "角色库",
        section: "personas",
        description: "设定性格与口吻，保存每一次成长",
      },
    },
    {
      path: "/tasks",
      component: () => import("./pages/TasksPage.vue"),
      meta: {
        title: "任务中心",
        section: "tasks",
        description: "安排提醒与定时查询，查看每一次执行",
      },
    },
    {
      path: "/observe/:view?",
      component: () => import("./pages/ObservePage.vue"),
      meta: {
        title: "观测中心",
        section: "observe",
        description: "从运行变化，追溯到具体请求",
      },
    },
    {
      path: "/configuration/:view?",
      component: () => import("./pages/ConfigurationPage.vue"),
      meta: {
        title: "模型与服务",
        section: "configuration",
        description: "配置推理能力，调整机器人的表达方式",
      },
    },
    {
      path: "/settings",
      component: () => import("./pages/SettingsPage.vue"),
      meta: {
        title: "系统设置",
        section: "settings",
        description: "管理员、数据保留与后台维护",
      },
    },
    {
      path: "/:pathMatch(.*)*",
      component: () => import("./pages/NotFound.vue"),
      meta: { title: "页面不存在" },
    },
  ],
});
router.beforeEach(async (to, from) => {
  if (session.ready && !session.authenticated && to.path !== "/login")
    return { path: "/login", query: { next: to.fullPath } };
  const changesEditor =
    to.path !== from.path ||
    (from.path === "/tasks" && to.query.view !== from.query.view) ||
    [
      "scope",
      "persona",
      "template",
      "override",
      "sticker",
      "memory",
      "task",
      "call",
      "trace",
    ].some((key) => to.query[key] !== from.query[key]);
  if (
    session.authenticated &&
    from.path !== "/login" &&
    changesEditor &&
    !(await allowLeave())
  )
    return false;
});
router.afterEach((to) => {
  document.title =
    String(to.meta.subtitle || to.meta.title || "工作台") + " · 机器人";
});
