"""Tests for chat_bot.py (private practice mode). No network, no real Telegram."""

import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat_bot as cb
import quiz_bot as qb


class FakeApi:
    """Records every Telegram call; sendPoll returns ids p1, p2, ..."""

    def __init__(self):
        self.calls = []
        self.polls = []
        self.fail_polls = False

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendPoll":
            if self.fail_polls:
                return qb.TelegramResult(ok=False, data={"description": "blocked"}, status=403)
            self.polls.append(payload)
            return qb.TelegramResult(
                ok=True, data={"result": {"poll": {"id": f"p{len(self.polls)}"}}}, status=200)
        return qb.TelegramResult(ok=True, data={"result": True}, status=200)

    def texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


USER = {"id": 42, "first_name": "Asha"}


def message(text, chat_type="private", user=USER):
    return {"message": {"chat": {"id": user["id"], "type": chat_type}, "from": user,
                        "text": text}}


def answer(poll_id, option, user=USER):
    return {"poll_answer": {"poll_id": poll_id, "user": user, "option_ids": [option]}}


class ChatCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "quizzes").mkdir()
        for name, count in (("ows900", 4), ("idm900", 2)):
            (self.tmp / "quizzes" / f"{name}.json").write_text(json.dumps(
                [[f"{name} question {i}?", ["a", "b", "c"], i % 3] for i in range(count)]))
        (self.tmp / "schedule.json").write_text("[]")
        self.patches = [
            mock.patch.object(qb, "QUIZ_DIR", self.tmp / "quizzes"),
            mock.patch.object(qb, "SCHEDULE_FILE", self.tmp / "schedule.json"),
            mock.patch.object(qb, "WEBHOOK_UPDATES", queue.Queue()),
        ]
        for p in self.patches:
            p.start()
        self.api = FakeApi()
        self.now = [1000.0]
        self.store = cb.PracticeStore(self.tmp / "practice.json")
        self.bot = cb.PracticeBot(self.api, self.store, clock=lambda: self.now[0])

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def send(self, text, **kw):
        self.bot.handle_update(message(text, **kw))

    def correct_option(self, poll_index):
        return self.api.polls[poll_index]["correct_option_id"]


class CommandTests(ChatCase):
    def test_start_sends_welcome_with_commands(self):
        self.send("/start")
        text = self.api.texts()[0]
        self.assertEqual(text, cb.WELCOME_TEXT)
        for command in ("/quizzes", "/quiz", "/score", "/help"):
            self.assertIn(command, text)

    def test_command_with_bot_suffix_and_help(self):
        self.send("/help@PYQxBot")
        self.assertEqual(self.api.texts()[0], cb.HELP_TEXT)

    def test_group_messages_are_ignored(self):
        self.send("/start", chat_type="supergroup")
        self.assertEqual(self.api.calls, [])

    def test_quizzes_lists_files(self):
        self.send("/quizzes")
        text = self.api.texts()[0]
        self.assertIn("ows900", text)
        self.assertIn("One Word Substitution", text)
        self.assertIn("4 questions", text)
        self.assertIn("idm900", text)

    def test_quiz_without_args_offers_buttons(self):
        self.send("/quiz")
        _method, payload = self.api.calls[-1]
        buttons = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"]
                   for b in row]
        self.assertEqual(sorted(buttons), ["quiz:idm900", "quiz:ows900"])

    def test_button_press_starts_quiz(self):
        self.bot.handle_update({"callback_query": {
            "id": "cq1", "from": USER, "data": "quiz:ows900",
            "message": {"chat": {"id": 42, "type": "private"}}}})
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "cq1"}), self.api.calls)
        self.assertEqual(len(self.api.polls), 1)

    def test_unknown_quiz_and_bad_count(self):
        self.send("/quiz nope")
        self.assertIn("do not know", self.api.texts()[-1])
        self.send("/quiz ows900 many")
        self.assertIn("1 or more", self.api.texts()[-1])
        self.assertEqual(self.api.polls, [])

    def test_unknown_text_gets_hint(self):
        self.send("hello")
        self.assertIn("/quizzes", self.api.texts()[-1])


