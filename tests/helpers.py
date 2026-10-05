"""Small HTTP fakes; SQLite and subprocesses in tests remain real."""

import json


def issue(number=1, title="An issue", *, repository="octocat/hello-world", **extra):
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{repository}/issues/{number}",
        **extra,
    }


class Response:
    def __init__(self, payload=None, *, status=200, headers=None, raw=None):
        self.status = status
        self.headers = headers or {}
        self.body = json.dumps(payload).encode("utf-8") if raw is None else raw
        self.offset = 0
        self.closed = False

    def read(self, limit=-1):
        stop = len(self.body) if limit < 0 else self.offset + limit
        result = self.body[self.offset:stop]
        self.offset += len(result)
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Transport:
    def __init__(self, payload=None, *, error=None, status=200, headers=None, raw=None):
        self.response = Response(payload, status=status, headers=headers, raw=raw)
        self.error = error
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response
