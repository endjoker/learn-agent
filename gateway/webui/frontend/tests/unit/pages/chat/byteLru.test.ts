import { describe, expect, it } from "vitest";

import { ByteLruCache, estimateUtf8Bytes } from "@/pages/chat/byteLru";

describe("ByteLruCache", () => {
  it("uses UTF-8 byte estimates and evicts least-recently-used entries", () => {
    const cache = new ByteLruCache<string>(10);
    cache.set("a", "1234");
    cache.set("b", "5678");
    expect(cache.get("a")).toBe("1234");
    cache.set("c", "xyz");
    expect(cache.get("b")).toBeUndefined();
    expect(cache.get("a")).toBe("1234");
    expect(cache.totalBytes).toBeLessThanOrEqual(10);
  });

  it("does not retain a single value larger than the budget", () => {
    const cache = new ByteLruCache<string>(4);
    cache.set("huge", "12345");
    expect(cache.size).toBe(0);
  });

  it("invalidates by key prefix and clears session-owned data", () => {
    const cache = new ByteLruCache<string>(100);
    cache.set("s1:a", "one");
    cache.set("s2:a", "two");
    cache.deletePrefix("s1:");
    expect(cache.get("s1:a")).toBeUndefined();
    expect(cache.get("s2:a")).toBe("two");
    cache.clear();
    expect(cache.totalBytes).toBe(0);
  });

  it("counts multi-byte text as UTF-8", () => {
    expect(estimateUtf8Bytes("中")).toBe(3);
  });


  it("never exceeds the default 64 MiB budget", () => {
    const cache = new ByteLruCache<string>();
    const oneMiB = "x".repeat(1024 * 1024);
    for (let index = 0; index < 80; index += 1) cache.set(`entry-${index}`, oneMiB);
    expect(cache.totalBytes).toBeLessThanOrEqual(64 * 1024 * 1024);
    expect(cache.size).toBe(64);
    expect(cache.get("entry-0")).toBeUndefined();
    expect(cache.get("entry-79")).toBe(oneMiB);
  });
});