class SessionTests(ChatCase):
    def test_full_quiz_scores_and_stores(self):
        self.send("/quiz ows900 3")
        self.assertEqual(len(self.api.polls), 1)
        first = self.api.polls[0]
        self.assertFalse(first["is_anonymous"])
        self.assertEqual(first["type"], "quiz")
        self.assertEqual(first["open_period"], 10)
        self.assertTrue(first["question"].startswith("Q1/3: "))

        self.bot.handle_update(answer("p1", self.correct_option(0)))
        self.bot.handle_update(answer("p2", self.correct_option(1)))
        wrong = (self.correct_option(2) + 1) % 3
        self.bot.handle_update(answer("p3", wrong))

        result = self.api.texts()[-1]
        self.assertIn("Score: 3.5/6", result)
        self.assertIn("Accuracy 67%", result)
        self.assertIn("#1 of 1", result)
        entry = self.store.get(42)
        self.assertEqual((entry["correct"], entry["wrong"], entry["points"]), (2, 1, 3.5))
        self.assertEqual(entry["quizzes"], 1)
        saved = json.loads((self.tmp / "practice.json").read_text())
        self.assertEqual(saved["users"]["42"]["name"], "Asha")
        self.assertEqual(self.bot.sessions, {})

    def test_timeout_counts_as_skipped(self):
        self.send("/quiz ows900 2")
        self.now[0] += 5
        self.bot.tick()                       # still inside the time limit
        self.assertEqual(len(self.api.polls), 1)
        self.now[0] += 10                     # now past timer (10 s) + grace
        self.bot.tick()
        self.assertEqual(len(self.api.polls), 2)
        self.now[0] += 20
        self.bot.tick()
        result = self.api.texts()[-1]
        self.assertIn("Score: 0/4", result)
        self.assertEqual(self.store.get(42)["skipped"], 2)

    def test_late_answer_after_timeout_is_ignored(self):
        self.send("/quiz ows900 2")
        self.now[0] += 20
        self.bot.tick()
        self.bot.handle_update(answer("p1", self.correct_option(0)))   # too late
        self.assertEqual(self.bot.sessions[42].skipped, 1)
        self.assertEqual(self.bot.sessions[42].correct, 0)
        self.assertTrue(qb.WEBHOOK_UPDATES.empty())

    def test_stop_records_only_finished_questions(self):
        self.send("/quiz ows900 4")
        self.bot.handle_update(answer("p1", self.correct_option(0)))
        self.send("/stop")
        self.assertIn("quiz stopped", self.api.texts()[-1])
        self.assertEqual(self.store.get(42)["correct"], 1)
        self.assertEqual(self.bot.sessions, {})
        self.bot.handle_update(answer("p2", 0))      # answer to the abandoned question
        self.assertTrue(qb.WEBHOOK_UPDATES.empty())

    def test_stop_before_any_answer_cancels(self):
        self.send("/quiz ows900 4")
        self.send("/stop")
        self.assertEqual(self.api.texts()[-1], "Quiz cancelled.")
        self.assertIsNone(self.store.get(42))

    def test_stop_without_quiz(self):
        self.send("/stop")
        self.assertIn("no quiz running", self.api.texts()[-1])

    def test_second_quiz_while_running_is_refused(self):
        self.send("/quiz ows900 3")
        self.send("/quiz idm900 1")
        self.assertIn("already have a quiz running", self.api.texts()[-1])
        self.assertEqual(len(self.api.polls), 1)

    def test_quiz_without_number_uses_every_question(self):
        self.send("/quiz ows900")
        session = self.bot.sessions[42]
        self.assertEqual(len(session.questions), 4)
        self.assertEqual(len({q[0] for q in session.questions}), 4)
        self.assertIn("all 4 questions", self.api.texts()[-1])
        self.assertTrue(self.api.polls[0]["question"].startswith("Q1/4: "))

    def test_explicit_count_is_honoured_and_clamped_to_quiz_size(self):
        self.send("/quiz ows900 2")
        self.assertEqual(len(self.bot.sessions[42].questions), 2)
        self.assertNotIn("all", self.api.texts()[-1])
        self.send("/stop")
        self.send("/quiz OWS900 500")                     # id is case-insensitive
        self.assertEqual(len(self.bot.sessions[42].questions), 4)
        self.assertIn("all 4 questions", self.api.texts()[-1])

    def test_bad_counts_are_rejected(self):
        for bad in ("0", "-3", "many"):
            self.send(f"/quiz ows900 {bad}")
            self.assertIn("1 or more", self.api.texts()[-1])
        self.assertEqual(self.api.polls, [])

    def test_button_starts_the_whole_quiz(self):
        self.bot.handle_update({"callback_query": {
            "id": "cq", "from": USER, "data": "quiz:ows900",
            "message": {"chat": {"id": 42, "type": "private"}}}})
        self.assertEqual(len(self.bot.sessions[42].questions), 4)

    def test_long_quiz_start_message_explains_stop_and_shorter_option(self):
        big = [[f"big question {i}?", ["a", "b", "c"], i % 3] for i in range(120)]
        (self.tmp / "quizzes" / "ows901.json").write_text(json.dumps(big))
        self.send("/quiz ows901")
        text = self.api.texts()[-1]
        self.assertIn("all 120 questions", text)
        self.assertIn("up to 20 min", text)            # 120 x 10 s
        self.assertIn("/stop", text)
        self.assertIn("/quiz ows901 20", text)
        self.assertTrue(self.api.polls[0]["question"].startswith("Q1/120: "))

    def test_stopped_long_quiz_resumes_with_unseen_questions(self):
        self.send("/quiz ows900")
        first_run = self.bot.sessions[42].questions
        done = {first_run[0][0], first_run[1][0]}
        self.bot.handle_update(answer("p1", self.correct_option(0)))
        self.bot.handle_update(answer("p2", self.correct_option(1)))
        self.send("/stop")
        self.send("/quiz ows900")
        resumed = self.bot.sessions[42].questions
        self.assertEqual(len(resumed), 4)                      # still the whole quiz
        self.assertFalse({q[0] for q in resumed[:2]} & done)   # the 2 not yet done come first
        self.assertEqual({q[0] for q in resumed[2:]}, done)

    def test_seen_questions_return_least_recent_first(self):
        self.send("/quiz ows900")
        order = [q[0] for q in self.bot.sessions[42].questions]
        for index in range(4):
            self.bot.handle_update(answer(f"p{index + 1}", self.correct_option(index)))
        self.assertEqual(self.bot.sessions, {})
        self.send("/quiz ows900 1")                            # everything is seen now
        self.assertEqual(self.bot.sessions[42].questions[0][0], order[0])

    def test_unseen_questions_come_first(self):
        self.send("/quiz ows900 3")
        for index in range(3):
            self.bot.handle_update(answer(f"p{index + 1}", self.correct_option(index)))
        self.assertEqual(self.bot.sessions, {})
        first_three = {p["question"].split(": ", 1)[1] for p in self.api.polls}
        self.send("/quiz ows900 1")
        fourth = self.api.polls[-1]["question"].split(": ", 1)[1]
        self.assertNotIn(fourth, first_three)

    def test_blocked_user_ends_session_quietly(self):
        self.api.fail_polls = True
        self.send("/quiz ows900 3")
        self.assertEqual(self.bot.sessions, {})

    def test_score_command(self):
        self.send("/score")
        self.assertIn("not finished a quiz", self.api.texts()[-1])
        self.send("/quiz idm900 1")
        self.bot.handle_update(answer("p1", self.correct_option(0)))
        self.send("/score")
        text = self.api.texts()[-1]
        self.assertIn("Rank #1 of 1", text)
        self.assertIn("Points: 2", text)
        self.assertIn("Last quiz: idm900", text)

    def test_ranking_between_users(self):
        other = {"id": 7, "first_name": "Ravi"}
        self.send("/quiz idm900 1")
        self.bot.handle_update(answer("p1", self.correct_option(0)))          # Asha +2
        self.bot.handle_update(message("/quiz idm900 1", user=other))
        self.bot.handle_update(answer("p2", self.correct_option(1), user=other))   # Ravi +2
        self.assertEqual(self.store.rank(42), (1, 2))        # tie shares the position
        self.send("/quiz idm900 2")
        self.bot.handle_update(answer("p3", self.correct_option(2)))
        self.bot.handle_update(answer("p4", self.correct_option(3)))
        self.assertEqual(self.store.rank(42), (1, 2))
        self.assertEqual(self.store.rank(7), (2, 2))


