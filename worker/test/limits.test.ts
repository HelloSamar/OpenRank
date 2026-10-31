import { beforeEach, describe, expect, it } from "vitest";
import { bumpBudget, reserveQuestions, setPlan, upsertChat } from "../src/db";
import { checkLimits, dailyCapFor, resolvePlan } from "../src/limits";
import { istDay, utcDay } from "../src/time";
import { createDb } from "./helpers/d1";
import { config, NOW } from "./helpers/fakes";

const DAY = 86_400;
const GROUP = -1001234567890;
const ME = 8630381516;

let ctx: ReturnType<typeof createDb>;
beforeEach(() => {
  ctx = createDb();
});

async function group(type: "group" | "supergroup" | "channel" = "supergroup") {
  await upsertChat(ctx.db, { chatId: GROUP, type }, NOW);
}
async function privateChat() {
  await upsertChat(ctx.db, { chatId: ME, type: "private" }, NOW);
}
const ask = (chatId: number, req: Parameters<typeof checkLimits>[3], now = NOW, cfg = config()) =>
  checkLimits(ctx.db, cfg, chatId, req, now);

describe("who may ask", () => {
  it("refuses a chat that never said /start", async () => {
    expect(await ask(GROUP, { questions: 10 })).toMatchObject({ allowed: false, reason: "unknown_chat" });
  });

  it("refuses a chat the bot was removed from", async () => {
    await group();
    ctx.raw.prepare("UPDATE chats SET active = 0").run();
    expect(await ask(GROUP, { questions: 10 })).toMatchObject({ allowed: false, reason: "chat_inactive" });
  });

  it("stops everything when the emergency switch is on, before any other check", async () => {
    const paused = config({ PAUSED: "true" });
    expect(await ask(123, { questions: 1 }, NOW, paused)).toMatchObject({ allowed: false, reason: "paused" });
    await group();
    expect(await ask(GROUP, {}, NOW, paused)).toMatchObject({ allowed: false, reason: "paused" });
  });
});

describe("the free plan: 100 questions a day", () => {
  beforeEach(async () => {
    await group();
  });

  it("allows a quiz that fits", async () => {
    expect(await ask(GROUP, { questions: 100 })).toMatchObject({
      allowed: true, plan: "free", dailyCap: 100, usedToday: 0, remainingToday: 100,
    });
  });

  it("refuses 101 questions and says how many are left", async () => {
    const decision = await ask(GROUP, { questions: 101 });
    expect(decision).toMatchObject({ allowed: false, reason: "daily_cap", remainingToday: 100 });
    expect(decision.message).toContain("100 questions left");
  });

  it("counts what was already used today", async () => {
    await reserveQuestions(ctx.db, GROUP, istDay(NOW), 70, 100);
    expect(await ask(GROUP, { questions: 30 })).toMatchObject({ allowed: true, usedToday: 70, remainingToday: 30 });
    const over = await ask(GROUP, { questions: 31 });
    expect(over).toMatchObject({ allowed: false, reason: "daily_cap", remainingToday: 30 });
    expect(over.message).toContain("Only 30 questions left");
  });

  it("explains when the day is used up", async () => {
    await reserveQuestions(ctx.db, GROUP, istDay(NOW), 100, 100);
    const decision = await ask(GROUP, { questions: 5 });
    expect(decision).toMatchObject({ allowed: false, reason: "daily_cap", remainingToday: 0 });
    expect(decision.message).toContain("resets at midnight IST");
  });

  it("says '1 question' in the singular", async () => {
    await reserveQuestions(ctx.db, GROUP, istDay(NOW), 99, 100);
    expect((await ask(GROUP, { questions: 2 })).message).toContain("Only 1 question left");
  });

  it("starts a new day at midnight India time, not midnight UTC", async () => {
    const beforeMidnight = Date.UTC(2026, 9, 26, 18, 29, 59) / 1000; // 23:59:59 IST
    await reserveQuestions(ctx.db, GROUP, istDay(beforeMidnight), 100, 100);
    expect(await ask(GROUP, { questions: 1 }, beforeMidnight)).toMatchObject({ allowed: false, reason: "daily_cap" });
    expect(await ask(GROUP, { questions: 100 }, beforeMidnight + 1)).toMatchObject({ allowed: true, remainingToday: 100 });
  });

  it("does not check the count when only the status is wanted", async () => {
    await reserveQuestions(ctx.db, GROUP, istDay(NOW), 100, 100);
    expect(await ask(GROUP, {})).toMatchObject({ allowed: true, remainingToday: 0 });
  });

  it("follows the configured free cap", async () => {
    const small = config({ FREE_DAILY_QUESTIONS: "20" });
    expect(await ask(GROUP, { questions: 21 }, NOW, small)).toMatchObject({ allowed: false, dailyCap: 20 });
    expect(await ask(GROUP, { questions: 20 }, NOW, small)).toMatchObject({ allowed: true });
  });
});

