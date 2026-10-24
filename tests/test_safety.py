"""Tests for the safety nets: owner alerts, late quizzes, name checks, rename helper,
test chat, anonymous scores and the weekly pin."""

import itertools
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz_bot as qb
import test_commands as tc

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%MZ")


class SafetyCase(tc.CommandCase):
    """CommandCase gives: a fake Telegram, OWNER_CHAT_ID=900, a quiz called ows900 (3 questions)."""

    def run_it(self, now=NOW):
        return qb.run_scheduled("tok", "-100", False, now=now)

    def schedule(self, *entries, filler=True):
        """Write schedule.json; a far-future entry stops the 'schedule is running out' alert."""
        items = [{"quiz": q, "at": iso(at), "timer": 10} for q, at in entries]
        if filler:
            items.append({"quiz": "ows900", "at": iso(NOW + timedelta(days=90)), "timer": 10})
        self.write("schedule.json", items)

    def sent_record(self):
        return json.loads((self.tmp / "sent.json").read_text())


# --------------------------------------------------------------------------- alerts
class AlertTests(SafetyCase):
    def test_a_problem_is_reported_once_per_period_and_repeated_later(self):
        self.assertTrue(qb.owner_alert("tok", "k1", "something broke", now=NOW))
        in_5h, in_7h = NOW + timedelta(hours=5), NOW + timedelta(hours=7)
        self.assertFalse(qb.owner_alert("tok", "k1", "something broke", now=in_5h))
        self.assertTrue(qb.owner_alert("tok", "k1", "something broke", now=in_7h))
        self.assertTrue(qb.owner_alert("tok", "k2", "another thing", now=NOW))      # other key
        self.assertEqual(len(self.fake.alerts()), 3)
        self.assertTrue(self.fake.alerts()[0].startswith("\u26A0\uFE0F something broke"))
        self.assertIn("k1", json.loads((self.tmp / "alerts.json").read_text()))

    def test_custom_repeat_period(self):
        qb.owner_alert("tok", "daily", "x", repeat_hours=24, now=NOW)
        self.assertFalse(qb.owner_alert("tok", "daily", "x", repeat_hours=24,
                                        now=NOW + timedelta(hours=23)))
        self.assertTrue(qb.owner_alert("tok", "daily", "x", repeat_hours=24,
                                       now=NOW + timedelta(hours=25)))

    def test_without_an_owner_nothing_is_sent(self):
        with mock.patch.dict(os.environ, {"OWNER_CHAT_ID": ""}):
            self.assertFalse(qb.owner_alert("tok", "k", "x", now=NOW))
        self.assertEqual(self.fake.sent, [])
        self.assertFalse((self.tmp / "alerts.json").exists())

    def test_an_unreachable_owner_is_retried_next_time(self):
        self.fake.fail_owner = True
        self.assertFalse(qb.owner_alert("tok", "k", "x", now=NOW))
        self.assertFalse((self.tmp / "alerts.json").exists())
        self.fake.fail_owner = False
        self.assertTrue(qb.owner_alert("tok", "k", "x", now=NOW))

    def test_an_alert_never_raises(self):
        with mock.patch.object(qb, "send_text", side_effect=RuntimeError("boom")):
            self.assertFalse(qb.owner_alert("tok", "k", "x", now=NOW))

    def test_old_alert_records_are_forgotten(self):
        self.write("alerts.json", {"old": iso(NOW - timedelta(days=40)),
                                   "recent": iso(NOW - timedelta(days=2))})
        qb.owner_alert("tok", "new", "x", now=NOW)
        self.assertEqual(sorted(json.loads((self.tmp / "alerts.json").read_text())),
                         ["new", "recent"])


