import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useSessionStore } from "./sessionStore";

class MemoryStorage {
  private readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

describe("session store", () => {
  beforeEach(() => {
    vi.stubGlobal("sessionStorage", new MemoryStorage());
    setActivePinia(createPinia());
  });

  it("keeps only the operator label, not a project or formal workpaper", () => {
    const store = useSessionStore();
    store.setOperator("审计员");
    expect(store.$state).toEqual({ operator: "审计员" });
    store.clearOperator();
    expect(store.operator).toBe("");
  });
});
