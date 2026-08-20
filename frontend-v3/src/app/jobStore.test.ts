import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useJobStore } from "./jobStore";

describe("job shell store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("holds transient scan progress and clears it across project changes", () => {
    const store = useJobStore();
    store.scan = { scan_id: "scan-1", status: "running", phase: "hash", done: 1, total: 2, problems: [], counts: {}, sample: { checked: 0, total: 0 }, error: "" };
    store.clear();
    expect(store.scan).toBeNull();
  });
});
