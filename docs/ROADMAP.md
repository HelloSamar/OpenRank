# OpenRank: from one group to a multi-group product at zero cost

Limits below were checked on 25 October 2026. Free tiers change, so re-check the sources at
the bottom before each phase. "Official" means a page of the provider itself.

## 1. How the bot works today

- **One script, one group.** `quiz_bot.py` (about 2,300 lines) is started by GitHub Actions
  every 15 minutes (`quiz.yml`, at :07, :22, :37, :52). It reads `schedule.json`, posts the
  quizzes that are due to the single group in the `CHAT_ID` secret, collects answers while the
  quiz runs (`getUpdates`), posts results, and commits `sent.json`, `scores.json`,
  `stats.json` and `alerts.json` back to the repository.
- **Content:** `quizzes/<Quiz Name>_<prefix>.json`; the prefix (`ows`, `idm`, `cmp`...) sets the
  usual speed. Each question is `[question, [options], correct_index, explanation]`. Today:
  4 quizzes, about 3,400 questions.
- **Commands** (`responder.py`): `/start`, `/quizlists`, `/contact`, answered in batches at each
  check, so a reply takes up to about 15 minutes.
- **Safety nets:** owner alerts, 12-hour late-quiz limit, schedule-end warning, anonymous score
  keys, rename workflow, test chat, self-repairing command menu (Phase 0).
- **Tests:** about 160 unit tests, run by `validate.yml`.

### Problems (this is why a new runtime is needed for the product)

1. **No real-time interaction.** GitHub cannot receive Telegram webhooks, so buttons, wizards
   and private chats would lag by minutes. GitHub's cron is also best effort, with the shortest
   interval 5 minutes and delays of 5 to 30 minutes or more.
2. **State lives in git.** Per-chat settings, plans and scores would mean a commit every run, a
   public history, and conflicts. The product needs a database.
3. **Single-group assumptions** (`CHAT_ID`, one `schedule.json`) run through the code.
4. **Quiz content is public.** All questions are in a public repository. Nobody pays for what
   anyone can read. Before charging, move `quizzes/` to a private repository (the build can read
   it with a deploy key).
5. **Personal data in history.** Older versions of `scores.json` contain 2 raw Telegram IDs and
   display names. No tokens, `.env` files or databases were ever committed (scanned all
   commits). Options: leave it, make the repository private, or rewrite history. See section 7.
6. **Docs claim things the code lacks** (README: "daily practice streak").

## 2. Limits that decide the design

**Telegram** (Bot API; official FAQ and API pages)
- Poll question 1 to 300 characters, 2 to 10 options of 1 to 100, explanation up to 200 with at
  most 2 line breaks, `open_period` 5 to 600 seconds (from client libraries mirroring the Bot API
  documentation).
- One message per second per chat; at most 20 messages per minute in a group; about 30 messages
  per second for the whole bot. Higher rates need paid broadcasts, which cost Stars (opt-in, keep
  off).
- **Channels cannot have per-person leaderboards.** Non-anonymous polls cannot be sent to
  channels, and a bot only receives `poll_answer` (who voted) for non-anonymous polls. Groups get
  leaderboards; channels can only get per-question results (for example "62% correct", read with
  `stopPoll`; to be tested in Phase 2).

**Cloudflare, free plan** (official docs; no credit card needed at sign-up)
| Resource | Free limit |
|---|---|
| Worker requests | 100,000 a day |
| CPU time per invocation | 10 ms (waiting for Telegram does not count) |
| Subrequests per invocation | 50 |
| Cron triggers | 5 per account |
| D1 | 5 GB per account, 500 MB per database, 5 million rows read and 100,000 rows written a day, 50 queries per invocation |
| Durable Objects (SQLite only on the free plan) | 100,000 requests and 13,000 GB-s a day, 100,000 rows written a day (**each alarm counts as one row written**), 5 GB in total and 1 GB per object |
| Static assets | requests are free and unlimited |

Over a free limit, calls **fail**, they are not billed; daily limits reset at **00:00 UTC
(05:30 IST)**. Python Workers exist but are in beta, and the 10 ms CPU limit is tight for them,
so the runtime should be TypeScript.

## 3. Hosting options

