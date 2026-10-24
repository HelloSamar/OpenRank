"""Integration tests: commands handled inside the scheduled run (no real Telegram)."""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz_bot as qb
import test_quiz_bot as tq

import responder

OWNER = 900
NOW = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)


def private(update_id, text, user_id=42, name="Asha", **extra):
    message = {"chat": {"id": user_id, "type": "private"},
               "from": {"id": user_id, "first_name": name}, "text": text}
    message.update(extra)
    return {"update_id": update_id, "message": message}


class FakeTelegram:
    """Behaves like Telegram: updates stay until confirmed with a higher offset."""

    def __init__(self):
        self.updates, self.sent, self.polls, self.calls = [], [], [], []
        self.next_id = 1000
        self.fail_updates = None            # e.g. 409
        self.on_poll = None                 # called with the poll number after each sendPoll
        self.fail_polls = False             # every sendPoll is refused
        self.fail_owner = False             # messages to the owner are refused
        self.pinned = []
        self.message_ids = 0

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add(self, update):
        self.updates.append(update)

    def new_id(self):
        """Update ids only ever go up, like Telegram's."""
        self.next_id += 1
        return self.next_id

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if method == "getUpdates":
            if self.fail_updates:
                return tq.FakeResponse({"ok": False, "description": "Conflict"}, self.fail_updates)
            offset = json.get("offset")
            if offset is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return tq.FakeResponse({"ok": True, "result": self.updates[: json.get("limit", 100)]})
        if method == "sendMessage":
            if self.fail_owner and str(json["chat_id"]) == str(OWNER):
                return tq.FakeResponse({"ok": False, "description": "Forbidden"}, 403)
            self.sent.append(json)
            self.message_ids += 1
            return tq.FakeResponse({"ok": True, "result": {"message_id": self.message_ids}})
        if method == "pinChatMessage":
            self.pinned.append(json)
            return tq.FakeResponse({"ok": True, "result": True})
        if method == "getMe":
            return tq.FakeResponse({"ok": True, "result": {"username": "PYQxBot"}})
        if method == "sendPoll":
            if self.fail_polls:
                return tq.FakeResponse({"ok": False, "description": "Bad Request"}, 400)
            self.polls.append(json)
            number = len(self.polls)
            self.add({"update_id": self.new_id(), "poll_answer": {
                "poll_id": str(number), "user": {"id": 7, "first_name": "Meera"},
                "option_ids": [json["correct_option_id"]]}})
            if self.on_poll:
                self.on_poll(number)
            return tq.FakeResponse({"ok": True, "result": {"poll": {"id": str(number)}}})
        return tq.FakeResponse({"ok": False, "description": "unknown"}, 404)

    def texts(self, chat_id):
        return [m["text"] for m in self.sent if m["chat_id"] == chat_id]

    def alerts(self):
        """Messages sent to the owner by the alert function (it sends the id as text)."""
        return [m["text"] for m in self.sent
                if str(m["chat_id"]) == str(OWNER) and m["text"].startswith("\u26A0")]


class CommandCase(tq.SandboxCase):
    def setUp(self):
        super().setUp()
        qb.COMMAND_INBOX.clear()
        self.fake = FakeTelegram()
        self.patches += [mock.patch.object(qb.requests, "Session", self.fake),
                         mock.patch.dict(os.environ, {"OWNER_CHAT_ID": str(OWNER)})]
        for patch in self.patches[-2:]:
            patch.start()
        self.make_quiz("ows900", 3)

    def tearDown(self):
        qb.COMMAND_INBOX.clear()
        super().tearDown()

    def schedule_due(self):
        self.write("schedule.json", [{"quiz": "ows900", "at": "2026-10-19T00:00Z", "timer": 10}])