class FailureAlertTests(SafetyCase):
    def test_a_missing_quiz_file_is_reported_once_not_at_every_check(self):
        self.schedule(("Missing_ows", NOW - timedelta(hours=1)))
        for minutes in (0, 15, 30, 45):
            self.assertEqual(self.run_it(NOW + timedelta(minutes=minutes)), 1)
        self.assertEqual(len(self.fake.alerts()), 1)
        text = self.fake.alerts()[0]
        self.assertIn("Missing", text)
        self.assertIn("could not be posted", text)
        self.assertEqual(self.fake.polls, [])

    def test_the_alert_says_when_a_similar_file_exists(self):
        self.make_quiz("Idioms_idm", 2)
        self.schedule(("idioms_idm", NOW - timedelta(hours=1)))
        self.run_it()
        self.assertIn("similar name exists: Idioms_idm", self.fake.alerts()[0])

    def test_nothing_posted_is_reported(self):
        self.fake.fail_polls = True
        self.schedule(("ows900", NOW - timedelta(hours=1)))
        self.assertEqual(self.run_it(), 1)
        self.assertEqual(len(self.fake.alerts()), 1)
        self.assertIn("Nothing could be posted", self.fake.alerts()[0])
        self.assertNotIn("ows900@" + iso(NOW - timedelta(hours=1)),
                         json.loads((self.tmp / "sent.json").read_text())
                         if (self.tmp / "sent.json").exists() else {})

    def test_a_broken_schedule_file_alerts_the_owner(self):
        (self.tmp / "schedule.json").write_text("{ not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BOT_TOKEN": "tok", "CHAT_ID": "-100"}), \
                mock.patch.object(qb.signal, "signal"):
            self.assertEqual(qb.main([]), 2)
        self.assertEqual(len(self.fake.alerts()), 1)
        self.assertIn("nothing will be posted", self.fake.alerts()[0])

    def test_manual_runs_do_not_send_alerts(self):
        (self.tmp / "schedule.json").write_text("{ not json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BOT_TOKEN": "tok", "CHAT_ID": "-100"}), \
                mock.patch.object(qb.signal, "signal"):
            qb.main(["--quiz", "Missing_ows"])
        self.assertEqual(self.fake.alerts(), [])

    def test_a_weekly_leaderboard_failure_is_reported(self):
        now = datetime(2026, 10, 25, 13, 45, tzinfo=timezone.utc)      # Sunday 19:15 IST
        start = qb.week_start_for(now)
        scores = {"weekly_posted": [], "weeks": {qb.week_key(start): {
            "users": {"u1": {"name": "Ravi", "points": 3}}}}}
        real = qb.send_message

        def refuse_group(token, chat_id, text, session=None):
            if chat_id == "-100":
                return qb.TelegramResult(ok=False, data={}, status=500)
            return real(token, chat_id, text, session)

        with mock.patch.object(qb, "send_message", refuse_group):
            self.assertFalse(qb.post_weekly_if_due("tok", "-100", scores, now))
        self.assertIn("weekly leaderboard could not be posted", self.fake.alerts()[0])


class ScheduleEndTests(SafetyCase):
    def test_a_schedule_about_to_run_out_is_reported_once_a_day(self):
        self.schedule(("ows900", NOW + timedelta(days=3)), filler=False)
        self.run_it()
        self.run_it(NOW + timedelta(hours=2))
        self.assertEqual(len(self.fake.alerts()), 1)
        self.assertIn("Only 3 day(s)", self.fake.alerts()[0])
        self.run_it(NOW + timedelta(hours=26))
        self.assertEqual(len(self.fake.alerts()), 2)

    def test_a_long_schedule_is_fine(self):
        self.schedule(("ows900", NOW + timedelta(days=60)), filler=False)
        self.run_it()
        self.assertEqual(self.fake.alerts(), [])

    def test_an_empty_schedule_is_reported(self):
        self.write("schedule.json", [])
        self.run_it()
        self.assertIn("No upcoming quizzes", self.fake.alerts()[0])


# --------------------------------------------------------------------------- late quizzes
class LateQuizTests(SafetyCase):
    def entries(self):
        return (("ows900", NOW - timedelta(hours=30)), ("ows900", NOW - timedelta(hours=20)),
                ("ows900", NOW - timedelta(hours=2)))

    def test_only_quizzes_within_the_limit_are_posted(self):
        self.schedule(*self.entries())
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(len(self.fake.polls), 3)                    # one quiz of 3 questions
        record = self.sent_record()
        late = [k for k, v in record.items() if v.get("late")]
        self.assertEqual(len(late), 2)
        self.assertEqual(len(record), 3)
        self.assertEqual(len(self.fake.alerts()), 1)
        self.assertIn("Skipped 2 quiz", self.fake.alerts()[0])
        self.assertIn("Run workflow", self.fake.alerts()[0])

    def test_skipped_quizzes_do_not_come_back_or_alert_again(self):
        self.schedule(*self.entries())
        self.run_it()
        self.run_it(NOW + timedelta(minutes=15))
        self.assertEqual(len(self.fake.polls), 3)
        self.assertEqual(len(self.fake.alerts()), 1)

    def test_the_limit_can_be_switched_off(self):
        self.schedule(*self.entries())
        with mock.patch.object(qb, "MAX_LATE_HOURS", 0), \
                mock.patch.object(qb, "DELAY_BETWEEN_POLLS", 0):
            self.run_it()
        self.assertEqual(len(self.fake.polls), 9)
        self.assertFalse(any(v.get("late") for v in self.sent_record().values()))

    def test_a_forgotten_record_update_after_a_rename_cannot_cause_a_burst(self):
        """Past entries that look 'never posted' are skipped, not posted all at once."""
        self.schedule(*[("ows900", NOW - timedelta(days=d, hours=1)) for d in (3, 2, 1)])
        self.run_it()
        self.assertEqual(self.fake.polls, [])

    def test_dry_run_labels_the_late_ones(self):
        self.schedule(*self.entries(), filler=False)
        sent_record, now = {}, NOW
        with self.assertLogs("quiz_bot", level="INFO") as logs:
            qb.dry_run_report(qb.load_schedule(), sent_record, {"weeks": {}, "weekly_posted": []},
                              {"questions": {}}, now)
        text = "\n".join(logs.output)
        self.assertEqual(text.count("LATE: skipped"), 2)
        self.assertEqual(text.count("| DUE "), 1)


