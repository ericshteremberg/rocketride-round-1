"""CLI: python -m rocketride {import,read} owner/name --db path.sqlite3."""

import argparse
import json
import os
import sys

from .connector import DEFAULT_DB, import_issues, read_issues


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Import and read persistent GitHub issue snapshots.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "read"):
        command = commands.add_parser(name)
        command.add_argument("repository", help="Public GitHub owner/name")
        command.add_argument("--db", default=DEFAULT_DB, help="SQLite file (default: %(default)s)")
        if name == "import":
            command.add_argument("--timeout", type=float, default=10, help="Request timeout in seconds, greater than 0 and at most 300 (default: %(default)s)")
    try:
        args = parser.parse_args(argv)
        if args.command == "import":
            result = import_issues(args.repository, args.db, token=os.environ.get("GITHUB_TOKEN") or None, timeout=args.timeout)
        else:
            result = read_issues(args.repository, args.db)
    except _UsageError as exc:
        result = {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": str(exc)}}
    except KeyboardInterrupt:
        result = {"ok": False, "error": {"code": "INTERRUPTED", "message": "Operation interrupted."}}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