describe("paid plans", () => {
  const expires = NOW + 10 * DAY;

  it("gives a group the paid cap and no 100-a-day limit", async () => {
    await group();
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: expires }, NOW);
    expect(await ask(GROUP, { questions: 500 })).toMatchObject({
      allowed: true, plan: "group", dailyCap: 2000, expiresAt: expires, inGrace: false, lapsed: false,
    });
    expect(await ask(GROUP, { questions: 2001 })).toMatchObject({ allowed: false, reason: "daily_cap" });
  });

  it("gives a person the individual plan in a private chat", async () => {
    await privateChat();
    await setPlan(ctx.db, { chatId: ME, plan: "individual", expiresAt: expires }, NOW);
    expect(await ask(ME, { questions: 300 })).toMatchObject({ allowed: true, plan: "individual" });
  });

  it("ignores a plan meant for the other kind of chat", async () => {
    await group();
    await privateChat();
    await setPlan(ctx.db, { chatId: GROUP, plan: "individual", expiresAt: expires }, NOW);
    await setPlan(ctx.db, { chatId: ME, plan: "group", expiresAt: expires }, NOW);
    expect((await ask(GROUP, { questions: 1 })).plan).toBe("free");
    expect((await ask(ME, { questions: 1 })).plan).toBe("free");
  });

  it("works up to the last second, then for the grace days, then the chat is free", async () => {
    await group();
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: expires }, NOW);
    expect(await ask(GROUP, {}, expires)).toMatchObject({ plan: "group", inGrace: false });
    expect(await ask(GROUP, {}, expires + 1)).toMatchObject({ plan: "group", inGrace: true, lapsed: false });
    expect(await ask(GROUP, {}, expires + 3 * DAY)).toMatchObject({ plan: "group", inGrace: true });
    expect(await ask(GROUP, {}, expires + 3 * DAY + 1)).toMatchObject({ plan: "free", inGrace: false, lapsed: true, expiresAt: expires });
  });

  it("has no grace when the grace period is set to zero", async () => {
    await group();
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: expires }, NOW);
    const none = config({ GRACE_DAYS: "0" });
    expect(await ask(GROUP, {}, expires + 1, none)).toMatchObject({ plan: "free", lapsed: true });
  });

  it("goes back to 100 a day after the plan lapses", async () => {
    await group();
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: expires }, NOW);
    const later = expires + 4 * DAY;
    expect(await ask(GROUP, { questions: 101 }, later)).toMatchObject({ allowed: false, plan: "free", reason: "daily_cap" });
  });

  it("a renewed plan works again", async () => {
    await group();
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: expires }, NOW);
    const later = expires + 10 * DAY;
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: later + 30 * DAY }, later);
    expect(await ask(GROUP, { questions: 500 }, later)).toMatchObject({ allowed: true, plan: "group", lapsed: false });
  });
});