class RoutingTests(ChatCase):
    def test_group_poll_answer_is_forwarded_to_quiz_bot(self):
        group_answer = {"poll_id": "group-poll", "user": USER, "option_ids": [1]}
        self.bot.handle_update({"poll_answer": group_answer})
        self.assertEqual(qb.WEBHOOK_UPDATES.get_nowait(), group_answer)

    def test_collector_reads_forwarded_answers(self):
        group_answer = {"poll_id": "g1", "user": USER, "option_ids": [1]}
        self.bot.handle_update({"poll_answer": group_answer})
        with mock.patch.object(qb, "EXTERNAL_UPDATES", True):
            collector = qb.AnswerCollector(mock.Mock(), "tok", True, self.tmp / ".offset")
            collector._read_once(1)
        self.assertIn("g1", collector.answers)

    def test_stale_or_foreign_answers_do_not_crash(self):
        for update in ({}, {"message": {}}, {"poll_answer": {"poll_id": 5}},
                       {"callback_query": {"data": "quiz:x"}}, {"message": {"text": 5}}):
            self.bot.handle_update(update)


class GroupQuizIntegrationTests(ChatCase):
    """A scheduled group quiz keeps its scoring while chat_bot is the only update reader."""

    def test_group_quiz_scores_answers_delivered_by_the_dispatcher(self):
        bot, tmp = self.bot, self.tmp

        class GroupTelegram:
            def __init__(self):
                self.messages, self.polls = [], []

            def __call__(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, json=None, timeout=None):
                method = url.rsplit("/", 1)[-1]
                response = mock.Mock(status_code=200)
                if method == "sendPoll":
                    self.polls.append(json)
                    poll_id = f"group-{len(self.polls)}"
                    # Telegram would now deliver the member's answer to chat_bot's reader.
                    bot.handle_update({"poll_answer": {
                        "poll_id": poll_id, "user": {"id": 9, "first_name": "Meera"},
                        "option_ids": [json["correct_option_id"]]}})
                    response.json.return_value = {
                        "ok": True, "result": {"poll": {"id": poll_id}}}
                elif method == "sendMessage":
                    self.messages.append(json["text"])
                    response.json.return_value = {"ok": True, "result": {}}
                else:
                    raise AssertionError(f"unexpected call {method}: the collector must not "
                                         "call getUpdates in external mode")
                return response

        fake = GroupTelegram()
        (tmp / "schedule.json").write_text(json.dumps(
            [{"quiz": "ows900", "at": "2026-10-19T00:00Z", "timer": 10}]))
        patches = [
            mock.patch.object(qb, "SENT_FILE", tmp / "sent.json"),
            mock.patch.object(qb, "SCORES_FILE", tmp / "scores.json"),
            mock.patch.object(qb, "STATS_FILE", tmp / "stats.json"),
            mock.patch.object(qb, "UPDATES_OFFSET_FILE", tmp / ".offset"),
            mock.patch.object(qb, "EXTERNAL_UPDATES", True),
            mock.patch.object(qb.time, "sleep", lambda *_: None),
            mock.patch.object(qb.AnswerCollector, "wait",
                              lambda self, seconds: self._read_once(0.05)),
            mock.patch.object(qb.requests, "Session", fake),
        ]
        for patch in patches:
            patch.start()
        try:
            from datetime import datetime, timezone
            code = qb.run_scheduled("tok", "-100", False,
                                    now=datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc))
        finally:
            for patch in reversed(patches):
                patch.stop()

        self.assertEqual(code, 0)
        self.assertEqual(len(fake.polls), 4)
        result = next(m for m in fake.messages if "Final Result" in m)
        self.assertIn("Meera", result)
        scores = json.loads((tmp / "scores.json").read_text())
        week = next(iter(scores["weeks"].values()))["users"]["9"]
        self.assertEqual((week["correct"], week["points"]), (4, 8.0))
        self.assertTrue(qb.WEBHOOK_UPDATES.empty())


