import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useProjectStore } from "./projectStore";

describe("project shell store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("clears only its in-memory API projection", () => {
    const store = useProjectStore();
    store.project = { path: "/tmp/audit.auditproj", project_name: "测试项目", units: [] };
    store.units = [{ id: 1, name: "甲单位", sort_order: 0, created_at: "" }];
    store.departments = ["销售"];
    store.clear();
    expect(store.$state).toEqual({
      project: null, units: [], departments: [], categories: [], issueNumberRule: { prefix: "", suffix: "" },
    });
  });
});
