#!/usr/bin/env python3
"""
Scheduled Telegram Quiz Poll poster.

Quiz sets live in quizzes/<id>.json (e.g. quizzes/ows001.json). The file
schedule.json says which set goes out at which time. Each run of this script
posts every set that is due and not yet posted, then records it in sent.json.

Every poll shows a countdown and closes when it ends. The countdown depends on the
quiz type (TIMER_BY_PREFIX: ows 10 s, idm 15 s, rsn 25 s, mth 60 s).
After the last poll closes, a result message is posted: a leaderboard with marks
(+2 right, -0.5 wrong), right/wrong/skipped counts and time taken. This needs
non-anonymous polls, which Telegram allows in groups but not in channels.

Usage:
    python quiz_bot.py                  post everything that is due
    python quiz_bot.py --quiz ows001    post one set right now (ignores schedule)
    python quiz_bot.py --quiz weekly    post this week's standings right now
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
import hashlib
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

TELEGRAM_API_BASE = "https://api.telegram.org"

DELAY_BETWEEN_POLLS = 15        # pause between quiz sets (and the wait if a quiz has no timer)
MAX_ATTEMPTS = 3                # total tries per request (1 initial + 2 retries)
BACKOFF_BASE = 2                # retry delay = BACKOFF_BASE ** attempt seconds
MAX_RETRY_AFTER = 120           # cap for Telegram's "retry_after" (HTTP 429)
REQUEST_TIMEOUT = (10, 30)      # (connect, read) timeouts in seconds

# --- Poll behaviour (change these to taste) ---------------------------------
# Countdown shown on every poll, in seconds. The poll closes when it reaches
# zero. Telegram allows 5-600; 0 means no timer.
# The countdown depends on the kind of quiz, taken from the letters at the start
# of the quiz id (ows001 -> "ows"). Other letters use the default value.
# One entry in schedule.json can also set its own: {"quiz": "...", "timer": 20}
POLL_TIMER_SECONDS = 15           # default
TIMER_BY_PREFIX = {
    "ows": 10,    # one word substitution
    "idm": 15,    # idioms
    "rsn": 25,    # reasoning
    "mth": 60,    # maths
}
POLL_GAP_SECONDS = 0              # extra pause after a poll closes, before the next one

# Choosing questions from a big set (schedule.json: "pick" and "strategy").
# Default for "do not repeat a question within this many days". One schedule entry
# can set its own with "no_repeat_days" (for example 90), so no code edit is needed.
REPEAT_COOLDOWN_DAYS = 7
STRATEGIES = ("random", "hardest")

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

# Weekly leaderboard (India time). Marks are added up from Sunday 8:00 PM to the
# next Sunday 8:00 PM; the leaderboard is posted one hour before that reset.
POST_WEEKLY = True
LOCAL_TZ_NAME = "IST"
LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))
WEEK_RESET_DAY = 6                    # Monday=0 ... Sunday=6
WEEK_RESET_HOUR = 20                  # 8:00 PM
WEEK_RESET_MINUTE = 0
WEEKLY_POST_HOURS_BEFORE_RESET = 1    # so the leaderboard goes out at 7:00 PM
WEEKLY_CATCHUP_HOURS = 12             # still post it if a run was late
WEEKLY_MAX_ROWS = 20                  # most names shown in the weekly leaderboard
WEEKS_TO_KEEP = 8                     # older weeks are dropped from scores.json

# Telegram limits for poll content.
MAX_QUESTION_LENGTH = 300
MAX_OPTION_LENGTH = 100
MAX_EXPLANATION_LENGTH = 200
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
SCORES_FILE = BASE_DIR / "scores.json"
STATS_FILE = BASE_DIR / "stats.json"

# Quiz IDs become file names, so only allow safe characters.
QUIZ_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# A question is: (question_text, [option1, option2, ...], correct_option_index)
# (the explanation is optional and may be an empty string)
Question = Tuple[str, List[str], int, str]

logger = logging.getLogger("quiz_bot")
QUESTION_ID_CACHE: Dict[Tuple[str, str], str] = {}

MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}


@dataclass(frozen=True)
class ScheduleEntry:
    """One line of schedule.json after parsing."""
    when: datetime   # timezone-aware, in UTC
    quiz_id: str
    key: str         # unique id used in sent.json, e.g. "ows001@2026-10-19T09:00Z"
    pick: Optional[int] = None      # how many questions to use (None = all of them)
    strategy: str = ""              # "random" or "hardest" (only together with pick)
    timer: Optional[int] = None     # countdown override in seconds
    no_repeat_days: Optional[int] = None   # how long a picked question is not repeated


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
    qid: str                  # stable question id (see question_id)


@dataclass
class Scorecard:
    """One participant's final numbers."""
    user_id: int
    name: str
    correct: int
    wrong: int
    skipped: int
    score: float
    seconds: float            # total time taken (skipped questions count the full timer)


@dataclass
class QuestionResult:
    """How one question did in one quiz (among people who answered at least once)."""
    question: str
    correct: int
    wrong: int
    skipped: int


@dataclass
class QuizOutcome:
    """What happened when one quiz set was posted."""
    sent: int
    failed: int
    skipped: int
    board: List[Scorecard]    # final scores (empty when names are not visible)
    stats: Dict[str, QuestionResult] = field(default_factory=dict)   # by question id
    asked: List[Tuple[str, str]] = field(default_factory=list)       # (id, text) in posted order


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


_JSON_STRING = r'"(?:\\.|[^"\\])*"'
_COMMENT_OR_STRING = re.compile(_JSON_STRING + r"|//[^\n]*|/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA_OR_STRING = re.compile(_JSON_STRING + r"|,(?=\s*[\]}])")


def strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments, and a comma before ] or }, from JSON text.

    This lets schedule.json carry notes. Quoted text is never touched, and line
    breaks are kept so an error message still points at the right line.
    """
    def drop_comment(match: "re.Match[str]") -> str:
        piece = match.group(0)
        return piece if piece.startswith('"') else "\n" * piece.count("\n")

    def drop_comma(match: "re.Match[str]") -> str:
        piece = match.group(0)
        return "" if piece == "," else piece

    return _TRAILING_COMMA_OR_STRING.sub(drop_comma, _COMMENT_OR_STRING.sub(drop_comment, text))


def read_json(path: Path, allow_comments: bool = False) -> Any:
    """Read and parse a JSON file (raises OSError / ValueError on problems)."""
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(strip_json_comments(text) if allow_comments else text)


def read_json_object(path: Path, label: str) -> Optional[Dict[str, Any]]:
    """Read a data file that must hold a JSON object; None if it does not exist yet.

    A damaged file is an error. It is never treated as empty, because the next
    save would then wipe the data.

    Raises:
        RuntimeError: if the file exists but is not a valid JSON object.
    """
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return data


def write_json_atomic(path: Path, data: Any) -> None:
    """Write a JSON file atomically (temp file, then rename)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_data(path: Path, data: Any, consequence: str, level: int = logging.ERROR) -> bool:
    """Write a data file. On failure log what it means and return False."""
    try:
        write_json_atomic(path, data)
    except OSError as exc:
        logger.log(level, "Could not save %s (%s). %s", path.name, exc, consequence)
        return False
    return True


def count_of(entry: Dict[str, Any], name: str) -> int:
    """A stored counter as a whole number (0 if missing or damaged)."""
    try:
        return max(0, int(entry.get(name, 0)))
    except (TypeError, ValueError):
        return 0


def number_of(entry: Dict[str, Any], name: str) -> float:
    """A stored number (0.0 if missing or damaged)."""
    try:
        return float(entry.get(name, 0))
    except (TypeError, ValueError):
        return 0.0


def stamp(moment: datetime, seconds: bool = False) -> str:
    """UTC time as text: '2026-10-19T03:30Z', or with seconds '2026-10-19T03:30:00Z'."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ" if seconds
                                                    else "%Y-%m-%dT%H:%MZ")


def format_utc(moment: datetime) -> str:
    """'2026-10-19 03:30 UTC' for log lines."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def log_outcome(quiz_id: str, outcome: "QuizOutcome") -> None:
    """One log line saying how a quiz set went."""
    logger.info("%s finished: sent=%d failed=%d skipped=%d",
                quiz_id, outcome.sent, outcome.failed, outcome.skipped)


def normalize_timer(value: Any) -> Optional[int]:
    """Turn a timer setting into whole seconds, or None for 'no timer'."""
    if value is None or value == 0:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and MIN_TIMER <= value <= MAX_TIMER:
        return value
    logger.warning("Timer %r is not 0 or a whole number from %d to %d; using no timer.",
                   value, MIN_TIMER, MAX_TIMER)
    return None


def quiz_prefix(quiz_id: str) -> str:
    """The letters at the start of a quiz id, in lower case ('OWS001' -> 'ows')."""
    match = re.match(r"[A-Za-z]+", quiz_id)
    return match.group(0).lower() if match else ""


def quiz_timer(quiz_id: str, override: Optional[int] = None) -> Optional[int]:
    """Countdown for a quiz: its own schedule value, else by quiz type, else the default."""
    if override is not None:
        return normalize_timer(override)
    prefix = quiz_prefix(quiz_id)
    if prefix in TIMER_BY_PREFIX:
        return normalize_timer(TIMER_BY_PREFIX[prefix])
    return normalize_timer(POLL_TIMER_SECONDS)


# --------------------------------------------------------------------------- #
# Quiz files
# --------------------------------------------------------------------------- #

def validate_question(item: Any) -> str:
    """Check one quiz entry against the expected format and Telegram limits.

    Returns:
        An empty string if the entry is valid, otherwise the reason it is not.
    """
    if not isinstance(item, (tuple, list)) or len(item) not in (3, 4):
        return "entry must be [question, [options], correct_index, optional explanation]"

    question, options, correct_index = item[0], item[1], item[2]

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

    if len(item) == 4:
        explanation = item[3]
        if not isinstance(explanation, str):
            return "explanation must be text"
        if len(explanation) > MAX_EXPLANATION_LENGTH:
            return f"explanation exceeds {MAX_EXPLANATION_LENGTH} characters"
        if explanation.count("\n") > 2:
            return "explanation can have at most 2 line breaks"

    return ""


def filter_valid(questions: List[Any]) -> Tuple[List[Question], int]:
    """Split a raw question list into usable questions and a skipped count.

    Invalid questions and repeats (same question and same answer as an earlier one)
    are logged and skipped; they never stop the program.
    """
    valid: List[Question] = []
    first_seen: Dict[str, int] = {}
    skipped = 0
    for number, item in enumerate(questions, start=1):
        problem = validate_question(item)
        if not problem:
            qid = question_id(item[0], item[1][item[2]])
            if qid in first_seen:
                problem = f"same question and answer as #{first_seen[qid]}"
            else:
                first_seen[qid] = number
        if problem:
            skipped += 1
            logger.warning("Skipping question #%d: %s", number, problem)
        else:
            valid.append((item[0], list(item[1]), item[2], item[3] if len(item) == 4 else ""))
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