class DispatcherTests(ChatCase):
    class FakeSession:
        def __init__(self, responses):
            self.responses, self.payloads = list(responses), []

        def post(self, url, json=None, timeout=None):
            self.payloads.append(json)
            status, body = self.responses.pop(0)
            response = mock.Mock(status_code=status)
            response.json.return_value = body
            return response

    def dispatcher(self, responses):
        dispatcher = cb.Dispatcher("tok", self.bot, self.tmp / ".chat_offset")
        dispatcher.session = self.FakeSession(responses)
        return dispatcher

    def test_updates_are_handled_and_offset_saved(self):
        updates = [{"update_id": 10, **message("/help")}, {"update_id": 11, **message("/start")}]
        dispatcher = self.dispatcher([(200, {"ok": True, "result": updates}),
                                      (200, {"ok": True, "result": []})])
        self.assertEqual(dispatcher.poll_once(), 2)
        self.assertEqual((self.tmp / ".chat_offset").read_text(), "12")
        dispatcher.poll_once()
        self.assertEqual(dispatcher.session.payloads[1]["offset"], 12)
        self.assertEqual(dispatcher.session.payloads[0]["allowed_updates"], cb.ALLOWED_UPDATES)

    def test_handler_error_does_not_stop_the_loop(self):
        bad = {"update_id": 1, **message("/help")}
        good = {"update_id": 2, **message("/start")}
        dispatcher = self.dispatcher([(200, {"ok": True, "result": [bad, good]})])
        original = self.bot.handle_update
        calls = []

        def flaky(update):
            calls.append(update["update_id"])
            if update["update_id"] == 1:
                raise RuntimeError("boom")
            original(update)

        self.bot.handle_update = flaky
        dispatcher.poll_once()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(self.api.texts(), [cb.WELCOME_TEXT])

    def test_conflict_is_reported_not_fatal(self):
        dispatcher = self.dispatcher([(409, {"ok": False, "description": "Conflict"})])
        with mock.patch.object(cb.shutdown_event, "wait") as wait:
            self.assertEqual(dispatcher.poll_once(), 0)
        wait.assert_called_with(10)


