# RocketRide GitHub issue connector

A small, dependency-free connector that imports one page of open GitHub issues into SQLite and reads the saved records offline. Imports are atomic, pull requests are excluded, and repeated imports update existing issues without creating duplicates.

## Run locally

Prerequisites: **Python 3.10 or later**, with the standard `sqlite3` module and SQLite 3.24 or later. Current standard Python installers include these. Internet access is needed only for imports and the live demo. No token, package install, service, or paid tool is required for public repositories.

```sh
cd rocketride-issue-connector
python --version
python -m rocketride import psf/requests --db issues.sqlite3
python -m rocketride read psf/requests --db issues.sqlite3
python -m unittest discover -s tests -v
```

Download or clone the source, then use `python3` if your system names Python that way. Commands run from the repository root. `--db` accepts a relative or absolute file path and defaults to `rocketride.sqlite3`; its parent directory must already exist. `:memory:` is rejected because calls must persist across program restarts.

Import and read commands write one JSON result to stdout. Success exits with `0`; connector and argument errors exit with `1`. `python -m rocketride --help` documents the commands. The optional `GITHUB_TOKEN` environment variable authenticates imports if you need a higher GitHub rate limit. Never commit a token. `--timeout 10` sets the HTTP timeout in seconds; valid values are greater than zero and at most 300.

## Reuse as functions

```python
from rocketride import import_issues, read_issues

result = import_issues("psf/requests", db_path="issues.sqlite3")
if result["ok"]:
    print(result["counts"])
else:
    print(result["error"]["code"], result["error"]["message"])

# Works later, in another process, without calling GitHub.
saved = read_issues("psf/requests", db_path="issues.sqlite3")
```

`import_issues(repository, db_path="rocketride.sqlite3", *, token=None, timeout=10, transport=None)` and `read_issues(repository, db_path="rocketride.sqlite3")` return JSON-compatible dictionaries. The function API takes an explicit token; the CLI reads `GITHUB_TOKEN`. The optional transport is a small testing seam with the same request/timeout shape as `urllib.request.urlopen`; production callers can omit it.

Example import result, **illustrative rather than live data**:

```json
{
  "ok": true,
  "repository": "example/project",
  "issues": [
    {
      "repository": "example/project",
      "number": 7,
      "title": "Clarify setup instructions",
      "url": "https://github.com/example/project/issues/7"
    }
  ],
  "counts": {
    "imported": 1,
    "inserted": 1,
    "updated": 0,
    "unchanged": 0,
    "pull_requests_skipped": 2,
    "total_saved": 1
  }
}
```

Reading that repository returns the same `ok`, `repository`, and `issues`, with `"total_saved": 1` at the top level. An identical second import reports `inserted: 0`, `updated: 0`, `unchanged: 1`, and `total_saved: 1`. A changed title or URL reports an update instead. Issue results are sorted by issue number.

Example invalid input:

```sh
python -m rocketride import invalid
```

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_REPOSITORY",
    "message": "Use a public GitHub repository in owner/name format."
  }
}
```

Error codes are stable; messages provide human-readable context. HTTP failures include a status when available. Missing repositories, rate limits, timeouts, invalid responses, and database failures are reported through the same error envelope. A failed import preserves previously saved records.

## What one page means

The request is `GET /repos/{owner}/{repo}/issues?state=open&per_page=100&page=1`. GitHub can mix pull requests into those 100 objects, so an import may contain fewer than 100 issues. No pagination is performed.

Saved data accumulates through upserts. An issue absent from a later page is **not deleted**: absence may mean page movement, and one page cannot establish that it closed. Consequently, reads are the stored observations for that repository, not a complete or continuously current list of all open issues. Repository names are normalized to lowercase; issue numbers are unique within a repository. Repository renames are not automatically merged with earlier local names.

## Verify and demonstrate

```sh
# Deterministic unit and integration tests; no network or token required.
python -m unittest discover -s tests -v

# Real API calls; saves every full result as JSON for inspection.
python scripts/demo.py --record demo-recording.json
```

The live demo uses a temporary database and separate Python processes. It demonstrates a real public import, an offline read with socket connections disabled, a repeated real import with unique issue identities, a real API 404, and preservation of saved data after the error. It removes `GITHUB_TOKEN` from child environments so the demonstration does not depend on credentials. GitHub data can change between calls; the demo validates uniqueness and explains counts rather than assuming the upstream page is frozen.

The tests cover updates, unchanged reimports, multiple repositories, pull-request filtering, persistence, offline reads, invalid input, API failures, malformed responses, and rollback boundaries. The included CI workflow configures the same suite on Linux and Windows with Python 3.10 and 3.14; the remote matrix has not been run. See [Architecture.MD](Architecture.MD) for the design and [DEMO.md](DEMO.md) for the recording.

## AI use and verification

This project was built with **OpenAI Codex in the Codex desktop app**, including parallel implementation, test, and independent review agents. Codex helped interpret the brief, check official documentation, implement the connector, generate adversarial tests, debug failures, and prepare the demo and documentation. This is disclosed as AI-assisted work, not unaided authorship.

The unfamiliar integration problem addressed with AI was the meaning of a GitHub “issues” page. Treating every returned object as an issue would import pull requests, and treating one page as a complete snapshot could incorrectly delete saved records. The proposed approach was checked against GitHub’s official repository-issues documentation, then verified with mixed issue/PR fixtures, repeated-import tests, malformed-page tests, and a live GitHub call. The implementation filters by the presence of `pull_request` and uses an atomic composite-key upsert without inferring deletions.

Other tools used: Python `unittest`, `unittest.mock`, and `subprocess` for independent checks; SQLite for persistent storage and transaction validation; Git for local version control and GitHub CLI to verify official CI action references; a GitHub Actions workflow for the operating-system and Python-version matrix; FFmpeg and Pillow for rendering a captioned terminal demo from captured command results. Python, SQLite, and the standard library are the only runtime dependencies of the connector. The demo rendering tools are not needed to use or test it.

Before presenting this work, run the commands yourself and inspect the short implementation. Be ready to explain the primary key, transaction boundary, pull-request filter, and the limits of a one-page import.

## Primary references

- [GitHub list repository issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
- [GitHub REST API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions)
- [GitHub REST API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- [SQLite UPSERT](https://www.sqlite.org/lang_upsert.html)
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
