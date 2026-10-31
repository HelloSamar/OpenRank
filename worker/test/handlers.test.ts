import { beforeEach, describe, expect, it } from "vitest";
import { getChat, reserveQuestions, setPlan } from "../src/db";
import { describeStatus, handleUpdate, parseCommand, TEXT, toChatType, type Deps, type TelegramUpdate } from "../src/handlers";
import { userKey } from "../src/hash";
import { checkLimits } from "../src/limits";
import { istDay } from "../src/time";
import { createDb } from "./helpers/d1";
import { config, FakeTelegram, NOW } from "./helpers/fakes";

const SALT = "test-salt";
const GROUP = -1001234567890;
const USER = 8630381516;
const OTHER = 42;
const DAY = 86_400;

let tg: FakeTelegram;
let ctx: ReturnType<typeof createDb>;
let deps: Deps;

beforeEach(() => {
  ctx = createDb();
  tg = new FakeTelegram();
  deps = { db: ctx.db, tg, config: config(), salt: SALT, now: () => NOW };
});

const message = (chatId: number, type: string, userId: number, text: string, extra: object = {}): TelegramUpdate => ({
  update_id: 1,
  message: { chat: { id: chatId, type, title: type === "private" ? undefined : "Quiz Club" }, from: { id: userId, first_name: "Asha" }, text, ...extra },
});
const member = (chatId: number, type: string, status: string, fromId = USER, fromIsBot = false): TelegramUpdate => ({
  update_id: 2,
  my_chat_member: { chat: { id: chatId, type, title: "Quiz Club" }, from: { id: fromId, is_bot: fromIsBot }, new_chat_member: { status } },
});

describe("being added to and removed from chats", () => {
  it("registers a group, remembers who added the bot only as a hash, and says hello", async () => {
    await handleUpdate(deps, member(GROUP, "supergroup", "member"));
    const row = await getChat(ctx.db, GROUP);
    expect(row).toMatchObject({ chat_type: "supergroup", title: "Quiz Club", active: 1 });
    expect(row?.added_by).toBe(await userKey(SALT, USER));
    expect(JSON.stringify(row)).not.toContain(String(USER));
    expect(tg.texts(GROUP)).toEqual([TEXT.greeting]);
  });

  it("registers a channel quietly, because channels have nobody to talk to", async () => {
    await handleUpdate(deps, member(-1009, "channel", "administrator"));
    expect(await getChat(ctx.db, -1009)).toMatchObject({ chat_type: "channel", active: 1 });
    expect(tg.sent).toHaveLength(0);
  });

  it("marks a chat as inactive when the bot leaves or is kicked, and active again when re-added", async () => {
    await handleUpdate(deps, member(GROUP, "group", "member"));
    for (const gone of ["left", "kicked"]) {
      await handleUpdate(deps, member(GROUP, "group", gone));
      expect((await getChat(ctx.db, GROUP))?.active, gone).toBe(0);
      await handleUpdate(deps, member(GROUP, "group", "administrator"));
      expect((await getChat(ctx.db, GROUP))?.active).toBe(1);
    }
  });

  it("treats a person blocking the bot as leaving", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "/start"));
    await handleUpdate(deps, member(USER, "private", "kicked", USER));
    expect((await getChat(ctx.db, USER))?.active).toBe(0);
  });

  it("does not invent a chat just because the bot left an unknown one", async () => {
    await handleUpdate(deps, member(-555, "group", "left"));
    expect(await getChat(ctx.db, -555)).toBeNull();
  });

  it("records no adder when another bot added it, and ignores unknown chat types", async () => {
    await handleUpdate(deps, member(GROUP, "group", "member", 99, true));
    expect((await getChat(ctx.db, GROUP))?.added_by).toBeNull();
    await handleUpdate(deps, member(-7, "forum-of-doom", "member"));
    expect(await getChat(ctx.db, -7)).toBeNull();
  });

  it("survives a chat that cannot receive the greeting", async () => {
    tg.failSend = true;
    await expect(handleUpdate(deps, member(GROUP, "group", "member"))).resolves.toBeUndefined();
    expect((await getChat(ctx.db, GROUP))?.active).toBe(1);
  });
});

describe("/start in a private chat", () => {
  it("registers the person without storing their name or id", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "/start"));
    const row = await getChat(ctx.db, USER);
    expect(row).toMatchObject({ chat_type: "private", title: null, active: 1 });
    expect(row?.added_by).toBe(await userKey(SALT, USER));
    const text = tg.texts(USER)[0] ?? "";
    expect(text).toContain("Welcome to OpenRank");
    expect(text).toContain("Plan: Free.");
    expect(text).toContain("Today: 0 of 100 questions used, 100 left.");
    expect(text).toContain(TEXT.comingSoon);
  });

  it("does not make a person a free-plan reset trick: /start again keeps today's usage", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "/start"));
    await reserveQuestions(ctx.db, USER, istDay(NOW), 60, 100);
    await handleUpdate(deps, message(USER, "private", USER, "/start"));
    expect(tg.texts(USER).at(-1)).toContain("60 of 100 questions used, 40 left");
  });
});

