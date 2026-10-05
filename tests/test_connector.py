"""Contract tests with fake HTTP, real SQLite, and a fresh-process read."""

from contextlib import closing
from http.client import BadStatusLine, IncompleteRead
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from rocketride import import_issues, read_issues
from tests.helpers import Transport, issue


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "octocat/hello-world"


class ConnectorTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "snapshot.sqlite3"

    def import_page(self, payload, repository=REPOSITORY):
        return import_issues(repository, self.db, transport=Transport(payload))

    def assert_error(self, result, code):
        self.assertIs(result.get("ok"), False, result)
        self.assertEqual(result["error"]["code"], code)
        self.assertIsInstance(result["error"]["message"], str)
        self.assertTrue(result["error"]["message"].strip())
        json.dumps(result, allow_nan=False)


class ImportAndReadTests(ConnectorTestCase):
    def test_import_then_read_has_consistent_json_compatible_fields(self):
        result = self.import_page([issue(7, "Fix Unicode: café 🚀")])
        expected = [{"repository": REPOSITORY, "number": 7,
                     "title": "Fix Unicode: café 🚀",
                     "url": "https://github.com/octocat/hello-world/issues/7"}]
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["repository"], REPOSITORY)
        self.assertEqual(result["issues"], expected)
        self.assertEqual(result["counts"], {
            "imported": 1, "inserted": 1, "updated": 0, "unchanged": 0,
            "pull_requests_skipped": 0, "total_saved": 1,
        })
        saved = read_issues(REPOSITORY, self.db)
        self.assertEqual(saved, {"ok": True, "repository": REPOSITORY,
                                 "issues": expected, "total_saved": 1})
        json.dumps(result, allow_nan=False)
        json.dumps(saved, allow_nan=False)

    def test_pull_requests_are_excluded_before_issue_validation(self):
        result = self.import_page([
            issue(1),
            {"number": 2, "pull_request": {"url": "https://api.github.com/pulls/2"}},
            issue(3, pull_request={}),
        ])
        self.assertIs(result["ok"], True, result)
        self.assertEqual([row["number"] for row in result["issues"]], [1])
        self.assertEqual(result["counts"]["imported"], 1)
        self.assertEqual(result["counts"]["pull_requests_skipped"], 2)
        self.assertEqual(read_issues(REPOSITORY, self.db)["total_saved"], 1)

    def test_fetches_exactly_one_open_issue_page_even_with_next_link(self):
        transport = Transport([issue()], headers={
            "Link": '<https://api.github.com/repos/octocat/hello-world/issues?page=2>; rel="next"'
        })
        result = import_issues(REPOSITORY, self.db, timeout=3.5, transport=transport)
        self.assertIs(result["ok"], True, result)
        self.assertEqual(len(transport.calls), 1)
        request, timeout = transport.calls[0]
        parsed = urlparse(request.full_url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.github.com")
        self.assertEqual(parsed.path, "/repos/octocat/hello-world/issues")
        query = parse_qs(parsed.query)
        self.assertEqual(query["state"], ["open"])
        self.assertEqual(query["per_page"], ["100"])
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 3.5)
        self.assertTrue(transport.response.closed)

    def test_repeated_identical_import_is_unchanged_without_duplicates(self):
        page = [issue(2), issue(1)]
        self.assertIs(self.import_page(page)["ok"], True)
        result = self.import_page(page)
        self.assertEqual(result["counts"], {
            "imported": 2, "inserted": 0, "updated": 0, "unchanged": 2,
            "pull_requests_skipped": 0, "total_saved": 2,
        })
        self.assertEqual([row["number"] for row in result["issues"]], [1, 2])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM issues").fetchone()[0], 2)

    def test_unchanged_import_does_not_execute_database_updates(self):
        self.import_page([issue(1, "Original")])
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "CREATE TRIGGER reject_updates BEFORE UPDATE ON issues "
                "BEGIN SELECT RAISE(ABORT, 'updates prohibited by test'); END"
            )
            connection.commit()
        result = self.import_page([issue(1, "Original")])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["counts"]["unchanged"], 1)
        # Positive control: the trigger really does reject an attempted change.
        self.assert_error(self.import_page([issue(1, "Changed")]), "DATABASE_ERROR")
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"][0]["title"], "Original")

    def test_upsert_updates_title_and_url_and_counts_each_change(self):
        self.import_page([issue(1, "Before"), issue(2, "Keep")])
        new_url = "https://github.com/octocat/hello-world/issues/1?updated=1"
        result = self.import_page([
            issue(1, "After", html_url=new_url), issue(2, "Keep"), issue(3, "New"),
        ])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["counts"], {
            "imported": 3, "inserted": 1, "updated": 1, "unchanged": 1,
            "pull_requests_skipped": 0, "total_saved": 3,
        })
        self.assertEqual(result["issues"][0]["title"], "After")
        self.assertEqual(result["issues"][0]["url"], new_url)
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], result["issues"])

    def test_url_only_change_is_counted_as_update(self):
        self.import_page([issue(1, "Same title")])
        result = self.import_page([issue(1, "Same title", html_url="https://github.com/new-owner/new-name/issues/1")])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["counts"]["updated"], 1)
        self.assertEqual(result["counts"]["unchanged"], 0)

    def test_one_page_cannot_justify_deleting_previously_saved_issues(self):
        self.import_page([issue(1), issue(2)])
        result = self.import_page([issue(3)])
        self.assertEqual(result["counts"]["imported"], 1)
        self.assertEqual(result["counts"]["total_saved"], 3)
        self.assertEqual([row["number"] for row in result["issues"]], [1, 2, 3])

    def test_empty_page_is_success_and_preserves_previous_snapshot(self):
        self.import_page([issue(1)])
        result = self.import_page([])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["counts"], {
            "imported": 0, "inserted": 0, "updated": 0, "unchanged": 0,
            "pull_requests_skipped": 0, "total_saved": 1,
        })

    def test_empty_initial_import_can_be_read(self):
        result = self.import_page([])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], [])

    def test_case_variants_share_one_repository_and_one_issue(self):
        self.import_page([issue(1, "Before")])
        result = self.import_page([issue(1, "After")], "OctoCat/Hello-World")
        self.assertEqual(result["repository"], REPOSITORY)
        self.assertEqual(result["counts"]["updated"], 1)
        self.assertEqual(result["counts"]["total_saved"], 1)
        self.assertEqual(read_issues("OCTOCAT/HELLO-WORLD", self.db)["issues"], result["issues"])

    def test_repository_scope_prevents_number_collision_or_cross_repo_reads(self):
        other = "python/cpython"
        first = self.import_page([issue(1, "First repository")])
        second = self.import_page([issue(1, "Second repository", repository=other)], other)
        self.assertEqual(second["counts"]["inserted"], 1)
        self.assertEqual(second["counts"]["total_saved"], 1)
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], first["issues"])
        self.assertEqual(read_issues(other, self.db)["issues"], second["issues"])
        self.assertEqual(read_issues("owner/unknown", self.db)["total_saved"], 0)

    def test_configured_database_paths_are_isolated(self):
        other_db = Path(self.temp.name) / "other.sqlite3"
        self.import_page([issue(1)])
        result = import_issues(REPOSITORY, other_db, transport=Transport([issue(2)]))
        self.assertIs(result["ok"], True, result)
        self.assertEqual([row["number"] for row in read_issues(REPOSITORY, self.db)["issues"]], [1])
        self.assertEqual([row["number"] for row in read_issues(REPOSITORY, other_db)["issues"]], [2])

    def test_read_works_in_a_fresh_process_with_network_disabled(self):
        expected = self.import_page([issue(9, "Persisted after restart")])["issues"]
        script = (
            "import json, sys, urllib.request, socket; "
            "from unittest.mock import patch; from rocketride import read_issues; "
            "blocked = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network called')); "
            "urllib.request.urlopen = blocked; socket.create_connection = blocked; "
            "print(json.dumps(read_issues(sys.argv[1], sys.argv[2])))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, REPOSITORY, str(self.db)],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["issues"], expected)

    def test_read_never_invokes_http_or_socket(self):
        self.import_page([issue()])
        with patch("urllib.request.urlopen", side_effect=AssertionError("HTTP used")), \
             patch("socket.create_connection", side_effect=AssertionError("socket used")):
            result = read_issues(REPOSITORY, self.db)
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["total_saved"], 1)

    def test_titles_are_sql_data_not_sql_statements(self):
        title = "'); DROP TABLE issues; --"
        result = self.import_page([issue(1, title)])
        self.assertIs(result["ok"], True, result)
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"][0]["title"], title)