class ProcessCommandsTests(CommandCase):
    def test_commands_are_answered_and_confirmed_once(self):
        self.fake.add(private(1, "/start"))
        self.fake.add(private(2, "/quizlists", user_id=43, name="Ravi"))
        self.fake.add({"update_id": 3, "poll_answer": {"poll_id": "old", "user": {"id": 1},
                                                      "option_ids": [0]}})        # stale answer
        self.fake.add({"update_id": 4, "message": {"chat": {"id": -5, "type": "supergroup"},
                                                   "from": {"id": 9}, "text": "chatter"}})
        self.assertTrue(qb.process_commands("tok"))
        self.assertIn("Hi Asha!", self.fake.texts(42)[0])
        self.assertIn("ows900", self.fake.texts(43)[0])
        self.assertIn("3 questions", self.fake.texts(43)[0])
        self.assertEqual(self.fake.updates, [])                      # all confirmed
        self.assertEqual(self.fake.calls[-1][1]["offset"], 5)
        before = len(self.fake.sent)
        qb.process_commands("tok")                                    # next run: nothing again
        self.assertEqual(len(self.fake.sent), before)

    def test_request_reaches_the_owner_and_owner_reply_comes_back(self):
        self.fake.add(private(1, "/request SSC CGL reasoning quiz"))
        qb.process_commands("tok")
        note = self.fake.texts(OWNER)[0]
        self.assertIn("SSC CGL reasoning quiz", note)
        self.assertIn("Your message was sent", self.fake.texts(42)[0])
        owner_reply = private(2, "Added for Friday", user_id=OWNER, name="Owner", reply_to_message={
            "from": {"id": 1, "is_bot": True}, "text": note})
        self.fake.add(owner_reply)
        qb.process_commands("tok")
        self.assertIn("Added for Friday", self.fake.texts(42)[-1])

    def test_inbox_and_refetch_are_not_answered_twice(self):
        update = private(5, "/quizlists")
        qb.COMMAND_INBOX.append(update)          # seen by the collector during a quiz...
        self.fake.add(update)                    # ...and delivered again, as Telegram does
        qb.process_commands("tok")
        self.assertEqual(len(self.fake.texts(42)), 1)
        self.assertEqual(qb.COMMAND_INBOX, [])

    def test_read_failure_is_logged_and_nothing_breaks(self):
        self.fake.fail_updates = 409
        self.fake.add(private(1, "/start"))
        self.assertTrue(qb.process_commands("tok"))
        self.assertEqual(self.fake.sent, [])

    def test_more_than_one_batch_is_read(self):
        for i in range(1, 251):
            self.fake.add(private(i, "/start", user_id=1000 + i, name=f"U{i}"))
        qb.process_commands("tok")
        self.assertEqual(len(self.fake.sent), 250)
        self.assertEqual(self.fake.updates, [])

    def test_switched_off_by_variable_or_webhook_or_shutdown(self):
        self.fake.add(private(1, "/start"))
        with mock.patch.dict(os.environ, {"DISABLE_COMMANDS": "true"}):
            qb.process_commands("tok")
        with mock.patch.object(qb, "WEBHOOK_ENABLED", True):
            qb.process_commands("tok")
        qb.shutdown_event.set()
        qb.process_commands("tok")
        self.assertEqual(self.fake.calls, [])

    def test_an_error_inside_the_responder_never_escapes(self):
        self.fake.add(private(1, "/start"))
        with mock.patch.object(responder.Responder, "handle", side_effect=RuntimeError("boom")):
            self.assertTrue(qb.process_commands("tok"))
        with mock.patch.object(qb, "_process_commands", side_effect=RuntimeError("boom")):
            self.assertFalse(qb.process_commands("tok"))

    def test_owner_chat_id_parsing(self):
        for raw, expected in (("900", 900), (" 12 ", 12), ("-100", -100), ("", None),
                              ("abc", None), ("12ab", None)):
            with mock.patch.dict(os.environ, {"OWNER_CHAT_ID": raw}):
                self.assertEqual(qb.owner_chat_id(), expected)


class CollectorTests(CommandCase):
    def test_collector_scores_answers_and_keeps_command_messages(self):
        self.fake.add({"update_id": 1, "poll_answer": {
            "poll_id": "p", "user": {"id": 5, "first_name": "Z"}, "option_ids": [1]}})
        self.fake.add(private(2, "/start"))
        self.fake.add({"update_id": 3, "message": {"chat": {"id": -5, "type": "supergroup"},
                                                   "from": {"id": 9}, "text": "chatter"}})
        collector = qb.AnswerCollector(self.fake, "tok", True, self.tmp / ".offset")
        collector._read_once(1)
        self.assertIn("p", collector.answers)
        self.assertEqual([u["update_id"] for u in qb.COMMAND_INBOX], [2])
        payload = self.fake.calls[0][1]
        self.assertEqual(payload["allowed_updates"], ["poll_answer", "message"])

    def test_inbox_is_bounded(self):
        with mock.patch.object(qb, "MAX_COMMAND_INBOX", 2):
            for i in range(1, 6):
                self.fake.add(private(i, "/start", user_id=i))
            qb.AnswerCollector(self.fake, "tok", True, self.tmp / ".offset")._read_once(1)
        self.assertEqual(len(qb.COMMAND_INBOX), 2)


