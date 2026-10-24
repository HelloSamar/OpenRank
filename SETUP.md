# OpenRank - setup and operations guide

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
| `responder.py` | Answers the bot's commands (`/start`, `/quizlists`, `/contact`) |
| `sent.json` | What was posted, with the question list of each quiz (automatic) |
| `stats.json` | How each question did and when it was last asked (automatic) |
| `scores.json` | Weekly totals per person, under an anonymous key (automatic) |
| `alerts.json` | When each problem was last reported to you, so you are not told every 15 minutes (automatic) |
| `.github/workflows/quiz.yml` | Checks every 15 minutes (at :07, :22, :37 and :52) and posts what is due, or posts a quiz manually |
| `.github/workflows/rename-quiz.yml` | The safe way to rename a quiz (button in the Actions tab) |
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

Create `quizzes/Quiz Name_ows.json` (see the naming rule below). Each question is `[question, [options], correct_index, "explanation"]`, and the index starts at 0. The explanation is optional.

```json
[
  ["One who does not believe in the existence of God", ["Theist", "Atheist", "Optimist", "Cynic"], 1,
   "Atheist = one who does not believe in God. A theist believes in God."]
]
```

- The explanation appears when someone picks a wrong answer or taps the lamp icon on the poll.
- Limits: question up to 288 characters (room is kept for the `Q12/100:` prefix), 2 to 10 options of up to 100 characters, explanation up to 200 characters with at most 2 line breaks.
- A question that is invalid, or repeats an earlier one in the same file (same question and same answer), is skipped with a warning. The validation check also shows it as a problem.
- **Naming: `Quiz Name_prefix.json`**, for example `Synonyms Set 1_ows.json` or `Idioms_idm.json`. The quiz name comes first and may contain spaces and special characters (letters of any language, `&`, `(`, `%` ...) but should not contain an underscore; after the underscore comes the prefix, which decides the usual speed (see `TIMER_BY_PREFIX` below): `ows`, `idm`, `rsn`, `mth`, `cmp`. The part after the last underscore is the prefix. People see only the name, exactly as written: `/quizlists` shows `Synonyms Set 1`, and the group's messages say `Synonyms Set 1 - Final Result`. A file name without an underscore, such as `ows000`, still works: it is shown whole and its leading letters are the prefix. Not allowed in a name: `/`, `\`, line breaks, a space or dot at the start, a space at the end, more than 100 characters. Tip: on Windows and many tools the characters `: * ? " < > |` cannot be used in file names, so avoid them. Also avoid look-alike characters (a non-breaking hyphen `‑` looks like `-` but is a different character): the name in `schedule.json` must match the file name exactly.
- **Renaming a quiz file changes its ID** (the whole file name without `.json`), and the ID is used in `schedule.json` and `sent.json`. **Do not rename by hand: use the Rename quiz workflow** (Actions -> Rename quiz -> Run workflow, see "Safety nets" below). It changes the file and all references in one commit. If you did rename a file by hand, run the workflow anyway: it then only updates the references.

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
| `MAX_LATE_HOURS` | 12 | A quiz later than this is skipped, not posted (0 = never skip) |
| `SCHEDULE_WARN_DAYS` | 14 | You are alerted when fewer days of quizzes are scheduled |
| `ALERT_REPEAT_HOURS` | 6 | A problem that is still there is repeated this often |
| `PIN_WEEKLY_LEADERBOARD` | False | Pin the weekly leaderboard (the bot needs the pin right) |

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
| `scores.json` | Weekly totals per person: Telegram name and an anonymous key (a keyed hash; the Telegram ID is not stored) | Last 8 weeks |

- A quiz in `sent.json` is found by its ID and time, for example `Idioms_idm@2026-10-20T09:00Z`.
- Both grow slowly, roughly 1 KB per 10 questions asked. Manual runs are not recorded.
- **Question statistics:** the rate is right / (right + wrong) among people who answered at least once; skipped answers are counted separately. A new question counts as 50%, and the rate is pulled towards 50% while there is little data, so reliable numbers need several quizzes with real participants. They need polls that show names, which means a group. Editing a question or its correct answer starts its numbers from zero. `python quiz_bot.py --dry-run` lists the five questions with the lowest rate.
- `scores.json` holds names and IDs. In a **public** repository anyone can read it, so make the repository private before you have real members. (`stats.json` has no personal data.)

## Commands: /start, /quizlists, /contact

The bot answers three commands. It has no always-on server: every 15-minute check (see `quiz.yml`) also reads the waiting messages and replies, so **an answer takes up to about 15 minutes** (longer while a quiz is being posted, because the check waits for it).

| Command | What it does |
|---|---|
| `/start` | Asks the person to join the group; the words "join our group" are a hyperlink to `https://t.me/OpenRank`. To use another group, change `GROUP_URL` at the top of `responder.py`. |
| `/quizlists` | One line per stored quiz with only its name and number of questions, nothing else. The prefix after the underscore (`_ows`, `_idm`, ...) is not shown; names appear exactly as written in the file name, sorted alphabetically. |
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

## Safety nets: alerts, late quizzes, renaming, test chat