| Option | Cost | Card | Fits? |
|---|---|---|---|
| **GitHub Actions** (today) | free for public repos | no | Good for your own group only (no webhooks, minutes of delay, auto-disabled after 60 days without repository activity) |
| **Cloudflare Workers + D1 + Durable Objects** | free | **no** | **Recommended.** Event-driven, exact timers, cannot be billed on the free plan |
| Google Cloud e2-micro | free in 3 US regions only, 1 GB egress; the free tier does not include an external IP address | **yes** (billing account) | Python could be reused, but a card is needed and mistakes cost money |
| Oracle Cloud always free | free | **yes** | Idle instances can be reclaimed (a quiet bot is idle) |
| Fly.io, Railway, Render | no real free tier (Render's free service sleeps) | yes / no | Not suitable |

**Recommendation: Cloudflare.** A Worker receives Telegram webhooks; D1 holds chats, plans,
usage and scores; a **Durable Object per running quiz** uses alarms as the timer. Quiz files are
deployed as static assets, so they stay in git (or move to a private repository) with no
rewrite. A cron trigger is not needed, and would be worse: it ticks once a minute at best, so
gaps under 60 seconds are impossible, and each invocation can run only 50 D1 queries.

## 4. The timer problem and what the free tier sustains

**State machine.** Each running quiz is one Durable Object:
`{run_id, chat_id, quiz_id, total, gap_s, next_index, state, next_due_at}`. Alarm fires, then:
check `check_limits`, send question `next_index`, **only after Telegram says ok** write
`next_index + 1`, set the next alarm to now plus the gap. A failed post does not advance (retry
with back-off, then pause and tell the admin). A question is marked sent in the same step as the
response, so the only double-post window is a crash between the response and that write.
A chat can have one running quiz at a time.

**Per-question cost:** 1 alarm and 1 state update (2 Durable Object rows written, because
setting an alarm counts as a row), 1 `sendPoll`. **Per answer:** 1 webhook request and 1 D1 row
written. Two separate ceilings follow:

- **Questions: at most about 50,000 a day** (100,000 Durable Object rows written, 2 per
  question).
- **Answers: at most about 90,000 a day** (100,000 Worker requests and D1 rows written, keeping
  10% spare). With A answerers per question, about 90,000 / A questions a day fit: A=3 gives
  30,000 questions (300 groups at 100 a day); A=10 gives 9,000 (90 groups); A=30 gives 3,000
  (30 big groups). Individuals (A=1): about 900 people using their full 100 a day.
- Concurrent quizzes are limited by Telegram's 30 messages a second: at a 5 second gap about 150
  at once, at 10 seconds about 300. A group can run only one at a time, and a 5 second gap is 12
  messages a minute, inside the 20 a minute limit.
- Durable Object time: about 0.03 GB-s per question, so 100,000 questions use about 3,000 of the
  13,000 GB-s.

## 5. What could cause a bill, and how it is stopped

| Risk | Stopper |
|---|---|
| Upgrading Cloudflare to a paid plan | Never attach a card; the free plan cannot be billed |
| Telegram paid broadcasts (Stars) | Opt-in in BotFather; keep off |
| Cloudflare changes free limits | Re-check section 7 sources each phase |
| A free limit is hit | Calls fail; the bot keeps a daily budget counter (flushed in batches to save writes), refuses new quizzes above 80% and tells admins |
| One chat floods the bot | Per-chat caps: 100 questions a day (free), 1 running quiz, rate limit below Telegram's, global `PAUSED` switch |
| GCP or Oracle (if ever used) | External IP, egress above 1 GB, wrong region: avoid these options |

## 6. Plan

**Phase 0: harden the existing bot (done).** Self-repairing command menu; stricter
validator (no identical options) with a shared test file; `.gitignore` for secrets and
databases; secret and personal-data guards in the tests; scheduler timeout 350 to 120 minutes.

**Phase 1: multi-group data model (built; see `docs/DEPLOY.md`).** `schema.sql` (D1): `chats`, `plans`, `usage_daily`,
`scores(chat_id, user_hash)`, `runs`. Worker skeleton in TypeScript with the webhook, chat
registration (`my_chat_member`), admin check (`getChatMember`), `check_limits(chat_id)` and the
daily counter (reset at 00:00 Asia/Kolkata). Python and TypeScript validators both pass
`tests/fixtures/quiz_validation_cases.json`. Your own group keeps running on GitHub Actions.

**Phase 2: category, quiz, count and gap flow (next).** Inline buttons (callback data under 64 bytes,
short ids). A build step reads `quizzes/` and writes `catalog.json`. Categories need no file
moves: `quizzes/categories.json` maps each prefix to a category label, order and a `free` flag
(and optional per-quiz overrides). Count validated against the quiz size, gap 5 to 600. The
Durable Object state machine, leaderboard for groups, per-question results for channels.

**Phase 3: plans, limits, manual payments.** `free`, `individual` (₹29), `group` (₹99), each with
`expires_at`; every command goes through `check_limits`. Manual UPI: the bot shows the UPI
details and the user sends a reference; admin command `/grant <chat_id> <plan> <days>`; expiry
reminders at 7 and 1 days and a short grace period. Payment code sits behind one interface so
Razorpay, Cashfree or Telegram Payments can be added later.

**Phase 4: move your own group over** once the new system has run for weeks.

### Phase 1 notes

- **A separate bot is required.** A webhook and `getUpdates` cannot share a bot, so the worker
  uses a new bot token until your own group is moved over (Phase 4).
- `check_limits` (`worker/src/limits.ts`) already enforces the plan, its expiry and the grace
  days, the 100-a-day free cap (per Asia/Kolkata day, reserved atomically so two quizzes
  starting together cannot both exceed it), one quiz at a time, the request budget and the
  emergency switch. Phase 2 and 3 only call it.
- A plan row for the wrong kind of chat (an individual plan on a group) is ignored, not trusted.
- Durable Objects are not used yet; they arrive with the quiz timer in Phase 2.

## 7. Security notes

- **Personal data in history.** Older commits of `scores.json` still hold 2 raw Telegram IDs.
  Going forward `scores.json` uses hashed keys (`SCORES_SALT`), and a test fails if raw IDs
  return. To remove old data: make the repository private, or rewrite history with
  `git filter-repo` and force-push (this changes every commit hash and breaks forks).
- Tokens and secrets live only in GitHub secrets (and later Cloudflare secrets). A test fails if
  a bot-token-shaped string or a database file is committed.
- Use only your own or properly licensed questions.

## Sources (official unless noted)

- Cloudflare Workers limits: developers.cloudflare.com/workers/platform/limits
- Cloudflare D1 pricing and limits: developers.cloudflare.com/d1/platform/pricing, /limits
- Cloudflare Durable Objects pricing: developers.cloudflare.com/durable-objects/platform/pricing;
  free-plan summary at cloudflare.com/plans
- Telegram Bots FAQ (rate limits): core.telegram.org/bots/faq
- Telegram polls: core.telegram.org/api/poll
- GitHub scheduled workflows and limits: GitHub documentation and community discussions
  (third-party summaries for the 5 to 30 minute delays)
- Google Cloud free tier and Oracle: third-party guides (not Google's or Oracle's own pages)
