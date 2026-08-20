import { describe, expect, it } from "vitest";

import { createProjectApi, type ApiRequest } from "./projects";

function createRequestSpy(): { request: ApiRequest; calls: Array<{ path: string; init?: RequestInit }> } {
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const request: ApiRequest = (path, init) => {
    calls.push({ path, init });
    return Promise.resolve({} as never);
  };
  return { request, calls };
}

describe("project API domain", () => {
  it("uses the project lifecycle endpoints with their request bodies", async () => {
    const { request, calls } = createRequestSpy();
    const projects = createProjectApi(request);

    await projects.openProject("/audit/demo");
    await projects.createProject("/audit", "2026 年报审计");
    await projects.deleteProject("/audit/demo");

    expect(calls).toEqual([
      { path: "/api/project/open", init: { method: "POST", body: JSON.stringify({ path: "/audit/demo" }) } },
      { path: "/api/project/create", init: { method: "POST", body: JSON.stringify({ path: "/audit", name: "2026 年报审计" }) } },
      { path: "/api/project/delete", init: { method: "POST", body: JSON.stringify({ path: "/audit/demo" }) } },
    ]);
  });

  it("keeps recent-project deletion path-safe and exposes project checks", async () => {
    const { request, calls } = createRequestSpy();
    const projects = createProjectApi(request);

    await projects.forgetRecent("/audit/甲 & 乙");
    await projects.health();
    await projects.summary();

    expect(calls).toEqual([
      { path: "/api/recent?path=%2Faudit%2F%E7%94%B2%20%26%20%E4%B9%99", init: { method: "DELETE" } },
      { path: "/api/project/health?sample_size=20", init: undefined },
      { path: "/api/project/summary", init: undefined },
    ]);
  });
});