**Alerts.** The bot sends you a Telegram message (to `OWNER_CHAT_ID`; you must have pressed START on the bot) when something needs you: a due quiz cannot be loaded (for example a name in `schedule.json` does not match a file) or nothing could be posted; a quiz was only partly posted; the weekly leaderboard failed; `schedule.json` is broken; fewer than 14 days of quizzes are left (once a day, `SCHEDULE_WARN_DAYS`); the record of posted quizzes could not be saved; or late quizzes were skipped. A problem that is still there is repeated at most every 6 hours (`ALERT_REPEAT_HOURS`). `alerts.json` remembers what was sent. If a name only differs from a file name by letter case or a look-alike dash, the message tells you which file it found.

**Late quizzes.** A quiz that is more than 12 hours late (`MAX_LATE_HOURS`) is skipped, not posted, and you are told. This stops a burst of old quizzes after an outage or a rename mistake. It is recorded in `sent.json` with `"late": true`, so it does not come back. To post one anyway: Actions -> Quiz Scheduler -> Run workflow, and type its name. Set `MAX_LATE_HOURS = 0` to switch the limit off.

**Renaming a quiz.** Actions -> **Rename quiz** -> Run workflow. *old* is the current name (as `schedule.json` spells it), *new* is the new name (`Quiz Name_prefix`). It renames the file and updates `schedule.json`, `sent.json`, and `stats.json` in a single commit, so nothing is posted twice. Tick *preview* to see what would change first. Never rename by hand.

**Test chat.** Make a private test group, add the bot, and add its ID (it starts with `-100`) as the secret `TEST_CHAT_ID`. Then Actions -> Quiz Scheduler -> Run workflow, type a quiz name and tick *test_chat*: the quiz goes only to the test group and nothing is recorded (no sent, score or statistics change). Use it before trusting any change.

**Anonymous scores.** `scores.json` no longer stores Telegram IDs. Each person is stored under a keyed hash; names stay (they are visible in the group anyway). Old entries are converted at the first run. Add a secret `SCORES_SALT` (any long random text) and never change it; without it the bot token is used, and a new token would start everyone on a fresh row. Note: older versions of `scores.json` remain in the repository history. If the repository is public, anyone can still read its contents; removing them entirely means making the repository private or recreating it.

**Weekly pin.** `PIN_WEEKLY_LEADERBOARD = True` pins the weekly leaderboard after it is posted. The bot needs the *Pin messages* right in the group. It is off by default; a refused pin is only logged.

**What the validation check looks for:** missing or invalid quiz files, quiz files whose names differ only by upper/lower case (a problem), look-alike characters in names such as a non-breaking hyphen (a note), entries that would be skipped as late, and how many days the schedule still covers.

**Making changes safely.** Change one thing at a time. When you upload files, choose *Create a new branch and start a pull request* in GitHub's commit box and merge only when the validation check is green; the bot keeps running on `main` whatever state it is in. Try new quizzes in the test chat first. Treat quiz names as permanent once they are scheduled.

## Telegram

1. The bot is **OpenRank**. Add it to your quiz **group**.
2. `CHAT_ID` is the group's numeric ID (negative, like `-1001234567890`).
3. Do not open `getUpdates` in a browser while a quiz is running, and do not set a webhook on this bot unless you also set `WEBHOOK_ENABLED=true` (then Telegram must post updates to `WEBHOOK_PORT`, optionally protected by `WEBHOOK_SECRET`). Otherwise, only one program can read updates at a time, and a clash switches scoring off for that quiz.
4. The script asks Telegram for poll answers and for messages (the commands below), and Telegram remembers that. To look up a chat ID later with `getUpdates`, add `&allowed_updates=[]` to the URL.

## GitHub (browser only)

1. Create the repository and upload the files (Add file -> Upload files). Create the two workflow files with Add file -> Create new file, named `.github/workflows/quiz.yml` and `.github/workflows/validate.yml`.
2. Settings -> Secrets and variables -> Actions: add `BOT_TOKEN` and `CHAT_ID`.
3. Settings -> Actions -> General -> Workflow permissions: **Read and write**.
4. Actions tab: enable workflows, then Run workflow once to test.
5. To update a file: open it, click the pencil, select all, paste the new version, commit. Uploading a file with the same name also replaces it.

## Good to know

- A quiz starts at the first 15-minute check after its time, and GitHub can add 5 to 20 minutes of delay. A time that has already passed runs at the next check.
- Public repositories have free Actions minutes. Private ones have a monthly allowance; if you make the repository private, check fewer times, for example `7,37 * * * *` in the cron line of `quiz.yml`.
- A quiz takes about (questions x seconds each); the workflow stops a run after 350 minutes. Private repositories charge per minute, so very long quizzes use up the monthly allowance.
- **Interrupted quizzes:** if a run is cancelled (SIGINT/SIGTERM) the bot stops posting, posts the partial result and does not start further quizzes. A quiz that had at least one poll posted is recorded in `sent.json` and is not posted again; delete its entry in `sent.json` to run it again.
- If a quiz is only partly posted it is still recorded as sent so it is not duplicated. If nothing posted, it is retried on the next run.
- If Telegram creates a poll but the answer is lost on the way, the retry can post that one question twice. Telegram offers no way to prevent this.
