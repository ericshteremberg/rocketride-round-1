#!/usr/bin/env python3
"""Run and record the real acceptance demo. Network access is required.

Every connector invocation runs in a fresh process. The transcript contains
actual JSON results; no API responses are mocked or substituted.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="psf/requests")
    parser.add_argument("--record", type=Path, help="Save the full JSON transcript")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    # This is deliberately a public, unauthenticated API demonstration.
    environment.pop("GITHUB_TOKEN", None)
    environment["PYTHONPATH"] = str(root)
    events = []

    with tempfile.TemporaryDirectory(prefix="rocketride-demo-") as directory:
        def run_step(name, command, *, exit_code=0, guard_network=False):
            display = "python -m rocketride " + shlex.join(command)
            prefix = [sys.executable, "-m", "rocketride"]
            if guard_network:
                # Run the same CLI in a new interpreter with socket connections
                # disabled. An attempted HTTP request makes this step fail.
                prefix = [sys.executable, "-c", (
                    "import runpy,socket,sys; "
                    "socket.socket.connect=lambda *a,**k: "
                    "(_ for _ in ()).throw(AssertionError('network forbidden')); "
                    "sys.argv=['rocketride']+sys.argv[1:]; "
                    "runpy.run_module('rocketride',run_name='__main__')"
                )]
            print(f"\n{name}\n$ {display}", flush=True)
            started = time.perf_counter()
            completed = subprocess.run(prefix + command, cwd=directory,
                                       env=environment, capture_output=True,
                                       text=True, timeout=30)
            elapsed = time.perf_counter() - started
            if completed.returncode != exit_code:
                raise RuntimeError(f"Unexpected exit {completed.returncode}: "
                                   f"{completed.stdout} {completed.stderr}")
            result = json.loads(completed.stdout)
            preview = dict(result)
            if "issues" in preview:
                preview["issues"] = preview["issues"][:1]
                preview["display_note"] = "Showing the first saved issue; full result is in the transcript."
            print(json.dumps(preview, indent=2, ensure_ascii=True), flush=True)
            event = {"name": name, "command": display, "result": result,
                     "exit_code": completed.returncode,
                     "elapsed_seconds": round(elapsed, 4),
                     "network_disabled": guard_network}
            events.append(event)
            return result

        first = run_step("1 Real GitHub import", ["import", args.repository, "--db", "demo.sqlite3"])
        assert first["ok"] and first["counts"]["imported"] > 0, "Choose a repository with open issues."
        assert first["counts"]["inserted"] == first["counts"]["total_saved"]
        saved = run_step("2 Offline read in a new process", ["read", args.repository, "--db", "demo.sqlite3"], guard_network=True)
        assert saved["issues"] == first["issues"], "Persisted data differs from the import."
        second = run_step("3 Repeat the real import", ["import", args.repository, "--db", "demo.sqlite3"])
        assert second["ok"]
        identities = [(issue["repository"], issue["number"]) for issue in second["issues"]]
        assert len(identities) == len(set(identities)), "Duplicate identities detected."
        assert second["counts"]["total_saved"] == first["counts"]["total_saved"] + second["counts"]["inserted"]
        failure = run_step("4 Real API error", ["import", "octocat/rocketride-deliberately-missing-repository-2026", "--db", "demo.sqlite3"], exit_code=1)
        assert not failure["ok"] and failure["error"].get("status") == 404
        final = run_step("5 Verify data after the failure", ["read", args.repository, "--db", "demo.sqlite3"], guard_network=True)
        assert final["issues"] == second["issues"], "Failed import changed saved data."

    record = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "repository": args.repository, "public_api_without_token": True,
              "events": events,
              "verification": "All assertions passed; each invocation used a fresh process."}
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(record, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print("\nPASS: real imports, offline persistence, unique identities, and API failure preservation.")


if __name__ == "__main__":
    main()