class StartupTests(unittest.TestCase):
    def test_seed_data_dir_copies_only_missing_files(self):
        base, data = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "vol"
        (base / "sent.json").write_text('{"a": 1}')
        (base / "scores.json").write_text('{"b": 2}')
        data.mkdir()
        (data / "scores.json").write_text('{"keep": true}')
        with mock.patch.object(qb, "BASE_DIR", base), mock.patch.object(qb, "DATA_DIR", data):
            cb.seed_data_dir()
        self.assertEqual(json.loads((data / "sent.json").read_text()), {"a": 1})
        self.assertEqual(json.loads((data / "scores.json").read_text()), {"keep": True})

    def test_setup_profile_sets_texts_and_commands(self):
        api = FakeApi()
        self.assertEqual(cb.setup_profile(api), 0)
        methods = dict(api.calls)
        self.assertEqual(methods["setMyShortDescription"]["short_description"], cb.ABOUT_TEXT)
        self.assertEqual(methods["setMyDescription"]["description"], cb.DESCRIPTION_TEXT)
        names = [c["command"] for c in methods["setMyCommands"]["commands"]]
        self.assertEqual(names, ["quizzes", "quiz", "score", "help"])

    def test_texts_fit_telegram_limits(self):
        self.assertLessEqual(len(cb.ABOUT_TEXT), 120)
        self.assertLessEqual(len(cb.DESCRIPTION_TEXT), 512)
        self.assertLessEqual(len(cb.WELCOME_TEXT), 4096)

    def test_main_requires_token(self):
        with mock.patch.dict("os.environ", {"BOT_TOKEN": ""}):
            self.assertEqual(cb.main([]), 2)


if __name__ == "__main__":
    unittest.main()
