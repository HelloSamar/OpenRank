# PYQxBot - setup and operations guide

(Your own `README.md` stays as the public description. This file is the technical guide.)

## How it works

Quiz sets live in `quizzes/<id>.json`. `schedule.json` says which set goes out and when. A GitHub Actions workflow wakes every 15 minutes, posts every set that is due and not yet posted, and records it in `sent.json`.

Each poll counts down from 15 seconds and closes. After the last poll closes, the bot posts the final result. The marks are also added to a weekly total (`scores.json`), and the weekly leaderboard is posted every Sunday at 7:00 PM IST.

## Files

| File | Purpose |
|---|---|
| `quiz_bot.py` | The script and its settings |
| `quizzes/*.json` | Quiz sets (one file per set) |
| `schedule.json` | Which set is posted when |
| `sent.json` | Record of what was posted (updated automatically) |
| `scores.json` | Weekly totals (created and updated automatically) |
| `stats.json` | How often each question was answered right or wrong, and when it was last asked (created automatically) |
| `.github/workflows/quiz.yml` | Posts quizzes every 15 minutes, or manually |
| `.github/workflows/validate.yml` | Checks your files whenever you edit them |

## Settings (top of `quiz_bot.py`)

| Setting | Default | Meaning |
|---|---|---|
| `TIMER_BY_PREFIX` | ows 10, idm 15, rsn 25, mth 60 | Countdown by quiz type (the letters at the start of the quiz id) |
| `POLL_TIMER_SECONDS` | 15 | Countdown for any other quiz type (5-600, 0 = none) |
| `POLL_GAP_SECONDS` | 0 | Extra pause after a poll closes, before the next one |
| `DELAY_BETWEEN_POLLS` | 15 | Pause between two quiz sets in one run |
| `REPEAT_COOLDOWN_DAYS` | 7 | A picked question will not repeat within this many days |
| `ANONYMOUS_POLLS` | False | False shows who answered (groups only) |
| `SHUFFLE_QUESTIONS` | True | New random order every time a set is posted |
| `SHOW_QUESTION_NUMBERS` | True | Each poll starts with `Q3/10:` |
| `POST_RESULTS` | True | Post the final result after each set |
| `MARKS_CORRECT` / `MARKS_WRONG` | 2 / 0.5 | +2 for right, -0.5 for wrong, 0 for skipped |
| `SHOW_ANSWER_KEY` | False | Also list the correct answers in the result |
| `POST_WEEKLY` | True | Post the weekly leaderboard |
| `WEEK_RESET_DAY/HOUR/MINUTE` | Sunday 20:00 | When the weekly scores reset (India time) |
| `WEEKLY_POST_HOURS_BEFORE_RESET` | 1 | Leaderboard goes out at 7:00 PM |

To change one: open `quiz_bot.py` on GitHub, click the pencil icon, edit the value, commit.

## Quiz sets and explanations

Create `quizzes/<id>.json` (letters, digits, `-`, `_`). Each question is `[question, [options], correct_index, "explanation"]`, and the index starts at 0. The explanation is optional:

```json
[
  ["One who does not believe in the existence of God", ["Theist", "Atheist", "Optimist", "Cynic"], 1,
   "Atheist = one who does not believe in God. A theist believes in God."]
]
```

- The explanation appears when someone picks a wrong answer or taps the lamp icon on the poll.
- Limits: question up to 288 characters (room is kept for the `Q12/150:` prefix), 2 to 10 options each up to 100 characters, explanation up to 200 characters with at most 2 line breaks.
- Invalid questions are skipped with a warning; the rest still post.

## Countdown per quiz type

The countdown comes from the **letters at the start of the quiz id**:

| Id starts with | Type | Countdown |
|---|---|---|
| `ows` | One word substitution | 10 seconds |
| `idm` | Idioms | 15 seconds |
| `rsn` | Reasoning | 25 seconds |
| `mth` | Maths | 60 seconds |
| anything else (e.g. `syn`) | | 15 seconds (`POLL_TIMER_SECONDS`) |

- Name your files `rsn001.json` and `mth001.json` to get the 25 and 60 second timers.
- To add a type, add a line to `TIMER_BY_PREFIX` in `quiz_bot.py`, for example `"syn": 10,`.
- One quiz can use its own countdown in the schedule: `{ "quiz": "mth002", "at": "...", "timer": 90 }` (5 to 600 seconds).
- The next poll appears when the previous one closes. A set takes about (number of questions x countdown): 10 reasoning questions take about 4 minutes, 10 maths questions about 10 minutes.

## Schedule

```json
[
  { "quiz": "ows001", "at": "2026-10-17T17:15+05:30" },
  { "quiz": "idm001", "at": "2026-10-20T16:45+05:30" }
]
```

- `Z` means UTC; `+05:30` is India time. A time with no zone is read as UTC.
- **Every entry except the last needs a comma after the `}`.** A missing comma makes the whole file invalid and nothing posts.
- A set whose time is already in the past and which is not in `sent.json` posts at the next check.
- A set posts once. How long it takes depends on its countdown (see above).

| I want to... | Do this |
|---|---|
| Reschedule a set that has not posted | Edit its `at` value |
| Cancel a set | Delete its line (watch the commas) |
| Post a set again | Add a new entry with a new time |
| Post a set right now | Actions -> Quiz Scheduler -> Run workflow -> type the id (this changes nothing in `sent.json` or `scores.json`) |

