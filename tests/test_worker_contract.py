"""The contract between the Python bot and the Cloudflare Worker, and the Worker's setup files.

The Worker has its own tests (worker/test, run with `npm test`). These tests check what the two
systems must agree on, and that nothing secret ends up in the Worker's files.
"""

import json
import os
import re
import sqlite3
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quiz_bot as qb

REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "worker"
TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")


def database():
    connection = sqlite3.connect(":memory:")
    connection.executescript((REPO / "schema.sql").read_text(encoding="utf-8"))
    return connection


class SchemaTests(unittest.TestCase):
    def test_the_schema_is_valid_sqlite_with_the_expected_tables(self):
        tables = [r[0] for r in database().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        expected = ["budget_daily", "chats", "plans", "runs", "scores", "usage_daily"]
        self.assertEqual(tables, expected)

    def test_the_migration_wrangler_applies_is_identical(self):
        self.assertEqual((WORKER / "migrations" / "0001_init.sql").read_text(encoding="utf-8"),
                         (REPO / "schema.sql").read_text(encoding="utf-8"))

    def test_no_table_can_hold_a_raw_telegram_user_id(self):
        connection = database()
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        for (table,) in tables.fetchall():
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            self.assertNotIn("user_id", columns, table)

    def test_only_one_quiz_can_run_per_chat(self):
        connection = database()
        connection.execute("INSERT INTO chats VALUES (-1, 'group', NULL, 1, NULL, 1, 1)")
        insert = ("INSERT INTO runs (run_id, chat_id, quiz_id, total, gap_seconds, state, "
                  "created_at, updated_at) VALUES (?, -1, 'q', 5, 10, ?, 1, 1)")
        connection.execute(insert, ("a", "running"))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(insert, ("b", "paused"))
        connection.execute("UPDATE runs SET state = 'done'")
        connection.execute(insert, ("c", "running"))

    def test_the_gap_between_questions_matches_telegrams_limits(self):
        connection = database()
        connection.execute("INSERT INTO chats VALUES (-1, 'group', NULL, 1, NULL, 1, 1)")
        insert = ("INSERT INTO runs (run_id, chat_id, quiz_id, total, gap_seconds, state, "
                  "created_at, updated_at) VALUES (?, -1, 'q', 5, ?, 'done', 1, 1)")
        for gap in (5, 600):
            connection.execute(insert, (f"ok{gap}", gap))
        for gap in (4, 601):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, (f"bad{gap}", gap))


class UserKeyContractTests(unittest.TestCase):
    def test_python_and_the_worker_give_a_person_the_same_key(self):
        fixture = REPO / "tests/fixtures/user_key_vectors.json"
        vectors = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(vectors["vectors"]), 12)
        for vector in vectors["vectors"]:
            with mock.patch.dict(os.environ, {"SCORES_SALT": vector["salt"]}):
                self.assertEqual(qb.user_key(vector["user_id"]), vector["key"])


class WorkerSetupTests(unittest.TestCase):
    def test_wrangler_config_has_no_secrets_and_a_placeholder_database(self):
        config = tomllib.loads((WORKER / "wrangler.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["main"], "src/index.ts")
        database_config = config["d1_databases"][0]
        self.assertEqual(database_config["binding"], "DB")
        self.assertEqual(database_config["database_id"], "REPLACE_WITH_D1_ID")
        self.assertTrue((WORKER / database_config["migrations_dir"]).is_dir())
        for name in config["vars"]:
            self.assertNotRegex(name, r"TOKEN|SECRET|SALT|KEY|PASSWORD", name)
        self.assertEqual(config["vars"]["FREE_DAILY_QUESTIONS"], "100")
        self.assertEqual(config["vars"]["PAUSED"], "false")

    def test_dependencies_are_pinned_exactly_for_repeatable_builds(self):
        package = json.loads((WORKER / "package.json").read_text(encoding="utf-8"))
        self.assertTrue(package["devDependencies"])
        for name, version in package["devDependencies"].items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+", f"{name} should be pinned, not {version}")
        self.assertTrue((WORKER / "package-lock.json").exists())
        self.assertIn("test", package["scripts"])
        self.assertIn("typecheck", package["scripts"])

    def test_typescript_runs_in_strict_mode(self):
        tsconfig = json.loads((WORKER / "tsconfig.json").read_text(encoding="utf-8"))
        self.assertIs(tsconfig["compilerOptions"]["strict"], True)

    def test_no_worker_source_contains_a_token_or_a_hard_coded_secret(self):
        assignment = re.compile(r"(BOT_TOKEN|WEBHOOK_SECRET|SCORES_SALT)\s*=\s*[\"'][^\"']+[\"']")
        for path in list((WORKER / "src").glob("*.ts")) + list((WORKER / "test").rglob("*.ts")):
            self.assertIsNone(TOKEN.search(path.read_text(encoding="utf-8")), path.name)
        for path in (WORKER / "src").glob("*.ts"):
            for line in path.read_text(encoding="utf-8").splitlines():
                self.assertIsNone(assignment.search(line), f"{path.name}: {line.strip()}")

    def test_node_modules_are_never_committed(self):
        ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("node_modules/", ignored)


class WorkflowTests(unittest.TestCase):
    def read(self, name):
        return (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_the_deploy_workflow_uses_its_own_bot_not_the_group_bot(self):
        text = self.read("deploy-worker.yml")
        self.assertIn("secrets.WORKER_BOT_TOKEN", text)
        for forbidden in ("secrets.BOT_TOKEN", "secrets.CHAT_ID", "secrets.OWNER_CHAT_ID"):
            self.assertNotIn(forbidden, text)

    def test_the_deploy_workflow_refuses_to_run_without_the_confirmation(self):
        text = self.read("deploy-worker.yml")
        self.assertIn("confirm_new_bot", text)
        self.assertRegex(text, r'if \[ "\$CONFIRM" != "true" \]')

    def test_the_deploy_workflow_tests_before_it_deploys(self):
        text = self.read("deploy-worker.yml")
        self.assertLess(text.index("npm test"), text.index("wrangler@4.152.0 deploy"))

    def test_the_deploy_workflow_never_prints_secrets(self):
        text = self.read("deploy-worker.yml")
        self.assertNotIn("set -x", text)
        self.assertNotRegex(text, r"echo[^\n]*\$\{?WORKER_(BOT_TOKEN|WEBHOOK_SECRET|SCORES_SALT)")
        self.assertIsNone(TOKEN.search(text))

    def test_validation_runs_the_worker_tests(self):
        text = self.read("validate.yml")
        for needle in ("worker/**", "schema.sql", "npm run typecheck", "npm test", "node-version"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
