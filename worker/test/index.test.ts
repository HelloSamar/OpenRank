import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import worker, { handleWebhook } from "../src/index";
import { getChat } from "../src/db";
import { resetMeterForTests } from "../src/meter";
import type { Env } from "../src/env";
import { createDb } from "./helpers/d1";

const SECRET = "hook-secret-123";
const TOKEN = "123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN12";
let ctx: ReturnType<typeof createDb>;
let telegramCalls: { url: string; body: Record<string, unknown> }[];

function env(overrides: Partial<Env> = {}): Env {
  return { DB: ctx.db, BOT_TOKEN: TOKEN, WEBHOOK_SECRET: SECRET, SCORES_SALT: "salt", ...overrides };
}

function post(body: unknown, headers: Record<string, string> = { "x-telegram-bot-api-secret-token": SECRET }): Request {
  return new Request("https://worker.example/webhook", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

const startUpdate = { update_id: 10, message: { chat: { id: 555, type: "private" }, from: { id: 555, first_name: "A" }, text: "/start" } };

beforeEach(() => {
  ctx = createDb();
  resetMeterForTests();
  telegramCalls = [];
  vi.stubGlobal("fetch", async (url: string, init: { body: string }) => {
    telegramCalls.push({ url, body: JSON.parse(init.body) as Record<string, unknown> });
    return new Response(JSON.stringify({ ok: true, result: true }), { status: 200 });
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("POST /webhook", () => {
  it("handles an update with the right secret", async () => {
    const response = await handleWebhook(post(startUpdate), env());
    expect(response.status).toBe(200);
    expect(await getChat(ctx.db, 555)).toMatchObject({ chat_type: "private", active: 1 });
    expect(telegramCalls).toHaveLength(1);
    expect(telegramCalls[0]?.url).toContain("/sendMessage");
    expect(telegramCalls[0]?.body.chat_id).toBe(555);
  });

  it("refuses a missing, wrong or almost-right secret, and does nothing", async () => {
    const wrong: Record<string, string>[] = [
      {},
      { "x-telegram-bot-api-secret-token": "nope" },
      { "x-telegram-bot-api-secret-token": SECRET + "x" },
      { "x-telegram-bot-api-secret-token": "" },
    ];
    for (const headers of wrong) {
      expect((await handleWebhook(post(startUpdate, headers), env())).status).toBe(401);
    }
    expect(await getChat(ctx.db, 555)).toBeNull();
    expect(telegramCalls).toHaveLength(0);
  });

  it("says it is not configured instead of running open when a secret is missing", async () => {
    for (const missing of ["WEBHOOK_SECRET", "BOT_TOKEN", "SCORES_SALT"] as const) {
      expect((await handleWebhook(post(startUpdate), env({ [missing]: "" }))).status, missing).toBe(500);
    }
    expect(await getChat(ctx.db, 555)).toBeNull();
  });

  it("rejects broken or oversized input", async () => {
    expect((await handleWebhook(post("{ not json"), env())).status).toBe(400);
    expect((await handleWebhook(post("[1,2]"), env())).status).toBe(400);
    expect((await handleWebhook(post({ message: {} }), env())).status).toBe(400);
    expect((await handleWebhook(post({ update_id: "7" }), env())).status).toBe(400);
    expect((await handleWebhook(post("null"), env())).status).toBe(400);
    const big = post(JSON.stringify({ update_id: 1, pad: "x".repeat(1_000_001) }));
    expect((await handleWebhook(big, env())).status).toBe(413);
  });

  it("acknowledges but ignores everything while paused", async () => {
    const response = await handleWebhook(post(startUpdate), env({ PAUSED: "true" }));
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("paused");
    expect(await getChat(ctx.db, 555)).toBeNull();
    expect(telegramCalls).toHaveLength(0);
  });

  it("still answers 200 when handling fails, so Telegram does not resend forever", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new Error(`network down for https://api.telegram.org/bot${TOKEN}/sendMessage`);
    });
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const response = await handleWebhook(post(startUpdate), env());
    expect(response.status).toBe(200);
    expect(JSON.stringify(spy.mock.calls)).not.toContain(TOKEN);
    spy.mockRestore();
  });

  it("answers 200 even if the database fails, and never prints the token", async () => {
    const broken = { prepare: () => { throw new Error(`boom ${TOKEN}`); } } as unknown as D1Database;
    const logs = vi.spyOn(console, "error").mockImplementation(() => {});
    const response = await handleWebhook(post(startUpdate), env({ DB: broken }));
    expect(response.status).toBe(200);
    expect(JSON.stringify(logs.mock.calls)).not.toContain(TOKEN);
    logs.mockRestore();
  });
});

describe("routing", () => {
  const run = (request: Request) => worker.fetch(request, env(), { waitUntil() {}, passThroughOnException() {} } as unknown as ExecutionContext);

  it("answers /health without secrets", async () => {
    const response = await run(new Request("https://worker.example/health"));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true, service: "openrank-worker" });
  });

  it("answers 404 for everything else, and for GET /webhook", async () => {
    expect((await run(new Request("https://worker.example/"))).status).toBe(404);
    expect((await run(new Request("https://worker.example/webhook"))).status).toBe(404);
    expect((await run(new Request("https://worker.example/health", { method: "POST" }))).status).toBe(404);
    expect((await run(new Request("https://worker.example/admin"))).status).toBe(404);
  });

  it("routes POST /webhook to the handler", async () => {
    const response = await run(post(startUpdate));
    expect(response.status).toBe(200);
    expect(await getChat(ctx.db, 555)).not.toBeNull();
  });
});
