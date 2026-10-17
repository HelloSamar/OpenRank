#!/usr/bin/env python3
"""
Scheduled Telegram Quiz Poll poster.

Quiz sets live in quizzes/<id>.json (e.g. quizzes/ows001.json). The file
schedule.json says which set goes out at which time. Each run of this script
posts every set that is due and not yet posted, then records it in sent.json.

Every poll shows a countdown (POLL_TIMER_SECONDS) and closes when it ends.
After the last poll closes, a result message is posted: a leaderboard with marks
(+2 right, -0.5 wrong), right/wrong/skipped counts and time taken. This needs
non-anonymous polls, which Telegram allows in groups but not in channels.

Usage:
    python quiz_bot.py                  post everything that is due
    python quiz_bot.py --quiz ows001    post one set right now (ignores schedule)
    python quiz_bot.py --dry-run        validate files and show status, send nothing

Configuration (environment variables only - nothing is hardcoded):
    BOT_TOKEN  Telegram bot token from @BotFather.
    CHAT_ID    Target chat: numeric ID (e.g. -1001234567890) or @channelusername.

Exit codes:
    0  success (or nothing was due)
    1  at least one poll/quiz failed, or a dry run found problems
    2  configuration or data-file error
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

TELEGRAM_API_BASE = "https://api.telegram.org"

DELAY_BETWEEN_POLLS = 15        # seconds to wait between polls
MAX_ATTEMPTS = 3                # total tries per request (1 initial + 2 retries)
BACKOFF_BASE = 2                # retry delay = BACKOFF_BASE ** attempt seconds
MAX_RETRY_AFTER = 120           # cap for Telegram's "retry_after" (HTTP 429)
REQUEST_TIMEOUT = (10, 30)      # (connect, read) timeouts in seconds

# --- Poll behaviour (change these to taste) ---------------------------------
# Countdown shown on every poll, in seconds. The poll closes when it reaches
# zero. Telegram allows 5-600. Use 0 for no timer.
POLL_TIMER_SECONDS = 15

# False  = Telegram shows who answered (and the script can build a leaderboard).
#          Works in groups. Telegram does NOT allow it in channels: there the
#          script automatically falls back to anonymous polls.
# True   = anonymous polls (only vote counts are visible).
ANONYMOUS_POLLS = False

# Order and numbering of the questions.
SHUFFLE_QUESTIONS = True        # random order every time a set is posted
SHOW_QUESTION_NUMBERS = True    # question text starts with "Q3/10: "
QUESTION_PREFIX_RESERVE = 12    # characters kept free for that prefix

# Result message after each quiz set.
POST_RESULTS = True             # post the final result when the quiz ends
MARKS_CORRECT = 2.0             # marks for a right answer
MARKS_WRONG = 0.5               # marks taken off for a wrong answer (skipped = 0)
SHOW_ANSWER_KEY = False         # also list the correct answers in the result
RESULTS_MAX_ROWS = 20           # most names shown in the leaderboard
RESULT_GRACE_SECONDS = 3        # extra wait after the last poll closes
MAX_LONG_POLL = 20              # longest single wait when reading answers

# Telegram limits for poll content.
MAX_QUESTION_LENGTH = 300
MAX_OPTION_LENGTH = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MIN_TIMER = 5
MAX_TIMER = 600
MAX_MESSAGE_LENGTH = 4000       # Telegram's hard limit is 4096

# Data files (resolved relative to this script, so the working directory
# does not matter).
BASE_DIR = Path(__file__).resolve().parent
QUIZ_DIR = BASE_DIR / "quizzes"
SCHEDULE_FILE = BASE_DIR / "schedule.json"
SENT_FILE = BASE_DIR / "sent.json"

# Quiz IDs become file names, so only allow safe characters.
QUIZ_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# A question is: (question_text, [option1, option2, ...], correct_option_index)
Question = Tuple[str, List[str], int]

logger = logging.getLogger("quiz_bot")


@dataclass(frozen=True)
class ScheduleEntry:
    """One line of schedule.json after parsing."""
    when: datetime   # timezone-aware, in UTC
    quiz_id: str
    key: str         # unique id used in sent.json, e.g. "ows001@2026-10-19T09:00Z"


@dataclass
class TelegramResult:
    """Outcome of one Telegram API call (after retries)."""
    ok: bool
    data: Dict[str, Any]
    status: Optional[int]    # HTTP status of the last response; None = network failure

    @property
    def description(self) -> str:
        return str(self.data.get("description", ""))


@dataclass
class SentPoll:
    """A poll that was posted successfully (needed to build the results)."""
    poll_id: Optional[str]
    question: str
    correct_index: int
    correct_text: str
    sent_at: float            # time.monotonic() when the poll was posted


@dataclass
class Scorecard:
    """One participant's final numbers."""
    name: str
    correct: int
    wrong: int
    skipped: int
    score: float
    seconds: float            # total time taken (skipped questions count the full timer)


