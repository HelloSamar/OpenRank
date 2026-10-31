import { describe, expect, it } from "vitest";
import {
  addScore, bumpBudget, getBudget, getChat, getPlan, getUsage, hasActiveRun, leaderboard,
  reserveQuestions, setChatActive, setPlan, upsertChat,
} from "../src/db";
import { createDb } from "./helpers/d1";

describe("chats", () => {
  it("registers a chat, then keeps the title and who added the bot when it is registered again", async () => {
    const { db } = createDb();
    await upsertChat(db, { chatId: -100, type: "supergroup", title: "Quiz Club", addedBy: "uAAA" }, 10);
    await upsertChat(db, { chatId: -100, type: "supergroup", title: null, addedBy: "uBBB" }, 20);
    const row = await getChat(db, -100);
    expect(row).toMatchObject({ title: "Quiz Club", added_by: "uAAA", active: 1, created_at: 10, updated_at: 20 });
  });

  it("takes a new title when there is one", async () => {
    const { db } = createDb();
    await upsertChat(db, { chatId: -1, type: "group", title: "Old" }, 1);
    await upsertChat(db, { chatId: -1, type: "group", title: "New" }, 2);
    expect((await getChat(db, -1))?.title).toBe("New");
  });

  it("marks a chat as left and present again", async () => {
    const { db } = createDb();
    await upsertChat(db, { chatId: 5, type: "private" }, 1);
    expect(await setChatActive(db, 5, false, 2)).toBe(true);
    expect((await getChat(db, 5))?.active).toBe(0);
    await upsertChat(db, { chatId: 5, type: "private" }, 3);
    expect((await getChat(db, 5))?.active).toBe(1);
  });

  it("does not invent a chat that is unknown", async () => {
    const { db } = createDb();
    expect(await setChatActive(db, 77, false, 1)).toBe(false);
    expect(await getChat(db, 77)).toBeNull();
  });
});

describe("plans", () => {
  it("grants a plan and resets the reminders when it is extended", async () => {
    const { db, raw } = createDb();
    await upsertChat(db, { chatId: -1, type: "group" }, 1);
    await setPlan(db, { chatId: -1, plan: "group", expiresAt: 1000, grantedBy: "uADM", note: "UPI ref 1" }, 5);
    raw.prepare("UPDATE plans SET reminded_7d = 1, reminded_1d = 1").run();
    await setPlan(db, { chatId: -1, plan: "group", expiresAt: 2000, grantedBy: "uADM", note: "UPI ref 2" }, 6);
    expect(await getPlan(db, -1)).toMatchObject({ expires_at: 2000, note: "UPI ref 2", reminded_7d: 0, reminded_1d: 0 });
  });

  it("has no plan for a free chat", async () => {
    const { db } = createDb();
    expect(await getPlan(db, -1)).toBeNull();
  });
});

describe("daily question reservation", () => {
  it("reserves while the cap allows and then refuses", async () => {
    const { db } = createDb();
    expect(await reserveQuestions(db, 1, "2026-10-26", 60, 100)).toBe(true);
    expect(await reserveQuestions(db, 1, "2026-10-26", 40, 100)).toBe(true);
    expect(await getUsage(db, 1, "2026-10-26")).toBe(100);
    expect(await reserveQuestions(db, 1, "2026-10-26", 1, 100)).toBe(false);
    expect(await getUsage(db, 1, "2026-10-26")).toBe(100);
  });

  it("refuses a reservation that would cross the cap and changes nothing", async () => {
    const { db } = createDb();
    await reserveQuestions(db, 1, "d", 70, 100);
    expect(await reserveQuestions(db, 1, "d", 31, 100)).toBe(false);
    expect(await getUsage(db, 1, "d")).toBe(70);
  });

  it("counts every chat and every day separately", async () => {
    const { db } = createDb();
    await reserveQuestions(db, 1, "2026-10-26", 100, 100);
    expect(await reserveQuestions(db, 2, "2026-10-26", 100, 100)).toBe(true);
    expect(await reserveQuestions(db, 1, "2026-10-27", 100, 100)).toBe(true);
  });

  it("refuses nonsense counts", async () => {
    const { db } = createDb();
    for (const bad of [0, -5, 1.5, Number.NaN, 101]) expect(await reserveQuestions(db, 1, "d", bad, 100)).toBe(false);
    expect(await getUsage(db, 1, "d")).toBe(0);
  });

  it("lets only as many simultaneous quizzes through as the cap allows", async () => {
    const { db } = createDb();
    const results = await Promise.all(Array.from({ length: 5 }, () => reserveQuestions(db, 1, "d", 30, 100)));
    expect(results.filter(Boolean)).toHaveLength(3);
    expect(await getUsage(db, 1, "d")).toBe(90);
  });
});

