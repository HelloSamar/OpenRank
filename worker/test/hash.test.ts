import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { safeEqual, userKey } from "../src/hash";

const { vectors } = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/user_key_vectors.json", import.meta.url), "utf8"),
) as { vectors: { salt: string; user_id: number; key: string }[] };

describe("userKey", () => {
  it("gives exactly the keys the Python bot gives", async () => {
    expect(vectors.length).toBeGreaterThanOrEqual(12);
    for (const v of vectors) expect(await userKey(v.salt, v.user_id)).toBe(v.key);
  });

  it("never contains the user id and has a fixed shape", async () => {
    const key = await userKey("salt", 8630381516);
    expect(key).toMatch(/^u[0-9a-f]{16}$/);
    expect(key).not.toContain("8630381516");
  });

  it("changes with the secret and with the user", async () => {
    const keys = new Set([await userKey("a", 1), await userKey("b", 1), await userKey("a", 2)]);
    expect(keys.size).toBe(3);
  });

  it("refuses to run without a secret", async () => {
    await expect(userKey("", 1)).rejects.toThrow("SCORES_SALT");
  });
});

describe("safeEqual", () => {
  it("compares secrets", () => {
    expect(safeEqual("secret", "secret")).toBe(true);
    expect(safeEqual("secret", "secreT")).toBe(false);
    expect(safeEqual("secret", "secret!")).toBe(false);
    expect(safeEqual("", "secret")).toBe(false);
    expect(safeEqual("", "")).toBe(true);
    expect(safeEqual("h\u00e9llo", "h\u00e9llo")).toBe(true);
  });
});
