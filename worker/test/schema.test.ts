import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { createDb, SCHEMA_PATH } from "./helpers/d1";

const tables = (raw: ReturnType<typeof createDb>["raw"]) =>
  (raw.prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").all() as { name: string }[]).map((r) => r.name);

describe("schema.sql", () => {
  it("is identical to the migration that wrangler applies", () => {
    const migration = new URL("../migrations/0001_init.sql", import.meta.url);
    expect(readFileSync(migration, "utf8")).toBe(readFileSync(SCHEMA_PATH, "utf8"));
  });

  it("creates the tables of the data model", () => {
    expect(tables(createDb().raw)).toEqual(["budget_daily", "chats", "plans", "runs", "scores", "usage_daily"]);
  });

  it("can be applied twice (migrations may be re-run)", () => {
    const { raw } = createDb();
    expect(() => raw.exec(readFileSync(SCHEMA_PATH, "utf8"))).not.toThrow();
  });

  it("stores no raw Telegram user id column anywhere", () => {
    const { raw } = createDb();
    for (const table of tables(raw)) {
      const columns = (raw.prepare(`PRAGMA table_info(${table})`).all() as { name: string }[]).map((c) => c.name);
      expect(columns, table).not.toContain("user_id");
    }
  });
});

describe("constraints", () => {
  const chat = (raw: ReturnType<typeof createDb>["raw"], id = -100, type = "supergroup") =>
    raw.prepare("INSERT INTO chats (chat_id, chat_type, created_at, updated_at) VALUES (?, ?, 1, 1)").run(id, type);
  const run = (raw: ReturnType<typeof createDb>["raw"], id: string, chatId: number, state: string, gap = 10) =>
    raw
      .prepare("INSERT INTO runs (run_id, chat_id, quiz_id, total, gap_seconds, state, created_at, updated_at) VALUES (?, ?, 'q', 5, ?, ?, 1, 1)")
      .run(id, chatId, gap, state);

  it("accepts the four chat types and refuses others", () => {
    const { raw } = createDb();
    for (const [i, type] of ["private", "group", "supergroup", "channel"].entries()) chat(raw, -i - 1, type);
    expect(() => chat(raw, -50, "bot")).toThrow();
  });

  it("allows only one running or paused quiz per chat", () => {
    const { raw } = createDb();
    chat(raw);
    run(raw, "a", -100, "running");
    expect(() => run(raw, "b", -100, "running")).toThrow();
    expect(() => run(raw, "c", -100, "paused")).toThrow();
    raw.prepare("UPDATE runs SET state = 'done' WHERE run_id = 'a'").run();
    expect(() => run(raw, "d", -100, "running")).not.toThrow();
    expect(() => run(raw, "e", -100, "done")).not.toThrow(); // finished runs can pile up
  });

  it("lets two different chats run at once", () => {
    const { raw } = createDb();
    chat(raw, -1);
    chat(raw, -2);
    run(raw, "a", -1, "running");
    expect(() => run(raw, "b", -2, "running")).not.toThrow();
  });

  it("keeps the gap between questions within Telegram's 5 to 600 seconds", () => {
    const { raw } = createDb();
    chat(raw);
    expect(() => run(raw, "a", -100, "done", 4)).toThrow();
    expect(() => run(raw, "b", -100, "done", 601)).toThrow();
    expect(() => run(raw, "c", -100, "done", 5)).not.toThrow();
    expect(() => run(raw, "d", -100, "done", 600)).not.toThrow();
  });

  it("refuses a plan for a chat that does not exist", () => {
    const { raw } = createDb();
    expect(() => raw.prepare("INSERT INTO plans (chat_id, plan, expires_at, updated_at) VALUES (-9, 'group', 1, 1)").run()).toThrow();
  });

  it("refuses negative usage and unknown plan names", () => {
    const { raw } = createDb();
    chat(raw);
    expect(() => raw.prepare("INSERT INTO usage_daily (chat_id, day, questions) VALUES (-100, '2026-10-26', -1)").run()).toThrow();
    expect(() => raw.prepare("INSERT INTO plans (chat_id, plan, expires_at, updated_at) VALUES (-100, 'gold', 1, 1)").run()).toThrow();
  });
});