describe("runs and budget", () => {
  it("sees a running quiz but not a finished one", async () => {
    const { db, raw } = createDb();
    await upsertChat(db, { chatId: -1, type: "group" }, 1);
    expect(await hasActiveRun(db, -1)).toBe(false);
    raw.prepare("INSERT INTO runs (run_id, chat_id, quiz_id, total, gap_seconds, state, created_at, updated_at) VALUES ('r', -1, 'q', 3, 10, 'running', 1, 1)").run();
    expect(await hasActiveRun(db, -1)).toBe(true);
    raw.prepare("UPDATE runs SET state = 'done'").run();
    expect(await hasActiveRun(db, -1)).toBe(false);
  });

  it("adds up the day's request count", async () => {
    const { db } = createDb();
    expect(await getBudget(db, "2026-10-26")).toBe(0);
    await bumpBudget(db, "2026-10-26", 25);
    await bumpBudget(db, "2026-10-26", 10);
    await bumpBudget(db, "2026-10-27", 1);
    expect(await getBudget(db, "2026-10-26")).toBe(35);
    expect(await getBudget(db, "2026-10-27")).toBe(1);
  });
});

describe("scores", () => {
  const delta = { chatId: -1, week: "2026-10-25", points: 0, correct: 0, wrong: 0, skipped: 0, seconds: 0, quizzes: 0 };

  it("adds scores up per chat, person and week", async () => {
    const { db } = createDb();
    await addScore(db, { ...delta, userHash: "uA", name: "Asha", points: 4, correct: 2, seconds: 10, quizzes: 1 });
    await addScore(db, { ...delta, userHash: "uA", name: null, points: 1.5, correct: 1, wrong: 1, seconds: 5, quizzes: 1 });
    const [row] = await leaderboard(db, -1, "2026-10-25");
    expect(row).toMatchObject({ user_hash: "uA", name: "Asha", points: 5.5, correct: 3, wrong: 1, seconds: 15, quizzes: 2 });
  });

  it("ranks by points, then by less time", async () => {
    const { db } = createDb();
    await addScore(db, { ...delta, userHash: "uSlow", points: 5, seconds: 90 });
    await addScore(db, { ...delta, userHash: "uFast", points: 5, seconds: 40 });
    await addScore(db, { ...delta, userHash: "uTop", points: 9, seconds: 300 });
    const names = (await leaderboard(db, -1, "2026-10-25")).map((r) => r.user_hash);
    expect(names).toEqual(["uTop", "uFast", "uSlow"]);
  });

  it("keeps chats and weeks apart and respects the limit", async () => {
    const { db } = createDb();
    await addScore(db, { ...delta, userHash: "uA", points: 3 });
    await addScore(db, { ...delta, chatId: -2, userHash: "uA", points: 8 });
    await addScore(db, { ...delta, week: "2026-11-01", userHash: "uA", points: 7 });
    for (const h of ["uB", "uC", "uD"]) await addScore(db, { ...delta, userHash: h, points: 1 });
    expect((await leaderboard(db, -1, "2026-10-25", 2))).toHaveLength(2);
    expect((await leaderboard(db, -2, "2026-10-25"))[0]?.points).toBe(8);
    expect((await leaderboard(db, -1, "2026-11-01"))[0]?.points).toBe(7);
  });
});