# --------------------------------------------------------------------------- #
# General helpers
# --------------------------------------------------------------------------- #

def setup_logging() -> None:
    """Configure timestamped logging to stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def validate_chat_id(chat_id: str) -> str:
    """Validate Telegram chat ID inputs.

    Accepts either a numeric chat ID (e.g. -1001234567890) or a public username
    like @channelname. Telegram rejects private channels/groups unless the bot is
    present and the numeric ID is used.
    """
    chat_id = chat_id.strip()
    if not chat_id:
        raise RuntimeError("CHAT_ID is empty.")

    if chat_id.startswith("@") and len(chat_id) > 1:
        return chat_id

    if re.fullmatch(r"-?\d+", chat_id):
        return chat_id

    # Common user mistakes while copying the value from a browser or docs.
    if chat_id.startswith("https://") or chat_id.startswith("http://"):
        raise RuntimeError(
            "CHAT_ID looks like a URL, not a Telegram chat ID. "
            "Use a numeric chat ID (for example -1001234567890) or @channelname."
        )

    raise RuntimeError(
        "Invalid CHAT_ID. Use a numeric Telegram chat ID such as -1001234567890 "
        "or a public @channelname. For private channels/groups, use the numeric chat ID "
        "from getUpdates and make sure the bot is a member/admin."
    )


def get_config() -> Tuple[str, str]:
    """Read BOT_TOKEN and CHAT_ID from the environment.

    Raises:
        RuntimeError: if either variable is missing, empty or malformed.
    """
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    missing = [name for name, value in (("BOT_TOKEN", token), ("CHAT_ID", chat_id))
               if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
    return token, validate_chat_id(chat_id)


def redact(text: Any, token: str) -> str:
    """Return `text` as a string with the bot token masked.

    Network exceptions often include the full request URL, which contains the
    token (https://api.telegram.org/bot<TOKEN>/...). Never log it.
    """
    if not token:
        return str(text)
    return str(text).replace(token, "***")


def read_json(path: Path) -> Any:
    """Read and parse a JSON file (raises OSError / ValueError on problems)."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def poll_timer() -> Optional[int]:
    """Return the poll countdown in seconds, or None when no timer is used."""
    if MIN_TIMER <= POLL_TIMER_SECONDS <= MAX_TIMER:
        return POLL_TIMER_SECONDS
    if POLL_TIMER_SECONDS != 0:
        logger.warning("POLL_TIMER_SECONDS=%s is outside %d-%d; polls will have no timer.",
                       POLL_TIMER_SECONDS, MIN_TIMER, MAX_TIMER)
    return None


# --------------------------------------------------------------------------- #
# Quiz files
# --------------------------------------------------------------------------- #

def validate_question(item: Any) -> str:
    """Check one quiz entry against the expected format and Telegram limits.

    Returns:
        An empty string if the entry is valid, otherwise the reason it is not.
    """
    if not isinstance(item, (tuple, list)) or len(item) != 3:
        return "entry must be [question, [options], correct_index]"

    question, options, correct_index = item

    if not isinstance(question, str) or not question.strip():
        return "question must be a non-empty string"
    max_question = MAX_QUESTION_LENGTH - (QUESTION_PREFIX_RESERVE if SHOW_QUESTION_NUMBERS else 0)
    if len(question) > max_question:
        return f"question exceeds {max_question} characters"

    if not isinstance(options, (list, tuple)):
        return "options must be a list"
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"need {MIN_OPTIONS}-{MAX_OPTIONS} options, got {len(options)}"
    for opt in options:
        if not isinstance(opt, str) or not opt.strip():
            return "every option must be a non-empty string"
        if len(opt) > MAX_OPTION_LENGTH:
            return f"an option exceeds {MAX_OPTION_LENGTH} characters"

    # bool is a subclass of int, so exclude it explicitly.
    if not isinstance(correct_index, int) or isinstance(correct_index, bool):
        return "correct_index must be an integer"
    if not 0 <= correct_index < len(options):
        return (f"correct_index {correct_index} is out of range "
                f"(0-{len(options) - 1})")

    return ""


def filter_valid(questions: List[Any]) -> Tuple[List[Question], int]:
    """Split a raw question list into valid questions and a skipped count.

    Invalid entries are logged and skipped; they never stop the program.
    """
    valid: List[Question] = []
    skipped = 0
    for number, item in enumerate(questions, start=1):
        problem = validate_question(item)
        if problem:
            skipped += 1
            logger.warning("Skipping question #%d: %s", number, problem)
        else:
            valid.append((item[0], list(item[1]), item[2]))
    return valid, skipped


def load_quiz(quiz_id: str) -> List[Any]:
    """Load the raw question list from quizzes/<quiz_id>.json.

    Raises:
        ValueError: if the id is unsafe or the file is missing/invalid.
    """
    if not QUIZ_ID_PATTERN.match(quiz_id):
        raise ValueError(
            f"invalid quiz id {quiz_id!r} (use letters, digits, '-' and '_' only)"
        )
    path = QUIZ_DIR / f"{quiz_id}.json"
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load quizzes/{path.name}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"quizzes/{path.name} must contain a JSON list of questions")
    return data


