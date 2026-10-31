/**
 * Counts webhook requests per day so the bot can refuse new quizzes before a free limit is hit.
 *
 * Writing to the database for every request would use up the free write allowance, so events
 * are counted in memory and written in batches. The count is approximate (each running
 * instance keeps its own pending number); the daily budget has 20% spare to allow for that.
 */
import { bumpBudget } from "./db";
import { utcDay } from "./time";

let pending = 0;
let lastFlush = 0;

export const FLUSH_EVERY_EVENTS = 25;
export const FLUSH_EVERY_SECONDS = 60;

interface Waitable {
  waitUntil(promise: Promise<unknown>): void;
}

export function recordEvent(db: D1Database, nowSeconds: number, ctx?: Waitable): void {
  pending += 1;
  if (pending < FLUSH_EVERY_EVENTS && nowSeconds - lastFlush < FLUSH_EVERY_SECONDS) return;
  const batch = pending;
  pending = 0;
  lastFlush = nowSeconds;
  const write = bumpBudget(db, utcDay(nowSeconds), batch).catch(() => {
    pending += batch; // keep the count; it is written with the next batch
  });
  ctx?.waitUntil(write);
}

export function resetMeterForTests(): void {
  pending = 0;
  lastFlush = 0;
}

export function pendingEventsForTests(): number {
  return pending;
}
