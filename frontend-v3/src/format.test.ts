import { describe, expect, it } from "vitest";

import { formatIssueNo } from "./format";

describe("formatIssueNo", () => {
  it("将前缀、序号和后缀按稳定顺序拼接", () => {
    expect(formatIssueNo(12, { prefix: "审-", suffix: "-A" })).toBe("审-12-A");
  });

  it("允许空规则，保持既有纯序号显示", () => {
    expect(formatIssueNo("007", { prefix: "", suffix: "" })).toBe("007");
  });
});
