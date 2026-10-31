import { describe, expect, it } from "vitest";
import { formatIst, istDay, istWeekStart, secondsUntilIstMidnight, utcDay } from "../src/time";

const at = (y: number, m: number, d: number, h = 0, min = 0, s = 0) => Date.UTC(y, m - 1, d, h, min, s) / 1000;

describe("days", () => {
  it("rolls the IST day at 18:30 UTC", () => {
    expect(istDay(at(2026, 10, 26, 18, 29, 59))).toBe("2026-10-26");
    expect(istDay(at(2026, 10, 26, 18, 30, 0))).toBe("2026-10-27");
  });
  it("rolls the UTC day at 00:00 UTC (05:30 IST)", () => {
    expect(utcDay(at(2026, 10, 26, 23, 59, 59))).toBe("2026-10-26");
    expect(utcDay(at(2026, 10, 27, 0, 0, 0))).toBe("2026-10-27");
  });
  it("handles the end of the year", () => {
    expect(istDay(at(2026, 12, 31, 18, 30, 0))).toBe("2027-01-01");
  });
  it("counts the seconds until IST midnight", () => {
    expect(secondsUntilIstMidnight(at(2026, 10, 26, 18, 29, 59))).toBe(1);
    expect(secondsUntilIstMidnight(at(2026, 10, 26, 18, 30, 0))).toBe(86_400);
  });
});

describe("score weeks start on Sunday at 20:00 IST", () => {
  it("is the previous Sunday until 20:00 IST (14:30 UTC) on Sunday", () => {
    expect(istWeekStart(at(2026, 10, 25, 14, 29, 59))).toBe("2026-10-18");
    expect(istWeekStart(at(2026, 10, 25, 14, 30, 0))).toBe("2026-10-25");
  });
  it("stays on that Sunday for the whole week", () => {
    expect(istWeekStart(at(2026, 10, 28, 6, 0, 0))).toBe("2026-10-25");
    expect(istWeekStart(at(2026, 11, 1, 14, 29, 59))).toBe("2026-10-25");
    expect(istWeekStart(at(2026, 11, 1, 14, 30, 0))).toBe("2026-11-01");
  });
  it("matches the Python bot on a Saturday in IST", () => {
    expect(istWeekStart(at(2026, 10, 24, 20, 0, 0))).toBe("2026-10-18");
  });
});

describe("formatIst", () => {
  it("writes a readable India time", () => {
    expect(formatIst(at(2026, 10, 27, 13, 5, 0))).toBe("27 Oct 2026, 18:35 IST");
    expect(formatIst(at(2026, 1, 1, 18, 30, 0))).toBe("2 Jan 2026, 00:00 IST");
  });
});
