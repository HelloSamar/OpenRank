# Deploying the multi-group worker (no command line needed)

The worker is the new product: it serves many groups and private chats from Cloudflare's free
plan. Your current group quizzes keep running on GitHub Actions and are not touched.

**It must use its own, NEW bot.** Setting a webhook on a bot stops it from receiving updates any
other way. If you pointed the worker at your group quiz bot, the scheduled quizzes would stop
collecting answers. The deploy workflow makes you confirm this.

What exists after this phase: the bot registers chats, checks that only admins configure
groups, tracks each chat's plan and daily question count, and answers `/start` and `/status`.
Quiz menus come in Phase 2, payments in Phase 3.

## One-time setup

1. **Make the new bot.** In Telegram open @BotFather, send `/newbot`, choose a name and a
   username ending in `bot`, and copy the token.
2. **Create a free Cloudflare account** at cloudflare.com (an email address is enough; no card).
   Find your **Account ID** in the dashboard (Workers & Pages overview, right-hand side).
3. **Create an API token.** Cloudflare dashboard, My Profile, API Tokens, Create Token. Start
   from the *Edit Cloudflare Workers* template, add the permission *Account, D1, Edit*, and
   limit it to your account. Copy the token (it is shown once). The permission names can change;
   if the deploy later says a permission is missing, add that one.
4. **Add GitHub secrets.** Repository Settings, Secrets and variables, Actions, *Secrets* tab:

   | Secret | Value |
   |---|---|
   | `CLOUDFLARE_API_TOKEN` | the token from step 3 |
   | `CLOUDFLARE_ACCOUNT_ID` | the Account ID from step 2 |
   | `WORKER_BOT_TOKEN` | the NEW bot's token from step 1 |
   | `WORKER_WEBHOOK_SECRET` | a random string of letters, digits, `_` and `-` (at least 20 characters) |
   | `WORKER_SCORES_SALT` | any long random text. Use the same text as your `SCORES_SALT` if you want a person to have the same anonymous key in both systems. Never change it later |
   | `WORKER_ADMIN_IDS` | optional: your numeric Telegram ID (comma separated for several owners) |

5. **Deploy.** Actions tab, *Deploy worker*, Run workflow. Tick **confirm_new_bot** (the token
   is a new bot), leave *set_webhook* ticked, and run. It type-checks and tests first (nothing is
   deployed if that fails), creates the database `openrank` on the first run, applies
   `schema.sql`, deploys, stores the secrets and sets the bot's webhook.
6. **Try it.** Message the new bot `/start`: it shows the Free plan and "0 of 100 questions
   used". Add it to a test group, then send `/start` there as a group admin. A normal member is
   told that only admins can do that. `/status` shows the plan and usage.

Run the workflow again after any change to the worker; the database and secrets are reused.

## Emergency stop and limits

- **Pause:** run *Deploy worker* with **paused** ticked. New quizzes stop and updates are
  acknowledged without being handled. Run it again unticked to resume.
- **Daily budget:** the worker counts its requests per day and refuses new quizzes at 80,000
  (the free limit is 100,000). `DAILY_BUDGET_EVENTS` in `worker/wrangler.toml` sets it.
- **Cost:** the free plan cannot be billed; over a limit, calls fail. Do not upgrade the plan
  and do not add a card. See `docs/ROADMAP.md` for the limits and what they allow.

## Not tested against a live account yet

The code and its 149 tests run, but this workflow has not been run against a real Cloudflare
account. If a step fails, open the run, copy the red lines and send them to whoever maintains
the repository; the likely causes are a token permission or a renamed Wrangler output field.