# --------------------------------------------------------------------------- name checks
class NameCheckTests(SafetyCase):
    def report(self, schedule=()):
        with self.assertLogs("quiz_bot", level="INFO") as logs:
            problems = qb.dry_run_report(list(schedule), {}, {"weeks": {}, "weekly_posted": []},
                                         {"questions": {}}, NOW)
        return problems, "\n".join(logs.output)

    def test_names_that_differ_only_by_case_are_a_problem(self):
        self.make_quiz("Idioms_idm", 2)
        self.make_quiz("idioms_idm", 2)
        problems, text = self.report()
        self.assertGreaterEqual(problems, 1)
        self.assertIn("differ only by upper/lower case", text)

    def test_look_alike_characters_are_noted_but_not_a_failure(self):
        self.make_quiz("One\u2011Word_ows", 2)
        problems, text = self.report()
        self.assertEqual(problems, 0)
        self.assertIn("look-alike characters", text)
        self.assertIn("U+2011", text)

    def test_clean_names_produce_no_notes(self):
        self.make_quiz("One-Word_ows", 2)
        problems, text = self.report()
        self.assertEqual(problems, 0)
        self.assertNotIn("look-alike", text)

    def test_lookalike_characters_function(self):
        self.assertEqual(qb.lookalike_characters("plain name - ok"), [])
        found = qb.lookalike_characters("a\u00A0b\u2011c\u200Bd")
        self.assertEqual(len(found), 3)

    def test_similar_ids_ignore_case_and_dash_variants(self):
        self.make_quiz("One\u2011Word-Substitutions_ows", 2)
        self.assertEqual(qb.similar_quiz_ids("one-word-substitutions_OWS"),
                         ["One\u2011Word-Substitutions_ows"])
        self.assertEqual(qb.similar_quiz_ids("ows900"), [])
        with self.assertRaises(ValueError) as caught:
            qb.load_quiz("ONE-WORD-SUBSTITUTIONS_ows")
        self.assertIn("similar name exists", str(caught.exception))

    def test_the_schedule_length_is_shown(self):
        self.schedule(("ows900", NOW + timedelta(days=5)), filler=False)
        problems, text = self.report(qb.load_schedule())
        self.assertIn("less than 14 days are scheduled", text)
        self.assertEqual(problems, 0)