class ScheduledRunTests(CommandCase):
    def run_it(self):
        return qb.run_scheduled("tok", "-100", False, now=NOW)

    def test_waiting_commands_are_answered_before_the_quiz_is_posted(self):
        self.schedule_due()
        self.fake.add(private(1, "/start"))
        self.assertEqual(self.run_it(), 0)
        order = [m for m, _ in self.fake.calls if m in ("sendMessage", "sendPoll")]
        first_reply = next(i for i, c in enumerate(self.fake.calls)
                           if c[0] == "sendMessage" and c[1]["chat_id"] == 42)
        first_poll = next(i for i, c in enumerate(self.fake.calls) if c[0] == "sendPoll")
        self.assertLess(first_reply, first_poll)
        self.assertEqual(len(self.fake.polls), 3)
        self.assertTrue(order)

    def test_a_message_sent_during_the_quiz_is_answered_after_it(self):
        self.schedule_due()
        self.fake.on_poll = lambda n: (
            self.fake.add(private(self.fake.new_id(), "/quizlists", user_id=55))
            if n == 2 else None)
        self.assertEqual(self.run_it(), 0)
        replies = self.fake.texts(55)
        self.assertEqual(len(replies), 1)
        self.assertIn("ows900", replies[0])
        last_poll = max(i for i, c in enumerate(self.fake.calls) if c[0] == "sendPoll")
        reply_at = next(i for i, c in enumerate(self.fake.calls)
                        if c[0] == "sendMessage" and c[1]["chat_id"] == 55)
        self.assertGreater(reply_at, last_poll)

    def test_group_scoring_still_works_with_commands_enabled(self):
        self.schedule_due()
        self.assertEqual(self.run_it(), 0)
        result = next(t for t in self.fake.texts("-100") if "Final Result" in t)
        self.assertIn("Meera", result)
        scores = json.loads((self.tmp / "scores.json").read_text())
        users = next(iter(scores["weeks"].values()))["users"]
        self.assertEqual(users[qb.user_key(7)]["correct"], 3)
        self.assertNotIn("7", users)                       # no raw Telegram id is stored

    def test_a_broken_responder_does_not_change_the_quiz_result(self):
        self.schedule_due()
        self.fake.add(private(1, "/start"))
        with mock.patch.object(responder.Responder, "handle", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_it(), 0)
        self.assertEqual(len(self.fake.polls), 3)
        self.assertIn("ows900@2026-10-19T00:00Z", json.loads((self.tmp / "sent.json").read_text()))

    def test_a_run_with_nothing_due_still_answers_commands(self):
        self.write("schedule.json", [])
        self.fake.add(private(1, "/start"))
        self.assertEqual(self.run_it(), 0)
        self.assertIn("Hi Asha!", self.fake.texts(42)[0])

    def test_dry_run_never_contacts_telegram(self):
        self.schedule_due()
        self.fake.add(private(1, "/start"))
        self.assertIn(qb.run_scheduled("", "", True, now=NOW), (0, 1))
        self.assertEqual(self.fake.calls, [])