def whole_number(item: Dict[str, Any], name: str, low: int, high: Optional[int],
                 rule: str) -> Optional[int]:
    """A whole-number field of a schedule entry (None if it is not there).

    Raises:
        ValueError: with a readable message if it is not a number in range.
    """
    value = item.get(name)
    if value is None:
        return None
    if (not isinstance(value, int) or isinstance(value, bool) or value < low
            or (high is not None and value > high)):
        raise ValueError(f"'{name}' must be {rule}")
    return value


def parse_schedule_entry(item: Any) -> ScheduleEntry:
    """Turn one entry of schedule.json into a ScheduleEntry.

    Raises:
        ValueError: if the entry is wrong.
    """
    if not isinstance(item, dict):
        raise ValueError('entry must look like {"quiz": "ows001", "at": "..."}')
    quiz_id = item.get("quiz")
    if not isinstance(quiz_id, str) or not QUIZ_ID_PATTERN.match(quiz_id):
        raise ValueError(f"invalid quiz id {quiz_id!r}")
    when = parse_time(item.get("at"))

    timer = whole_number(item, "timer", MIN_TIMER, MAX_TIMER,
                         f"a whole number of seconds from {MIN_TIMER} to {MAX_TIMER}")
    pick = whole_number(item, "pick", 0, None,
                        "a whole number: 0 for all questions, or how many to choose")
    pick = pick or None            # 0 means "all questions", the same as leaving it out
    no_repeat = whole_number(item, "no_repeat_days", 0, 3650,
                              "a whole number of days, 0 to 3650")
    strategy = item.get("strategy")
    if strategy is not None and strategy not in STRATEGIES:
        raise ValueError("'strategy' must be one of: " + ", ".join(STRATEGIES))
    if pick is None and (strategy is not None or no_repeat is not None):
        raise ValueError("'strategy' and 'no_repeat_days' only work together with "
                         "'pick' (1 or more)")

    return ScheduleEntry(when=when, quiz_id=quiz_id, key=f"{quiz_id}@{stamp(when)}",
                         pick=pick, strategy=strategy or ("random" if pick else ""),
                         timer=timer, no_repeat_days=no_repeat)


def load_schedule() -> List[ScheduleEntry]:
    """Read schedule.json (notes with // and a comma after the last entry are allowed).

    Bad entries are skipped with a warning.

    Raises:
        RuntimeError: if the file itself is missing or not a JSON list.
    """
    try:
        data = read_json(SCHEDULE_FILE, allow_comments=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load schedule.json: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("schedule.json must contain a JSON list")

    entries: List[ScheduleEntry] = []
    seen = set()
    for number, item in enumerate(data, start=1):
        try:
            entry = parse_schedule_entry(item)
        except ValueError as exc:
            logger.warning("Skipping schedule entry #%d: %s", number, exc)
            continue
        if entry.key in seen:
            logger.warning("Skipping schedule entry #%d: duplicate of %s", number, entry.key)
            continue
        seen.add(entry.key)
        entries.append(entry)

    entries.sort(key=lambda entry: entry.when)
    return entries


def load_sent() -> Dict[str, Any]:
    """Read sent.json (the record of what was already posted).

    A missing file means nothing has been sent yet. A damaged file is an error:
    treating it as empty would re-post everything.
    """
    return read_json_object(SENT_FILE, "sent.json") or {}


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


def send_text(token: str, chat_id: str, text: str,
              session: Optional[requests.Session] = None) -> bool:
    """Send a plain-text message (with the usual retries). True if it was posted."""
    payload = {"chat_id": chat_id, "text": text}
    if session is not None:
        return call_telegram(session, token, "sendMessage", payload).ok
    with requests.Session() as own_session:
        return call_telegram(own_session, token, "sendMessage", payload).ok


def send_quiz_poll(
    session: requests.Session,
    token: str,
    chat_id: str,
    question: str,
    options: List[str],
    correct_index: int,
    anonymous: bool,
    explanation: str = "",
    timer: Optional[int] = None,
) -> TelegramResult:
    """Send one Quiz Poll with a countdown timer and an optional explanation."""
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "question": question,
        "options": list(options),
        "type": "quiz",
        "is_anonymous": anonymous,
        "correct_option_id": correct_index,
    }
    if explanation:
        payload["explanation"] = explanation   # shown on a wrong answer or the lamp icon
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


def ranked(rows: List[Any], key: Callable[[Any], Any]) -> Iterator[Tuple[int, Any]]:
    """Yield (rank, row) for rows already sorted best first; equal keys share a rank."""
    rank, previous = 0, None
    for index, row in enumerate(rows, start=1):
        row_key = key(row)
        if row_key != previous:
            rank, previous = index, row_key
        yield rank, row


def rank_marker(rank: int) -> str:
    """A medal for the top three, otherwise '4.', '5.' ..."""
    return MEDALS.get(rank, f"{rank}.")


def stat_line(correct: int, wrong: int, skipped: int, seconds: float,
              quizzes: Optional[int] = None) -> str:
    """The indented numbers line under a name (weekly rows also show the quiz count)."""
    played = ""
    if quizzes is not None:
        played = f"  \U0001F4DD {quizzes} {'quiz' if quizzes == 1 else 'quizzes'}"
    return (f"   \u2705 {correct}  \u274C {wrong}  \u23ED {skipped}{played}"
            f"  \u23F1 {format_duration(seconds)}")


def join_message(lines: List[str]) -> str:
    """Join lines into one Telegram message, cut to Telegram's length limit."""
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH - 10].rstrip() + "\n..."
    return text