# --------------------------------------------------------------------------- rename helper
class RenameTests(SafetyCase):
    def setUp(self):
        super().setUp()
        self.make_quiz("Old Name_ows", 2)
        self.write("schedule.json", [
            {"quiz": "Old Name_ows", "at": "2026-10-19T00:00Z", "timer": 10},
            {"quiz": "ows900", "at": "2026-10-20T00:00Z", "timer": 10},
            {"quiz": "Old Name_ows", "at": "2026-10-21T00:00Z", "timer": 10}])
        self.write("sent.json", {"Old Name_ows@2026-10-19T00:00Z": {"sent": 2, "failed": 0},
                                 "ows900@2026-10-20T00:00Z": {"sent": 3, "failed": 0}})
        self.write("stats.json", {"questions": {"abc": {"q": "x", "quiz": "Old Name_ows"},
                                                "def": {"q": "y", "quiz": "ows900"}}})

    def files(self):
        return {p.name: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()
                and p.name not in ("alerts.json", ".offset")}

    def test_everything_is_renamed_together(self):
        lines = qb.rename_quiz("Old Name_ows", "New Name_ows")
        self.assertFalse((self.tmp / "quizzes" / "Old Name_ows.json").exists())
        self.assertTrue((self.tmp / "quizzes" / "New Name_ows.json").exists())
        schedule = json.loads((self.tmp / "schedule.json").read_text())
        self.assertEqual([e["quiz"] for e in schedule], ["New Name_ows", "ows900", "New Name_ows"])
        sent = json.loads((self.tmp / "sent.json").read_text())
        self.assertEqual(sorted(sent),
                         ["New Name_ows@2026-10-19T00:00Z", "ows900@2026-10-20T00:00Z"])
        stats = json.loads((self.tmp / "stats.json").read_text())
        self.assertEqual(stats["questions"]["abc"]["quiz"], "New Name_ows")
        self.assertEqual(stats["questions"]["def"]["quiz"], "ows900")
        self.assertTrue(any("2 reference(s) changed" in line for line in lines))

    def test_nothing_is_due_after_a_rename(self):
        qb.rename_quiz("Old Name_ows", "New Name_ows")
        schedule = qb.load_schedule()
        record = qb.load_sent()
        due = [e for e in schedule if e.when <= NOW and e.key not in record
               and e.quiz_id == "New Name_ows"]
        self.assertEqual([e.when.day for e in due], [21])        # the one never posted stays due

    def test_special_characters_and_escaped_forms(self):
        self.make_quiz("A\u2011B \u0939\u093f_ows", 1)
        escaped = '[{"quiz": "A\\u2011B \\u0939\\u093f_ows", "at": "2026-10-19T00:00Z"}]'
        (self.tmp / "schedule.json").write_text(escaped, encoding="utf-8")
        record = {"A\u2011B \u0939\u093f_ows@2026-10-19T00:00Z": {"sent": 1}}
        (self.tmp / "sent.json").write_text(json.dumps(record), encoding="utf-8")
        qb.rename_quiz("A\u2011B \u0939\u093f_ows", "Plain-name_ows")
        self.assertEqual(qb.load_schedule()[0].quiz_id, "Plain-name_ows")
        self.assertIn("Plain-name_ows@2026-10-19T00:00Z", qb.load_sent())

    def test_a_file_renamed_by_hand_only_needs_its_references_updated(self):
        folder = self.tmp / "quizzes"
        (folder / "Old Name_ows.json").rename(folder / "New Name_ows.json")
        lines = qb.rename_quiz("Old Name_ows", "New Name_ows")
        self.assertIn("already has the new name", lines[0])
        self.assertEqual(qb.load_schedule()[0].quiz_id, "New Name_ows")

    def test_preview_changes_nothing(self):
        before = self.files()
        lines = qb.rename_quiz("Old Name_ows", "New Name_ows", apply=False)
        self.assertTrue(lines[0].startswith("Preview only"))
        self.assertEqual(self.files(), before)

    def test_bad_requests_change_nothing(self):
        self.make_quiz("Taken_ows", 1)
        before = self.files()
        cases = [("Old Name_ows", "Old Name_ows"), ("Old Name_ows", "a/b"), ("Old Name_ows", ""),
                 ("../x", "New_ows"), ("Nope_ows", "New_ows"), ("Old Name_ows", "Taken_ows"),
                 ("Old Name_ows", "TAKEN_ows")]
        for old, new in cases:
            with self.assertRaises(ValueError, msg=(old, new)):
                qb.rename_quiz(old, new)
        self.assertEqual(self.files(), before)

    def test_a_failure_halfway_is_undone(self):
        before = self.files()
        real = qb._write_text_atomic
        calls = []

        def flaky(path, text):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError("disk full")
            real(path, text)

        with mock.patch.object(qb, "_write_text_atomic", flaky):
            with self.assertRaises(ValueError):
                qb.rename_quiz("Old Name_ows", "New Name_ows")
        self.assertEqual(self.files(), before)

    def test_command_line(self):
        with mock.patch.object(qb.signal, "signal"):
            self.assertEqual(qb.main(["--rename", "Old Name_ows", "New Name_ows", "--dry-run"]), 0)
            self.assertTrue((self.tmp / "quizzes" / "Old Name_ows.json").exists())
            self.assertEqual(qb.main(["--rename", "Old Name_ows", "New Name_ows"]), 0)
            self.assertTrue((self.tmp / "quizzes" / "New Name_ows.json").exists())
            self.assertEqual(qb.main(["--rename", "Old Name_ows", "Again_ows"]), 2)


