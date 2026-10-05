# Architecture

The connector fetches one public GitHub issues page, validates it, and atomically upserts its issue records into a configurable SQLite file. A separate read function queries only that file. The design favors a small, explainable integration with strong failure boundaries.

## Interface and components

`rocketride/__init__.py` exports `import_issues(repository, db_path, *, token=None, timeout=10, transport=None)` and `read_issues(repository, db_path)`. Both return JSON-compatible dictionaries with `ok`, a canonical `repository` when valid, and either saved `issues` or an `error` containing a stable `code` and useful `message`. HTTP errors also include `status`; rate-limit errors may include numeric `retry_after` seconds. Import success includes change counts; read success includes `total_saved`.

`rocketride/connector.py` owns input validation, HTTP access, response validation, SQLite transactions, and error translation. These responsibilities are separated into small private functions without extra framework layers. `rocketride/__main__.py` is a thin CLI adapter: parse arguments, obtain an optional environment token, call the public interface, print JSON, and set the exit code. `tests/` exercises the public API and CLI using real temporary SQLite files and a mocked HTTP transport. `scripts/demo.py` uses the live API and fresh subprocesses.

## Data flow

1. Validate `owner/name`, normalize its casing, and validate configuration.
2. Request `https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=100&page=1`, using GitHub's JSON media type, a User-Agent, and API version `2026-03-10`. No pagination or application retry loop is performed; the standard HTTP client may follow redirects.
3. Read a bounded response of at most 16 MiB. Parse the JSON list, skip every object containing `pull_request`, and validate all remaining records before opening a write transaction. Reject invalid fields, duplicate issue numbers, and invalid Unicode for the whole batch.
4. Open SQLite and execute `BEGIN IMMEDIATE`. Create the table if necessary, classify changes, and execute parameterized upserts. Read the repository's saved rows in ascending issue-number order, commit, then return success. Any failure before commit leaves earlier saved rows intact; connection closure rolls back uncommitted work.
5. Reads open the existing database with SQLite `mode=ro`, query the repository, and close the connection. They neither create a missing database nor invoke the HTTP path.

Network work happens before the transaction, keeping the write lock short. `BEGIN IMMEDIATE` prevents a competing writer from invalidating the inserted/updated/unchanged counts between classification and upsert. The five-second SQLite busy timeout gives a competing writer a bounded opportunity to finish.

## Schema

```sql
CREATE TABLE issues (
    repository TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number > 0),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (repository, number)
) WITHOUT ROWID;
```

The composite primary key enforces repository-scoped uniqueness and supports repository reads in number order. `WITHOUT ROWID` avoids a second row identifier for this natural key. `ON CONFLICT(repository, number) DO UPDATE` refreshes title and URL; a `WHERE` predicate skips writes when both are unchanged. Values are always SQL parameters. The stored URL is GitHub's `html_url`, not the API endpoint or a reconstructed link.

## Configuration and errors

The database defaults to `rocketride.sqlite3` and accepts a filesystem path; its parent must exist. In-memory databases are rejected because each operation opens a fresh connection. The API takes an explicit optional token; only the CLI reads `GITHUB_TOKEN`. Public imports need none. HTTP timeout defaults to ten seconds and accepts values greater than zero through 300 seconds.

The error family includes invalid repository/configuration/CLI arguments, API and rate-limit errors, network failures, timeouts, malformed responses, missing databases, and database errors. Raw remote messages and exception strings are not echoed, preventing accidental token or sensitive-path disclosure. There are no silent retries, partial-page saves, or silent database recreation on reads.

## Key tradeoffs

- **One-page observations, not full synchronization.** An absent issue may have moved to another page. Existing rows remain; closure state, deletion, pagination, and repository-rename reconciliation are outside this challenge.
- **Standard library only.** `urllib`, `sqlite3`, `argparse`, and `unittest` minimize setup and make the project easy to run. An injected transport makes API failures deterministic in tests without building an HTTP abstraction framework.
- **Correctness before concurrency.** SQLite's single-writer model is appropriate for a local connector. Explicit transactions and a composite key provide stronger guarantees than application-side duplicate checks alone. Concurrent writers serialize or receive a useful database error.
- **Return the saved state.** Both successful functions return all stored issues for the requested repository. Imports classify only the fetched issue records. This is convenient at challenge scale; a much larger production archive would benefit from paginated local reads rather than loading all saved rows.
- **No extra product surface.** No UI, deployment, background jobs, ORM, credentials store, or RocketRide setup is needed. Conditional requests and migrations would add state without improving the required one-page demonstration.

The README links the official API and SQLite references used to verify these decisions.
