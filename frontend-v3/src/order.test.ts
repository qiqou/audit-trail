import { describe, expect, it } from "vitest";

import { moveIdBefore } from "./order";

describe("moveIdBefore", () => {
  it("把源对象移动到目标对象之前", () => {
    expect(moveIdBefore([1, 2, 3, 4], 4, 2)).toEqual([1, 4, 2, 3]);
  });

  it("同一对象或不在当前范围时不改变排序", () => {
    const original = [1, 2, 3];
    expect(moveIdBefore(original, 2, 2)).toBe(original);
    expect(moveIdBefore(original, 9, 2)).toBe(original);
  });
});
