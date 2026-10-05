"""One GitHub page per import; local-only reads; atomic, idempotent storage."""

from __future__ import annotations

import json
from http.client import HTTPException
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_DB = "rocketride.sqlite3"
API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_OWNER = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    repository TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number > 0),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (repository, number)
) WITHOUT ROWID
"""
_UPSERT = """
INSERT INTO issues (repository, number, title, url) VALUES (?, ?, ?, ?)
ON CONFLICT(repository, number) DO UPDATE SET title=excluded.title, url=excluded.url
WHERE issues.title != excluded.title OR issues.url != excluded.url
"""


class _Failure(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.error = {"code": code, "message": message, **details}


def _failure_result(exc: _Failure, repository: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": exc.error}
    if repository is not None:
        result["repository"] = repository
    return result


def _repository(value: Any) -> str:
    if isinstance(value, str):
        parts = value.strip().split("/")
        if len(parts) == 2:
            owner, name = parts
            if (len(owner) <= 39 and _OWNER.fullmatch(owner)
                    and _NAME.fullmatch(name) and name not in {".", ".."}):
                return f"{owner.lower()}/{name.lower()}"
    raise _Failure("INVALID_REPOSITORY", "Use a public GitHub repository in owner/name format.")


def _database_path(value: Any) -> Path:
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw or raw == ":memory:":
            raise ValueError
        return Path(raw).expanduser().absolute()
    except (TypeError, ValueError, RuntimeError, OSError):
        raise _Failure("INVALID_CONFIGURATION", "db_path must name a persistent SQLite file.") from None


def _database_failure(exc: sqlite3.Error) -> _Failure:
    # Never include raw exceptions: they can contain source data or sensitive paths.
    # SQLite error attributes/constants were added in Python 3.11. Numeric base
    # codes keep the advertised Python 3.10 support, with a generic fallback.
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in {5, 6}:  # SQLITE_BUSY, SQLITE_LOCKED
        message = "SQLite is busy. Close other writers and try again."
    elif code in {11, 26}:  # SQLITE_CORRUPT, SQLITE_NOTADB
        message = "The database file is not a valid SQLite database. Choose another --db path."
    elif code in {8, 14}:  # SQLITE_READONLY, SQLITE_CANTOPEN
        message = "Cannot access the database. Check the parent directory and file permissions."
    else:
        message = "SQLite could not complete the operation. Check the database schema, disk space and permissions."
    return _Failure("DATABASE_ERROR", message)


def _rows(connection: sqlite3.Connection, repository: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT repository, number, title, url FROM issues WHERE repository=? ORDER BY number",
        (repository,),
    ).fetchall()
    return [dict(zip(("repository", "number", "title", "url"), row)) for row in rows]


def _http_failure(status: int, headers: Any, body: bytes = b"") -> _Failure:
    remaining = headers.get("X-RateLimit-Remaining") if headers else None
    retry_after = headers.get("Retry-After") if headers else None
    # GitHub reports secondary limits in the JSON message, sometimes without headers.
    try:
        payload = json.loads(body)
        message = str(payload.get("message", "")).lower() if isinstance(payload, dict) else ""
    except (ValueError, UnicodeDecodeError, RecursionError):
        message = ""
    rate_limited = status == 429 or (status == 403 and (
        remaining == "0" or retry_after is not None or "rate limit" in message
    ))
    if rate_limited:
        details: dict[str, Any] = {"status": status}
        # Return only an unambiguous numeric delay, never arbitrary response text.
        if retry_after is not None and str(retry_after).isdigit():
            details["retry_after"] = int(retry_after)
        return _Failure("RATE_LIMITED", "GitHub rate limit reached. Wait before importing again; an optional GITHUB_TOKEN may increase the limit.", **details)
    messages = {
        401: "GitHub rejected authentication. Check the optional GITHUB_TOKEN.",
        403: "GitHub denied this request. Check repository access and token permissions.",
        404: "Repository not found or not publicly accessible. Check owner/name.",
        410: "GitHub issues are unavailable for this repository.",
        422: "GitHub rejected the request parameters.",
    }
    message = messages.get(status, "GitHub could not complete the request. Try again later.")
    return _Failure("API_ERROR", message, status=status)


def _fetch(repository: str, token: str | None, timeout: float,
           transport: Callable[..., Any] | None) -> list[Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "rocketride-issue-connector/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"https://api.github.com/repos/{repository}/issues?state=open&per_page=100&page=1",
        headers=headers,
        method="GET",
    )
    try:
        with (transport or urlopen)(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
            if not 200 <= status < 300:
                raise _http_failure(status, response.headers, body)
    except HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError, HTTPException):
            body = b""
        finally:
            exc.close()
        raise _http_failure(exc.code, exc.headers, body) from None
    except (TimeoutError, socket.timeout):
        raise _Failure("TIMEOUT", "GitHub did not respond before the timeout. Try again or increase --timeout.") from None
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise _Failure("TIMEOUT", "GitHub did not respond before the timeout. Try again or increase --timeout.") from None
        raise _Failure("NETWORK_ERROR", "Could not connect to GitHub. Check your internet connection and proxy settings.") from None
    except (OSError, HTTPException):
        raise _Failure("NETWORK_ERROR", "The connection to GitHub failed. Check your connection and try again.") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise _Failure("INVALID_API_RESPONSE", "GitHub response exceeded the 16 MiB safety limit; nothing was saved.")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Failure("INVALID_API_RESPONSE", "GitHub returned invalid JSON; nothing was saved.") from None
    if not isinstance(payload, list):
        raise _Failure("INVALID_API_RESPONSE", "Expected a GitHub issues list; nothing was saved.")
    return payload


def _validate_page(repository: str, payload: list[Any]) -> tuple[list[tuple[Any, ...]], int]:
    rows: list[tuple[Any, ...]] = []
    seen: set[int] = set()
    skipped = 0
    for item in payload:
        if not isinstance(item, dict):
            raise _Failure("INVALID_API_RESPONSE", "GitHub returned a malformed issue record; nothing was saved.")
        if "pull_request" in item:
            skipped += 1
            continue
        number, title, url = item.get("number"), item.get("title"), item.get("html_url")
        if (type(number) is not int or not 0 < number <= 2**63 - 1
                or not isinstance(title, str) or not isinstance(url, str) or not url
                or number in seen):
            raise _Failure("INVALID_API_RESPONSE", "GitHub returned an invalid or duplicate issue record; nothing was saved.")
        try:
            title.encode("utf-8")
            url.encode("utf-8")
        except UnicodeEncodeError:
            raise _Failure("INVALID_API_RESPONSE", "GitHub returned invalid Unicode in an issue; nothing was saved.") from None
        seen.add(number)
        rows.append((repository, number, title, url))
    return rows, skipped


def import_issues(repository: str, db_path: str | os.PathLike[str] = DEFAULT_DB, *,
                  token: str | None = None, timeout: float = 10,
                  transport: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Fetch one page of open issues and atomically upsert them into a SQLite file.

    ``transport`` follows ``urllib.request.urlopen(request, timeout=...)`` and is
    injectable for tests. API callers pass tokens explicitly; only the CLI reads
    GITHUB_TOKEN. Returned issues include all previously saved rows for the repo.
    An issue absent from this page is preserved: this is a snapshot accumulator,
    not a complete synchronization of GitHub's current open issue set.
    """
    canonical = None
    connection = None
    try:
        canonical = _repository(repository)
        path = _database_path(db_path)
        try:
            valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                             and math.isfinite(timeout) and 0 < timeout <= 300)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise _Failure("INVALID_CONFIGURATION", "timeout must be a finite number of seconds greater than 0 and no more than 300.")
        if token is not None and (not isinstance(token, str) or not token
                                  or any(not 33 <= ord(character) <= 126 for character in token)):
            raise _Failure("INVALID_CONFIGURATION", "token must be nonempty printable ASCII without whitespace, or None.")
        if transport is not None and not callable(transport):
            raise _Failure("INVALID_CONFIGURATION", "transport must be callable, or None.")
        payload = _fetch(canonical, token, timeout, transport)
        incoming, skipped = _validate_page(canonical, payload)
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_SCHEMA)
        existing = {row["number"]: row for row in _rows(connection, canonical)}
        counts = {"imported": len(incoming), "inserted": 0, "updated": 0,
                  "unchanged": 0, "pull_requests_skipped": skipped, "total_saved": 0}
        for _, number, title, url in incoming:
            old = existing.get(number)
            kind = "inserted" if old is None else (
                "unchanged" if (old["title"], old["url"]) == (title, url) else "updated"
            )
            counts[kind] += 1
        connection.executemany(_UPSERT, incoming)
        saved = _rows(connection, canonical)
        counts["total_saved"] = len(saved)
        connection.commit()
        return {"ok": True, "repository": canonical, "issues": saved, "counts": counts}
    except _Failure as exc:
        return _failure_result(exc, canonical)
    except sqlite3.Error as exc:
        return _failure_result(_database_failure(exc), canonical)
    finally:
        if connection is not None:
            connection.close()  # Uncommitted work is rolled back on every failure.


def read_issues(repository: str, db_path: str | os.PathLike[str] = DEFAULT_DB) -> dict[str, Any]:
    """Read saved issues without making network calls or creating a database."""
    canonical = None
    connection = None
    try:
        canonical = _repository(repository)
        path = _database_path(db_path)
        if not path.exists():
            raise _Failure("DATABASE_NOT_FOUND", "Database not found. Import a repository first, or check --db.")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        saved = _rows(connection, canonical)
        return {"ok": True, "repository": canonical, "issues": saved, "total_saved": len(saved)}
    except _Failure as exc:
        return _failure_result(exc, canonical)
    except sqlite3.Error as exc:
        return _failure_result(_database_failure(exc), canonical)
    except OSError:
        return _failure_result(_Failure("DATABASE_ERROR", "Cannot access the database. Check its path and permissions."), canonical)
    finally:
        if connection is not None:
            connection.close()
