import { beforeEach, describe, expect, it, vi } from "vitest";
import * as database from "../src/db";
import { getBudget } from "../src/db";
import { FLUSH_EVERY_EVENTS, FLUSH_EVERY_SECONDS, pendingEventsForTests, recordEvent, resetMeterForTests } from "../src/meter";
import { utcDay } from "../src/time";
import { createDb } from "./helpers/d1";
import { NOW } from "./helpers/fakes";

beforeEach(() => {
  resetMeterForTests();
  vi.restoreAllMocks();
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("request meter", () => {
  it("writes to the database in batches, not for every request", async () => {
    const { db } = createDb();
    const spy = vi.spyOn(database, "bumpBudget");
    recordEvent(db, NOW); // the very first event is written at once
    for (let i = 0; i < FLUSH_EVERY_EVENTS - 1; i++) recordEvent(db, NOW + 1);
    await settle();
    expect(spy).toHaveBeenCalledTimes(1);                    // 24 more requests: nothing written yet
    expect(pendingEventsForTests()).toBe(FLUSH_EVERY_EVENTS - 1);
    recordEvent(db, NOW + 1);                                // the 25th pending request
    await settle();
    expect(spy).toHaveBeenCalledTimes(2);                    // 26 requests, 2 writes
    expect(pendingEventsForTests()).toBe(0);
    expect(await getBudget(db, utcDay(NOW))).toBe(FLUSH_EVERY_EVENTS + 1);
  });

  it("flushes after a minute even when few requests arrive", async () => {
    const { db } = createDb();
    recordEvent(db, NOW);
    await settle();
    recordEvent(db, NOW + 5);
    recordEvent(db, NOW + 10);
    expect(pendingEventsForTests()).toBe(2);
    recordEvent(db, NOW + FLUSH_EVERY_SECONDS + 1);
    await settle();
    expect(pendingEventsForTests()).toBe(0);
    expect(await getBudget(db, utcDay(NOW))).toBe(4);
  });

  it("hands the write to the runtime so it finishes after the response", async () => {
    const { db } = createDb();
    const waits: Promise<unknown>[] = [];
    recordEvent(db, NOW, { waitUntil: (p) => waits.push(p) });
    expect(waits).toHaveLength(1);
    await Promise.all(waits);
    expect(await getBudget(db, utcDay(NOW))).toBe(1);
  });

  it("does not lose the count when the database write fails", async () => {
    const { db } = createDb();
    vi.spyOn(database, "bumpBudget").mockRejectedValueOnce(new Error("D1 down"));
    recordEvent(db, NOW);
    await settle();
    expect(pendingEventsForTests()).toBe(1); // kept for the next batch
  });
});
