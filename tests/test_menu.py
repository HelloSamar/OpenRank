"""The bot repairs its own command menu (SYNC_COMMANDS)."""

import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz_bot as qb
import responder
import test_safety as ts

WANTED = [{"command": c, "description": d} for c, d in responder.COMMANDS]


class MenuCase(ts.SafetyCase):
    def setUp(self):
        super().setUp()
        self.fake.menus = {}                  # these tests start with NO menu
        self.schedule()                       # only the far-future filler entry: nothing is due

    def calls(self, method):
        return [scope for m, scope in self.fake.menu_calls if m == method]

    def restored_alerts(self):
        return [a for a in self.fake.alerts() if "command menu" in a]


class RepairTests(MenuCase):
    def test_a_missing_menu_is_set_and_the_owner_is_told_once(self):
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(self.fake.menus["default"], WANTED)
        self.assertEqual(len(self.restored_alerts()), 1)
        self.assertIn("restored it", self.restored_alerts()[0])
        self.run_it(ts.NOW + timedelta(minutes=15))
        self.assertEqual(self.calls("setMyCommands"), ["default"])         # only the first time
        self.assertEqual(len(self.restored_alerts()), 1)

    def test_a_menu_with_wrong_text_is_corrected(self):
        self.fake.menus["default"] = [dict(WANTED[0], description="old words"), *WANTED[1:]]
        self.run_it()
        self.assertEqual(self.fake.menus["default"], WANTED)

    def test_an_old_menu_with_other_commands_is_replaced(self):
        self.fake.menus["default"] = [{"command": "quiz", "description": "Start a practice quiz"}]
        self.run_it()
        self.assertEqual([c["command"] for c in self.fake.menus["default"]],
                         ["start", "quizlists", "contact"])

    def test_the_command_order_matters_and_is_restored(self):
        self.fake.menus["default"] = list(reversed(WANTED))
        self.run_it()
        self.assertEqual(self.fake.menus["default"], WANTED)

    def test_a_correct_menu_is_left_alone_without_any_alert(self):
        self.fake.menus["default"] = list(WANTED)
        self.run_it()
        self.assertEqual(self.calls("setMyCommands") + self.calls("deleteMyCommands"), [])
        self.assertEqual(self.restored_alerts(), [])


class ScopeOverrideTests(MenuCase):
    def test_a_different_list_for_private_chats_is_removed(self):
        self.fake.menus["default"] = list(WANTED)
        self.fake.menus["all_private_chats"] = [{"command": "old", "description": "Old thing"}]
        self.run_it()
        self.assertNotIn("all_private_chats", self.fake.menus)
        self.assertEqual(self.fake.menus["default"], WANTED)
        self.assertIn("all private chats", self.restored_alerts()[0])

    def test_every_kind_of_override_is_checked(self):
        for scope in qb.COMMAND_SCOPES:
            self.fake.menus[scope] = [{"command": "x", "description": "Something else"}]
        self.fake.menus["default"] = list(WANTED)
        self.run_it()
        self.assertEqual(sorted(self.calls("deleteMyCommands")), sorted(qb.COMMAND_SCOPES))
        self.assertEqual(list(self.fake.menus), ["default"])

    def test_an_override_that_is_already_right_is_kept(self):
        self.fake.menus["default"] = list(WANTED)
        self.fake.menus["all_private_chats"] = list(WANTED)
        self.run_it()
        self.assertEqual(self.calls("deleteMyCommands"), [])
        self.assertEqual(self.fake.menus["all_private_chats"], WANTED)

    def test_empty_overrides_are_not_touched(self):
        self.fake.menus["default"] = list(WANTED)
        self.fake.menus["all_group_chats"] = []
        self.run_it()
        self.assertEqual(self.calls("deleteMyCommands"), [])


class SafetyOfTheRepairTests(MenuCase):
    def test_a_refusing_telegram_never_stops_the_quizzes(self):
        self.schedule(("ows900", ts.NOW - timedelta(hours=1)))
        self.fake.fail_menu = True
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(len(self.fake.polls), 3)
        self.assertEqual(self.restored_alerts(), [])

    def test_an_unexpected_error_never_escapes(self):
        with mock.patch.object(qb, "_sync_bot_commands", side_effect=RuntimeError("boom")):
            self.assertFalse(qb.sync_bot_commands("tok"))
        self.assertEqual(self.run_it(), 0)

    def test_it_can_be_switched_off(self):
        with mock.patch.object(qb, "SYNC_COMMANDS", False):
            self.run_it()
        with mock.patch.dict(os.environ, {"DISABLE_COMMANDS": "true"}):
            self.run_it(ts.NOW + timedelta(minutes=15))
        self.assertEqual(self.fake.menu_calls, [])

    def test_the_menu_is_checked_before_messages_are_read(self):
        self.run_it()
        names = [method for method, _ in self.fake.calls]
        self.assertLess(names.index("getMyCommands"), names.index("getUpdates"))

    def test_dry_runs_and_manual_runs_never_touch_the_menu(self):
        self.assertIn(qb.run_scheduled("", "", True, now=ts.NOW), (0, 1))
        with mock.patch.dict(os.environ, {"BOT_TOKEN": "tok", "CHAT_ID": "-100"}), \
                mock.patch.object(qb.signal, "signal"):
            qb.main(["--quiz", "ows900"])
        self.assertEqual(self.fake.menu_calls, [])

    def test_the_menu_text_is_what_botfather_accepts(self):
        for command, description in responder.COMMANDS:
            self.assertRegex(command, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(3 <= len(description) <= 256)


if __name__ == "__main__":
    unittest.main()
