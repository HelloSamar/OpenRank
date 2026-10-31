/** A small Telegram Bot API client. It never puts the bot token in an error message. */
export class TelegramError extends Error {
  constructor(
    readonly method: string,
    readonly status: number,
    readonly description: string,
    /** Seconds Telegram asks us to wait (HTTP 429), if given. */
    readonly retryAfter?: number,
  ) {
    super(`Telegram ${method} failed (${status}): ${description}`);
    this.name = "TelegramError";
  }
}

export type ChatType = "private" | "group" | "supergroup" | "channel";

export interface TelegramUser {
  id: number;
  is_bot?: boolean;
  first_name?: string;
  username?: string;
}

export interface TelegramChat {
  id: number;
  type: string;
  title?: string;
}

export interface ChatMember {
  status: string;
}

export interface Telegram {
  sendMessage(chatId: number, text: string, extra?: Record<string, unknown>): Promise<void>;
  getChatMember(chatId: number, userId: number): Promise<ChatMember>;
  answerCallbackQuery(callbackQueryId: string, text?: string): Promise<void>;
}

const API = "https://api.telegram.org";

export function createTelegram(token: string, fetchImpl: typeof fetch = fetch): Telegram {
  async function call<T>(method: string, payload: Record<string, unknown>): Promise<T> {
    let response: Response;
    try {
      response = await fetchImpl(`${API}/bot${token}/${method}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      // The raw error could contain the URL, and with it the token.
      throw new TelegramError(method, 0, "network error");
    }
    let body: { ok?: boolean; result?: T; description?: string; parameters?: { retry_after?: number } };
    try {
      body = (await response.json()) as typeof body;
    } catch {
      throw new TelegramError(method, response.status, "invalid response");
    }
    if (!response.ok || body.ok !== true) {
      throw new TelegramError(
        method,
        response.status,
        String(body.description ?? "unknown error").replaceAll(token, "***"),
        body.parameters?.retry_after,
      );
    }
    return body.result as T;
  }

  return {
    async sendMessage(chatId, text, extra = {}) {
      await call("sendMessage", { chat_id: chatId, text, ...extra });
    },
    getChatMember(chatId, userId) {
      return call<ChatMember>("getChatMember", { chat_id: chatId, user_id: userId });
    },
    async answerCallbackQuery(callbackQueryId, text) {
      await call("answerCallbackQuery", { callback_query_id: callbackQueryId, ...(text ? { text } : {}) });
    },
  };
}

/** Who may configure a group or channel: its owner and its administrators. */
export function isAdminStatus(status: string): boolean {
  return status === "creator" || status === "administrator";
}