def build_scoreboard(polls: List[SentPoll],
                    answers: Dict[str, Dict[int, Tuple[str, List[int], float]]],
                    timer: Optional[int] = None) -> List[Scorecard]:
    """Score everyone who answered at least once, best first.

    Marks: +MARKS_CORRECT per right answer, -MARKS_WRONG per wrong one, 0 if
    skipped. Ties are broken by the shorter total time.
    """
    limit = float(timer or 0)
    total_polls = len(polls)

    names: Dict[int, str] = {}
    answered_counts: Dict[int, int] = {}
    correct_counts: Dict[int, int] = {}
    wrong_counts: Dict[int, int] = {}
    elapsed_totals: Dict[int, float] = {}

    for poll in polls:
        if poll.poll_id is None:
            continue
        poll_answers = answers.get(poll.poll_id, {})
        for user_id, (name, chosen, received) in poll_answers.items():
            if not chosen:
                continue
            names[user_id] = name
            answered_counts[user_id] = answered_counts.get(user_id, 0) + 1
            if chosen == [poll.correct_index]:
                correct_counts[user_id] = correct_counts.get(user_id, 0) + 1
            else:
                wrong_counts[user_id] = wrong_counts.get(user_id, 0) + 1
            elapsed = max(0.0, received - poll.sent_at)
            elapsed_totals[user_id] = elapsed_totals.get(user_id, 0.0) + (
                min(elapsed, limit) if limit else elapsed
            )

    cards: List[Scorecard] = []
    for user_id, name in names.items():
        correct = correct_counts.get(user_id, 0)
        wrong = wrong_counts.get(user_id, 0)
        skipped = total_polls - answered_counts.get(user_id, 0)
        seconds = elapsed_totals.get(user_id, 0.0) + skipped * (limit if limit else 0.0)
        score = correct * MARKS_CORRECT - wrong * MARKS_WRONG
        cards.append(Scorecard(user_id, name, correct, wrong, skipped, score, seconds))

    cards.sort(key=lambda card: (-card.score, round(card.seconds), card.name.lower()))
    return cards


def build_results_message(title: str,
                          polls: List[SentPoll],
                          board: List[Scorecard],
                          scores_available: bool,
                          reason: str) -> str:
    """Compose the plain-text result message."""
    total = len(polls)

    if scores_available:
        lines = [f"\U0001F3C6 {title} - Final Result"]
        if board:
            max_score = format_marks(total * MARKS_CORRECT)
            for rank, card in ranked(board[:RESULTS_MAX_ROWS],
                                     lambda c: (c.score, round(c.seconds))):
                lines += [
                    "",
                    f"{rank_marker(rank)} {card.name}: {format_marks(card.score)}/{max_score}",
                    stat_line(card.correct, card.wrong, card.skipped, card.seconds),
                ]
            if len(board) > RESULTS_MAX_ROWS:
                lines += ["", f"...and {len(board) - RESULTS_MAX_ROWS} more"]
        else:
            lines += ["", "Nobody answered this time."]
    else:
        lines = [f"\U0001F3C6 {title} - Quiz complete", "", reason]

    # When scores cannot be shown, give the answer key instead so the end is still useful.
    if SHOW_ANSWER_KEY or not scores_available:
        lines += ["", "Answer key"]
        lines += [f"Q{number}: {poll.correct_text}" for number, poll in enumerate(polls, start=1)]
    return join_message(lines)


def post_results(session: requests.Session, token: str, chat_id: str, title: str,
                 polls: List[SentPoll], collector: AnswerCollector,
                 anonymous: bool, timer: Optional[int] = None) -> Tuple[bool, List[Scorecard]]:
    """Build and send the result message. Returns (posted, final scores)."""
    if anonymous:
        scores_available = False
        reason = "Scores are not shown because these polls are anonymous."
    elif not collector.enabled:
        scores_available = False
        reason = "Scores could not be collected this time."
    else:
        scores_available = True
        reason = ""

    board = build_scoreboard(polls, collector.answers, timer) if scores_available else []
    if scores_available:
        logger.info("Collected answers from %d participant(s).", len(board))

    text = build_results_message(title, polls, board, scores_available, reason)
    posted = send_text(token, chat_id, text, session)
    if posted:
        logger.info("SUCCESS: result message posted.")
    else:
        logger.warning("The result message could not be posted.")
    return posted, board


