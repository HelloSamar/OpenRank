# Running the always-on bot (private practice + group quizzes)

`chat_bot.py` is the program that makes `/start`, `/quizzes`, `/quiz`, `/score` and `/stop`
work in a private chat with the bot. It also posts the scheduled group quizzes from
`schedule.json`, so **it replaces the GitHub Actions scheduler**.

Why: Telegram lets only ONE program read a bot's updates. If GitHub and your host both
did, each would steal answers from the other. `chat_bot.py` reads everything and passes the
group answers to the quiz code, so group scoring keeps working.

## 1. Pick a host that stays on

Any of these works (a small plan is enough):

- **Railway / Render (Background Worker) / Fly.io**: connect this GitHub repo. It starts
  `python chat_bot.py` from the `Procfile` and redeploys on every push, so editing
  `schedule.json` or a quiz on GitHub keeps working as before.
- **A VPS**: `pip install -r requirements.txt`, then run `python chat_bot.py` under systemd.

## 2. Settings (environment variables on the host)

| Name | Value |
|---|---|
| `BOT_TOKEN` | the token from BotFather |
| `CHAT_ID` | your group/channel id (leave empty to run private practice only) |
| `DATA_DIR` | a folder on a **persistent disk/volume**, e.g. `/data` |

`DATA_DIR` matters: it holds `sent.json`, `scores.json`, `stats.json` and `practice.json`
(everyone's practice results). Without a persistent disk these files vanish on every
redeploy and the group quizzes would be posted again. On the first start the bot copies the
current `sent.json`, `scores.json` and `stats.json` from the repo into `DATA_DIR`, so make
sure the repo has the latest ones when you switch.

## 3. Turn the GitHub scheduler off (do not skip)

In the GitHub repo: **Settings > Secrets and variables > Actions > Variables > New
repository variable**, name `HOSTED_BOT`, value `true`. The "Quiz Scheduler" workflow then
skips itself. (Or disable the workflow in the Actions tab.)

## 4. Set the bot's profile once

Run this once from any computer or the host's console:

    BOT_TOKEN=... python chat_bot.py --setup-profile

It sets the About text, the Description shown above START, and the command menu. You can
edit the wording at the top of `chat_bot.py`.

## 5. Check it works

Open a private chat with the bot and send `/start`, then `/quizzes`, then
`/quiz ows000 5`. The host log should say `Listening for messages and answers...`.
If it says *Another program is reading this bot's updates*, the GitHub scheduler or a
webhook is still active (step 3).

## Good to know

- **Privacy:** `practice.json` stores names and Telegram ids. It lives in `DATA_DIR` and is
  listed in `.gitignore`; never commit it. Back up `DATA_DIR` now and then.
- **Size:** one process and a JSON file are fine for hundreds of active users. For thousands,
  the next step is moving `practice.json` to a database.
- **Practice quizzes use all quiz files** in `quizzes/`, with each quiz's own timer.
  `/quiz ows000` gives the person every question in that quiz; `/quiz ows000 20` gives 20.
  Unseen questions come first, then the ones seen longest ago, so a person who stops
  halfway (`/stop`) continues with new questions next time.
- **Group vs. private:** the scheduled group quizzes stay free and unchanged (same
  `schedule.json`, same group scoring). The bot ignores commands sent in groups; private
  chats are where individual practice happens.
- **Payments are not included.** When you add paid features, also add `/support`,
  `/paysupport` and `/terms` commands and check Telegram's current rules for digital goods.
