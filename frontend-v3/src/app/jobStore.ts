import { defineStore } from "pinia";

import type { ScanStatus } from "../api/client";

/** 后端异步任务的界面进度投影；任务结果不作为项目数据持久化。 */
export const useJobStore = defineStore("audit-jobs", {
  state: () => ({ scan: null as ScanStatus | null }),
  actions: {
    clear(): void {
      this.scan = null;
    },
  },
});