def run_quiz(questions: List[Any], token: str, chat_id: str, quiz_id: str = "quiz",
             entry: Optional[ScheduleEntry] = None,
             stats: Optional[Dict[str, Any]] = None,
             now: Optional[datetime] = None) -> QuizOutcome:
    """Choose, shuffle and post one quiz set, then post the result.

    `entry` (from schedule.json) can narrow the set down ("pick") and override the
    timer. Without it the whole set is posted with the timer for its quiz type.

    Returns:
        A QuizOutcome with the counts, the final scores and per-question results.
    """
    title = quiz_id.upper()
    timer = quiz_timer(quiz_id, entry.timer if entry else None)
    valid, skipped = filter_valid(questions)

    if entry is not None and entry.pick is not None:
        before = len(valid)
        if entry.strategy == "hardest" and stats is None:
            logger.warning("Question statistics are not available; picking randomly instead.")
        cooldown = (entry.no_repeat_days if entry.no_repeat_days is not None
                    else REPEAT_COOLDOWN_DAYS)
        valid = select_questions(valid, entry.pick, entry.strategy, stats,
                                 now or datetime.now(timezone.utc), cooldown)
        if entry.pick > before:
            logger.info("'pick' is %d but the quiz has only %d questions, so all are used.",
                        entry.pick, before)
        else:
            logger.info("Selected %d of %d questions (%s, no repeats within %d days).",
                        len(valid), before, entry.strategy, cooldown)

    if SHUFFLE_QUESTIONS and len(valid) > 1:
        random.shuffle(valid)
        logger.info("Question order shuffled.")
    total = len(valid)
    gap = (timer or DELAY_BETWEEN_POLLS) + POLL_GAP_SECONDS
    logger.info("Countdown: %s per poll.", f"{timer} seconds" if timer else "none")

    sent = failed = 0
    sent_polls: List[SentPoll] = []
    board: List[Scorecard] = []
    question_stats: Dict[str, QuestionResult] = {}
    anonymous = ANONYMOUS_POLLS

    with requests.Session() as session:
        collector = AnswerCollector(session, token, enabled=POST_RESULTS and not anonymous)

        for position, (question, options, correct_index, explanation) in enumerate(valid, start=1):
            shown = f"Q{position}/{total}: {question}" if SHOW_QUESTION_NUMBERS else question
            logger.info("Posting poll %d/%d: %s", position, total, question)
            try:
                result = send_quiz_poll(session, token, chat_id, shown, options,
                                        correct_index, anonymous, explanation, timer)

                # Channels refuse non-anonymous polls. If the chat did not simply
                # fail to exist, try once anonymously and stay anonymous if it works.
                if (not result.ok and not anonymous and result.status == 400
                        and "chat not found" not in result.description.lower()):
                    retry = send_quiz_poll(session, token, chat_id, shown, options,
                                           correct_index, True, explanation, timer)
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
                    qid=question_id(question, options[correct_index]),
                ))
                logger.info("SUCCESS: poll %d/%d posted.", position, total)
            else:
                failed += 1
                logger.error("FAILED: poll %d/%d could not be posted.", position, total)

            # Wait between polls (not after the last one), collecting answers.
            if position < total:
                logger.info("Waiting %d seconds before the next poll...", gap)
                collector.wait(gap)

        if POST_RESULTS and sent_polls:
            # Let the last poll close first, so the result does not give away its answer.
            wait_seconds = (timer or DELAY_BETWEEN_POLLS) + RESULT_GRACE_SECONDS
            logger.info("Waiting %d seconds for the last poll to close...", wait_seconds)
            collector.wait(wait_seconds)
            try:
                _posted, board = post_results(session, token, chat_id, title,
                                              sent_polls, collector, anonymous, timer)
            except Exception as exc:  # the result is a bonus; never fail the quiz for it
                logger.error("Could not post the result: %s", redact(exc, token))

        question_stats = build_question_stats(sent_polls, collector)

    return QuizOutcome(sent=sent, failed=failed, skipped=skipped, board=board,
                       stats=question_stats,
                       asked=[(poll.qid, poll.question) for poll in sent_polls])


# --------------------------------------------------------------------------- #
# Weekly leaderboard
# --------------------------------------------------------------------------- #

def format_local(moment: datetime) -> str:
    """'Sun 18 Oct, 8:00 PM' in India time."""
    local = moment.astimezone(LOCAL_TZ)
    hour12 = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{local:%a %d %b}, {hour12}:{local.minute:02d} {suffix}"


def week_start_for(moment: datetime) -> datetime:
    """The most recent weekly reset (Sunday 8:00 PM IST) at or before `moment`, in UTC."""
    local = moment.astimezone(LOCAL_TZ)
    days_back = (local.weekday() - WEEK_RESET_DAY) % 7
    start = (local - timedelta(days=days_back)).replace(
        hour=WEEK_RESET_HOUR, minute=WEEK_RESET_MINUTE, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=7)
    return start.astimezone(timezone.utc)


def week_key(start: datetime) -> str:
    """Stable text id for a week, e.g. '2026-10-11T14:30Z'."""
    return stamp(start)


def load_scores() -> Dict[str, Any]:
    """Read scores.json (weekly totals). A missing file means no scores yet."""
    data = read_json_object(SCORES_FILE, "scores.json") or {}
    weeks, posted = data.get("weeks"), data.get("weekly_posted")
    return {"weeks": weeks if isinstance(weeks, dict) else {},
            "weekly_posted": posted if isinstance(posted, list) else []}


def add_to_scores(scores: Dict[str, Any], board: List[Scorecard], when: datetime) -> None:
    """Add one quiz's final scores to the week that `when` falls in."""
    key = week_key(week_start_for(when))
    users = scores["weeks"].setdefault(key, {}).setdefault("users", {})
    for card in board:
        entry = users.setdefault(str(card.user_id), {})
        entry["name"] = card.name
        entry["points"] = round(number_of(entry, "points") + card.score, 2)
        entry["correct"] = count_of(entry, "correct") + card.correct
        entry["wrong"] = count_of(entry, "wrong") + card.wrong
        entry["skipped"] = count_of(entry, "skipped") + card.skipped
        entry["seconds"] = round(number_of(entry, "seconds") + card.seconds, 1)
        entry["quizzes"] = count_of(entry, "quizzes") + 1
    for old_key in sorted(scores["weeks"])[:-WEEKS_TO_KEEP]:
        del scores["weeks"][old_key]


def build_weekly_message(week_start: datetime, users: Dict[str, Any], now: datetime) -> str:
    """Compose the plain-text weekly leaderboard."""
    week_end = week_start + timedelta(days=7)
    rows = [entry for entry in users.values() if isinstance(entry, dict)]
    rows.sort(key=lambda e: (-number_of(e, "points"), round(number_of(e, "seconds")),
                              str(e.get("name", "")).lower()))

    lines = [
        "\U0001F3C6 Weekly Leaderboard",
        f"{format_local(week_start)} to {format_local(week_end)} {LOCAL_TZ_NAME}",
    ]
    for rank, entry in ranked(rows[:WEEKLY_MAX_ROWS],
                              lambda e: (number_of(e, "points"), round(number_of(e, "seconds")))):
        lines += [
            "",
            f"{rank_marker(rank)} {entry.get('name', 'Unknown')}: "
            f"{format_marks(number_of(entry, 'points'))} pts",
            stat_line(count_of(entry, "correct"), count_of(entry, "wrong"),
                      count_of(entry, "skipped"), number_of(entry, "seconds"),
                      count_of(entry, "quizzes")),
        ]
    if len(rows) > WEEKLY_MAX_ROWS:
        lines += ["", f"...and {len(rows) - WEEKLY_MAX_ROWS} more"]

    lines.append("")
    if now < week_end:
        lines.append(f"Scores reset on {format_local(week_end)} {LOCAL_TZ_NAME}.")
    else:
        lines.append("A new week has started.")
    return join_message(lines)


