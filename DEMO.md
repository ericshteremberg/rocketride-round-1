# Recorded demo

The submission video is a captioned, narrated terminal replay of **real captured command results**, condensed to fit the two-minute limit. It is not a simulated API run. The complete JSON results are in [demo/evidence.json](demo/evidence.json); `scripts/demo.py` reproduces the live checks.

## Recorded evidence

The capture completed on October 5, 2026 at 20:59 UTC against the public `psf/requests` repository, without a GitHub token.

| Demonstration | Observed result |
| --- | --- |
| Import one real GitHub page | 39 issues inserted; 61 pull requests skipped |
| Read in a fresh Python process with socket connections disabled | The same 39 saved issues returned |
| Repeat the real import | 0 inserted, 0 updated, 39 unchanged; 39 total saved |
| Import a deliberately nonexistent repository | Structured `API_ERROR`, HTTP 404, process exit 1 |
| Read again after the error | All 39 saved issues unchanged |

The video displays excerpts for readability. Each full API-function result is preserved in the evidence file. Public GitHub data changes; a future run may produce different counts. The automated tests verify title/URL updates and exact idempotence with controlled inputs, in addition to this live demonstration.

## Reproduce

```sh
python -m unittest discover -s tests -v
python scripts/demo.py --record demo-recording.json
```

The script runs each operation in a new interpreter, creates a temporary database, performs real HTTP requests for both imports and the 404, and checks the returned data. The offline steps deliberately disable socket connections in the child interpreter. The temporary database is removed after the demo.

## Decision explained

The database primary key is `(repository, number)`. A single transaction makes each import atomic, and SQLite's upsert updates existing titles and URLs. Unchanged rows avoid unnecessary writes. Records missing from page one are preserved because one page cannot establish that an issue was closed or deleted.

Narration is synthetic; the outputs and verification results are recorded from execution. No user voice or personal experience is represented.
