# PYQxBot - setup and operations guide

(Your own `README.md` stays as the public description. This file is the technical guide.)

## How it works

Questions live in `quizzes/<id>.json`. `schedule.json` says which quiz goes out and when. A GitHub Actions workflow wakes every 15 minutes, posts every quiz that is due and records it in `sent.json`.

Each question is a poll with a countdown; the next poll appears when the previous one closes, so the gap between polls equals the seconds of the question (plus about 1 second of network time). After the last poll the bot posts the result, adds the marks to the weekly total, and every Sunday at 7:00 PM IST posts the weekly leaderboard.

## Files

| File | Purpose |
|---|---|
| `schedule.json` | Which quiz runs when, how many questions, how many seconds. Plain JSON (no comments); fields are listed below |
| `quizzes/*.json` | The questions, one file per quiz |
| `quiz_bot.py` | The script and its settings |
| `responder.py` | Answers the bot's commands (`/start`, `/quizlists`, `/request`, `/contact`) |
| `sent.json` | What was posted, with the question list of each quiz (automatic) |
| `stats.json` | How each question did and when it was last asked (automatic) |
| `scores.json` | Weekly totals per person (automatic) |
| `.github/workflows/quiz.yml` | Posts quizzes every 15 minutes, or manually |
| `.github/workflows/validate.yml` | Checks your files whenever you edit them |

## schedule.json

Everything about a run is set here. The file must be **strict JSON** (no `//` comments, no trailing commas). Edit it on GitHub: open the file, click the pencil, change a line, commit.

Fields of each entry: `quiz` (file name without `.json`), `at` (e.g. `"2026-10-19T12:00+05:30"` or `...Z`), and optionally `timer` (5-600 seconds), `pick` (how many questions to choose; 0 or missing = all), `strategy` (`"random"` or `"hardest"`, needs `pick`), `no_repeat_days` (0-3650, needs `pick`) and `intro` (text posted before the first poll).

| I want to... | Do this |
|---|---|
| Reschedule a quiz that has not run | Change its `at` |
| Cancel a quiz | Delete its line |
| Run a quiz again | Add a new line with a new time |
| Run a quiz right now | Actions -> Quiz Scheduler -> Run workflow -> type the quiz name. This posts the whole quiz and records nothing. |
| See the current weekly standings | Same, but type `weekly` |

Every edit is checked by `validate.yml`: a green tick means fine, a red cross means something is wrong (a missing comma, a bad value, a missing quiz file). Open the failed run to see the exact line. The same check lists the speed, question count and repeat rule of every entry, and any quiz file that is not in the schedule yet.

## Quiz files

Create `quizzes/<name>.json` (letters, digits, `-`, `_`). Each question is `[question, [options], correct_index, "explanation"]`, and the index starts at 0. The explanation is optional.

```json
[
  ["One who does not believe in the existence of God", ["Theist", "Atheist", "Optimist", "Cynic"], 1,
   "Atheist = one who does not believe in God. A theist believes in God."]
]
```

- The explanation appears when someone picks a wrong answer or taps the lamp icon on the poll.
- Limits: question up to 288 characters (room is kept for the `Q12/100:` prefix), 2 to 10 options of up to 100 characters, explanation up to 200 characters with at most 2 line breaks.
- A question that is invalid, or repeats an earlier one in the same file (same question and same answer), is skipped with a warning. The validation check also shows it as a problem.
- Name quizzes `prefix_quizname`, for example `ows_synonyms` or `idm_idioms_set1`. The letters before the first underscore decide the usual speed (see `TIMER_BY_PREFIX` below): `ows`, `idm`, `rsn`, `mth`, `cmp`. People see only the name after the prefix, with underscores shown as spaces (`/quizlists` shows `idioms set1`). A name without that pattern, such as `ows000`, is shown whole.
- **Renaming a quiz file changes its ID, and the ID is used in `schedule.json` and in `sent.json`.** If you rename a file and only update `schedule.json`, every earlier entry of that quiz looks as if it was never posted, and the next check posts all of them at once. When you rename a quiz, change the old ID to the new one in all three places: the file name, the `"quiz"` lines in `schedule.json`, and the keys in `sent.json` (they look like `ows000@2026-10-19T06:30Z`; change only the part before the `@`). Then run the validation check: no past entry should be listed as due.

## Settings in quiz_bot.py

