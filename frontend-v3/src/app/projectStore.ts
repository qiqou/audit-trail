import { defineStore } from "pinia";

import type { ProjectInfo, Unit } from "../api/client";

/**
 * 当前项目的界面投影。此 store 不持久化；重新打开项目后始终以 API 返回结果覆盖，
 * 因而 SQLite 才是底稿、附件和项目配置的唯一事实来源。
 */
export const useProjectStore = defineStore("audit-project", {
  state: () => ({
    project: null as ProjectInfo | null,
    units: [] as Unit[],
    departments: [] as string[],
    categories: [] as string[],
    issueNumberRule: { prefix: "", suffix: "" },
  }),
  actions: {
    clear(): void {
      this.project = null;
      this.units = [];
      this.departments = [];
      this.categories = [];
      this.issueNumberRule = { prefix: "", suffix: "" };
    },
  },
});