describe("/start and /status in a group: admins only", () => {
  it("lets a group admin set the group up", async () => {
    tg.setStatus(GROUP, OTHER, "administrator");
    await handleUpdate(deps, message(GROUP, "supergroup", OTHER, "/start"));
    expect(await getChat(ctx.db, GROUP)).toMatchObject({ active: 1, title: "Quiz Club" });
    expect(tg.texts(GROUP)[0]).toContain(TEXT.groupReady);
  });

  it("lets the group owner in too", async () => {
    tg.setStatus(GROUP, OTHER, "creator");
    await handleUpdate(deps, message(GROUP, "group", OTHER, "/start"));
    expect(await getChat(ctx.db, GROUP)).not.toBeNull();
  });

  it("turns an ordinary member away and registers nothing", async () => {
    tg.setStatus(GROUP, OTHER, "member");
    await handleUpdate(deps, message(GROUP, "supergroup", OTHER, "/start"));
    expect(tg.texts(GROUP)).toEqual([TEXT.adminOnly]);
    expect(await getChat(ctx.db, GROUP)).toBeNull();
  });

  it("treats a failed admin check as 'not an admin'", async () => {
    tg.failMember = true;
    await handleUpdate(deps, message(GROUP, "supergroup", OTHER, "/start"));
    expect(tg.texts(GROUP)).toEqual([TEXT.adminOnly]);
    expect(await getChat(ctx.db, GROUP)).toBeNull();
  });

  it("lets the bot owner in without asking Telegram", async () => {
    deps = { ...deps, config: config({ ADMIN_IDS: String(USER) }) };
    await handleUpdate(deps, message(GROUP, "supergroup", USER, "/start"));
    expect(tg.memberCalls).toBe(0);
    expect(await getChat(ctx.db, GROUP)).not.toBeNull();
  });

  it("applies the same rule to /status", async () => {
    await handleUpdate(deps, member(GROUP, "supergroup", "member"));
    tg.sent.length = 0;
    await handleUpdate(deps, message(GROUP, "supergroup", OTHER, "/status"));
    expect(tg.texts(GROUP)).toEqual([TEXT.adminOnly]);
  });
});

describe("/status", () => {
  it("asks for /start in a chat that is not set up", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "/status"));
    expect(tg.texts(USER)).toEqual([TEXT.notSetUp]);
    expect(await getChat(ctx.db, USER)).toBeNull();
  });

  it("shows plan and usage", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "/start"));
    await reserveQuestions(ctx.db, USER, istDay(NOW), 30, 100);
    await handleUpdate(deps, message(USER, "private", USER, "/status"));
    const text = tg.texts(USER).at(-1) ?? "";
    expect(text).toContain("OpenRank status");
    expect(text).toContain("30 of 100 questions used, 70 left");
    expect(text).not.toContain(TEXT.comingSoon);
  });

  it("describes a paid plan, its grace days and a lapsed plan", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "/start"));
    await setPlan(ctx.db, { chatId: USER, plan: "individual", expiresAt: NOW + 5 * DAY }, NOW);
    const at = async (now: number) => describeStatus(await checkLimits(ctx.db, config(), USER, {}, now), now);
    expect(await at(NOW)).toContain("Plan: Individual, valid until 31 Oct 2026");
    expect(await at(NOW + 6 * DAY)).toContain("still works for a few more days");
    expect(await at(NOW + 9 * DAY)).toContain("You are on the Free plan");
  });
});

describe("what the bot ignores", () => {
  it("ignores plain text, unknown commands, bots and messages without text", async () => {
    await handleUpdate(deps, message(USER, "private", USER, "hello there"));
    await handleUpdate(deps, message(USER, "private", USER, "/quiz"));
    await handleUpdate(deps, message(USER, "private", USER, "/"));
    await handleUpdate(deps, { update_id: 3, message: { chat: { id: USER, type: "private" }, from: { id: USER, is_bot: true }, text: "/start" } });
    await handleUpdate(deps, { update_id: 4, message: { chat: { id: USER, type: "private" }, from: { id: USER } } });
    await handleUpdate(deps, { update_id: 5, message: { chat: { id: USER, type: "private" }, text: "/start" } });
    await handleUpdate(deps, { update_id: 6 });
    expect(tg.sent).toHaveLength(0);
    expect(await getChat(ctx.db, USER)).toBeNull();
  });

  it("ignores commands addressed to another bot", async () => {
    deps = { ...deps, botUsername: "OpenRankBot" };
    await handleUpdate(deps, message(USER, "private", USER, "/start@SomeOtherBot"));
    expect(tg.sent).toHaveLength(0);
    await handleUpdate(deps, message(USER, "private", USER, "/START@openrankbot"));
    expect(tg.sent).toHaveLength(1);
  });

  it("does not answer commands typed in a channel", async () => {
    await handleUpdate(deps, message(-1009, "channel", USER, "/start"));
    expect(tg.sent).toHaveLength(0);
  });

  it("keeps working when the reply cannot be sent", async () => {
    tg.failSend = true;
    await expect(handleUpdate(deps, message(USER, "private", USER, "/start"))).resolves.toBeUndefined();
    expect(await getChat(ctx.db, USER)).not.toBeNull();
  });
});

describe("helpers", () => {
  it("parses commands", () => {
    expect(parseCommand("/start")).toEqual({ command: "start" });
    expect(parseCommand("  /Status@MyBot  now ")).toEqual({ command: "status", target: "mybot" });
    expect(parseCommand("start")).toBeNull();
    expect(parseCommand("/")).toBeNull();
    expect(parseCommand("")).toBeNull();
  });

  it("maps chat types", () => {
    expect(["private", "group", "supergroup", "channel"].map(toChatType)).toEqual(["private", "group", "supergroup", "channel"]);
    expect(toChatType("sender")).toBeNull();
  });
});
