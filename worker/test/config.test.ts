import { describe, expect, it } from "vitest";
import { DEFAULTS, loadConfig } from "../src/env";

describe("loadConfig", () => {
  it("uses the product defaults", () => {
    expect(loadConfig({})).toEqual({
      freeDailyQuestions: 100, paidDailyQuestions: 2000, graceDays: 3, dailyBudgetEvents: 80_000,
      paused: false, adminIds: new Set(),
    });
    expect(DEFAULTS.freeDailyQuestions).toBe(100);
  });

  it("reads settings and ignores nonsense", () => {
    expect(loadConfig({ FREE_DAILY_QUESTIONS: "50" }).freeDailyQuestions).toBe(50);
    for (const bad of ["", "abc", "0", "-3", "1.5x", "5O", "50 questions", "1e3", "12345678901"]) {
      expect(loadConfig({ FREE_DAILY_QUESTIONS: bad }).freeDailyQuestions, bad).toBe(100);
    }
    expect(loadConfig({ GRACE_DAYS: "0" }).graceDays).toBe(0);
    expect(loadConfig({ GRACE_DAYS: "-1" }).graceDays).toBe(3);
    expect(loadConfig({ GRACE_DAYS: "2days" }).graceDays).toBe(3);
    expect(loadConfig({ GRACE_DAYS: " 5 " }).graceDays).toBe(5);
  });

  it("understands the emergency switch", () => {
    for (const on of ["1", "true", "TRUE", " yes ", "on"]) expect(loadConfig({ PAUSED: on }).paused, on).toBe(true);
    for (const off of ["", "0", "false", "no", undefined]) expect(loadConfig({ PAUSED: off }).paused, String(off)).toBe(false);
  });

  it("reads the owner ids", () => {
    expect([...loadConfig({ ADMIN_IDS: "8630381516, 42 ,x,-5,0,,12abc,1.5" }).adminIds]).toEqual([8630381516, 42]);
  });
});
