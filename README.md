# Telegram Quiz Bot (@PYQxBot)

Posts anonymous Telegram Quiz Polls. Each quiz set is its own file, and `schedule.json` decides which set goes out at which date and time. A GitHub Actions workflow checks the schedule every 15 minutes.

## Files

| File | Purpose |
|---|---|
| `quiz_bot.py` | The script |
| `quizzes/ows001.json`, `quizzes/idm001.json` | Quiz sets (one file per set) |
| `schedule.json` | Which set is posted when |
| `sent.json` | Record of what was already posted (updated automatically) |
| `.github/workflows/quiz.yml` | Runs the script every 15 minutes, or manually |
| `requirements.txt`, `.env.example`, `.gitignore` | Dependencies, local config template, ignore rules |

## Quiz sets

Naming: `ows001`, `ows002`, ... (one word substitution), `idm001`, `idm002`, ... (idioms), and so on. Use letters, digits, `-` and `_` only; lowercase is recommended.

To add a set, create `quizzes/<id>.json`. It is a JSON list; each question is `[question, [options], correct_index]`, and the index starts at 0:

```json
[
  ["One who does not believe in the existence of God", ["Theist", "Atheist", "Optimist", "Cynic"], 1],
  ["A person who loves and collects books", ["Philatelist", "Numismatist", "Bibliophile", "Oenophile"], 2]
]
```

Limits (from Telegram): question up to 300 characters, 2 to 10 options, each up to 100 characters. Invalid questions are skipped with a warning; the rest still post.

## Schedule

`schedule.json` is a list of entries:

```json
[
  { "quiz": "ows001", "at": "2026-10-19T09:00Z" },
  { "quiz": "idm001", "at": "2026-10-20T14:30+05:30" }
]
```

- `Z` means UTC. You can also write an offset such as `+05:30`. A time with no zone is treated as UTC.
- A set is posted by the first run at or after its time, and only once.

| I want to... | Do this |
|---|---|
| Reschedule a set that has not been posted | Edit its `at` value and commit |
| Cancel a set | Delete its entry |
| Post a set again | Add a new entry with a new time (or delete its line from `sent.json`) |
| Post a set right now | Actions tab -> Quiz Scheduler -> Run workflow -> type the id (e.g. `ows001`) |

You can edit files on GitHub in the browser (open the file, click the pencil icon, commit). The new schedule applies from the next run. A manual run does not change `sent.json`.

## 1. Telegram setup

1. Your bot is **@PYQxBot**. The bot token comes from **@BotFather** (`/mybots` -> PYQxBot -> API Token).
2. Add @PYQxBot to your channel or group. **In channels (and groups where only admins can post), make it an administrator** that is allowed to post.
3. Find the chat ID: use `@yourchannelname` for a public channel. Otherwise send a message in the chat, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and read `chat.id` (groups and channels are negative, e.g. `-1001234567890`).

## 2. Create the repository (browser only, no git needed)

1. On github.com choose **New repository**, give it a name, tick **Add a README file**, and click **Create repository**.
2. Choose **Add file -> Upload files** and drag in `quiz_bot.py`, `requirements.txt`, `schedule.json`, `sent.json` and the `quizzes` folder. Click **Commit changes**. (`.gitignore` and `.env.example` are only needed for running locally; skip them.)
3. Choose **Add file -> Create new file**. In the name box type `.github/workflows/quiz.yml` (each `/` creates a folder), paste the contents of `quiz.yml`, and click **Commit changes**.

On a phone you cannot upload folders. Use **Create new file** with the names `quizzes/ows001.json` and `quizzes/idm001.json` and paste each file's contents.

Nothing is ever pushed from your computer. The workflow saves `sent.json` itself.

## 3. Add the secrets

Repository -> **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**:

| Name | Value |
|---|---|
| `BOT_TOKEN` | the token from @BotFather |
| `CHAT_ID` | your chat ID or `@channelname` |

## 4. Enable the workflow

1. Open the **Actions** tab. If asked, click **"I understand my workflows, go ahead and enable them"**.
2. Select **Quiz Scheduler**. If it shows as disabled, click **Enable workflow**.
3. Also check Settings -> Actions -> General -> **Workflow permissions** is set to **Read and write permissions** (needed to save `sent.json`).
4. Test: **Run workflow**, type `ows001` in the field, and run it. The 10 polls should appear in your chat (about 5 minutes, because of the 30-second gap).

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python quiz_bot.py --dry-run          # checks all files, sends nothing, needs no token

cp .env.example .env                  # then edit .env
set -a; source .env; set +a
python quiz_bot.py --quiz ows001      # post one set now
python quiz_bot.py                    # post whatever is due (updates sent.json)
```

## Good to know

- **Timing is approximate.** GitHub often starts scheduled runs 5 to 20 minutes late, so a quiz set for 09:00 may appear a little later.
- **Usage limits.** Public repositories get free Actions minutes. Private repositories get a monthly allowance (typically 2,000 minutes on the Free plan; check your plan), and every run is billed at least 1 minute. Running every 15 minutes is about 2,900 minutes a month, which is too much for that allowance. In a private repo, change the cron line in `quiz.yml` to `*/30 * * * *` (about 1,450 minutes) or limit the hours, e.g. `*/15 6-18 * * *`.
- **Partial failures.** If a set posts some polls but not all, it is still recorded as sent so it is not duplicated; the run is marked as failed. If nothing could be posted, it is not recorded and is retried on the next run.
- **Keep sets short and spread out.** Each poll takes 30 seconds, so a 10-question set takes about 5 minutes. Avoid scheduling many sets at the same time.
- **Pausing.** In public repositories GitHub can pause scheduled workflows after 60 days without repository activity; re-enable them in the Actions tab.
