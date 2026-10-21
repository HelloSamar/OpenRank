"""Tests for responder.py (the /start /quizlists /request /contact replies)."""

import sys
import unittest
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import responder as rs

Result = namedtuple("Result", "ok data status description")
OWNER = 900
USER = {"id": 42, "first_name": "Asha", "last_name": "K", "username": "asha"}


class FakeApi:
    def __init__(self, fail_chats=()):
        self.calls, self.fail_chats = [], set(fail_chats)

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return Result(True, {"result": {"username": "PYQxBot"}}, 200, "")
        if method == "sendMessage" and payload["chat_id"] in self.fail_chats:
            return Result(False, {"description": "blocked"}, 403, "blocked")
        return Result(True, {"result": True}, 200, "")

    def sent(self, chat_id=None):
        return [p for m, p in self.calls
                if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)]

    def texts(self, chat_id):
        return [p["text"] for p in self.sent(chat_id)]


def msg(text, user=USER, chat_type="private", reply=None, chat_id=None):
    message = {"chat": {"id": chat_id if chat_id is not None else user["id"], "type": chat_type},
               "from": user, "text": text}
    if reply is not None:
        message["reply_to_message"] = reply
    return {"update_id": 1, "message": message}


def bot_message(text):
    return {"from": {"id": 1, "is_bot": True}, "text": text}


def make(owner=OWNER, fail_chats=(), max_per_user=5):
    api = FakeApi(fail_chats)
    return api, rs.Responder(api, lambda: "QUIZ LIST", owner, max_per_user)


