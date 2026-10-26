"""Guards against committing secrets or personal data by accident."""

import json
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")           # the shape of a Telegram bot token
MAX_FILE_BYTES = 5 * 1024 * 1024


def tracked_files():
    """Files in git, or (outside a git checkout) every file except tooling folders."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
        names = [n for n in out.stdout.decode("utf-8").split("\0") if n]
        return [REPO / n for n in names if (REPO / n).is_file()]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", "node_modules", ".wrangler", "__pycache__"}
        return [p for p in REPO.rglob("*") if p.is_file() and not skip & set(p.parts)]


class RepoHygieneTests(unittest.TestCase):
    def test_no_telegram_bot_token_in_any_file(self):
        for path in tracked_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(TOKEN.search(text),
                              f"{path.name} contains something that looks like a bot token; "
                              "remove it and revoke the token")

    def test_no_secret_or_database_files_are_tracked(self):
        bad = re.compile(r"(^|/)(\.env(\..*)?|\.dev\.vars)$|\.(db|sqlite3?|pem|key|db-wal|db-shm)$")
        for path in tracked_files():
            relative = path.relative_to(REPO).as_posix()
            if relative.endswith(".env.example"):
                continue
            self.assertIsNone(bad.search(relative), f"{relative} must not be committed")

    def test_gitignore_covers_secrets_and_local_data(self):
        lines = {line.strip() for line in (REPO / ".gitignore").read_text().splitlines()}
        for needed in (".env", ".dev.vars", "*.db", "*.sqlite", "*.sqlite3", "node_modules/",
                       ".wrangler/", "__pycache__/", "!.env.example"):
            self.assertIn(needed, lines, f".gitignore should list {needed}")

    def test_no_file_is_unreasonably_big(self):
        for path in tracked_files():
            self.assertLessEqual(path.stat().st_size, MAX_FILE_BYTES, path.name)

    def test_scores_json_stores_no_raw_telegram_ids(self):
        path = REPO / "scores.json"
        if not path.exists():
            self.skipTest("no scores.json")
        for week in json.loads(path.read_text(encoding="utf-8")).get("weeks", {}).values():
            for key in week.get("users", {}):
                self.assertFalse(key.isdigit(), "scores.json must use anonymous keys, not "
                                                "Telegram IDs")

    def test_workflows_never_contain_secret_values(self):
        for path in (REPO / ".github" / "workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(TOKEN.search(text), path.name)
            for line in text.splitlines():
                if re.match(r"\s+(BOT_TOKEN|SCORES_SALT|OWNER_CHAT_ID|CHAT_ID|TEST_CHAT_ID):",
                            line):
                    self.assertIn("secrets.", line, f"{path.name}: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
