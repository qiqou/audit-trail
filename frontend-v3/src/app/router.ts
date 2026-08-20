import { createMemoryHistory, createRouter, createWebHashHistory, type RouteRecordRaw } from "vue-router";

const RouteMarker = { name: "RouteMarker", render: () => null };

// App.vue 仍是既有工作台壳层。路由在第一阶段仅管理可恢复的入口层级，
// 不把正在编辑的底稿、附件或会话放入 URL/浏览器持久化存储。
export const appRoutes: RouteRecordRaw[] = [
  { path: "/", name: "home", component: RouteMarker },
  { path: "/workspace", name: "workspace", component: RouteMarker },
  { path: "/:pathMatch(.*)*", redirect: { name: "home" } },
];

export const router = createRouter({
  // 自动测试和未来桌面壳的 SSR 预检没有 location；实际浏览器仍固定使用 hash 路由。
  history: typeof window === "undefined" ? createMemoryHistory() : createWebHashHistory(),
  routes: appRoutes,
});
