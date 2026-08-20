import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useRuntimeStore } from "./runtimeStore";

describe("runtime shell store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("does not persist project data and records only the active shell", () => {
    const store = useRuntimeStore();
    expect(store.screen).toBe("home");
    store.setScreen("workspace");
    expect(store.$state).toEqual({ screen: "workspace" });
  });
});