def week_users(scores: Dict[str, Any], start: datetime) -> Dict[str, Any]:
    """The per-user totals stored for the week starting at `start` (may be empty)."""
    week = scores["weeks"].get(week_key(start))
    users = week.get("users") if isinstance(week, dict) else None
    return users if isinstance(users, dict) else {}


def post_weekly_if_due(token: str, chat_id: str, scores: Dict[str, Any],
                      now: datetime) -> bool:
    """Post the weekly leaderboard when its time has come (once per week).

    Returns:
        False only if posting was attempted and failed.
    """
    if not POST_WEEKLY:
        return True

    current = week_start_for(now)
    for start in (current, current - timedelta(days=7)):
        key = week_key(start)
        week_end = start + timedelta(days=7)
        post_time = week_end - timedelta(hours=WEEKLY_POST_HOURS_BEFORE_RESET)
        if post_time > now or now - post_time > timedelta(hours=WEEKLY_CATCHUP_HOURS):
            continue
        if key in scores["weekly_posted"]:
            return True
        users = week_users(scores, start)
        if not users:
            logger.info("The weekly leaderboard is due but nobody has scored this week.")
            return True

        logger.info("=== Posting the weekly leaderboard ===")
        if not send_text(token, chat_id, build_weekly_message(start, users, now)):
            logger.error("The weekly leaderboard could not be posted; it will be "
                         "retried on the next run.")
            return False
        logger.info("SUCCESS: weekly leaderboard posted.")

        scores["weekly_posted"] = (list(scores["weekly_posted"]) + [key])[-WEEKS_TO_KEEP:]
        return save_data(SCORES_FILE, scores, "The leaderboard may be posted again.",
                         logging.CRITICAL)
    return True


# --------------------------------------------------------------------------- #
# Question statistics and selection
# --------------------------------------------------------------------------- #

def question_id(question: str, correct_text: str) -> str:
    """Short stable id for a question.

    It changes if the question text or the correct answer is edited, because the
    old numbers would no longer describe the edited question.
    """
    normalized_question = " ".join(question.lower().split())
    normalized_answer = " ".join(correct_text.lower().split())
    key = (normalized_question, normalized_answer)
    cached = QUESTION_ID_CACHE.get(key)
    if cached is not None:
        return cached
    text = f"{normalized_question}|{normalized_answer}"
    value = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    QUESTION_ID_CACHE[key] = value
    return value


def load_stats() -> Dict[str, Any]:
    """Read stats.json (per-question results). A missing file means no data yet."""
    data = read_json_object(STATS_FILE, "stats.json") or {}
    questions = data.get("questions")
    return {"questions": questions if isinstance(questions, dict) else {}}


def smoothed_rate(entry: Dict[str, Any]) -> float:
    """Share of right answers, pulled towards 50% while there is little data.

    A question never asked scores 0.50; 0 right out of 5 scores about 0.14;
    5 right out of 5 scores about 0.86. Lower means harder.
    """
    correct = count_of(entry, "correct")
    wrong = count_of(entry, "wrong")
    return (correct + 1) / (correct + wrong + 2)


def parse_stamp(value: Any) -> Optional[datetime]:
    """Parse a stored 'last_asked' time (None if missing or damaged)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_time(value)
    except ValueError:
        return None


def build_question_stats(polls: List[SentPoll],
                          collector: AnswerCollector) -> Dict[str, QuestionResult]:
    """Right/wrong/skipped counts per question, among people who answered at least once.

    When names are not visible (anonymous polls) the counts are zero, but the
    question is still listed so that 'last asked' can be recorded.
    """
    participants: set[int] = set()
    for poll in polls:
        if not poll.poll_id:
            continue
        for user_id, (_name, chosen, _received) in collector.answers.get(poll.poll_id, {}).items():
            if chosen:
                participants.add(user_id)

    results: Dict[str, QuestionResult] = {}
    for poll in polls:
        result = results.setdefault(poll.qid, QuestionResult(poll.question, 0, 0, 0))
        answered: set[int] = set()
        answers = collector.answers.get(poll.poll_id, {}) if poll.poll_id else {}
        for user_id, (_name, chosen, _received) in answers.items():
            if not chosen:
                continue
            answered.add(user_id)
            if chosen == [poll.correct_index]:
                result.correct += 1
            else:
                result.wrong += 1
        result.skipped += len(participants - answered)
    return results


def record_question_stats(stats: Dict[str, Any], results: Dict[str, QuestionResult],
                          quiz_id: str, when: datetime) -> None:
    """Add one quiz's per-question results to the running totals."""
    questions = stats.setdefault("questions", {})
    asked_at = stamp(when, seconds=True)
    for qid, result in results.items():
        entry = questions.setdefault(qid, {})
        entry["q"] = result.question[:80]
        entry["quiz"] = quiz_id
        entry["asked"] = count_of(entry, "asked") + 1
        entry["correct"] = count_of(entry, "correct") + result.correct
        entry["wrong"] = count_of(entry, "wrong") + result.wrong
        entry["skipped"] = count_of(entry, "skipped") + result.skipped
        entry["last_asked"] = asked_at