# --------------------------------------------------------------------------- test chat
class TestChatTests(SafetyCase):
    def run_main(self, args, **env):
        base = {"BOT_TOKEN": "tok", "CHAT_ID": "-100", **env}
        with mock.patch.dict(os.environ, base), mock.patch.object(qb.signal, "signal"):
            return qb.main(args)

    def test_a_manual_quiz_goes_to_the_test_chat_and_changes_no_records(self):
        code = self.run_main(["--quiz", "ows900", "--test-chat"], TEST_CHAT_ID="-777")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.polls), 3)
        self.assertTrue(all(p["chat_id"] == "-777" for p in self.fake.polls))
        self.assertTrue(all(str(m["chat_id"]) != "-100" for m in self.fake.sent))
        for name in ("sent.json", "scores.json", "stats.json"):
            self.assertFalse((self.tmp / name).exists(), name)

    def test_without_the_flag_the_real_group_is_used(self):
        self.run_main(["--quiz", "ows900"], TEST_CHAT_ID="-777")
        self.assertTrue(all(p["chat_id"] == "-100" for p in self.fake.polls))

    def test_it_refuses_to_run_without_a_test_chat_or_a_manual_quiz(self):
        self.assertEqual(self.run_main(["--quiz", "ows900", "--test-chat"], TEST_CHAT_ID=""), 2)
        self.assertEqual(self.run_main(["--test-chat"], TEST_CHAT_ID="-777"), 2)
        self.assertEqual(self.run_main(["--quiz", "ows900", "--test-chat"], TEST_CHAT_ID="oops"), 2)
        self.assertEqual(self.fake.polls, [])


# --------------------------------------------------------------------------- anonymous scores
class AnonymousScoresTests(SafetyCase):
    def test_keys_are_stable_hashes_without_the_id(self):
        key = qb.user_key(8630381516)
        self.assertEqual(key, qb.user_key(8630381516))
        self.assertNotEqual(key, qb.user_key(8630381517))
        self.assertNotIn("8630381516", key)
        self.assertRegex(key, r"^u[0-9a-f]{16}$")

    def test_the_secret_changes_the_key(self):
        with mock.patch.dict(os.environ, {"SCORES_SALT": "one"}):
            first = qb.user_key(5)
        with mock.patch.dict(os.environ, {"SCORES_SALT": "two"}):
            second = qb.user_key(5)
        with mock.patch.dict(os.environ, {"SCORES_SALT": "", "BOT_TOKEN": "t1"}):
            third = qb.user_key(5)
        self.assertEqual(len({first, second, third}), 3)

    def test_old_scores_are_converted_and_merged(self):
        scores = {"weekly_posted": [], "weeks": {"w": {"users": {
            "123": {"name": "Asha", "points": 4.0, "correct": 2, "wrong": 0, "skipped": 1,
                    "seconds": 10.0, "quizzes": 1},
            qb.user_key(123): {"name": "Asha K", "points": 1.5, "correct": 1, "wrong": 1,
                               "skipped": 0, "seconds": 5.0, "quizzes": 1},
            "999": {"name": "Ravi", "points": 2.0}}}}}
        self.assertTrue(qb.anonymise_scores(scores))
        users = scores["weeks"]["w"]["users"]
        self.assertEqual(sorted(users), sorted([qb.user_key(123), qb.user_key(999)]))
        merged = users[qb.user_key(123)]
        self.assertEqual((merged["points"], merged["correct"], merged["quizzes"]), (5.5, 3, 2))
        self.assertEqual(merged["name"], "Asha K")
        self.assertFalse(qb.anonymise_scores(scores))                  # nothing left to do

    def test_the_first_scheduled_run_rewrites_scores_json_without_ids(self):
        self.write("schedule.json", [{"quiz": "ows900", "at": "2099-01-01T00:00Z"}])
        self.write("scores.json", {"weekly_posted": [], "weeks": {"2026-10-18T14:30Z": {"users": {
            "8630381516": {"name": "samar", "points": 10.0}}}}})
        self.run_it()
        text = (self.tmp / "scores.json").read_text()
        self.assertNotIn("8630381516", text)
        self.assertIn("samar", text)
        self.assertIn(qb.user_key(8630381516), text)

    def test_a_dry_run_does_not_touch_the_file(self):
        self.write("schedule.json", [])
        users = {"5": {"name": "A", "points": 1}}
        self.write("scores.json", {"weekly_posted": [], "weeks": {"w": {"users": users}}})
        before = (self.tmp / "scores.json").read_text()
        qb.run_scheduled("", "", True, now=NOW)
        self.assertEqual((self.tmp / "scores.json").read_text(), before)

    def test_new_scores_use_the_hash_and_the_leaderboard_still_shows_names(self):
        self.schedule(("ows900", NOW - timedelta(hours=1)))
        self.run_it()
        weeks = json.loads((self.tmp / "scores.json").read_text())["weeks"]
        users = next(iter(weeks.values()))["users"]
        self.assertEqual(list(users), [qb.user_key(7)])
        start = qb.week_start_for(NOW)
        message = qb.build_weekly_message(start, users, NOW)
        self.assertIn("Meera", message)


