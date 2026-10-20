#!/usr/bin/env python3
"""Always-on Telegram bot: private practice quizzes (+ the scheduled group quizzes).

Run it on a host that stays on (Railway, Render, Fly.io, a VPS ...):

    BOT_TOKEN=... CHAT_ID=-100123... python chat_bot.py

Commands people can use in a private chat with the bot:
    /start /help /quizzes /quiz [id] [count] /score /stop

This program is the ONLY reader of Telegram updates for the bot token. It answers
private-chat messages itself and hands the group quiz poll answers to quiz_bot.py through
quiz_bot.WEBHOOK_UPDATES, so the group quizzes keep their scoring (no getUpdates clash).
Disable the GitHub Actions scheduler when you use this (see HOSTING.md).

Use `python chat_bot.py --setup-profile` once to push the About text, Description and the
command menu to Telegram (instead of typing them in BotFather).
"""

import argparse
import logging
import os
import random
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import quiz_bot as qb

logger = logging.getLogger("chat_bot")
shutdown_event = qb.shutdown_event

EXAMPLE_COUNT = 20              # only used in example texts; /quiz without a number = ALL
LONG_QUIZ_QUESTIONS = 50        # above this, the start message suggests a shorter practice
ANSWER_GRACE_SECONDS = 2        # extra time before an unanswered question counts as skipped
NO_TIMER_SECONDS = 60           # skip deadline for quizzes that have no timer
MAX_REMEMBERED_POLLS = 20000    # finished practice polls remembered (to ignore late answers)
SEEN_LIMIT = 5000               # questions remembered per user per quiz
LONG_POLL_SECONDS = 5           # short, so a shutdown request is honoured quickly
SCHEDULER_INTERVAL = 60         # how often the scheduled group quizzes are checked
CATALOG_CACHE_SECONDS = 60
ALLOWED_UPDATES = ["message", "callback_query", "poll_answer"]

ABOUT_TEXT = ("Daily timed quizzes for SSC, Railway & Banking aspirants. "
              "Practice, score, and climb the leaderboard.")

DESCRIPTION_TEXT = (
    "\U0001F4DA PYQxBot: smart daily quizzes for SSC, Railway, Banking & other "
    "competitive exams.\n\n"
    "\u26A1 Timed questions (10\u201360 sec)\n"
    "\U0001F3AF Exam-style marks: +2 right, \u22120.5 wrong\n"
    "\U0001F4A1 Instant explanations\n"
    "\U0001F3C6 Weekly leaderboard\n\n"
    "Tap START and begin your first quiz.")

WELCOME_TEXT = (
    "\U0001F44B Welcome to PYQxBot!\n\n"
    "Sharpen your speed and accuracy for SSC, Railway, Banking and other competitive "
    "exams, one quick quiz at a time.\n\n"
    "\u26A1 Every question is timed (10\u201360 seconds)\n"
    "\U0001F3AF Exam-style marking: +2 for a correct answer, \u22120.5 for a wrong one, "
    "0 if skipped\n"
    "\U0001F4A1 Explanations after answers, so you learn from every mistake\n"
    "\U0001F3C6 Weekly leaderboard to compete with other aspirants\n\n"
    "Get started:\n"
    "/quizzes \u2013 see all available quizzes\n"
    "/quiz \u2013 start a practice quiz\n"
    "/score \u2013 your marks and rank\n"
    "/help \u2013 how it all works\n\n"
    "Practice daily. Improve daily. \U0001F4AA")

HELP_TEXT = (
    "How it works\n\n"
    "\u2022 /quizzes lists every quiz and how many questions it has.\n"
    "\u2022 /quiz opens a menu and starts the whole quiz (every question in it). "
    f"For a shorter practice type a number, e.g. /quiz ows000 {EXAMPLE_COUNT}.\n"
    "\u2022 Each question has a countdown. Tap an option before it ends; the next "
    "question follows at once. Unanswered questions count as skipped.\n"
    "\u2022 Marks: +2 right, \u22120.5 wrong, 0 skipped.\n"
    "\u2022 Questions you have not seen yet come first, then the ones you saw longest ago.\n"
    "\u2022 /stop ends your quiz early, /score shows your marks and rank.")

COMMANDS = [
    ("quizzes", "See all available quizzes"),
    ("quiz", "Start a practice quiz"),
    ("score", "Your marks and rank"),
    ("help", "How the bot works"),
]

ApiFunc = Callable[[str, Dict[str, Any]], qb.TelegramResult]


