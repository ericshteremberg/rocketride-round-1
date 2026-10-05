"""Exercise the CLI as a real process without depending on internet access."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from rocketride import import_issues
from tests.helpers import Transport, issue


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "cli.sqlite3"

    def command(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "rocketride", *arguments],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        )

    def test_read_prints_json_and_exits_successfully(self):
        seeded = import_issues("octocat/hello-world", self.db, transport=Transport([issue(5)]))
        self.assertIs(seeded["ok"], True, seeded)
        completed = self.command("read", "octocat/hello-world", "--db", str(self.db))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIs(result["ok"], True)
        self.assertEqual(result["issues"], seeded["issues"])
        self.assertEqual(completed.stderr, "")

    def test_invalid_repository_prints_json_and_exits_one(self):
        completed = self.command("import", "invalid", "--db", str(self.db))
        self.assertEqual(completed.returncode, 1, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error"]["code"], "INVALID_REPOSITORY")
        self.assertFalse(self.db.exists())
        self.assertNotIn("Traceback", completed.stderr)

    def test_missing_database_prints_json_and_exits_one(self):
        completed = self.command("read", "octocat/hello-world", "--db", str(self.db))
        self.assertEqual(completed.returncode, 1, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["error"]["code"], "DATABASE_NOT_FOUND")
        self.assertFalse(self.db.exists())

    def test_help_documents_import_read_and_database_option(self):
        completed = self.command("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("import", completed.stdout)
        self.assertIn("read", completed.stdout)
        command_help = self.command("import", "--help")
        self.assertEqual(command_help.returncode, 0)
        self.assertIn("--db", command_help.stdout)


if __name__ == "__main__":
    unittest.main()