## Picking questions from a big set

Keep one big file (for example `ows000.json` with hundreds of questions) and let the schedule choose from it:

```json
{ "quiz": "ows000", "at": "2026-10-21T09:00+05:30", "pick": 100, "strategy": "hardest" }
```

| Field | Meaning |
|---|---|
| `pick` | How many questions to use. Leave it out to post the whole file. |
| `strategy` | `hardest` = the questions people get right least often. `random` = any questions. If you give `pick` alone, `random` is used. |

- A question asked in the last 7 days is left out (`REPEAT_COOLDOWN_DAYS`), so the same hard questions do not come back every day. If the file is too small, the least recently asked fill the gap.
- 100 questions with a 10 second countdown takes about 20 minutes. Many people prefer 10 to 20 questions per quiz.
- The questions are still shuffled and numbered `Q1/100:` to `Q100/100:`.

## Question statistics

After every scheduled quiz the script adds to `stats.json`, for each question: how many people got it right, wrong or skipped, how many times it was asked, and when it was last asked. You can open the file to read it. `python quiz_bot.py --dry-run` also lists the five questions with the lowest right-answer rate.

- **Only people who answered at least once in that quiz count.** The rate is right / (right + wrong). Skipped answers are counted separately and are not treated as wrong.
- **A new question counts as 50%.** The rate is pulled towards 50% while there is little data, so one wrong answer does not make a question look hard. Reliable numbers need several quizzes with real participants.
- **This needs polls that show names, which means a group.** In a channel, Telegram hides who answered, so only "asked" and "last asked" are recorded.
- **Editing a question or its correct answer starts its statistics from zero**, because the old numbers no longer describe it. Fixing a typo in the explanation or in a wrong option does not.
- Manual runs do not change `stats.json`.

## Result message and scores

```
🏆 OWS001 - Final Result

🥇 Asha: 17.5/20
   ✅ 9  ❌ 1  ⏭ 0  ⏱ 1 min 12 sec
```

- Ranked by marks, then by shorter total time. Time taken is accurate to about a second, and a skipped question counts as the full 15 seconds.
- Names and scores need **non-anonymous polls, which Telegram allows in groups but not in channels**. Telegram never tells a bot who voted in an anonymous poll, so a channel cannot show personal scores. There the bot switches to anonymous polls automatically (a warning appears in the log) and the end message shows the answer key instead.

## Weekly leaderboard

- Marks from every scheduled quiz are added up from **Sunday 8:00 PM IST to the next Sunday 8:00 PM IST**.
- The leaderboard is posted on **Sunday at 7:00 PM IST**. After 8:00 PM everyone starts again from zero.
- A quiz belongs to the week of its **scheduled time**. Avoid scheduling quizzes between 7:00 and 8:00 PM on Sunday, because they count for the old week after its leaderboard is already out.
- If GitHub runs late, the leaderboard is still posted up to 12 hours after 7:00 PM.
- Manual runs do not count towards the weekly totals.
- To see the current standings at any time: Actions -> Run workflow -> type `weekly`. This posts them without recording anything or resetting.
- `scores.json` keeps the last 8 weeks. Do not edit it by hand unless you know the format.

## File checks

`validate.yml` runs `python quiz_bot.py --dry-run` whenever you change `quizzes/`, `schedule.json` or `quiz_bot.py`. A red cross on the commit means something is wrong, such as a missing comma, a bad question, or a scheduled quiz whose file is missing. Open the failed run to see the exact line. It also lists quiz files that are not in the schedule yet.

## Telegram

1. The bot is **@PYQxBot**. Add it to your quiz **group**.
2. `CHAT_ID` is the group's numeric ID (negative, like `-1001234567890`).
3. Do not open `getUpdates` in a browser while a quiz is running. Only one program can read updates at a time, and a clash switches scoring off for that set.
4. Do not set a webhook on this bot. It stops scoring.
5. The script asks Telegram only for poll answers, and Telegram remembers that. To look up a chat ID later with `getUpdates`, add `&allowed_updates=[]` to the URL.

## GitHub (browser only)

1. Create the repository and upload the files (Add file -> Upload files). Create the two workflow files with Add file -> Create new file, named `.github/workflows/quiz.yml` and `.github/workflows/validate.yml`.
2. Settings -> Secrets and variables -> Actions: add `BOT_TOKEN` and `CHAT_ID`.
3. Settings -> Actions -> General -> Workflow permissions: **Read and write**.
4. Actions tab: enable workflows, then Run workflow once to test.
5. To update a file: open it, click the pencil, select all, paste the new version, commit.

## Good to know

- Posting starts at the first 15-minute check after the scheduled time, and GitHub can add 5 to 20 minutes of delay.
- Public repositories have free Actions minutes. Private ones have a monthly allowance; if you make the repo private, use `*/30` in the cron line of `quiz.yml`.
- The job stops after 60 minutes (`timeout-minutes`). A set is recorded only when it finishes, so raise that number if you schedule very long sets.
- `scores.json` stores Telegram names and user IDs for the last 8 weeks. In a **public** repository anyone can read them, so make the repository private before you have real members. (`stats.json` has no personal data.)
- If a set is only partly posted it is still recorded as sent so it is not duplicated. If nothing posted, it is retried on the next run.