def approx_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def parse_command(text: str) -> Tuple[str, List[str]]:
    """'/quiz@MyBot ows000 20' -> ('quiz', ['ows000', '20']); plain text -> ('', words)."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", parts
    return parts[0][1:].split("@", 1)[0].lower(), parts[1:]


# --------------------------------------------------------------------------- #
# Stored results
# --------------------------------------------------------------------------- #

class PracticeStore:
    """Per-user practice totals, saved to practice.json (kept private, never committed)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        data = qb.read_json_object(path, path.name) or {}
        users = data.get("users")
        self.users: Dict[str, Any] = users if isinstance(users, dict) else {}

    def _entry(self, user_id: int) -> Dict[str, Any]:
        entry = self.users.get(str(user_id))
        return entry if isinstance(entry, dict) else {}

    def seen_order(self, user_id: int, quiz_id: str) -> List[str]:
        """Question ids this person has done in a quiz, the least recent first."""
        with self.lock:
            seen = self._entry(user_id).get("seen")
            ids = seen.get(quiz_id) if isinstance(seen, dict) else None
            return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.lock:
            entry = self.users.get(str(user_id))
            return dict(entry) if isinstance(entry, dict) else None

    def rank(self, user_id: int) -> Tuple[int, int]:
        """(position by points, number of users); ties share the better position."""
        with self.lock:
            mine = qb.number_of(self._entry(user_id), "points")
            higher = sum(1 for u in self.users.values()
                         if isinstance(u, dict) and qb.number_of(u, "points") > mine)
            return higher + 1, len(self.users)

    def record_session(self, user_id: int, name: str, quiz_id: str, correct: int, wrong: int,
                       skipped: int, seconds: float, score: float, done: int,
                       qids: List[str]) -> bool:
        with self.lock:
            entry = self._entry(user_id)
            entry["name"] = name
            entry["points"] = round(qb.number_of(entry, "points") + score, 2)
            entry["correct"] = qb.count_of(entry, "correct") + correct
            entry["wrong"] = qb.count_of(entry, "wrong") + wrong
            entry["skipped"] = qb.count_of(entry, "skipped") + skipped
            entry["seconds"] = round(qb.number_of(entry, "seconds") + seconds, 1)
            entry["quizzes"] = qb.count_of(entry, "quizzes") + 1
            entry["last"] = {"quiz": quiz_id, "score": score, "done": done,
                             "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
            seen = entry.get("seen")
            if not isinstance(seen, dict):
                seen = entry["seen"] = {}
            known = seen.get(quiz_id)
            known = known if isinstance(known, list) else []
            again = set(qids)
            # Questions done now move to the "most recent" end of the history.
            seen[quiz_id] = ([q for q in known if q not in again] + list(qids))[-SEEN_LIMIT:]
            self.users[str(user_id)] = entry
            return qb.save_data(self.path, {"users": self.users},
                                "Practice results were not saved.")


# --------------------------------------------------------------------------- #
# One person practising
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    user_id: int
    chat_id: int
    name: str
    quiz_id: str
    questions: List[qb.Question]
    timer: Optional[int]
    index: int = 0                      # questions finished (answered or skipped)
    poll_id: Optional[str] = None       # the open question, if any
    sent_at: float = 0.0
    deadline: float = 0.0
    correct: int = 0
    wrong: int = 0
    skipped: int = 0
    seconds: float = 0.0
    qids: List[str] = field(default_factory=list)


class PracticeBot:
    """Handles Telegram updates. All state changes happen under one lock."""

    def __init__(self, api: ApiFunc, store: PracticeStore,
                 clock: Callable[[], float] = time.monotonic):
        self.api = api
        self.store = store
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions: Dict[int, Session] = {}
        self.polls: Dict[str, int] = {}          # practice poll id -> user id (recent ones)
        self._catalog: Tuple[float, List[Dict[str, Any]]] = (-1e9, [])

    # ----- sending -------------------------------------------------------- #
    def say(self, chat_id: int, text: str, markup: Optional[Dict[str, Any]] = None) -> bool:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        result = self.api("sendMessage", payload)
        if not result.ok:
            logger.warning("Could not message chat %s: %s", chat_id, result.description)
        return result.ok

    # ----- catalog -------------------------------------------------------- #
    def quizzes(self) -> List[Dict[str, Any]]:
        stamp, rows = self._catalog
        if self.clock() - stamp > CATALOG_CACHE_SECONDS:
            rows = [r for r in qb.quiz_catalog() if r["questions"] > 0 and not r["error"]]
            self._catalog = (self.clock(), rows)
        return rows

    # ----- update routing ------------------------------------------------- #
    def handle_update(self, update: Dict[str, Any]) -> None:
        if isinstance(update.get("message"), dict):
            self.handle_message(update["message"])
        elif isinstance(update.get("callback_query"), dict):
            self.handle_callback(update["callback_query"])
        elif isinstance(update.get("poll_answer"), dict):
            self.handle_poll_answer(update["poll_answer"])

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat, user, text = message.get("chat"), message.get("from"), message.get("text")
        if (not isinstance(chat, dict) or not isinstance(user, dict)
                or not isinstance(text, str) or chat.get("type") != "private"
                or not isinstance(user.get("id"), int)):
            return
        chat_id, command, args = chat["id"], *parse_command(text)

        if command == "start":
            self.say(chat_id, WELCOME_TEXT)
        elif command == "help":
            self.say(chat_id, HELP_TEXT)
        elif command == "quizzes":
            self.show_quizzes(chat_id)
        elif command == "quiz":
            self.quiz_command(user, chat_id, args)
        elif command == "score":
            self.show_score(user, chat_id)
        elif command == "stop":
            self.stop(user, chat_id)
        else:
            self.say(chat_id, "Send /quizzes to see the quizzes or /quiz to start one.")

    def handle_callback(self, query: Dict[str, Any]) -> None:
        user, message, data = query.get("from"), query.get("message"), query.get("data")
        if query.get("id"):
            self.api("answerCallbackQuery", {"callback_query_id": query["id"]})
        chat = message.get("chat") if isinstance(message, dict) else None
        if (not isinstance(user, dict) or not isinstance(chat, dict)
                or chat.get("type") != "private" or not isinstance(data, str)
                or not data.startswith("quiz:")):
            return
        self.start_session(user, chat["id"], data[len("quiz:"):], None)

    # ----- commands ------------------------------------------------------- #
    def show_quizzes(self, chat_id: int) -> None:
        rows = self.quizzes()
        if not rows:
            self.say(chat_id, "No quizzes are available right now. Please try again later.")
            return
        lines = ["\U0001F4DA Available quizzes", ""]
        for row in rows:
            timer = f"{row['timer']} s per question" if row["timer"] else "no timer"
            lines.append(f"\u2022 {row['id']} \u2013 {row['topic']}\n"
                         f"   {row['questions']} questions, {timer}")
        lines += ["", "Start one with /quiz (all questions) or a shorter one like "
                      f"/quiz {rows[0]['id']} {EXAMPLE_COUNT}"]
        self.say(chat_id, "\n".join(lines))

    def quiz_command(self, user: Dict[str, Any], chat_id: int, args: List[str]) -> None:
        if not args:
            rows = [r for r in self.quizzes() if len(f"quiz:{r['id']}") <= 64]
            if not rows:
                self.say(chat_id, "No quizzes are available right now.")
                return
            keyboard = {"inline_keyboard": [[{"text": f"{r['topic']} ({r['id']})",
                                              "callback_data": f"quiz:{r['id']}"}]
                                            for r in rows]}
            self.say(chat_id, "Choose a quiz:", keyboard)
            return
        count: Optional[int] = None          # None = every question in the quiz
        if len(args) > 1:
            try:
                count = int(args[1])
            except ValueError:
                count = 0
            if count < 1:
                self.say(chat_id, f"Use a number of 1 or more, e.g. /quiz {args[0]} "
                                  f"{EXAMPLE_COUNT}. Leave it out to get every question.")
                return
        self.start_session(user, chat_id, args[0], count)

    def show_score(self, user: Dict[str, Any], chat_id: int) -> None:
        entry = self.store.get(user["id"])
        if not entry or not qb.count_of(entry, "quizzes"):
            self.say(chat_id, "You have not finished a quiz yet. Try /quiz to start one.")
            return
        correct, wrong = qb.count_of(entry, "correct"), qb.count_of(entry, "wrong")
        rank, total = self.store.rank(user["id"])
        lines = [f"\U0001F4CA Your practice stats, {qb.display_name(user)}", "",
                 f"\U0001F3C5 Rank #{rank} of {total}",
                 f"Points: {qb.format_marks(qb.number_of(entry, 'points'))}",
                 qb.stat_line(correct, wrong, qb.count_of(entry, "skipped"),
                              qb.number_of(entry, "seconds"), qb.count_of(entry, "quizzes"))]
        if correct + wrong:
            lines.append(f"   \U0001F3AF Accuracy {round(100 * correct / (correct + wrong))}%")
        last = entry.get("last")
        if isinstance(last, dict) and last.get("quiz"):
            lines.append(f"Last quiz: {last['quiz']}")
        self.say(chat_id, "\n".join(lines))

    def stop(self, user: Dict[str, Any], chat_id: int) -> None:
        with self.lock:
            session = self.sessions.get(user["id"])
            if session is None:
                self.say(chat_id, "You have no quiz running. Start one with /quiz.")
                return
            self._finish(session, stopped=True)

    # ----- running a quiz ------------------------------------------------- #
    def start_session(self, user: Dict[str, Any], chat_id: int, quiz_id: str,
                      count: Optional[int]) -> None:
        user_id = user["id"]
        with self.lock:
            if user_id in self.sessions:
                self.say(chat_id, "You already have a quiz running. Finish it or send /stop.")
                return
            known = {r["id"].lower(): r["id"] for r in self.quizzes()}
            real_id = known.get(quiz_id.lower())
            if real_id is None:
                self.say(chat_id, "I do not know that quiz. Send /quizzes to see the list.")
                return
            try:
                valid, _ = qb.filter_valid(qb.load_quiz(real_id))
            except (ValueError, RuntimeError, OSError) as exc:
                logger.error("Could not load %s: %s", real_id, exc)
                self.say(chat_id, "That quiz cannot be opened right now. Try another one.")
                return
            everything = count is None or count >= len(valid)
            questions = self._pick(user_id, real_id, valid, count or len(valid))
            session = Session(user_id=user_id, chat_id=chat_id, name=qb.display_name(user),
                              quiz_id=real_id, questions=questions,
                              timer=qb.quiz_timer(real_id))
            self.sessions[user_id] = session
            timer = f"{session.timer} seconds each" if session.timer else "no time limit"
            text = (f"\u25B6\uFE0F {real_id}: {'all ' if everything else ''}"
                    f"{len(questions)} questions, {timer}. Good luck! Send /stop to end early.")
            if len(questions) > LONG_QUIZ_QUESTIONS:
                longest = len(questions) * (session.timer or NO_TIMER_SECONDS)
                text += (f"\n\nThis is a long one (up to {approx_duration(longest)}). Answer as "
                         "many as you like, then send /stop to finish and save your score. "
                         f"For a shorter practice use e.g. /quiz {real_id} {EXAMPLE_COUNT}.")
            self.say(chat_id, text)
            self._send_next(session)

    def _pick(self, user_id: int, quiz_id: str, valid: List[qb.Question],
              count: int) -> List[qb.Question]:
        """Unseen questions first (random order), then seen ones, least recent first."""
        position = {qid: i for i, qid in enumerate(self.store.seen_order(user_id, quiz_id))}
        unseen: List[qb.Question] = []
        seen: List[Tuple[int, qb.Question]] = []
        for question in valid:
            qid = qb.question_id(question[0], question[1][question[2]])
            if qid in position:
                seen.append((position[qid], question))
            else:
                unseen.append(question)
        random.shuffle(unseen)
        seen.sort(key=lambda item: item[0])
        return (unseen + [question for _, question in seen])[:count]

    def _send_next(self, session: Session) -> None:
        if session.index >= len(session.questions):
            self._finish(session, stopped=False)
            return
        question, options, correct_index, explanation = session.questions[session.index]
        position, total = session.index + 1, len(session.questions)
        shown = f"Q{position}/{total}: {question}" if qb.SHOW_QUESTION_NUMBERS else question
        if len(shown) > qb.MAX_QUESTION_LENGTH:
            shown = question
        payload: Dict[str, Any] = {
            "chat_id": session.chat_id, "question": shown, "options": options,
            "type": "quiz", "correct_option_id": correct_index, "is_anonymous": False,
        }
        if explanation:
            payload["explanation"] = explanation
        if session.timer:
            payload["open_period"] = session.timer
        result = self.api("sendPoll", payload)
        poll_id = qb.extract_poll_id(result.data) if result.ok else None
        if poll_id is None:
            logger.error("Could not send a practice poll: %s", result.description)
            self._finish(session, stopped=True)
            return
        now = self.clock()
        session.poll_id, session.sent_at = poll_id, now
        session.deadline = now + (session.timer or NO_TIMER_SECONDS) + ANSWER_GRACE_SECONDS
        self.polls[poll_id] = session.user_id
        while len(self.polls) > MAX_REMEMBERED_POLLS:
            self.polls.pop(next(iter(self.polls)))

    def handle_poll_answer(self, answer: Dict[str, Any]) -> None:
        poll_id = str(answer.get("poll_id", ""))
        user = answer.get("user")
        option_ids = answer.get("option_ids")
        with self.lock:
            owner = self.polls.get(poll_id)
            if owner is None:
                # Not a practice poll, so it belongs to a group quiz run by quiz_bot.py.
                qb.WEBHOOK_UPDATES.put(answer)
                return
            session = self.sessions.get(owner)
            if (session is None or session.poll_id != poll_id or not isinstance(user, dict)
                    or user.get("id") != owner or not isinstance(option_ids, list)
                    or not option_ids):
                return
            correct_index = session.questions[session.index][2]
            elapsed = max(0.0, self.clock() - session.sent_at)
            if option_ids == [correct_index]:
                session.correct += 1
            else:
                session.wrong += 1
            session.seconds += min(elapsed, session.timer) if session.timer else elapsed
            self._advance(session)

    def tick(self) -> None:
        """Called about once a second: skip questions whose time ran out."""
        with self.lock:
            now = self.clock()
            for session in list(self.sessions.values()):
                if session.poll_id and now >= session.deadline:
                    session.skipped += 1
                    session.seconds += session.timer or NO_TIMER_SECONDS
                    self._advance(session)

    def _advance(self, session: Session) -> None:
        question = session.questions[session.index]
        session.qids.append(qb.question_id(question[0], question[1][question[2]]))
        session.poll_id = None
        session.index += 1
        self._send_next(session)

    def _finish(self, session: Session, stopped: bool) -> None:
        self.sessions.pop(session.user_id, None)
        session.poll_id = None
        done = session.index
        if done == 0:
            self.say(session.chat_id, "Quiz cancelled.")
            return
        score = session.correct * qb.MARKS_CORRECT - session.wrong * qb.MARKS_WRONG
        self.store.record_session(session.user_id, session.name, session.quiz_id,
                                  session.correct, session.wrong, session.skipped,
                                  session.seconds, score, done, session.qids)
        rank, total = self.store.rank(session.user_id)
        points = qb.number_of(self.store.get(session.user_id) or {}, "points")
        title = "stopped" if stopped else "complete"
        answered = session.correct + session.wrong
        lines = [f"\U0001F3C1 {session.quiz_id} \u2013 quiz {title}", "",
                 f"Score: {qb.format_marks(score)}/{qb.format_marks(done * qb.MARKS_CORRECT)}",
                 qb.stat_line(session.correct, session.wrong, session.skipped, session.seconds)]
        if answered:
            lines.append(f"   \U0001F3AF Accuracy {round(100 * session.correct / answered)}%")
        lines += ["", f"\U0001F3C5 Overall rank: #{rank} of {total} "
                      f"({qb.format_marks(points)} points)",
                  "", "Practise again: /quiz    Your stats: /score"]
        self.say(session.chat_id, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Reading Telegram updates (the only getUpdates reader for this bot)
# --------------------------------------------------------------------------- #

class Dispatcher:
    def __init__(self, token: str, bot: PracticeBot, offset_file: Path):
        self.token = token
        self.bot = bot
        self.offset_file = offset_file
        self.session = requests.Session()
        self.offset: Optional[int] = None
        try:
            self.offset = int(offset_file.read_text().strip())
        except (OSError, ValueError):
            self.offset = None

    def _wait(self, seconds: float) -> None:
        shutdown_event.wait(seconds)

    def poll_once(self) -> int:
        payload: Dict[str, Any] = {"timeout": LONG_POLL_SECONDS,
                                   "allowed_updates": ALLOWED_UPDATES}
        if self.offset is not None:
            payload["offset"] = self.offset
        url = f"{qb.TELEGRAM_API_BASE}/bot{self.token}/getUpdates"
        try:
            response = self.session.post(url, json=payload,
                                         timeout=(10, LONG_POLL_SECONDS + 10))
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("getUpdates failed: %s", qb.redact(exc, self.token))
            self._wait(2)
            return 0

        if response.status_code == 409:
            logger.error("Another program is reading this bot's updates (is the GitHub "
                         "quiz workflow or a webhook still active?). Retrying in 10 s.")
            self._wait(10)
            return 0
        if response.status_code == 429:
            retry = (data.get("parameters") or {}).get("retry_after", 5)
            self._wait(float(retry) if isinstance(retry, (int, float)) else 5)
            return 0
        if not data.get("ok") or not isinstance(data.get("result"), list):
            logger.warning("getUpdates error: %s", qb.redact(data.get("description", ""),
                                                             self.token))
            self._wait(3)
            return 0

        updates = data["result"]
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = update_id + 1
            try:
                self.bot.handle_update(update)
            except Exception:  # one bad update must never stop the bot
                logger.exception("Error while handling an update")
        if updates and self.offset is not None:
            try:
                self.offset_file.write_text(str(self.offset))
            except OSError:
                logger.warning("Could not save the updates offset.")
        return len(updates)

    def run(self) -> None:
        logger.info("Listening for messages and answers...")
        while not shutdown_event.is_set():
            self.poll_once()


# --------------------------------------------------------------------------- #
# Start-up helpers
# --------------------------------------------------------------------------- #

def make_api(token: str) -> ApiFunc:
    local = threading.local()

    def api(method: str, payload: Dict[str, Any]) -> qb.TelegramResult:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        return qb.call_telegram(session, token, method, payload)

    return api


def seed_data_dir() -> None:
    """On first start with a separate DATA_DIR, copy the repo's records into it."""
    if qb.DATA_DIR == qb.BASE_DIR:
        return
    qb.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("sent.json", "scores.json", "stats.json"):
        source, target = qb.BASE_DIR / name, qb.DATA_DIR / name
        if source.exists() and not target.exists():
            shutil.copyfile(source, target)
            logger.info("Copied %s into %s", name, qb.DATA_DIR)


def setup_profile(api: ApiFunc) -> int:
    calls = [
        ("setMyCommands", {"commands": [{"command": c, "description": d}
                                        for c, d in COMMANDS]}),
        ("setMyDescription", {"description": DESCRIPTION_TEXT}),
        ("setMyShortDescription", {"short_description": ABOUT_TEXT}),
    ]
    failed = 0
    for method, payload in calls:
        result = api(method, payload)
        logger.info("%s: %s", method, "ok" if result.ok else result.description)
        failed += not result.ok
    return 1 if failed else 0


def ticker_loop(bot: PracticeBot) -> None:
    while not shutdown_event.wait(1.0):
        try:
            bot.tick()
        except Exception:
            logger.exception("Error in the question timer")


def scheduler_loop(token: str, chat_id: str) -> None:
    logger.info("Scheduled group quizzes are ON (chat %s).", chat_id)
    while not shutdown_event.is_set():
        try:
            qb.run_scheduled(token, chat_id, False)
        except Exception:
            logger.exception("Error in the quiz scheduler")
        shutdown_event.wait(SCHEDULER_INTERVAL)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PYQxBot: private practice + group quizzes.")
    parser.add_argument("--setup-profile", action="store_true",
                        help="set the About text, Description and commands, then exit")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="only run private practice; do not post the scheduled quizzes")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    qb.setup_logging()
    args = parse_args(argv)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        logger.error("Missing required environment variable: BOT_TOKEN")
        return 2
    chat_id = os.environ.get("CHAT_ID", "").strip()
    try:
        if chat_id:
            chat_id = qb.validate_chat_id(chat_id)
    except (ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 2

    api = make_api(token)
    if args.setup_profile:
        return setup_profile(api)

    def request_shutdown(signum, _frame):
        logger.warning("Received signal %s - shutting down.", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        qb.DATA_DIR.mkdir(parents=True, exist_ok=True)
        seed_data_dir()
        store = PracticeStore(qb.DATA_DIR / "practice.json")
    except (OSError, RuntimeError) as exc:
        logger.error("Cannot start: %s", exc)
        return 2

    # This program is the only getUpdates reader; group answers reach quiz_bot via a queue.
    qb.EXTERNAL_UPDATES = True
    api("deleteWebhook", {})          # a leftover webhook would block getUpdates

    bot = PracticeBot(api, store)
    threading.Thread(target=ticker_loop, args=(bot,), daemon=True).start()
    if chat_id and not args.no_scheduler:
        threading.Thread(target=scheduler_loop, args=(token, chat_id), daemon=True).start()
    else:
        logger.info("Scheduled group quizzes are OFF (no CHAT_ID or --no-scheduler).")

    Dispatcher(token, bot, qb.DATA_DIR / ".chat_offset").run()
    logger.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