describe("quiz access", () => {
  beforeEach(async () => {
    await group();
  });

  it("keeps paid quizzes away from free chats", async () => {
    expect(await ask(GROUP, { questions: 10, quizIsFree: false })).toMatchObject({ allowed: false, reason: "paid_quiz" });
    expect(await ask(GROUP, { questions: 10, quizIsFree: true })).toMatchObject({ allowed: true });
    expect(await ask(GROUP, { questions: 10 })).toMatchObject({ allowed: true }); // a quiz is free unless marked
  });

  it("lets paid chats, even in their grace days, run paid quizzes", async () => {
    await setPlan(ctx.db, { chatId: GROUP, plan: "group", expiresAt: NOW - 1 }, NOW);
    expect(await ask(GROUP, { questions: 10, quizIsFree: false })).toMatchObject({ allowed: true, inGrace: true });
  });
});

describe("one quiz at a time and the request budget", () => {
  beforeEach(async () => {
    await group();
  });

  it("refuses a second quiz while one runs", async () => {
    ctx.raw.prepare("INSERT INTO runs (run_id, chat_id, quiz_id, total, gap_seconds, state, created_at, updated_at) VALUES ('r', ?, 'q', 3, 10, 'running', 1, 1)").run(GROUP);
    expect(await ask(GROUP, { questions: 5 })).toMatchObject({ allowed: false, reason: "quiz_running" });
    expect(await ask(GROUP, {})).toMatchObject({ allowed: true }); // /status still works
  });

  it("refuses new quizzes when the day's request budget is used up", async () => {
    await bumpBudget(ctx.db, utcDay(NOW), 79_999);
    expect(await ask(GROUP, { questions: 5 })).toMatchObject({ allowed: true });
    await bumpBudget(ctx.db, utcDay(NOW), 1);
    expect(await ask(GROUP, { questions: 5 })).toMatchObject({ allowed: false, reason: "busy" });
    expect(await ask(GROUP, {})).toMatchObject({ allowed: true }); // status is never blocked
  });

  it("counts the budget per UTC day, so it starts again at 05:30 IST", async () => {
    await bumpBudget(ctx.db, "2026-10-26", 80_000);
    const nextDay = Date.UTC(2026, 9, 27, 0, 0, 0) / 1000;
    expect(await ask(GROUP, { questions: 5 }, nextDay)).toMatchObject({ allowed: true });
  });

  it("checks in a fixed order: busy, then running, then access, then the daily cap", async () => {
    await bumpBudget(ctx.db, utcDay(NOW), 80_000);
    ctx.raw.prepare("INSERT INTO runs (run_id, chat_id, quiz_id, total, gap_seconds, state, created_at, updated_at) VALUES ('r', ?, 'q', 3, 10, 'running', 1, 1)").run(GROUP);
    expect((await ask(GROUP, { questions: 500, quizIsFree: false })).reason).toBe("busy");
    ctx.raw.prepare("DELETE FROM budget_daily").run();
    expect((await ask(GROUP, { questions: 500, quizIsFree: false })).reason).toBe("quiz_running");
    ctx.raw.prepare("UPDATE runs SET state = 'done'").run();
    expect((await ask(GROUP, { questions: 500, quizIsFree: false })).reason).toBe("paid_quiz");
    expect((await ask(GROUP, { questions: 500 })).reason).toBe("daily_cap");
  });
});

describe("helpers", () => {
  it("knows the cap of each plan", () => {
    const cfg = config();
    expect([dailyCapFor("free", cfg), dailyCapFor("group", cfg), dailyCapFor("individual", cfg)]).toEqual([100, 2000, 2000]);
  });

  it("resolves a free chat without a plan row", async () => {
    expect(await resolvePlan(ctx.db, 1, "private", NOW, config())).toEqual({ plan: "free", expiresAt: null, inGrace: false, lapsed: false });
  });
});