class BasicCommandTests(unittest.TestCase):
    def test_start_greets_by_name_and_mentions_the_delay(self):
        api, bot = make()
        bot.handle(msg("/start"))
        text = api.texts(42)[0]
        self.assertIn("Hi Asha!", text)
        self.assertIn("15 minutes", text)
        for command in ("/quizlists", "/request", "/contact"):
            self.assertIn(command, text)

    def test_quizlists_sends_the_catalog(self):
        api, bot = make()
        bot.handle(msg("/quizlists"))
        self.assertEqual(api.texts(42), ["QUIZ LIST"])

    def test_bot_name_suffix_and_case(self):
        api, bot = make()
        bot.handle(msg("/QuizLists@PYQxBot"))
        self.assertEqual(api.texts(42), ["QUIZ LIST"])

    def test_unknown_command_and_plain_text_get_a_hint(self):
        api, bot = make()
        bot.handle(msg("/score"))
        bot.handle(msg("hello there"))
        self.assertEqual(api.texts(42), [rs.HINT_TEXT, rs.HINT_TEXT])

    def test_command_list_is_valid_for_botfather(self):
        self.assertEqual([c for c, _ in rs.COMMANDS], ["start", "quizlists", "request", "contact"])
        for command, description in rs.COMMANDS:
            self.assertRegex(command, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(3 <= len(description) <= 256)

    def test_parse_command(self):
        self.assertEqual(rs.parse_command("/request@Bot  SSC CGL  "), ("request", "SSC CGL"))
        self.assertEqual(rs.parse_command("/start"), ("start", ""))
        self.assertEqual(rs.parse_command("  just text "), ("", "just text"))


class ForwardingTests(unittest.TestCase):
    def test_request_with_text_goes_to_the_owner(self):
        api, bot = make()
        bot.handle(msg("/request SSC CGL - Reasoning - syllogisms"))
        note = api.texts(OWNER)[0]
        self.assertTrue(note.startswith(f"{rs.REQUEST_MARKER} #u42"))
        for part in ("Asha K", "@asha", "id 42", "SSC CGL - Reasoning - syllogisms",
                     "Reply to this message"):
            self.assertIn(part, note)
        self.assertEqual(api.texts(42), [rs.SENT_TEXT["request"]])

    def test_contact_with_text_goes_to_the_owner(self):
        api, bot = make()
        bot.handle(msg("/contact Please add maths quizzes"))
        note = api.texts(OWNER)[0]
        self.assertTrue(note.startswith("\u2709\uFE0F Message #u42"))
        self.assertIn("Please add maths quizzes", note)
        self.assertEqual(api.texts(42), [rs.SENT_TEXT["contact"]])

    def test_empty_command_asks_with_a_force_reply_prompt(self):
        api, bot = make()
        for command, kind in (("/request", "request"), ("/contact", "contact")):
            bot.handle(msg(command))
            prompt = api.sent(42)[-1]
            self.assertTrue(prompt["text"].startswith(rs.MARKERS[kind]))
            self.assertTrue(prompt["reply_markup"]["force_reply"])
        self.assertEqual(api.sent(OWNER), [])               # nothing forwarded yet

    def test_reply_to_the_prompt_is_forwarded(self):
        api, bot = make()
        bot.handle(msg("Banking - data interpretation", reply=bot_message(rs.REQUEST_PROMPT)))
        self.assertTrue(api.texts(OWNER)[0].startswith(rs.REQUEST_MARKER))
        self.assertIn("Banking - data interpretation", api.texts(OWNER)[0])
        bot.handle(msg("Thanks for the quizzes!", reply=bot_message(rs.CONTACT_PROMPT)))
        self.assertIn("Message #u42", api.texts(OWNER)[1])

    def test_reply_to_some_other_bot_message_is_just_chatter(self):
        api, bot = make()
        bot.handle(msg("hi", reply=bot_message("Some other message")))
        self.assertEqual(api.sent(OWNER), [])

    def test_without_an_owner_nothing_is_forwarded(self):
        api, bot = make(owner=None)
        bot.handle(msg("/request something"))
        self.assertEqual(api.texts(42), [rs.UNAVAILABLE_TEXT])
        self.assertEqual(len(api.sent()), 1)

    def test_owner_who_never_started_the_bot_gives_an_apology(self):
        api, bot = make(fail_chats={OWNER})
        bot.handle(msg("/contact hello"))
        self.assertEqual(api.texts(42), [rs.UNAVAILABLE_TEXT])

    def test_too_long_text_is_refused(self):
        api, bot = make()
        bot.handle(msg("/request " + "x" * (rs.MAX_TEXT_LENGTH + 1)))
        self.assertIn("too long", api.texts(42)[0])
        self.assertEqual(api.sent(OWNER), [])


class OwnerReplyTests(unittest.TestCase):
    NOTE = (f"{rs.REQUEST_MARKER} #u42\nFrom: Asha K\n\nSSC please\n\n"
            "Reply to this message to answer.")

    def test_owner_reply_is_relayed_to_the_person(self):
        api, bot = make()
        owner = {"id": OWNER, "first_name": "Owner"}
        bot.handle(msg("Done, quiz added tomorrow", user=owner, reply=bot_message(self.NOTE)))
        self.assertEqual(api.texts(42),
                         ["\U0001F4AC Reply from the owner:\n\nDone, quiz added tomorrow"])
        self.assertEqual(api.texts(OWNER), ["\u2705 Sent."])

    def test_failed_delivery_is_reported_to_the_owner(self):
        api, bot = make(fail_chats={42})
        owner = {"id": OWNER, "first_name": "Owner"}
        bot.handle(msg("hello", user=owner, reply=bot_message(self.NOTE)))
        self.assertIn("Could not deliver", api.texts(OWNER)[0])

    def test_only_the_owner_can_relay(self):
        api, bot = make()
        bot.handle(msg("send this to 42", user={"id": 77, "first_name": "Eve"},
                       reply=bot_message(self.NOTE)))
        self.assertEqual(api.sent(42), [])                        # nothing relayed
        self.assertEqual(api.texts(77), [rs.HINT_TEXT])

    def test_owner_can_still_use_the_commands_and_plain_replies_are_ignored(self):
        api, bot = make()
        owner = {"id": OWNER, "first_name": "Owner"}
        bot.handle(msg("/start", user=owner))
        self.assertIn("Hi Owner!", api.texts(OWNER)[0])
        bot.handle(msg("note to self", user=owner, reply=bot_message("no tag here")))
        self.assertEqual(len(api.texts(OWNER)), 2)                # only the hint

    def test_long_relay_is_cut(self):
        api, bot = make()
        owner = {"id": OWNER, "first_name": "Owner"}
        bot.handle(msg("y" * 6000, user=owner, reply=bot_message(self.NOTE)))
        self.assertLessEqual(len(api.texts(42)[0]), 4096)


class GroupAndSafetyTests(unittest.TestCase):
    def test_group_command_gets_one_pointer_to_the_private_chat(self):
        api, bot = make()
        group = {"chat_id": -100123, "chat_type": "supergroup"}
        bot.handle(msg("/quizlists@PYQxBot", **group))
        bot.handle(msg("/start@PYQxBot", user={"id": 8, "first_name": "B"}, **group))
        self.assertEqual(api.texts(-100123),
                         ["Please send this command to me in a private chat: https://t.me/PYQxBot"])

    def test_group_chatter_and_unknown_group_commands_are_ignored(self):
        api, bot = make()
        bot.handle(msg("good morning everyone", chat_type="supergroup", chat_id=-5))
        bot.handle(msg("/somethingelse", chat_type="supergroup", chat_id=-5))
        self.assertEqual(api.calls, [])

    def test_bots_and_broken_updates_are_ignored(self):
        api, bot = make()
        bot.handle(msg("/start", user={"id": 5, "is_bot": True}))
        for update in ({}, {"message": "x"}, {"message": {}}, {"message": {"chat": {}, "from": {}}},
                       {"message": {"chat": {"id": 1}, "from": {"id": 2}, "text": 5}},
                       {"message": {"chat": {"id": "1"}, "from": {"id": 2}, "text": "/start"}}):
            bot.handle(update)
        self.assertEqual(api.calls, [])

    def test_spam_guard_limits_answers_per_person(self):
        api, bot = make(max_per_user=3)
        for _ in range(8):
            bot.handle(msg("/quizlists"))
        self.assertEqual(len(api.texts(42)), 3)
        bot.handle(msg("/quizlists", user={"id": 43, "first_name": "Other"}))
        self.assertEqual(len(api.texts(43)), 1)

    def test_wanted_keeps_only_relevant_messages(self):
        yes = [msg("anything", chat_type="private"), msg("/start", chat_type="group"),
               msg("answer", chat_type="group", reply=bot_message("x"))]
        no = [msg("chatter", chat_type="supergroup"), {"poll_answer": {}}, {"message": {"text": 1}},
              {"message": {"chat": {"type": "private"}}}]
        self.assertTrue(all(rs.wanted(u) for u in yes))
        self.assertFalse(any(rs.wanted(u) for u in no))


if __name__ == "__main__":
    unittest.main()
