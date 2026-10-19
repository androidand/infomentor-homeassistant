"""Test doubles for the HTTP layer: one response, one scripted `do_request`."""

from __future__ import annotations

from collections import namedtuple

Call = namedtuple("Call", "method url body headers")


class Headers:
    """The slice of aiohttp's header mapping the code uses; keys are case-insensitive."""

    def __init__(self, pairs):
        self._multi: dict[str, list[str]] = {}
        for key, value in pairs:
            self._multi.setdefault(key.lower(), []).append(value)

    def items(self):
        return [(key, values[0]) for key, values in self._multi.items()]

    def get(self, key, default=None):
        values = self._multi.get(key.lower())
        return values[0] if values else default

    def getall(self, key, default=None):
        return list(self._multi.get(key.lower(), default or []))


class FakeResponse:
    def __init__(self, status=200, text="", *, location=None, set_cookies=(), headers=None):
        self.status = status
        self._text = text
        pairs = list((headers or {}).items())
        if location is not None:
            pairs.append(("Location", location))
        pairs += [("Set-Cookie", cookie) for cookie in set_cookies]
        self.headers = Headers(pairs)

    async def text(self):
        return self._text

    def release(self):
        return None

    close = release


class Router:
    """`do_request(method, url, body, headers)` answering from scripted routes.

    `add(method, where, response)`: `where` is a substring of the URL or a predicate;
    `response` is a FakeResponse or a callable taking the Call. Every call is recorded.
    """

    def __init__(self):
        self.routes = []
        self.calls: list[Call] = []

    def add(self, method, where, response):
        self.routes.append((method, where, response))
        return self

    async def __call__(self, method, url, body=None, headers=None):
        call = Call(method, str(url), body, headers or {})
        self.calls.append(call)
        for route_method, where, response in self.routes:
            hit = where(call.url) if callable(where) else where in call.url
            if route_method == method and hit:
                return response(call) if callable(response) else response
        raise AssertionError(f"unexpected call: {method} {call.url}")