Only settings that are not in `schedule.json`. To change one: open `quiz_bot.py` on GitHub, click the pencil, edit the value, commit.

| Setting | Default | Meaning |
|---|---|---|
| `TIMER_BY_PREFIX` | ows 10, idm 15, rsn 25, mth 60 | Usual seconds per question, by the letters at the start of the quiz name. Used only when a schedule line has no `timer`. Add a type with a line like `"syn": 10,` |
| `POLL_TIMER_SECONDS` | 15 | Usual seconds for any other quiz name |
| `POLL_GAP_SECONDS` | 0 | Extra pause after a poll closes, before the next opens |
| `DELAY_BETWEEN_POLLS` | 15 | Pause between two quizzes in the same run |
| `REPEAT_COOLDOWN_DAYS` | 7 | Usual "do not repeat" days when a schedule line has no `no_repeat_days` |
| `ANONYMOUS_POLLS` | False | False shows who answered (groups only) |
| `SHUFFLE_QUESTIONS` | True | New random order every time |
| `SHOW_QUESTION_NUMBERS` | True | Each poll starts with `Q3/10:` |
| `POST_RESULTS` | True | Post the final result after each quiz |
| `MARKS_CORRECT` / `MARKS_WRONG` | 2 / 0.5 | +2 right, -0.5 wrong, 0 skipped |
| `SHOW_ANSWER_KEY` | False | Also list the correct answers in the result |
| `POST_WEEKLY` | True | Post the weekly leaderboard |
| `WEEK_RESET_DAY/HOUR/MINUTE` | Sunday 20:00 | When weekly scores reset (India time) |
| `WEEKLY_POST_HOURS_BEFORE_RESET` | 1 | Leaderboard goes out at 7:00 PM |

## Results and the weekly leaderboard

```
🏆 OWS001 - Final Result

🥇 Asha: 17.5/20
   ✅ 9  ❌ 1  ⏭ 0  ⏱ 1 min 12 sec
```

- Ranked by marks, then by shorter total time. Time is accurate to about a second, and a skipped question counts as its full seconds.
- Names and scores need **polls that show names, which Telegram allows in groups but not in channels**. Telegram never tells a bot who voted in an anonymous poll. In a channel the bot switches to anonymous polls automatically (a warning appears in the log) and the end message shows the answer key instead.
- Weekly marks are added up from **Sunday 8:00 PM IST to the next Sunday 8:00 PM IST**. The leaderboard is posted on Sunday at 7:00 PM IST, and after 8:00 PM everyone starts again from zero. A late GitHub run still posts it within 12 hours.
- A quiz belongs to the week of its **scheduled time**, so avoid scheduling one between 7:00 and 8:00 PM on Sunday: it counts for the old week after that week's leaderboard is out.
- Manual runs do not count towards weekly totals.

## What is remembered, and where

Nothing expires, except `scores.json`:

| File | What it remembers | For how long |
|---|---|---|
| `sent.json` | Each scheduled quiz: when it was posted and **its question list** in posted order (id plus the first 60 characters) | Forever |
| `stats.json` | Each question: times asked, right, wrong, skipped, and when it was last asked | Forever |
| `scores.json` | Weekly totals per person (Telegram name and user ID) | Last 8 weeks |

- A quiz in `sent.json` is found by its name and time, for example `ows000@2026-10-20T09:00Z`.
- Both grow slowly, roughly 1 KB per 10 questions asked. Manual runs are not recorded.
- **Question statistics:** the rate is right / (right + wrong) among people who answered at least once; skipped answers are counted separately. A new question counts as 50%, and the rate is pulled towards 50% while there is little data, so reliable numbers need several quizzes with real participants. They need polls that show names, which means a group. Editing a question or its correct answer starts its numbers from zero. `python quiz_bot.py --dry-run` lists the five questions with the lowest rate.
- `scores.json` holds names and IDs. In a **public** repository anyone can read it, so make the repository private before you have real members. (`stats.json` has no personal data.)

## Commands: /start, /quizlists, /contact

The bot answers three commands. It has no always-on server: every 15-minute check (see `quiz.yml`) also reads the waiting messages and replies, so **an answer takes up to about 15 minutes** (longer while a quiz is being posted, because the check waits for it).

