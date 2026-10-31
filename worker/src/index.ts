/** The Worker: receives Telegram webhooks. Telegram posts to /webhook with a secret header. */
import { loadConfig, type Env } from "./env";
import { handleUpdate, type TelegramUpdate } from "./handlers";
import { safeEqual } from "./hash";
import { recordEvent } from "./meter";
import { createTelegram } from "./telegram";

const MAX_BODY_BYTES = 1_000_000;

function reply(status: number, body: string): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}

export async function handleWebhook(request: Request, env: Env, ctx?: ExecutionContext): Promise<Response> {
  if (!env.WEBHOOK_SECRET || !env.BOT_TOKEN || !env.SCORES_SALT) return reply(500, "not configured");

  const supplied = request.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!safeEqual(supplied, env.WEBHOOK_SECRET)) return reply(401, "unauthorized");

  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) return reply(413, "too large");
  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) return reply(413, "too large");

  let update: TelegramUpdate;
  try {
    update = JSON.parse(raw) as TelegramUpdate;
  } catch {
    return reply(400, "bad json");
  }
  if (typeof update !== "object" || update === null || typeof update.update_id !== "number") {
    return reply(400, "not an update");
  }

  const now = Math.floor(Date.now() / 1000);
  recordEvent(env.DB, now, ctx);
  const config = loadConfig(env);
  if (config.paused) return reply(200, "paused"); // acknowledged, so Telegram does not retry

  try {
    await handleUpdate(
      { db: env.DB, tg: createTelegram(env.BOT_TOKEN), config, salt: env.SCORES_SALT, now: () => now },
      update,
    );
  } catch (error) {
    // Always acknowledge: a failing update must not be re-sent by Telegram again and again.
    console.error("update failed", error instanceof Error ? error.name : "error");
  }
  return reply(200, "ok");
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const { pathname } = new URL(request.url);
    if (request.method === "GET" && pathname === "/health") {
      return Response.json({ ok: true, service: "openrank-worker" });
    }
    if (request.method === "POST" && pathname === "/webhook") return handleWebhook(request, env, ctx);
    return reply(404, "not found");
  },
};
