import { beforeEach, describe, expect, it, vi } from "vitest";

import { HttpClient } from "./http";

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

describe("local HTTP transport", () => {
  const storage = new MemoryStorage();
  const dispatchEvent = vi.fn();

  beforeEach(() => {
    storage.removeItem("audit_token");
    storage.removeItem("audit_operator");
    dispatchEvent.mockReset();
    vi.stubGlobal("sessionStorage", storage);
    vi.stubGlobal("window", { dispatchEvent, setTimeout: vi.fn() });
    vi.stubGlobal("CustomEvent", class { constructor(public readonly type: string) {} });
  });

  it("adds the session header once and serializes JSON requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const http = new HttpClient();
    http.setSession("session-1", "审计员");

    await expect(http.request<{ ok: boolean }>("/api/example", {
      method: "POST", body: JSON.stringify({ value: 1 }),
    })).resolves.toEqual({ ok: true });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("X-Session")).toBe("session-1");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("clears the local session and signals the shell on an expired session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "使用人会话无效，请重新进入" }), { status: 401 },
    )));
    const http = new HttpClient();
    http.setSession("session-1", "审计员");

    await expect(http.request("/api/example")).rejects.toThrow("使用人会话无效");
    expect(storage.getItem("audit_token")).toBeNull();
    expect(dispatchEvent).toHaveBeenCalledOnce();
  });
});
