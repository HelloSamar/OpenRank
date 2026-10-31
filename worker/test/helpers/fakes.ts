import type { Config } from "../../src/env";
import { loadConfig } from "../../src/env";
import type { ChatMember, Telegram } from "../../src/telegram";
import { TelegramError } from "../../src/telegram";

export class FakeTelegram implements Telegram {
  sent: { chatId: number; text: string; extra?: Record<string, unknown> }[] = [];
  statuses = new Map<string, string>(); // "chat:user" -> member status
  failSend = false;
  failMember = false;
  memberCalls = 0;

  setStatus(chatId: number, userId: number, status: string): void {
    this.statuses.set(`${chatId}:${userId}`, status);
  }

  async sendMessage(chatId: number, text: string, extra?: Record<string, unknown>): Promise<void> {
    if (this.failSend) throw new TelegramError("sendMessage", 403, "bot was blocked");
    this.sent.push({ chatId, text, ...(extra ? { extra } : {}) });
  }

  async getChatMember(chatId: number, userId: number): Promise<ChatMember> {
    this.memberCalls += 1;
    if (this.failMember) throw new TelegramError("getChatMember", 400, "chat not found");
    return { status: this.statuses.get(`${chatId}:${userId}`) ?? "member" };
  }

  async answerCallbackQuery(): Promise<void> {}

  texts(chatId: number): string[] {
    return this.sent.filter((m) => m.chatId === chatId).map((m) => m.text);
  }
}

export const NOW = Date.UTC(2026, 9, 26, 6, 30, 0) / 1000; // 26 Oct 2026, 12:00 IST

export function config(overrides: Record<string, string> = {}): Config {
  return loadConfig(overrides);
}
