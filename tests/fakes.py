"""Test doubles shared by the network tests: a urlopen replacement that never touches the network."""

import io


class FakeResp:
    """What urlopen returns: a context manager with read(), headers and status."""

    def __init__(self, body: bytes, status: int = 200):
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Routes urlopen by exact URL and records what was asked for.

    routes[url] is the response body (bytes), or an exception to raise.
    An unrouted URL raises KeyError, so a test fails loudly if the code
    under test asks for something it shouldn't.
    """

    def __init__(self):
        self.routes = {}      # url -> bytes | Exception
        self.requests = []    # (url, Accept header)

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_header("Accept")))
        result = self.routes[req.full_url]
        if isinstance(result, Exception):
            raise result
        return FakeResp(result)