# --------------------------------------------------------------------------- weekly pin
class PinTests(SafetyCase):
    def weekly(self):
        now = datetime(2026, 10, 25, 13, 45, tzinfo=timezone.utc)
        start = qb.week_start_for(now)
        scores = {"weekly_posted": [], "weeks": {qb.week_key(start): {
            "users": {"u1": {"name": "Ravi", "points": 3}}}}}
        return qb.post_weekly_if_due("tok", "-100", scores, now), scores

    def test_off_by_default(self):
        self.assertFalse(qb.PIN_WEEKLY_LEADERBOARD)
        self.assertTrue(self.weekly()[0])
        self.assertEqual(self.fake.pinned, [])

    def test_the_leaderboard_is_pinned_when_switched_on(self):
        with mock.patch.object(qb, "PIN_WEEKLY_LEADERBOARD", True):
            ok, _ = self.weekly()
        self.assertTrue(ok)
        self.assertEqual(len(self.fake.pinned), 1)
        self.assertEqual(self.fake.pinned[0]["message_id"], 1)
        self.assertTrue(self.fake.pinned[0]["disable_notification"])

    def test_a_refused_pin_is_not_fatal(self):
        real_post = self.fake.post

        def refuse_pin(url, json=None, timeout=None):
            if url.endswith("pinChatMessage"):
                return tc.tq.FakeResponse({"ok": False, "description": "not enough rights"}, 400)
            return real_post(url, json=json, timeout=timeout)

        self.fake.post = refuse_pin
        with mock.patch.object(qb, "PIN_WEEKLY_LEADERBOARD", True):
            ok, scores = self.weekly()
        self.assertTrue(ok)
        self.assertEqual(len(scores["weekly_posted"]), 1)


# --------------------------------------------------------------------------- workflows
class WorkflowFileTests(unittest.TestCase):
    """Plain-text checks (no YAML library needed in CI)."""

    def read(self, name):
        return (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_the_scheduler_avoids_the_top_of_the_hour(self):
        match = re.search(r'cron:\s*"([^"]+)"', self.read("quiz.yml"))
        minutes = [int(m) for m in match.group(1).split()[0].split(",")]
        self.assertEqual(len(minutes), 4)
        self.assertNotIn(0, minutes)
        self.assertEqual([b - a for a, b in itertools.pairwise(minutes)], [15, 15, 15])

    def test_the_scheduler_has_the_new_inputs_and_secrets(self):
        text = self.read("quiz.yml")
        for needle in ("test_chat:", "type: boolean", "TEST_CHAT_ID", "SCORES_SALT",
                       "OWNER_CHAT_ID", "--test-chat", "alerts.json"):
            self.assertIn(needle, text)

    def test_the_rename_workflow_shares_the_scheduler_lock(self):
        rename = self.read("rename-quiz.yml")
        group = re.search(r"group:\s*(\S+)", self.read("quiz.yml")).group(1)
        self.assertIn(f"group: {group}", rename)
        for needle in ("workflow_dispatch", "old:", "new:", "contents: write", "--rename",
                       "--dry-run", "git push"):
            self.assertIn(needle, rename)

    def test_inputs_are_never_pasted_into_shell_scripts(self):
        """Typed input must reach the script only through env variables (no script injection)."""
        for name in ("quiz.yml", "rename-quiz.yml"):
            for line in self.read(name).splitlines():
                if "${{ inputs." in line:
                    allowed = r"^\s+([A-Z_]+: \$\{\{ inputs\.\w+ \}\}|if: .*)\s*$"
                    self.assertRegex(line, allowed, line)


if __name__ == "__main__":
    unittest.main()