class ValidationAndFailureTests(ConnectorTestCase):
    def test_invalid_repository_fails_before_network_or_database_creation(self):
        invalid = ["", "owner", "/repo", "owner/", "owner/repo/extra", "../repo",
                   "https://github.com/owner/repo", "owner/repo?state=closed",
                   "owner/repo#fragment", "owner/repo with spaces", None, 123]
        for repository in invalid:
            with self.subTest(repository=repository):
                transport = Transport([issue()])
                result = import_issues(repository, self.db, transport=transport)
                self.assert_error(result, "INVALID_REPOSITORY")
                self.assertEqual(transport.calls, [])
                self.assertFalse(self.db.exists())
                self.assert_error(read_issues(repository, self.db), "INVALID_REPOSITORY")

    def test_http_failure_keeps_saved_data_and_returns_status(self):
        original = self.import_page([issue(1, "Keep me")])["issues"]
        error = HTTPError("https://api.github.com/example", 503, "Unavailable", {}, io.BytesIO(b'{"message":"Unavailable"}'))
        result = import_issues(REPOSITORY, self.db, transport=Transport(error=error))
        self.assert_error(result, "API_ERROR")
        self.assertEqual(result["error"]["status"], 503)
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], original)

    def test_unknown_or_private_repository_has_useful_404_error(self):
        error = HTTPError("https://api.github.com/example", 404, "Not Found", {}, io.BytesIO(b'{"message":"Not Found"}'))
        result = import_issues("missing-owner/missing-repo", self.db, transport=Transport(error=error))
        self.assert_error(result, "API_ERROR")
        self.assertEqual(result["error"]["status"], 404)
        self.assertFalse(self.db.exists())

    def test_rate_limit_has_distinct_code_and_retry_information(self):
        error = HTTPError("https://api.github.com/example", 429, "Too Many Requests", {"Retry-After": "60"}, io.BytesIO(b"{}"))
        result = import_issues(REPOSITORY, self.db, transport=Transport(error=error))
        self.assert_error(result, "RATE_LIMITED")
        self.assertEqual(result["error"]["status"], 429)
        self.assertEqual(str(result["error"]["retry_after"]), "60")

    def test_network_failure_is_json_error(self):
        result = import_issues(REPOSITORY, self.db,
                               transport=Transport(error=URLError("DNS lookup failed")))
        self.assert_error(result, "NETWORK_ERROR")
        self.assertFalse(self.db.exists())

    def test_incomplete_or_malformed_http_does_not_modify_saved_data(self):
        original = self.import_page([issue(1, "Original")])["issues"]
        for error in [IncompleteRead(b"partial"), BadStatusLine("not HTTP")]:
            with self.subTest(error=repr(error)):
                result = import_issues(REPOSITORY, self.db, transport=Transport(error=error))
                self.assert_error(result, "NETWORK_ERROR")
                self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], original)

    def test_invalid_timeout_fails_before_network(self):
        for timeout in [0, -1, 301, 1e300, float("nan"), float("inf"), True, "10", 10**1000]:
            with self.subTest(timeout=repr(timeout)):
                transport = Transport([issue()])
                result = import_issues(REPOSITORY, self.db, timeout=timeout, transport=transport)
                self.assert_error(result, "INVALID_CONFIGURATION")
                self.assertEqual(transport.calls, [])
                self.assertFalse(self.db.exists())

    def test_maximum_supported_timeout_is_accepted(self):
        transport = Transport([issue()])
        result = import_issues(REPOSITORY, self.db, timeout=300, transport=transport)
        self.assertIs(result["ok"], True, result)
        self.assertEqual(transport.calls[0][1], 300)

    def test_timeout_is_json_error(self):
        for error in [TimeoutError("Timed out"), URLError(socket.timeout("Timed out"))]:
            with self.subTest(error=repr(error)):
                result = import_issues(REPOSITORY, self.db, transport=Transport(error=error))
                self.assert_error(result, "TIMEOUT")
        self.assertFalse(self.db.exists())

    def test_non_200_returned_response_does_not_get_imported(self):
        result = import_issues(REPOSITORY, self.db,
                               transport=Transport([issue()], status=500))
        self.assert_error(result, "API_ERROR")
        self.assertFalse(self.db.exists())

    def test_malformed_json_fails_without_database_creation(self):
        for raw in [b"<html>Bad gateway</html>", b"[", b"\xff\xfe"]:
            with self.subTest(raw=raw):
                result = import_issues(REPOSITORY, self.db, transport=Transport(raw=raw))
                self.assert_error(result, "INVALID_API_RESPONSE")
                self.assertFalse(self.db.exists())

    def test_wrong_top_level_api_types_are_rejected(self):
        for payload in [{"message": "not a list"}, None, "issues", 42]:
            with self.subTest(payload=payload):
                self.assert_error(self.import_page(payload), "INVALID_API_RESPONSE")
                self.assertFalse(self.db.exists())

    def test_bad_later_record_does_not_partially_update_existing_snapshot(self):
        original = self.import_page([issue(1, "Original")])["issues"]
        bad_records = [
            None, "invalid", {}, issue(True), issue(0), issue(-2), issue("2"),
            issue(2**63), issue(2, title=None), issue(2, html_url=None),
            issue(2, html_url=""), issue(2, title="Broken surrogate: \ud800"),
            issue(2, html_url="https://github.com/owner/repo/issues/\ud800"),
        ]
        for bad_record in bad_records:
            with self.subTest(bad_record=bad_record):
                result = self.import_page([issue(1, "Must roll back"), issue(3, "Must not insert"), bad_record])
                self.assert_error(result, "INVALID_API_RESPONSE")
                self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], original)

    def test_duplicate_api_records_are_rejected_without_ambiguous_counts(self):
        original = self.import_page([issue(1, "Original")])["issues"]
        result = self.import_page([issue(1, "Version A"), issue(1, "Version B")])
        self.assert_error(result, "INVALID_API_RESPONSE")
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], original)

    def test_missing_database_read_is_explicit_and_does_not_create_file(self):
        result = read_issues(REPOSITORY, self.db)
        self.assert_error(result, "DATABASE_NOT_FOUND")
        self.assertFalse(self.db.exists())

    def test_corrupt_database_produces_useful_errors(self):
        self.db.write_bytes(b"This is not a SQLite database.")
        self.assert_error(read_issues(REPOSITORY, self.db), "DATABASE_ERROR")
        self.assert_error(self.import_page([issue()]), "DATABASE_ERROR")
        self.assertEqual(self.db.read_bytes(), b"This is not a SQLite database.")

    def test_directory_as_database_path_produces_useful_errors(self):
        directory = Path(self.temp.name)
        result = import_issues(REPOSITORY, directory, transport=Transport([issue()]))
        self.assert_error(result, "DATABASE_ERROR")
        self.assert_error(read_issues(REPOSITORY, directory), "DATABASE_ERROR")

    def test_database_failure_mid_batch_rolls_back_all_upserts(self):
        original = self.import_page([issue(1, "Before")])["issues"]
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "CREATE TRIGGER reject_second BEFORE INSERT ON issues "
                "WHEN NEW.number = 2 BEGIN SELECT RAISE(ABORT, 'test write failure'); END"
            )
            connection.commit()
        result = self.import_page([issue(1, "After"), issue(2, "Rejected")])
        self.assert_error(result, "DATABASE_ERROR")
        self.assertEqual(read_issues(REPOSITORY, self.db)["issues"], original)


if __name__ == "__main__":
    unittest.main()
