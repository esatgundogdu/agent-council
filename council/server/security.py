"""Who is allowed to talk to the daemon.

Binding to 127.0.0.1 is not on its own a security boundary. Any page open in the
user's browser can POST to localhost, and this daemon starts agents that read whatever
is in a project directory — so a stray request is not merely noise. Two cheap gates
close that:

* a **token**, written to `~/.council/daemon.json` (which only the user can read) and
  required on every request but `/api/health`;
* an **origin allowlist**, so a page served from anywhere else cannot use the browser's
  credentials, and a DNS-rebinding host cannot reach the API at all.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request

TOKEN_HEADER = "x-council-token"
TOKEN_COOKIE = "council_token"
TOKEN_QUERY = "token"

#: Hostnames that may appear in Host/Origin. Anything else is a rebinding attempt.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

OPEN_PATHS = {"/api/health"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Guard:
    """Request gate. Held by the app and consulted by a dependency on every route."""

    def __init__(self, token: str, extra_origins: tuple[str, ...] = ()) -> None:
        self.token = token
        self.extra_origins = tuple(extra_origins)

    def check_origin(self, request: Request) -> None:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host and host not in LOCAL_HOSTS:
            raise HTTPException(403, f"refused: unexpected Host header '{host}'")

        origin = request.headers.get("origin")
        if not origin:
            return  # same-origin fetches and non-browser clients send none
        if origin in self.extra_origins:
            return
        hostname = urlparse(origin).hostname or ""
        if hostname not in LOCAL_HOSTS:
            raise HTTPException(403, f"refused: origin '{origin}' is not local")

    def check_token(self, request: Request) -> None:
        supplied = (
            request.headers.get(TOKEN_HEADER)
            or request.cookies.get(TOKEN_COOKIE)
            or request.query_params.get(TOKEN_QUERY)
            or ""
        )
        if not secrets.compare_digest(supplied, self.token):
            raise HTTPException(
                401,
                "missing or wrong token. The CLI sends it for you; a browser gets it "
                "from the URL `council up` prints.",
            )

    def check(self, request: Request) -> None:
        self.check_origin(request)
        if request.url.path in OPEN_PATHS:
            return
        self.check_token(request)