class QuizListTextTests(CommandCase):
    def drop_default_quiz(self):
        (self.tmp / "quizzes" / "ows900.json").unlink()

    def test_only_the_name_and_the_number_of_questions_without_the_prefix(self):
        self.drop_default_quiz()
        self.make_quiz("Synonyms_ows", 3)
        self.make_quiz("Idioms Set 1_idm", 1)
        self.make_quiz("Computer Awareness_cmp", 2)
        self.assertEqual(qb.quiz_list_text(),
                         "\u2022 Computer Awareness \u2013 2 questions\n"
                         "\u2022 Idioms Set 1 \u2013 1 question\n"
                         "\u2022 Synonyms \u2013 3 questions")

    def test_spaces_special_characters_and_other_scripts_are_kept_as_written(self):
        self.drop_default_quiz()
        hindi = "\u0939\u093f\u0902\u0926\u0940 \u0935\u094d\u092f\u093e\u0915\u0930\u0923"
        self.make_quiz("One\u2011Word-Substitutions_ows", 2)           # a non-breaking hyphen
        self.make_quiz("Maths & Reasoning (Set 2) - 50% Easy!_mth", 1)
        self.make_quiz(f"{hindi}_ows", 4)
        text = qb.quiz_list_text()
        self.assertIn("\u2022 One\u2011Word-Substitutions \u2013 2 questions", text)
        self.assertIn("\u2022 Maths & Reasoning (Set 2) - 50% Easy! \u2013 1 question", text)
        self.assertIn(f"\u2022 {hindi} \u2013 4 questions", text)

    def test_nothing_else_is_added(self):
        self.drop_default_quiz()
        self.make_quiz("Synonyms_ows", 3)
        text = qb.quiz_list_text()
        for extra in ("Stored", "_ows", "ows", "per question", "/request", "Computer", "One Word",
                      "scheduled"):
            self.assertNotIn(extra, text)

    def test_ids_that_do_not_follow_the_pattern_are_shown_whole(self):
        shown = {"Quiz Name_ows": "Quiz Name", "A & B (set 1)_idm": "A & B (set 1)",
                 "A_B_ows": "A_B", "ows000": "ows000", "ows_": "ows_", "_ows": "_ows",
                 "quiz": "quiz"}
        for quiz_id, expected in shown.items():
            self.assertEqual(qb.quiz_display_name(quiz_id), expected, quiz_id)

    def test_quizzes_keep_their_usual_speed_and_topic(self):
        for quiz_id in ("Synonyms_ows", "Idioms_idm", "Basics_cmp", "Whatever_zzz"):
            self.make_quiz(quiz_id, 2)
        rows = {r["id"]: r for r in qb.quiz_catalog()}
        self.assertEqual((rows["Synonyms_ows"]["timer"], rows["Synonyms_ows"]["topic"]),
                         (10, "One Word Substitution"))
        self.assertEqual(rows["Idioms_idm"]["timer"], 15)
        self.assertEqual(rows["Basics_cmp"]["topic"], "Computer Knowledge")
        self.assertEqual(rows["Whatever_zzz"]["topic"], "General")      # unknown prefix
        self.assertEqual(rows["Synonyms_ows"]["questions"], 2)

    def test_list_is_sorted_by_name_ignoring_case(self):
        self.drop_default_quiz()
        for quiz_id in ("cherry_ows", "Banana_ows", "apple_ows"):
            self.make_quiz(quiz_id, 1)
        names = [line.split(" \u2013 ")[0] for line in qb.quiz_list_text().splitlines()]
        self.assertEqual(names, ["\u2022 apple", "\u2022 Banana", "\u2022 cherry"])

    def test_long_lists_are_cut_to_fit_one_message(self):
        for i in range(250):
            self.make_quiz(f"Gen Quiz Number {i:03d}_gen", 2)
        text = qb.quiz_list_text()
        self.assertLessEqual(len(text), qb.MAX_MESSAGE_LENGTH)
        self.assertRegex(text, r"and \d+ more")

    def test_empty_folder(self):
        for path in (self.tmp / "quizzes").glob("*.json"):
            path.unlink()
        self.assertEqual(qb.quiz_list_text(), "No quizzes are stored yet.")

    def test_owner_username_parsing(self):
        for raw, expected in (("@SamarOwner", "SamarOwner"), (" Samar_1 ", "Samar_1"),
                              ("", None), ("abc", None), ("1abcde", None), ("has space", None)):
            with mock.patch.dict(os.environ, {"OWNER_USERNAME": raw}):
                self.assertEqual(qb.owner_username(), expected)


class ContactThroughTheRunTests(CommandCase):
    def test_contact_menu_offers_the_direct_chat_button(self):
        self.fake.add(private(1, "/contact"))
        with mock.patch.dict(os.environ, {"OWNER_USERNAME": "SamarOwner"}):
            qb.process_commands("tok")
        payload = next(m for m in self.fake.sent if m["chat_id"] == 42)
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/SamarOwner")

    def test_start_links_to_the_group(self):
        self.fake.add(private(1, "/start"))
        qb.process_commands("tok")
        payload = next(m for m in self.fake.sent if m["chat_id"] == 42)
        self.assertIn('<a href="https://t.me/GovtScore">', payload["text"])
        self.assertEqual(payload["parse_mode"], "HTML")


if __name__ == "__main__":
    unittest.main()
