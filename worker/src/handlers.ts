/**
 * What the bot does with Telegram updates. Phase 1: it registers chats and answers /start and
 * /status. The quiz menus come in Phase 2.
 */
import type { Config } from "./env";
import { getChat, setChatActive, upsertChat } from "./db";
import { userKey } from "./hash";
import { checkLimits, type Decision } from "./limits";
import { formatIst } from "./time";
import {
  isAdminStatus,
  type ChatType,
  type Telegram,
  type TelegramChat,
  type TelegramUser,
} from "./telegram";

export interface IncomingMessage {
  message_id?: number;
  from?: TelegramUser;
  chat: TelegramChat;
  text?: string;
}

export interface ChatMemberUpdate {
  chat: TelegramChat;
  from: TelegramUser;
  new_chat_member: { status: string };
}

export interface TelegramUpdate {
  update_id: number;
  message?: IncomingMessage;
  my_chat_member?: ChatMemberUpdate;
}

export interface Deps {
  db: D1Database;
  tg: Telegram;
  config: Config;
  /** Secret for hashing user ids (SCORES_SALT). */
  salt: string;
  /** Current time in unix seconds. */
  now: () => number;
  /** Optional: ignore commands addressed to another bot (/start@OtherBot). */
  botUsername?: string;
}

export const TEXT = {
  adminOnly: "Only group admins can do that.",
  greeting: "Thanks for adding OpenRank! An admin of this group should send /start to set it up.",
  welcome: "\u{1F44B} Welcome to OpenRank!",
  groupReady: "OpenRank is set up in this group.",
  notSetUp: "This chat is not set up yet. Send /start first.",
  comingSoon: "Quiz menus are coming soon.",
} as const;

const PRESENT = new Set(["member", "administrator", "creator"]);
const GONE = new Set(["left", "kicked"]);

export function toChatType(type: string): ChatType | null {
  return type === "private" || type === "group" || type === "supergroup" || type === "channel" ? type : null;
}

/** "/start@OpenRankBot now" gives { command: "start", target: "openrankbot" }. */
export function parseCommand(text: string): { command: string; target?: string } | null {
  const first = text.trim().split(/\s+/, 1)[0] ?? "";
  if (!first.startsWith("/") || first.length < 2) return null;
  const [name = "", target] = first.slice(1).split("@", 2);
  return { command: name.toLowerCase(), ...(target ? { target: target.toLowerCase() } : {}) };
}

export function describeStatus(decision: Decision, now: number): string {
  const label = decision.plan === "individual" ? "Individual" : decision.plan === "group" ? "Group" : "Free";
  const lines: string[] = [];
  if (decision.lapsed && decision.expiresAt !== null) {
    lines.push(`Your plan ended on ${formatIst(decision.expiresAt)}. You are on the Free plan.`);
  } else if (decision.inGrace && decision.expiresAt !== null) {
    lines.push(`Plan: ${label}. It ended on ${formatIst(decision.expiresAt)} and still works for a few more days. Please renew.`);
  } else if (decision.expiresAt !== null) {
    lines.push(`Plan: ${label}, valid until ${formatIst(decision.expiresAt)}.`);
  } else {
    lines.push("Plan: Free.");
  }
  lines.push(
    `Today: ${decision.usedToday} of ${decision.dailyCap} questions used, ${decision.remainingToday} left. ` +
      "The count resets at midnight IST.",
  );
  void now;
  return lines.join("\n");
}

async function trySend(deps: Deps, chatId: number, text: string): Promise<void> {
  try {
    await deps.tg.sendMessage(chatId, text);
  } catch (error) {
    // A chat that blocked the bot or removed it must never break the rest of the update.
    console.warn("sendMessage failed", error instanceof Error ? error.name : "error");
  }
}

async function isAllowedToConfigure(deps: Deps, chat: TelegramChat, user: TelegramUser): Promise<boolean> {
  if (deps.config.adminIds.has(user.id)) return true;
  try {
    const member = await deps.tg.getChatMember(chat.id, user.id);
    return isAdminStatus(member.status);
  } catch {
    return false;
  }
}

async function onMyChatMember(deps: Deps, update: ChatMemberUpdate): Promise<void> {
  const type = toChatType(update.chat.type);
  if (type === null) return;
  const status = update.new_chat_member.status;
  const now = deps.now();
  if (PRESENT.has(status)) {
    const addedBy = update.from.is_bot ? null : await userKey(deps.salt, update.from.id);
    await upsertChat(deps.db, { chatId: update.chat.id, type, title: update.chat.title ?? null, addedBy, active: true }, now);
    if (type === "group" || type === "supergroup") await trySend(deps, update.chat.id, TEXT.greeting);
  } else if (GONE.has(status)) {
    await setChatActive(deps.db, update.chat.id, false, now);
  }
}

async function onMessage(deps: Deps, message: IncomingMessage): Promise<void> {
  const { chat, from, text } = message;
  if (!from || from.is_bot || !text) return;
  const parsed = parseCommand(text);
  if (parsed === null) return;
  if (parsed.target !== undefined && deps.botUsername && parsed.target !== deps.botUsername.toLowerCase()) return;
  const type = toChatType(chat.type);
  if (type === null || type === "channel") return;
  if (parsed.command !== "start" && parsed.command !== "status") return;

  if (type !== "private" && !(await isAllowedToConfigure(deps, chat, from))) {
    await trySend(deps, chat.id, TEXT.adminOnly);
    return;
  }

  const now = deps.now();
  if (parsed.command === "start") {
    const addedBy = await userKey(deps.salt, from.id);
    await upsertChat(deps.db, { chatId: chat.id, type, title: type === "private" ? null : chat.title ?? null, addedBy, active: true }, now);
  } else if ((await getChat(deps.db, chat.id)) === null) {
    await trySend(deps, chat.id, TEXT.notSetUp);
    return;
  }

  const decision = await checkLimits(deps.db, deps.config, chat.id, {}, now);
  const heading = parsed.command === "start" ? (type === "private" ? TEXT.welcome : TEXT.groupReady) : "OpenRank status";
  const tail = parsed.command === "start" ? `\n\n${TEXT.comingSoon}` : "";
  await trySend(deps, chat.id, `${heading}\n\n${describeStatus(decision, now)}${tail}`);
}

export async function handleUpdate(deps: Deps, update: TelegramUpdate): Promise<void> {
  if (update.my_chat_member) await onMyChatMember(deps, update.my_chat_member);
  else if (update.message) await onMessage(deps, update.message);
}
