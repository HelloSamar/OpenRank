/** What the Worker receives from Cloudflare: the database binding, secrets and settings. */
export interface Env {
  DB: D1Database;
  /** Secrets (wrangler secret put). Never put these in wrangler.toml or git. */
  BOT_TOKEN: string;
  WEBHOOK_SECRET: string;
  SCORES_SALT: string;
  /** Telegram user ids (comma separated) of the bot's owner(s); they may run /grant later. */
  ADMIN_IDS?: string;
  /** Settings (plain text in wrangler.toml). */
  PAUSED?: string;
  FREE_DAILY_QUESTIONS?: string;
  PAID_DAILY_QUESTIONS?: string;
  GRACE_DAYS?: string;
  DAILY_BUDGET_EVENTS?: string;
}

export interface Config {
  /** Questions per chat per day on the free plan. */
  freeDailyQuestions: number;
  /** Fair-use cap per chat per day on paid plans (protects the free hosting limits). */
  paidDailyQuestions: number;
  /** Days a plan keeps working after it expires. */
  graceDays: number;
  /** Stop new quizzes when the day's request count reaches this (free limit is 100,000). */
  dailyBudgetEvents: number;
  /** Emergency switch: no new quizzes. */
  paused: boolean;
  adminIds: ReadonlySet<number>;
}

export const DEFAULTS = {
  freeDailyQuestions: 100,
  paidDailyQuestions: 2000,
  graceDays: 3,
  dailyBudgetEvents: 80_000,
} as const;

/** Whole numbers only: "50" is 50, but "5O", "1.5" and "50 questions" fall back to the default. */
function wholeNumber(value: string | undefined): number | null {
  const text = (value ?? "").trim();
  return /^\d{1,9}$/.test(text) ? Number(text) : null;
}

function positiveInt(value: string | undefined, fallback: number): number {
  const parsed = wholeNumber(value);
  return parsed !== null && parsed > 0 ? parsed : fallback;
}

export function loadConfig(env: Partial<Env>): Config {
  const admins = new Set<number>();
  for (const part of (env.ADMIN_IDS ?? "").split(",")) {
    const text = part.trim();
    if (/^\d{1,15}$/.test(text) && Number(text) > 0) admins.add(Number(text));
  }
  return {
    freeDailyQuestions: positiveInt(env.FREE_DAILY_QUESTIONS, DEFAULTS.freeDailyQuestions),
    paidDailyQuestions: positiveInt(env.PAID_DAILY_QUESTIONS, DEFAULTS.paidDailyQuestions),
    graceDays: wholeNumber(env.GRACE_DAYS) ?? DEFAULTS.graceDays,
    dailyBudgetEvents: positiveInt(env.DAILY_BUDGET_EVENTS, DEFAULTS.dailyBudgetEvents),
    paused: ["1", "true", "yes", "on"].includes((env.PAUSED ?? "").trim().toLowerCase()),
    adminIds: admins,
  };
}
