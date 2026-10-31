/**
 * check_limits: the one place that decides whether a chat may start a quiz.
 * Every command that would start or change a quiz must go through checkLimits().
 *
 * Order of the checks: emergency switch, chat known and present, the day's request budget,
 * one quiz at a time, free-plan quiz access, then the daily question cap.
 */
import type { Config } from "./env";
import { getBudget, getChat, getPlan, getUsage, hasActiveRun, type PlanName } from "./db";
import type { ChatType } from "./telegram";
import { istDay, utcDay } from "./time";

export type EffectivePlan = "free" | PlanName;

export type DenyReason =
  | "paused"
  | "unknown_chat"
  | "chat_inactive"
  | "busy"
  | "quiz_running"
  | "paid_quiz"
  | "daily_cap";

export interface PlanStatus {
  plan: EffectivePlan;
  /** When the paid plan ends (unix seconds), if there is one. */
  expiresAt: number | null;
  /** True during the grace days after expiry: the plan still works. */
  inGrace: boolean;
  /** The plan row exists but has run out (past the grace days): the chat is on the free plan. */
  lapsed: boolean;
}

export interface Decision extends PlanStatus {
  allowed: boolean;
  reason?: DenyReason;
  dailyCap: number;
  usedToday: number;
  remainingToday: number;
  message: string;
}

export interface Request {
  /** How many questions the new quiz would use (omit when only asking for the status). */
  questions?: number;
  /** Whether the chosen quiz is a free one. Defaults to true. */
  quizIsFree?: boolean;
}

const DAY = 86_400;

/** The plan that applies right now. A plan for the wrong kind of chat is ignored. */
export async function resolvePlan(
  db: D1Database,
  chatId: number,
  chatType: ChatType,
  now: number,
  config: Config,
): Promise<PlanStatus> {
  const row = await getPlan(db, chatId);
  const free: PlanStatus = { plan: "free", expiresAt: null, inGrace: false, lapsed: false };
  if (row === null) return free;
  const fits = (row.plan === "individual") === (chatType === "private");
  if (!fits) return free;
  if (now <= row.expires_at) return { plan: row.plan, expiresAt: row.expires_at, inGrace: false, lapsed: false };
  if (now <= row.expires_at + config.graceDays * DAY) {
    return { plan: row.plan, expiresAt: row.expires_at, inGrace: true, lapsed: false };
  }
  return { ...free, expiresAt: row.expires_at, lapsed: true };
}

export function dailyCapFor(plan: EffectivePlan, config: Config): number {
  return plan === "free" ? config.freeDailyQuestions : config.paidDailyQuestions;
}

export async function checkLimits(
  db: D1Database,
  config: Config,
  chatId: number,
  request: Request,
  now: number,
): Promise<Decision> {
  const empty: PlanStatus = { plan: "free", expiresAt: null, inGrace: false, lapsed: false };
  const deny = (reason: DenyReason, message: string, status: PlanStatus = empty, cap = config.freeDailyQuestions, used = 0): Decision => ({
    ...status, allowed: false, reason, message, dailyCap: cap, usedToday: used, remainingToday: Math.max(0, cap - used),
  });

  if (config.paused) return deny("paused", "OpenRank is paused for maintenance. Please try again later.");

  const chat = await getChat(db, chatId);
  if (chat === null) return deny("unknown_chat", "Send /start first so I can set this chat up.");
  if (chat.active === 0) return deny("chat_inactive", "I was removed from this chat. Add me again and send /start.");

  const status = await resolvePlan(db, chatId, chat.chat_type, now, config);
  const cap = dailyCapFor(status.plan, config);
  const used = await getUsage(db, chatId, istDay(now));
  const remaining = Math.max(0, cap - used);
  const base = { ...status, dailyCap: cap, usedToday: used, remainingToday: remaining };

  if (request.questions !== undefined) {
    if ((await getBudget(db, utcDay(now))) >= config.dailyBudgetEvents) {
      return { ...base, allowed: false, reason: "busy", message: "OpenRank is very busy today. Please try again later." };
    }
    if (await hasActiveRun(db, chatId)) {
      return { ...base, allowed: false, reason: "quiz_running", message: "A quiz is already running here. Wait for it to finish or stop it first." };
    }
    if (status.plan === "free" && request.quizIsFree === false) {
      return { ...base, allowed: false, reason: "paid_quiz", message: "This quiz is for paid plans." };
    }
    if (request.questions > remaining) {
      const left = remaining === 1 ? "1 question" : `${remaining} questions`;
      return {
        ...base, allowed: false, reason: "daily_cap",
        message: remaining === 0
          ? `The daily limit of ${cap} questions is used up. It resets at midnight IST.`
          : `Only ${left} left today (limit ${cap} a day). Choose a smaller number.`,
      };
    }
  }
  return { ...base, allowed: true, message: "OK" };
}
