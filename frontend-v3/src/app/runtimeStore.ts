import { defineStore } from "pinia";

export type AppScreen = "home" | "workspace";

/**
 * 仅保存壳层状态。审计数据仍以本地后端和数据库为唯一事实来源，
 * 防止 Pinia/浏览器缓存成为未留痕的第二份底稿。
 */
export const useRuntimeStore = defineStore("audit-runtime", {
  state: () => ({ screen: "home" as AppScreen }),
  actions: {
    setScreen(screen: AppScreen): void {
      this.screen = screen;
    },
  },
});
