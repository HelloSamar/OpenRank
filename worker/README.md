# OpenRank worker

The multi-group Telegram quiz bot, written for Cloudflare Workers (free plan) in TypeScript.
Deploy it with the *Deploy worker* workflow; see `docs/DEPLOY.md`.

## What it does now (Phase 1)

| File | Job |
|---|---|
| `src/index.ts` | Webhook entry point. Checks Telegram's secret header, rejects bad or oversized input, always acknowledges |
| `src/handlers.ts` | Registers chats (`my_chat_member`, `/start`), admin-only setup in groups, `/status` |
| `src/limits.ts` | `checkLimits()`: the one place that decides whether a chat may start a quiz (plan, expiry and grace days, 100 questions a day on the free plan, one quiz at a time, daily request budget, emergency switch) |
| `src/db.ts` | All database statements (parameterised) |
| `src/validator.ts` | Quiz question validator, the same rules as `quiz_bot.py` |
| `src/hash.ts` | Anonymous user keys, identical to `user_key` in `quiz_bot.py`; constant-time secret comparison |
| `src/meter.ts` | Counts requests per day in batches, to stay inside the free limits |
| `src/time.ts` | IST day (the daily cap resets at midnight India time), UTC day (Cloudflare's limits reset at 00:00 UTC), score weeks |
| `../schema.sql` | The database schema (also `migrations/0001_init.sql`, which must be identical) |

## Rules this code keeps

- No raw Telegram user ids are stored anywhere, only keyed hashes.
- No secret is in the repository: the token, webhook secret and salt are Worker secrets.
- A free chat gets 100 questions per day (`FREE_DAILY_QUESTIONS`), counted per Asia/Kolkata day;
  every paid plan has a fair-use cap (`PAID_DAILY_QUESTIONS`) that protects the free hosting.
- Both languages must agree: `tests/fixtures/quiz_validation_cases.json` (validator) and
  `tests/fixtures/user_key_vectors.json` (hashing) are used by the Python and TypeScript tests.

## Working on it

```
cd worker
npm ci
npm run typecheck
npm test
```

The tests run the real `schema.sql` and the real queries on Node's built-in SQLite, which is
what D1 is, so no Cloudflare account is needed.
