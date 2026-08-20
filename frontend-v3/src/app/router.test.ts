import { describe, expect, it } from "vitest";

import { router } from "./router";

describe("application routes", () => {
  it("maps the two supported shell states", () => {
    expect(router.resolve("/").name).toBe("home");
    expect(router.resolve("/workspace").name).toBe("workspace");
  });

  it("redirects unknown routes to the project entry", () => {
    const resolved = router.resolve("/unknown");
    expect(resolved.matched.at(-1)?.redirect).toEqual({ name: "home" });
  });
});
