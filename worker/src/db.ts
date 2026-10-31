/** Database access. Every statement is parameterised; nothing is built from user text. */
import type { ChatType } from "./telegram";

export type PlanName = "individual" | "group";

export interface ChatRow {
  chat_id: number;
  chat_type: ChatType;
  title: string | null;
  active: number;
  added_by: string | null;
  created_at: number;
  updated_at: number;
}

export interface PlanRow {
  chat_id: number;
  plan: PlanName;
  expires_at: number;
  granted_by: string | null;
  note: string | null;
  reminded_7d: number;
  reminded_1d: number;
  updated_at: number;
}

export async function upsertChat(
  db: D1Database,
  chat: { chatId: number; type: ChatType; title?: string | null; addedBy?: string | null; active?: boolean },
  now: number,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO chats (chat_id, chat_type, title, active, added_by, created_at, updated_at)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6)
       ON CONFLICT (chat_id) DO UPDATE SET
         chat_type = excluded.chat_type,
         title = COALESCE(excluded.title, chats.title),
         active = excluded.active,
         added_by = COALESCE(chats.added_by, excluded.added_by),
         updated_at = excluded.updated_at`,
    )
    .bind(chat.chatId, chat.type, chat.title ?? null, chat.active === false ? 0 : 1, chat.addedBy ?? null, now)
    .run();
}

/** Mark a known chat as left (0) or present (1). Returns false if the chat is unknown. */
export async function setChatActive(db: D1Database, chatId: number, active: boolean, now: number): Promise<boolean> {
  const result = await db
    .prepare("UPDATE chats SET active = ?1, updated_at = ?2 WHERE chat_id = ?3")
    .bind(active ? 1 : 0, now, chatId)
    .run();
  return result.meta.changes > 0;
}

export function getChat(db: D1Database, chatId: number): Promise<ChatRow | null> {
  return db.prepare("SELECT * FROM chats WHERE chat_id = ?1").bind(chatId).first<ChatRow>();
}

export function getPlan(db: D1Database, chatId: number): Promise<PlanRow | null> {
  return db.prepare("SELECT * FROM plans WHERE chat_id = ?1").bind(chatId).first<PlanRow>();
}

/** Grant or extend a paid plan (used by the manual /grant command in Phase 3). */
export async function setPlan(
  db: D1Database,
  plan: { chatId: number; plan: PlanName; expiresAt: number; grantedBy?: string | null; note?: string | null },
  now: number,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO plans (chat_id, plan, expires_at, granted_by, note, updated_at)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6)
       ON CONFLICT (chat_id) DO UPDATE SET
         plan = excluded.plan,
         expires_at = excluded.expires_at,
         granted_by = excluded.granted_by,
         note = excluded.note,
         reminded_7d = 0,
         reminded_1d = 0,
         updated_at = excluded.updated_at`,
    )
    .bind(plan.chatId, plan.plan, plan.expiresAt, plan.grantedBy ?? null, plan.note ?? null, now)
    .run();
}

export async function getUsage(db: D1Database, chatId: number, day: string): Promise<number> {
  const row = await db
    .prepare("SELECT questions FROM usage_daily WHERE chat_id = ?1 AND day = ?2")
    .bind(chatId, day)
    .first<{ questions: number }>();
  return row?.questions ?? 0;
}

/**
 * Reserve `count` questions for today, but only if that keeps the day's total within `cap`.
 * One statement decides, so two quizzes starting at the same moment cannot both squeeze in.
 */
export async function reserveQuestions(
  db: D1Database,
  chatId: number,
  day: string,
  count: number,
  cap: number,
): Promise<boolean> {
  if (!Number.isInteger(count) || count <= 0 || count > cap) return false;
  await db
    .prepare("INSERT OR IGNORE INTO usage_daily (chat_id, day, questions) VALUES (?1, ?2, 0)")
    .bind(chatId, day)
    .run();
  const result = await db
    .prepare(
      `UPDATE usage_daily SET questions = questions + ?3
       WHERE chat_id = ?1 AND day = ?2 AND questions + ?3 <= ?4`,
    )
    .bind(chatId, day, count, cap)
    .run();
  return result.meta.changes > 0;
}

export async function hasActiveRun(db: D1Database, chatId: number): Promise<boolean> {
  const row = await db
    .prepare("SELECT 1 AS found FROM runs WHERE chat_id = ?1 AND state IN ('running', 'paused') LIMIT 1")
    .bind(chatId)
    .first<{ found: number }>();
  return row !== null;
}

export async function getBudget(db: D1Database, day: string): Promise<number> {
  const row = await db.prepare("SELECT events FROM budget_daily WHERE day = ?1").bind(day).first<{ events: number }>();
  return row?.events ?? 0;
}

export async function bumpBudget(db: D1Database, day: string, events: number): Promise<void> {
  await db
    .prepare(
      `INSERT INTO budget_daily (day, events) VALUES (?1, ?2)
       ON CONFLICT (day) DO UPDATE SET events = events + excluded.events`,
    )
    .bind(day, events)
    .run();
}

export interface ScoreDelta {
  chatId: number;
  userHash: string;
  week: string;
  name?: string | null;
  points: number;
  correct: number;
  wrong: number;
  skipped: number;
  seconds: number;
  quizzes: number;
}

export async function addScore(db: D1Database, delta: ScoreDelta): Promise<void> {
  await db
    .prepare(
      `INSERT INTO scores (chat_id, user_hash, week, name, points, correct, wrong, skipped, seconds, quizzes)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
       ON CONFLICT (chat_id, user_hash, week) DO UPDATE SET
         name = COALESCE(excluded.name, scores.name),
         points = scores.points + excluded.points,
         correct = scores.correct + excluded.correct,
         wrong = scores.wrong + excluded.wrong,
         skipped = scores.skipped + excluded.skipped,
         seconds = scores.seconds + excluded.seconds,
         quizzes = scores.quizzes + excluded.quizzes`,
    )
    .bind(
      delta.chatId, delta.userHash, delta.week, delta.name ?? null, delta.points,
      delta.correct, delta.wrong, delta.skipped, delta.seconds, delta.quizzes,
    )
    .run();
}

export interface LeaderboardRow {
  user_hash: string;
  name: string | null;
  points: number;
  correct: number;
  wrong: number;
  skipped: number;
  seconds: number;
  quizzes: number;
}

/** Best first; equal points are ordered by less time. */
export async function leaderboard(db: D1Database, chatId: number, week: string, limit = 10): Promise<LeaderboardRow[]> {
  const result = await db
    .prepare(
      `SELECT user_hash, name, points, correct, wrong, skipped, seconds, quizzes FROM scores
       WHERE chat_id = ?1 AND week = ?2 ORDER BY points DESC, seconds ASC, user_hash ASC LIMIT ?3`,
    )
    .bind(chatId, week, limit)
    .all<LeaderboardRow>();
  return result.results;
}