# --------------------------------------------------------------------------- #
# Schedule and sent record
# --------------------------------------------------------------------------- #

def parse_time(value: Any) -> datetime:
    """Parse an ISO-8601 time into an aware UTC datetime.

    Accepts "2026-10-19T09:00", "2026-10-19T09:00Z" and offsets such as
    "2026-10-19T14:30+05:30". A time with no zone is treated as UTC.

    Raises:
        ValueError: if the value cannot be parsed.
    """
    if not isinstance(value, str):
        raise ValueError("'at' must be text like 2026-10-19T09:00Z")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_schedule() -> List[ScheduleEntry]:
    """Read schedule.json. Bad entries are skipped with a warning.

    Raises:
        RuntimeError: if the file itself is missing or not a JSON list.
    """
    try:
        data = read_json(SCHEDULE_FILE)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load schedule.json: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("schedule.json must contain a JSON list")

    entries: List[ScheduleEntry] = []
    seen = set()
    for number, item in enumerate(data, start=1):
        try:
            if not isinstance(item, dict):
                raise ValueError("entry must look like {\"quiz\": \"ows001\", \"at\": \"...\"}")
            quiz_id = item.get("quiz")
            if not isinstance(quiz_id, str) or not QUIZ_ID_PATTERN.match(quiz_id):
                raise ValueError(f"invalid quiz id {quiz_id!r}")
            when = parse_time(item.get("at"))
        except ValueError as exc:
            logger.warning("Skipping schedule entry #%d: %s", number, exc)
            continue

        key = f"{quiz_id}@{when:%Y-%m-%dT%H:%MZ}"
        if key in seen:
            logger.warning("Skipping schedule entry #%d: duplicate of %s", number, key)
            continue
        seen.add(key)
        entries.append(ScheduleEntry(when=when, quiz_id=quiz_id, key=key))

    entries.sort(key=lambda entry: entry.when)
    return entries