| Command | What it does |
|---|---|
| `/start` | Asks the person to join the group; the words "join our group" are a hyperlink to `https://t.me/GovtScore`. To use another group, change `GROUP_URL` at the top of `responder.py`. |
| `/quizlists` | One line per stored quiz with only its name and number of questions, nothing else. The letters before the first underscore (`ows_`, `idm_`, ...) are not shown. |
| `/contact` | For asking for a quiz on an exam topic or subject, or for anything else. It shows a button that opens a direct chat with the owner. The person can also send `/contact your message`, which is passed on to the owner. (`/request` still works as another name for it.) |

- **Set up (once):** (1) In @BotFather send `/setcommands`, choose the bot and paste the three lines below (this replaces the old list). (2) Open the bot yourself and press **START**; Telegram only lets a bot message you after that. (3) Add your numeric Telegram ID (for example from @userinfobot) as the **secret** `OWNER_CHAT_ID`, and your username without the `@` as the **variable** `OWNER_USERNAME` (Settings -> Secrets and variables -> Actions; secrets and variables are separate tabs). `OWNER_CHAT_ID` is how messages reach you; `OWNER_USERNAME` is only for the direct-chat button. Without the secret, `/contact` can only offer the direct chat; without the variable, it asks the person to reply with their message instead of showing the button; without both it says it is not available.

```
start - Join our group
quizlists - See all quizzes and how many questions each has
contact - Request a quiz or chat with the owner directly
```

- **Answering people:** a message sent through `/contact` reaches you in the bot's chat with the person's name, username and ID. Reply to that message (use Reply) and your answer is passed on to the person. Only your replies are passed on.
- **In the group:** the bot does not answer commands there; it points the person to a private chat with the bot, once per run.
- **Limits:** at most 5 commands per person per run, messages up to 1000 characters. Only messages that are commands, replies or sent in a private chat are read. Nothing is stored: handled messages are confirmed to Telegram and dropped.
- **Turn it off:** in Settings -> Secrets and variables -> Actions -> **Variables**, add a variable named `DISABLE_COMMANDS` with the value `true`. Quiz posting and scoring are never affected by this feature: a problem in the command handling is only logged.
- Messages sent before this feature was switched on are not delivered; Telegram keeps unanswered messages for about 24 hours.

## Telegram

1. The bot is **@PYQxBot**. Add it to your quiz **group**.
2. `CHAT_ID` is the group's numeric ID (negative, like `-1001234567890`).
3. Do not open `getUpdates` in a browser while a quiz is running, and do not set a webhook on this bot unless you also set `WEBHOOK_ENABLED=true` (then Telegram must post updates to `WEBHOOK_PORT`, optionally protected by `WEBHOOK_SECRET`). Otherwise only one program can read updates at a time, and a clash switches scoring off for that quiz.
4. The script asks Telegram for poll answers and for messages (the commands below), and Telegram remembers that. To look up a chat ID later with `getUpdates`, add `&allowed_updates=[]` to the URL.

## GitHub (browser only)

1. Create the repository and upload the files (Add file -> Upload files). Create the two workflow files with Add file -> Create new file, named `.github/workflows/quiz.yml` and `.github/workflows/validate.yml`.
2. Settings -> Secrets and variables -> Actions: add `BOT_TOKEN` and `CHAT_ID`.
3. Settings -> Actions -> General -> Workflow permissions: **Read and write**.
4. Actions tab: enable workflows, then Run workflow once to test.
5. To update a file: open it, click the pencil, select all, paste the new version, commit. Uploading a file with the same name also replaces it.

## Good to know

- A quiz starts at the first 15-minute check after its time, and GitHub can add 5 to 20 minutes of delay. A time that has already passed runs at the next check.
- Public repositories have free Actions minutes. Private ones have a monthly allowance; if you make the repository private, use `*/30` in the cron line of `quiz.yml`.
- A quiz takes about (questions x seconds each); the workflow stops a run after 350 minutes. Private repositories pay for every minute, so very long quizzes use up the monthly allowance.
- **Interrupted quizzes:** if a run is cancelled (SIGINT/SIGTERM) the bot stops posting, posts the partial result and does not start further quizzes. A quiz that had at least one poll posted is recorded in `sent.json` and is not posted again; delete its entry in `sent.json` to run it again.
- If a quiz is only partly posted it is still recorded as sent so it is not duplicated. If nothing posted, it is retried on the next run.
- If Telegram creates a poll but the answer is lost on the way, the retry can post that one question twice. Telegram offers no way to prevent this.
