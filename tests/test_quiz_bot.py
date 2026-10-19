"""Regression tests for quiz_bot.py (no network, no real Telegram calls).

Run with:  python -m unittest discover -s tests -v
"""

import http.client
import json
import queue
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz_bot as qb

REPO = Path(__file__).resolve().parent.parent


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeTelegram:
    """Stands in for requests.Session: records messages, answers every poll correctly."""

    def __init__(self):
        self.messages, self.polls, self.delivered = [], [], 0

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        if method == "sendPoll":
            self.polls.append(json)
            poll_id = str(len(self.polls))
            return FakeResponse({"ok": True, "result": {"poll": {"id": poll_id}}})
        if method == "sendMessage":
            self.messages.append(json["text"])
            return FakeResponse({"ok": True, "result": {}})
        if method == "getUpdates":
            updates = []
            while self.delivered < len(self.polls):
                poll = self.polls[self.delivered]
                self.delivered += 1
                updates.append({
                    "update_id": 100 + self.delivered,
                    "poll_answer": {"poll_id": str(self.delivered),
                                    "user": {"id": 7, "first_name": "Asha"},
                                    "option_ids": [poll["correct_option_id"]]},
                })
            return FakeResponse({"ok": True, "result": updates})
        return FakeResponse({"ok": False, "description": "unknown"}, 404)