def select_questions(valid: List[Question], count: int, strategy: str,
                     stats: Optional[Dict[str, Any]], now: datetime,
                     cooldown_days: Optional[int] = None) -> List[Question]:
    """Choose `count` questions from a bigger set.

    "hardest": the lowest right-answer rate first (new questions count as 50%).
    "random":  any questions.
    Questions asked within the last `cooldown_days` (default REPEAT_COOLDOWN_DAYS)
    are left out, unless there are not enough others, in which case the least
    recently asked fill in.
    """
    if count >= len(valid):
        return list(valid)

    known: Dict[str, Any] = stats["questions"] if stats else {}

    def info(item: Question) -> Dict[str, Any]:
        entry = known.get(question_id(item[0], item[1][item[2]]))
        return entry if isinstance(entry, dict) else {}

    days = REPEAT_COOLDOWN_DAYS if cooldown_days is None else cooldown_days
    cutoff = now - timedelta(days=days)
    fresh: List[Question] = []
    recent: List[Question] = []
    for item in valid:
        asked = parse_stamp(info(item).get("last_asked"))
        (recent if asked is not None and asked > cutoff else fresh).append(item)

    if strategy == "hardest":
        fresh.sort(key=lambda item: (smoothed_rate(info(item)), random.random()))
    else:
        random.shuffle(fresh)
    chosen = fresh[:count]

    if len(chosen) < count:
        recent.sort(key=lambda item: str(info(item).get("last_asked", "")))
        chosen += recent[:count - len(chosen)]
    return chosen


# --------------------------------------------------------------------------- #
# Run modes
# --------------------------------------------------------------------------- #

def inspect_quiz(quiz_id: str) -> Tuple[int, int, str]:
    """Check one quiz file. Returns (valid_count, invalid_count, error_text)."""
    try:
        valid, skipped = filter_valid(load_quiz(quiz_id))
    except ValueError as exc:
        return 0, 0, str(exc)
    return len(valid), skipped, ""


def dry_run_report(schedule: List[ScheduleEntry], sent_record: Dict[str, Any],
                   scores: Optional[Dict[str, Any]], stats: Optional[Dict[str, Any]],
                   now: datetime) -> int:
    """Log the state of the schedule, every quiz file, scores and question statistics.

    Returns:
        The number of problems found (0 means everything is fine).
    """
    problems = 0
    scheduled_ids = {entry.quiz_id for entry in schedule}
    quiz_files = {path.stem for path in QUIZ_DIR.glob("*.json")}
    checked = {quiz_id: inspect_quiz(quiz_id) for quiz_id in sorted(quiz_files | scheduled_ids)}

    def describe(quiz_id: str, timer: Optional[int]) -> Tuple[str, bool]:
        valid, invalid, error = checked[quiz_id]
        if error:
            return f"PROBLEM: {error}", True
        detail = f"{valid} valid question(s), {timer} s timer" if timer else \
            f"{valid} valid question(s), no timer"
        if invalid or not valid:
            return detail + f", {invalid} invalid", True
        return detail, False

    logger.info("--- Schedule ---")
    if not schedule:
        logger.info("schedule.json has no valid entries.")
    for entry in schedule:
        if entry.key in sent_record:
            status = "already posted"
        elif entry.when <= now:
            status = "DUE"
        else:
            status = "upcoming"
        detail, bad = describe(entry.quiz_id, quiz_timer(entry.quiz_id, entry.timer))
        if entry.pick is not None:
            days = (entry.no_repeat_days if entry.no_repeat_days is not None
                    else REPEAT_COOLDOWN_DAYS)
            detail += f", picks {entry.pick} ({entry.strategy}), no repeats for {days} d"
            available = checked[entry.quiz_id][0]
            if entry.pick > available > 0:
                detail += f" - the quiz has only {available}, so all are used"
        else:
            detail += ", runs all questions"
        problems += bad
        logger.info("%s | %-10s | %-14s | %s",
                    format_utc(entry.when), entry.quiz_id, status, detail)

    unscheduled = [quiz_id for quiz_id in sorted(quiz_files) if quiz_id not in scheduled_ids]
    if unscheduled:
        logger.info("--- Quiz files not in the schedule ---")
    for quiz_id in unscheduled:
        detail, bad = describe(quiz_id, quiz_timer(quiz_id))
        problems += bad
        logger.info("%-10s | %s", quiz_id, detail)

    logger.info("--- Weekly leaderboard ---")
    if scores is None:
        problems += 1
        logger.info("scores.json is not valid (see the error above).")
    else:
        start = week_start_for(now)
        logger.info("Current week: %s to %s %s; %d participant(s) so far.",
                    format_local(start), format_local(start + timedelta(days=7)),
                    LOCAL_TZ_NAME, len(week_users(scores, start)))
        post_time = start + timedelta(days=7) - timedelta(hours=WEEKLY_POST_HOURS_BEFORE_RESET)
        logger.info("The leaderboard is posted on %s %s.", format_local(post_time), LOCAL_TZ_NAME)

    logger.info("--- Question statistics ---")
    if stats is None:
        problems += 1
        logger.info("stats.json is not valid (see the error above).")
    else:
        tracked = [entry for entry in stats["questions"].values() if isinstance(entry, dict)]
        logger.info("%d question(s) tracked.", len(tracked))
        tried = [e for e in tracked if count_of(e, "correct") + count_of(e, "wrong") >= 3]
        tried.sort(key=smoothed_rate)
        if tried:
            logger.info("Lowest right-answer rate first:")
        for entry in tried[:5]:
            right, wrong = count_of(entry, "correct"), count_of(entry, "wrong")
            logger.info("  %3d%% right (%d of %d) - %s",
                        round(100 * right / (right + wrong)), right, right + wrong,
                        entry.get("q", "?"))
    return problems


