#!/usr/bin/env python3
"""
Scheduled Telegram Quiz Poll poster.

Quiz sets live in quizzes/<id>.json (e.g. quizzes/ows001.json). The file
schedule.json says which set goes out at which time. Each run of this script
posts every set that is due and not yet posted, then records it in sent.json.

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
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

TELEGRAM_API_BASE = "https://api.telegram.org"

DELAY_BETWEEN_POLLS = 30        # seconds to wait between polls
MAX_ATTEMPTS = 3                # total tries per poll (1 initial + 2 retries)
BACKOFF_BASE = 2                # retry delay = BACKOFF_BASE ** attempt seconds
MAX_RETRY_AFTER = 120           # cap for Telegram's "retry_after" (HTTP 429)
REQUEST_TIMEOUT = (10, 30)      # (connect, read) timeouts in seconds

# Telegram limits for poll content.
MAX_QUESTION_LENGTH = 300
MAX_OPTION_LENGTH = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10

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


def get_config() -> Tuple[str, str]:
    """Read BOT_TOKEN and CHAT_ID from the environment.

    Raises:
        RuntimeError: if either variable is missing or empty.
    """
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    missing = [name for name, value in (("BOT_TOKEN", token), ("CHAT_ID", chat_id))
               if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
    return token, chat_id


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
    if len(question) > MAX_QUESTION_LENGTH:
        return f"question exceeds {MAX_QUESTION_LENGTH} characters"

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

def send_quiz_poll(
    session: requests.Session,
    token: str,
    chat_id: str,
    question: str,
    options: List[str],
    correct_index: int,
) -> bool:
    """Send one anonymous Quiz Poll, retrying on transient failures.

    Retries on: network errors/timeouts, HTTP 429 (honouring retry_after),
    and HTTP 5xx. Permanent client errors (e.g. 400, 401, 403) are not retried.

    Returns:
        True if Telegram accepted the poll, False otherwise.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendPoll"
    payload = {
        "chat_id": chat_id,
        "question": question,
        "options": list(options),
        "type": "quiz",
        "is_anonymous": True,
        "correct_option_id": correct_index,
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        delay = BACKOFF_BASE ** attempt  # default wait before the next attempt

        try:
            response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as exc:
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
                return True

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
                return False

        if attempt < MAX_ATTEMPTS:
            logger.info("Retrying in %d seconds...", delay)
            time.sleep(delay)

    return False


def run_quiz(questions: List[Any], token: str, chat_id: str) -> Tuple[int, int, int]:
    """Validate and post one quiz set.

    Returns:
        (sent, failed, skipped) counts.
    """
    valid, skipped = filter_valid(questions)

    sent = failed = 0
    with requests.Session() as session:
        for position, (question, options, correct_index) in enumerate(valid, start=1):
            logger.info("Posting poll %d/%d: %s", position, len(valid), question)
            try:
                ok = send_quiz_poll(session, token, chat_id,
                                    question, options, correct_index)
            except Exception as exc:  # last-resort guard: never stop the run
                ok = False
                logger.error("Unexpected error: %s", redact(exc, token))

            if ok:
                sent += 1
                logger.info("SUCCESS: poll %d/%d posted.", position, len(valid))
            else:
                failed += 1
                logger.error("FAILED: poll %d/%d could not be posted.",
                             position, len(valid))

            # Wait between polls (not after the last one).
            if position < len(valid):
                logger.info("Waiting %d seconds before the next poll...",
                            DELAY_BETWEEN_POLLS)
                time.sleep(DELAY_BETWEEN_POLLS)

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

        sent, failed, skipped = run_quiz(questions, token, chat_id)
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
    sent, failed, skipped = run_quiz(questions, token, chat_id)
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