class SandboxCase(unittest.TestCase):
    """Runs the bot against a temporary directory with the waiting removed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "quizzes").mkdir()
        self.patches = [
            mock.patch.object(qb, "QUIZ_DIR", self.tmp / "quizzes"),
            mock.patch.object(qb, "SCHEDULE_FILE", self.tmp / "schedule.json"),
            mock.patch.object(qb, "SENT_FILE", self.tmp / "sent.json"),
            mock.patch.object(qb, "SCORES_FILE", self.tmp / "scores.json"),
            mock.patch.object(qb, "STATS_FILE", self.tmp / "stats.json"),
            mock.patch.object(qb, "UPDATES_OFFSET_FILE", self.tmp / ".offset"),
            mock.patch.object(qb, "WEBHOOK_ENABLED", False),
            mock.patch.object(qb.time, "sleep", lambda *_: None),
            mock.patch.object(qb.AnswerCollector, "wait",
                              lambda self, seconds: self._read_once(1)),
        ]
        for p in self.patches:
            p.start()
        qb.shutdown_event.clear()

    def tearDown(self):
        qb.shutdown_event.clear()
        for p in self.patches:
            p.stop()

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data), encoding="utf-8")

    def make_quiz(self, quiz_id="ows900", count=3):
        self.write(f"quizzes/{quiz_id}.json",
                   [[f"Question number {i}?", ["a", "b", "c"], i % 3] for i in range(count)])


class WeeklyTests(unittest.TestCase):
    def test_week_users_empty_and_present(self):
        start = qb.week_start_for(datetime(2026, 10, 20, tzinfo=timezone.utc))
        scores = {"weeks": {}, "weekly_posted": []}
        self.assertEqual(qb.week_users(scores, start), {})
        scores["weeks"][qb.week_key(start)] = {"users": {"1": {"name": "A"}}}
        self.assertEqual(qb.week_users(scores, start), {"1": {"name": "A"}})

    def test_post_weekly_if_due_posts_once(self):
        now = datetime(2026, 10, 25, 13, 45, tzinfo=timezone.utc)  # Sun 19:15 IST
        start = qb.week_start_for(now)
        scores = {"weeks": {qb.week_key(start): {"users": {
            "1": {"name": "Asha", "points": 4, "correct": 2, "wrong": 0,
                  "skipped": 0, "seconds": 10, "quizzes": 1}}}},
            "weekly_posted": []}
        sent = []
        with mock.patch.object(qb, "send_text", lambda *a, **k: sent.append(a[2]) or True), \
                mock.patch.object(qb, "save_data", lambda *a, **k: True):
            self.assertTrue(qb.post_weekly_if_due("t", "c", scores, now))
            self.assertTrue(qb.post_weekly_if_due("t", "c", scores, now))
        self.assertEqual(len(sent), 1)
        self.assertIn("Weekly Leaderboard", sent[0])
        self.assertEqual(scores["weekly_posted"], [qb.week_key(start)])

    def test_post_weekly_not_due_midweek(self):
        now = datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc)
        scores = {"weeks": {}, "weekly_posted": []}
        with mock.patch.object(qb, "send_text", side_effect=AssertionError):
            self.assertTrue(qb.post_weekly_if_due("t", "c", scores, now))


class RepoDataTests(unittest.TestCase):
    def test_dry_run_on_repository_data(self):
        self.assertEqual(qb.main(["--dry-run"]), 0)

    def test_schedule_and_quizzes_valid(self):
        self.assertTrue(qb.load_schedule())
        for path in (REPO / "quizzes").glob("*.json"):
            valid, skipped = qb.filter_valid(qb.load_quiz(path.stem))
            self.assertTrue(valid, path.name)
            self.assertEqual(skipped, 0, path.name)


class ScheduledRunTests(SandboxCase):
    def due_entry(self, quiz="ows900"):
        return {"quiz": quiz, "at": "2026-10-19T00:00Z", "timer": 10}

    def test_full_scheduled_run(self):
        self.make_quiz()
        self.write("schedule.json", [self.due_entry()])
        fake = FakeTelegram()
        now = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)
        with mock.patch.object(qb.requests, "Session", fake):
            code = qb.run_scheduled("tok", "-100", False, now=now)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.polls), 3)
        self.assertTrue(any("Final Result" in m and "Asha" in m for m in fake.messages))
        sent = json.loads((self.tmp / "sent.json").read_text())
        self.assertIn("ows900@2026-10-19T00:00Z", sent)
        scores = json.loads((self.tmp / "scores.json").read_text())
        week = next(iter(scores["weeks"].values()))["users"]["7"]
        self.assertEqual(week["correct"], 3)
        self.assertEqual(week["points"], 6.0)
        self.assertTrue((self.tmp / "stats.json").exists())

    def test_weekly_posted_at_end_of_run(self):
        self.write("schedule.json", [])
        now = datetime(2026, 10, 25, 13, 45, tzinfo=timezone.utc)
        start = qb.week_start_for(now)
        self.write("scores.json", {"weekly_posted": [], "weeks": {qb.week_key(start): {
            "users": {"1": {"name": "Ravi", "points": 3}}}}})
        fake = FakeTelegram()
        with mock.patch.object(qb.requests, "Session", fake):
            code = qb.run_scheduled("tok", "-100", False, now=now)
        self.assertEqual(code, 0)
        self.assertEqual(sum("Weekly Leaderboard" in m for m in fake.messages), 1)

    def test_shutdown_skips_remaining_quizzes_and_intro(self):
        self.make_quiz("ows900")
        self.make_quiz("ows901")
        entry = self.due_entry()
        entry["intro"] = "hello"
        self.write("schedule.json", [entry, dict(self.due_entry("ows901"))])
        fake = FakeTelegram()
        qb.shutdown_event.set()
        with mock.patch.object(qb.requests, "Session", fake):
            code = qb.run_scheduled("tok", "-100", False,
                                    now=datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc))
        self.assertEqual(code, 1)
        self.assertEqual(fake.polls, [])
        self.assertEqual(fake.messages, [])
        self.assertFalse((self.tmp / "sent.json").exists())

    def test_intro_not_sent_for_empty_quiz(self):
        self.write("quizzes/ows902.json", [])
        fake = FakeTelegram()
        with mock.patch.object(qb.requests, "Session", fake):
            outcome = qb.run_quiz([], "tok", "-100", "ows902", manual_intro="hi")
        self.assertEqual(outcome.sent, 0)
        self.assertEqual(fake.messages, [])

    def test_atomic_write_cleans_temp_file_on_error(self):
        target = self.tmp / "x.json"
        with self.assertRaises(TypeError):
            qb.write_json_atomic(target, {"bad": object()})
        self.assertEqual([p.name for p in self.tmp.glob(".tmp-*")], [])


class WebhookTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.queue_patch = mock.patch.object(qb, "WEBHOOK_UPDATES", queue.Queue())
        self.queue_patch.start()
        self.secret_patch = mock.patch.object(qb, "WEBHOOK_SECRET", "s3cret")
        self.secret_patch.start()
        self.port_patch = mock.patch.object(qb, "WEBHOOK_PORT", 0)
        self.port_patch.start()
        self.server = qb.run_webhook_server()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in (self.port_patch, self.secret_patch, self.queue_patch):
            p.stop()
        super().tearDown()

    def post(self, body, headers=None, raw_length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"X-Webhook-Secret": "s3cret", "Content-Type": "application/json"}
        hdrs.update(headers or {})
        if raw_length is not None:
            hdrs["Content-Length"] = raw_length
        conn.request("POST", "/", body=body, headers=hdrs)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_valid_poll_answer_is_queued_and_collected(self):
        answer = {"poll_id": "p1", "user": {"id": 5, "first_name": "Zed"}, "option_ids": [1]}
        self.assertEqual(self.post(json.dumps({"update_id": 1, "poll_answer": answer})), 200)
        with mock.patch.object(qb, "WEBHOOK_ENABLED", True):
            collector = qb.AnswerCollector(mock.Mock(), "tok", True, self.tmp / ".offset")
            collector._read_once(1)
        self.assertEqual(collector.answers["p1"][5][:2], ("Zed", [1]))

    def test_bad_requests_do_not_crash_or_queue(self):
        self.assertEqual(self.post("[1, 2]"), 200)             # non-object JSON
        self.assertEqual(self.post("not json"), 200)
        self.assertEqual(self.post("{}", raw_length="abc"), 400)
        self.assertEqual(self.post("{}", headers={"X-Webhook-Secret": "wrong"}), 403)
        self.assertTrue(qb.WEBHOOK_UPDATES.empty())


if __name__ == "__main__":
    unittest.main()