def run_scheduled(token: str, chat_id: str, dry_run: bool,
                 now: Optional[datetime] = None) -> int:
    """Post every scheduled quiz that is due, then the weekly leaderboard if due."""
    schedule = load_schedule()
    sent_record = load_sent()
    exit_code = 0

    # Broken scores.json / stats.json must not stop the quizzes; those features are
    # skipped for this run instead.
    scores: Optional[Dict[str, Any]]
    try:
        scores = load_scores()
    except RuntimeError as exc:
        logger.error("%s. Weekly scores are switched off for this run.", exc)
        scores = None
        exit_code = 1
    stats: Optional[Dict[str, Any]]
    try:
        stats = load_stats()
    except RuntimeError as exc:
        logger.error("%s. Question statistics are switched off for this run.", exc)
        stats = None
        exit_code = 1

    def current_time() -> datetime:
        return now if now is not None else datetime.now(timezone.utc)

    if dry_run:
        return 1 if dry_run_report(schedule, sent_record, scores, stats, current_time()) else 0

    start_time = current_time()
    due = [e for e in schedule if e.when <= start_time and e.key not in sent_record]
    if not due:
        upcoming = [e for e in schedule if e.when > start_time and e.key not in sent_record]
        if upcoming:
            logger.info("Nothing due. Next: %s at %s.", upcoming[0].quiz_id,
                        format_utc(upcoming[0].when))
        else:
            logger.info("Nothing due and nothing upcoming.")
    else:
        logger.info("%d quiz set(s) due: %s", len(due), ", ".join(e.quiz_id for e in due))

    for index, entry in enumerate(due, start=1):
        logger.info("=== Posting %s (scheduled for %s) ===", entry.quiz_id,
                    format_utc(entry.when))
        try:
            questions = load_quiz(entry.quiz_id)
        except ValueError as exc:
            logger.error("%s", exc)
            exit_code = 1
            continue

        outcome = run_quiz(questions, token, chat_id, entry.quiz_id, entry=entry,
                           stats=stats, now=current_time())
        log_outcome(entry.quiz_id, outcome)

        if outcome.sent == 0:
            # Nothing reached the chat, so leave it unrecorded and retry next run.
            logger.error("Nothing was posted for %s; it stays due and will be "
                         "retried on the next run.", entry.quiz_id)
            exit_code = 1
        else:
            if outcome.failed:
                logger.warning("%s was only partly posted (%d failed). It is "
                               "recorded as sent; remove it from sent.json to "
                               "post it again.", entry.quiz_id, outcome.failed)
                exit_code = 1
            sent_record[entry.key] = {
                "posted_at": stamp(datetime.now(timezone.utc), seconds=True),
                "sent": outcome.sent,
                "failed": outcome.failed,
                "skipped": outcome.skipped,
                "questions": [{"id": qid, "q": text[:60]} for qid, text in outcome.asked],
            }
            # Without this record the set could be posted twice, so stop if it fails.
            if not save_data(SENT_FILE, sent_record,
                             "Stopping to avoid duplicate posts.", logging.CRITICAL):
                return 1

            # Add this quiz to the weekly totals (the week is decided by its scheduled time).
            if scores is not None and outcome.board:
                add_to_scores(scores, outcome.board, entry.when)
                if not save_data(SCORES_FILE, scores,
                                 "This quiz was not added to the weekly totals."):
                    exit_code = 1

            # Remember how each question did and when it was asked.
            if stats is not None and outcome.stats:
                record_question_stats(stats, outcome.stats, entry.quiz_id, entry.when)
                if not save_data(STATS_FILE, stats,
                                 "Question statistics for this quiz were not saved."):
                    exit_code = 1

        if index < len(due):
            logger.info("Waiting %d seconds before the next quiz set...",
                        DELAY_BETWEEN_POLLS)
            time.sleep(DELAY_BETWEEN_POLLS)

    if scores is not None and not post_weekly_if_due(token, chat_id, scores, current_time()):
        exit_code = 1

    return exit_code


def run_weekly_preview(token: str, chat_id: str, dry_run: bool) -> int:
    """Post (or just show) this week's standings now. Nothing is recorded or reset."""
    try:
        scores = load_scores()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    now = datetime.now(timezone.utc)
    start = week_start_for(now)
    users = week_users(scores, start)
    if not users:
        logger.info("Nobody has scored yet this week; nothing to post.")
        return 0

    text = build_weekly_message(start, users, now)
    if dry_run:
        logger.info("Weekly leaderboard preview:\n%s", text)
        return 0
    if send_text(token, chat_id, text):
        logger.info("SUCCESS: this week's standings were posted.")
        return 0
    logger.error("The standings could not be posted.")
    return 1


def run_manual(quiz_id: str, token: str, chat_id: str, dry_run: bool) -> int:
    """Post one whole quiz set right now.

    Does not read or change schedule.json, sent.json, scores.json or stats.json.
    """
    quiz_id = quiz_id.strip()
    if quiz_id.lower() == "weekly":
        return run_weekly_preview(token, chat_id, dry_run)

    try:
        questions = load_quiz(quiz_id)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    if dry_run:
        valid, skipped = filter_valid(questions)
        logger.info("%s: %d valid question(s), %d invalid.", quiz_id, len(valid), skipped)
        return 0 if valid and not skipped else 1

    logger.info("Manual run: posting %s now (schedule, sent record, weekly scores and "
                "question statistics are unchanged).", quiz_id)
    outcome = run_quiz(questions, token, chat_id, quiz_id)
    log_outcome(quiz_id, outcome)
    return 0 if outcome.sent > 0 and outcome.failed == 0 else 1


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