def load_sent() -> Dict[str, Any]:
    """Read sent.json (the record of what was already posted).

    A missing file means nothing has been sent yet. A corrupt file is an
    error: treating it as empty would re-post everything.

    Raises:
        RuntimeError: if the file exists but cannot be read as a JSON object.
    """
    if not SENT_FILE.exists():
        return {}
    try:
        data = read_json(SENT_FILE)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load sent.json: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("sent.json must contain a JSON object")
    return data


def save_sent(sent: Dict[str, Any]) -> None:
    """Write sent.json atomically (temp file, then rename)."""
    fd, tmp_name = tempfile.mkstemp(dir=SENT_FILE.parent, prefix=".sent-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sent, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, SENT_FILE)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def call_telegram(
    session: requests.Session,
    token: str,
    method: str,
    payload: Dict[str, Any],
) -> TelegramResult:
    """Call a Telegram Bot API method, retrying on transient failures.

    Retries on: network errors/timeouts, HTTP 429 (honouring retry_after),
    and HTTP 5xx. Permanent client errors (e.g. 400, 401, 403) are not retried.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    last = TelegramResult(ok=False, data={}, status=None)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = BACKOFF_BASE ** attempt  # default wait before the next attempt

        try:
            response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            last = TelegramResult(ok=False, data={}, status=None)
            logger.warning("Attempt %d/%d: network error: %s",
                           attempt, MAX_ATTEMPTS, redact(exc, token))
        else:
            try:
                data = response.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}

            if response.status_code == 200 and data.get("ok") is True:
                return TelegramResult(ok=True, data=data, status=200)

            last = TelegramResult(ok=False, data=data, status=response.status_code)
            description = data.get("description") or response.text[:200]
            logger.warning("Attempt %d/%d: HTTP %d: %s", attempt, MAX_ATTEMPTS,
                           response.status_code, redact(description, token))

            if response.status_code == 429:
                retry_after = (data.get("parameters") or {}).get("retry_after")
                if isinstance(retry_after, int) and retry_after > 0:
                    delay = min(retry_after + 1, MAX_RETRY_AFTER)
            elif response.status_code < 500:
                # Permanent error (bad token, bad chat, bad payload...).
                logger.error("Not retrying: this error will not fix itself.")
                if "chat not found" in str(description).lower():
                    logger.error(
                        "Telegram says the chat was not found. Check CHAT_ID, make sure "
                        "the bot is a member/admin of the target chat, and use the numeric "
                        "ID for private channels/groups."
                    )
                return last

        if attempt < MAX_ATTEMPTS:
            logger.info("Retrying in %d seconds...", delay)
            time.sleep(delay)

    return last


def send_quiz_poll(
    session: requests.Session,
    token: str,
    chat_id: str,
    question: str,
    options: List[str],
    correct_index: int,
    anonymous: bool,
) -> TelegramResult:
    """Send one Quiz Poll with a countdown timer."""
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "question": question,
        "options": list(options),
        "type": "quiz",
        "is_anonymous": anonymous,
        "correct_option_id": correct_index,
    }
    timer = poll_timer()
    if timer:
        payload["open_period"] = timer   # Telegram shows a countdown and closes the poll
    return call_telegram(session, token, "sendPoll", payload)


def extract_poll_id(data: Dict[str, Any]) -> Optional[str]:
    """Pull the poll id out of a sendPoll response (None if absent)."""
    result = data.get("result")
    poll = result.get("poll") if isinstance(result, dict) else None
    poll_id = poll.get("id") if isinstance(poll, dict) else None
    return poll_id if isinstance(poll_id, str) else None


def display_name(user: Dict[str, Any]) -> str:
    """Readable name for a Telegram user object."""
    full = " ".join(part for part in (user.get("first_name"), user.get("last_name"))
                    if isinstance(part, str) and part.strip()).strip()
    if full:
        return full
    username = user.get("username")
    if isinstance(username, str) and username:
        return "@" + username
    return f"User {user.get('id')}"


class AnswerCollector:
    """Collects who answered which poll while the script waits.

    Telegram only reports answers (poll_answer updates) for NON-anonymous polls,
    and only through getUpdates. If that is not possible (anonymous polls,
    a webhook is set, ...) the collector switches itself off and simply sleeps.
    """

    def __init__(self, session: requests.Session, token: str, enabled: bool) -> None:
        self.session = session
        self.token = token
        self.enabled = enabled
        self.offset: Optional[int] = None
        # poll_id -> {user_id: (display_name, option_ids, time_received)}
        self.answers: Dict[str, Dict[int, Tuple[str, List[int], float]]] = {}

    def disable(self, reason: str) -> None:
        if self.enabled and reason:
            logger.warning("%s", reason)
        self.enabled = False

    def wait(self, seconds: float) -> None:
        """Wait `seconds`, reading answers meanwhile when enabled."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not self.enabled:
                time.sleep(remaining)
                return
            self._read_once(remaining)

    def _read_once(self, remaining: float) -> None:
        long_poll = max(1, min(int(remaining), MAX_LONG_POLL))
        payload: Dict[str, Any] = {"timeout": long_poll, "allowed_updates": ["poll_answer"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        url = f"{TELEGRAM_API_BASE}/bot{self.token}/getUpdates"

        try:
            response = self.session.post(url, json=payload, timeout=(10, long_poll + 10))
        except requests.exceptions.RequestException as exc:
            logger.warning("Could not read answers: %s", redact(exc, self.token))
            time.sleep(min(2, remaining))
            return

        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        if response.status_code == 409:
            self.disable("Cannot read answers (a webhook is set, or something else is "
                         "reading this bot's updates). Scores will not be shown.")
            return
        if response.status_code != 200 or data.get("ok") is not True:
            logger.warning("Could not read answers: HTTP %d: %s", response.status_code,
                           redact(data.get("description", ""), self.token))
            time.sleep(min(2, remaining))
            return

        for update in data.get("result") or []:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = update_id + 1   # confirms this update to Telegram
            self._record(update.get("poll_answer"))

    def _record(self, poll_answer: Any) -> None:
        if not isinstance(poll_answer, dict):
            return
        poll_id = poll_answer.get("poll_id")
        user = poll_answer.get("user")
        if not isinstance(poll_id, str) or not isinstance(user, dict):
            return
        user_id = user.get("id")
        if not isinstance(user_id, int):
            return
        option_ids = poll_answer.get("option_ids")
        chosen = [o for o in option_ids if isinstance(o, int)] if isinstance(option_ids, list) else []
        self.answers.setdefault(poll_id, {})[user_id] = (
            display_name(user), chosen, time.monotonic())


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

def format_marks(value: float) -> str:
    """17.5 -> '17.5', 20.0 -> '20'."""
    return f"{value:g}"


def format_duration(seconds: float) -> str:
    """Seconds -> '45 sec', '1 min 12 sec' or '1 hr 5 min'."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} hr {minutes} min"
    if minutes:
        return f"{minutes} min {secs} sec"
    return f"{secs} sec"


def build_scoreboard(polls: List[SentPoll],
                     answers: Dict[str, Dict[int, Tuple[str, List[int], float]]]
                     ) -> List[Scorecard]:
    """Score everyone who answered at least once, best first.

    Marks: +MARKS_CORRECT per right answer, -MARKS_WRONG per wrong one, 0 if
    skipped. Ties are broken by the shorter total time.
    """
    timer = float(poll_timer() or 0)

    names: Dict[int, str] = {}
    for poll in polls:
        if poll.poll_id is None:
            continue
        for user_id, (name, chosen, _received) in answers.get(poll.poll_id, {}).items():
            if chosen:
                names[user_id] = name

    cards: List[Scorecard] = []
    for user_id, name in names.items():
        correct = wrong = skipped = 0
        seconds = 0.0
        for poll in polls:
            record = answers.get(poll.poll_id, {}).get(user_id) if poll.poll_id else None
            if record and record[1]:
                if record[1] == [poll.correct_index]:
                    correct += 1
                else:
                    wrong += 1
                elapsed = max(0.0, record[2] - poll.sent_at)
                seconds += min(elapsed, timer) if timer else elapsed
            else:
                skipped += 1
                seconds += timer
        score = correct * MARKS_CORRECT - wrong * MARKS_WRONG
        cards.append(Scorecard(name, correct, wrong, skipped, score, seconds))

    cards.sort(key=lambda card: (-card.score, round(card.seconds), card.name.lower()))
    return cards


def build_results_message(title: str,
                          polls: List[SentPoll],
                          board: List[Scorecard],
                          scores_available: bool,
                          reason: str) -> str:
    """Compose the plain-text result message."""
    total = len(polls)
    medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}

    if scores_available:
        lines = [f"\U0001F3C6 {title} - Final Result"]
        if board:
            max_score = total * MARKS_CORRECT
            rank, previous = 0, None
            for index, card in enumerate(board[:RESULTS_MAX_ROWS], start=1):
                key = (card.score, round(card.seconds))
                if key != previous:          # same score and time share a rank
                    rank, previous = index, key
                marker = medals.get(rank, f"{rank}.")
                lines += [
                    "",
                    f"{marker} {card.name}: {format_marks(card.score)}/{format_marks(max_score)}",
                    f"   \u2705 {card.correct}  \u274C {card.wrong}  \u23ED {card.skipped}"
                    f"  \u23F1 {format_duration(card.seconds)}",
                ]
            if len(board) > RESULTS_MAX_ROWS:
                lines += ["", f"...and {len(board) - RESULTS_MAX_ROWS} more"]
        else:
            lines += ["", "Nobody answered this time."]
    else:
        lines = [f"\U0001F3C6 {title} - Quiz complete", "", reason]

    if SHOW_ANSWER_KEY:
        lines += ["", "Answer key"]
        lines += [f"Q{number}: {poll.correct_text}" for number, poll in enumerate(polls, start=1)]

    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH - 10].rstrip() + "\n..."
    return text


def post_results(session: requests.Session, token: str, chat_id: str, title: str,
                 polls: List[SentPoll], collector: AnswerCollector,
                 anonymous: bool) -> bool:
    """Build and send the result message. Failure only logs a warning."""
    if anonymous:
        scores_available = False
        reason = "Scores are not shown because these polls are anonymous."
    elif not collector.enabled:
        scores_available = False
        reason = "Scores could not be collected this time."
    else:
        scores_available = True
        reason = ""

    board = build_scoreboard(polls, collector.answers) if scores_available else []
    if scores_available:
        logger.info("Collected answers from %d participant(s).", len(board))

    text = build_results_message(title, polls, board, scores_available, reason)
    result = call_telegram(session, token, "sendMessage", {"chat_id": chat_id, "text": text})
    if result.ok:
        logger.info("SUCCESS: result message posted.")
    else:
        logger.warning("The result message could not be posted.")
    return result.ok


def run_quiz(questions: List[Any], token: str, chat_id: str,
             title: str = "Quiz") -> Tuple[int, int, int]:
    """Validate, shuffle and post one quiz set, then post the result.

    Returns:
        (sent, failed, skipped) counts.
    """
    valid, skipped = filter_valid(questions)
    if SHUFFLE_QUESTIONS and len(valid) > 1:
        random.shuffle(valid)
        logger.info("Question order shuffled.")
    total = len(valid)

    sent = failed = 0
    sent_polls: List[SentPoll] = []
    anonymous = ANONYMOUS_POLLS

    with requests.Session() as session:
        collector = AnswerCollector(session, token, enabled=POST_RESULTS and not anonymous)

        for position, (question, options, correct_index) in enumerate(valid, start=1):
            shown = f"Q{position}/{total}: {question}" if SHOW_QUESTION_NUMBERS else question
            logger.info("Posting poll %d/%d: %s", position, total, question)
            try:
                result = send_quiz_poll(session, token, chat_id, shown, options,
                                        correct_index, anonymous)

                # Channels refuse non-anonymous polls. If the chat did not simply
                # fail to exist, try once anonymously and stay anonymous if it works.
                if (not result.ok and not anonymous and result.status == 400
                        and "chat not found" not in result.description.lower()):
                    retry = send_quiz_poll(session, token, chat_id, shown, options,
                                           correct_index, True)
                    if retry.ok:
                        logger.warning(
                            "Telegram refused a non-anonymous poll (normal for channels). "
                            "Switching to anonymous polls; names and scores will not be shown.")
                        anonymous = True
                        collector.disable("")
                        result = retry
            except Exception as exc:  # last-resort guard: never stop the run
                result = TelegramResult(ok=False, data={}, status=None)
                logger.error("Unexpected error: %s", redact(exc, token))

            if result.ok:
                sent += 1
                sent_polls.append(SentPoll(
                    poll_id=extract_poll_id(result.data),
                    question=question,
                    correct_index=correct_index,
                    correct_text=options[correct_index],
                    sent_at=time.monotonic(),
                ))
                logger.info("SUCCESS: poll %d/%d posted.", position, total)
            else:
                failed += 1
                logger.error("FAILED: poll %d/%d could not be posted.", position, total)

            # Wait between polls (not after the last one), collecting answers.
            if position < total:
                logger.info("Waiting %d seconds before the next poll...",
                            DELAY_BETWEEN_POLLS)
                collector.wait(DELAY_BETWEEN_POLLS)

        if POST_RESULTS and sent_polls:
            # Let the last poll close first, so the result does not give away its answer.
            wait_seconds = (poll_timer() or DELAY_BETWEEN_POLLS) + RESULT_GRACE_SECONDS
            logger.info("Waiting %d seconds for the last poll to close...", wait_seconds)
            collector.wait(wait_seconds)
            try:
                post_results(session, token, chat_id, title, sent_polls, collector, anonymous)
            except Exception as exc:  # the result is a bonus; never fail the quiz for it
                logger.error("Could not post the result: %s", redact(exc, token))

    return sent, failed, skipped


# --------------------------------------------------------------------------- #
# Run modes
# --------------------------------------------------------------------------- #

def run_scheduled(token: str, chat_id: str, dry_run: bool) -> int:
    """Post every scheduled quiz that is due and not yet recorded as sent."""
    schedule = load_schedule()
    sent_record = load_sent()
    now = datetime.now(timezone.utc)

    if dry_run:
        problems = 0
        if not schedule:
            logger.info("schedule.json has no valid entries.")
        for entry in schedule:
            if entry.key in sent_record:
                status = "already posted"
            elif entry.when <= now:
                status = "DUE"
            else:
                status = "upcoming"
            try:
                valid, skipped = filter_valid(load_quiz(entry.quiz_id))
                detail = f"{len(valid)} valid question(s)"
                if skipped or not valid:
                    problems += 1
                    detail += f", {skipped} invalid"
            except ValueError as exc:
                problems += 1
                detail = f"PROBLEM: {exc}"
            logger.info("%s | %-10s | %-14s | %s",
                        entry.when.strftime("%Y-%m-%d %H:%M UTC"),
                        entry.quiz_id, status, detail)
        return 1 if problems else 0

    due = [e for e in schedule if e.when <= now and e.key not in sent_record]
    if not due:
        upcoming = [e for e in schedule if e.when > now and e.key not in sent_record]
        if upcoming:
            logger.info("Nothing due. Next: %s at %s.", upcoming[0].quiz_id,
                        upcoming[0].when.strftime("%Y-%m-%d %H:%M UTC"))
        else:
            logger.info("Nothing due and nothing upcoming.")
        return 0

    logger.info("%d quiz set(s) due: %s", len(due), ", ".join(e.quiz_id for e in due))
    exit_code = 0

    for index, entry in enumerate(due, start=1):
        logger.info("=== Posting %s (scheduled for %s) ===", entry.quiz_id,
                    entry.when.strftime("%Y-%m-%d %H:%M UTC"))
        try:
            questions = load_quiz(entry.quiz_id)
        except ValueError as exc:
            logger.error("%s", exc)
            exit_code = 1
            continue

        sent, failed, skipped = run_quiz(questions, token, chat_id, entry.quiz_id.upper())
        logger.info("%s finished: sent=%d failed=%d skipped=%d",
                    entry.quiz_id, sent, failed, skipped)

        if sent == 0:
            # Nothing reached the chat, so leave it unrecorded and retry next run.
            logger.error("Nothing was posted for %s; it stays due and will be "
                         "retried on the next run.", entry.quiz_id)
            exit_code = 1
        else:
            if failed:
                logger.warning("%s was only partly posted (%d failed). It is "
                               "recorded as sent; remove it from sent.json to "
                               "post it again.", entry.quiz_id, failed)
                exit_code = 1
            sent_record[entry.key] = {
                "posted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sent": sent,
                "failed": failed,
                "skipped": skipped,
            }
            try:
                save_sent(sent_record)
            except OSError as exc:
                # Without a record this set could be posted twice, so stop here.
                logger.critical("Could not save sent.json (%s). Stopping to avoid "
                                "duplicate posts.", exc)
                return 1

        if index < len(due):
            logger.info("Waiting %d seconds before the next quiz set...",
                        DELAY_BETWEEN_POLLS)
            time.sleep(DELAY_BETWEEN_POLLS)

    return exit_code


def run_manual(quiz_id: str, token: str, chat_id: str, dry_run: bool) -> int:
    """Post one quiz set right now. Does not read or change schedule/sent files."""
    quiz_id = quiz_id.strip()
    try:
        questions = load_quiz(quiz_id)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    if dry_run:
        valid, skipped = filter_valid(questions)
        logger.info("%s: %d valid question(s), %d invalid.", quiz_id, len(valid), skipped)
        return 0 if valid and not skipped else 1

    logger.info("Manual run: posting %s now (schedule and sent record unchanged).",
                quiz_id)
    sent, failed, skipped = run_quiz(questions, token, chat_id, quiz_id.upper())
    logger.info("%s finished: sent=%d failed=%d skipped=%d",
                quiz_id, sent, failed, skipped)
    return 0 if sent > 0 and failed == 0 else 1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def parse_args(argv: Any = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Post scheduled Telegram Quiz Polls."
    )
    parser.add_argument("--quiz", metavar="ID",
                        help="post this quiz set now, ignoring the schedule (e.g. ows001)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate files and show what would happen; send nothing")
    return parser.parse_args(argv)


def main(argv: Any = None) -> int:
    """Program entry point. Returns the process exit code."""
    setup_logging()
    args = parse_args(argv)

    token = chat_id = ""
    if not args.dry_run:
        try:
            token, chat_id = get_config()
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2

    try:
        if args.quiz:
            return run_manual(args.quiz, token, chat_id, args.dry_run)
        return run_scheduled(token, chat_id, args.dry_run)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
