"""A deliberately misconfigured local web server, for Scenario F.

This is the "deliberately vulnerable local test application" the acceptance
criteria ask for: it answers on 127.0.0.1 with a set of configuration defects
that a non-destructive probe can *confirm from the response alone*, so the
security tooling can be exercised against a real socket instead of a mock.

What it is vulnerable to, and why each one is checkable without sending a
payload:

* no ``Strict-Transport-Security`` and no ``Content-Security-Policy``
* no ``X-Content-Type-Options``
* ``X-Frame-Options: ALLOWALL`` — present but ineffective, which is the case
  that a naive "is the header there?" check reports as configured
* ``Server: LabServer/1.2.3`` and ``X-Powered-By: PHP/5.6.40`` banners
* ``/admin`` returns 200 to an unauthenticated request, and
* ``/redirect`` answers ``302`` to a plain-``http`` URL

It is **loopback-only** and binds ``127.0.0.1``. It serves nothing from disk,
runs no user input, and has no dependencies beyond the standard library. Run it
with::

    python scripts/vulnerable_lab.py [port]

and stop it with Ctrl-C, or ``POST /shutdown`` (which is the lab's own
convenience, not a Zaytoon feature).
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = b"<html><body><h1>Vulnerable lab</h1></body></html>"


class LabHandler(BaseHTTPRequestHandler):
    server_version = "LabServer/1.2.3"
    sys_version = ""

    def _headers(self, status: int, extra: dict | None = None) -> None:
        self.send_response(status)
        # Deliberately absent: Strict-Transport-Security, Content-Security-Policy,
        # X-Content-Type-Options, Referrer-Policy.
        # Deliberately ineffective: ALLOWALL is meaningless to every browser and
        # reads as "configured" to a presence-only check.
        self.send_header("X-Frame-Options", "ALLOWALL")
        self.send_header("X-Powered-By", "PHP/5.6.40")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
        path = self.path.split("?")[0]
        if path == "/redirect":
            self._headers(302, {"Location": "http://127.0.0.1/landing"})
            return
        if path == "/shutdown":
            self._headers(200)
            self.wfile.write(b"stopping")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/admin":
            # The point of the fixture: an unauthenticated 200 on an
            # administrative path is confirmable from the status line.
            self._headers(200)
            self.wfile.write(b"<html><body>Admin console (no auth required)</body></html>")
            return
        self._headers(200)
        self.wfile.write(BODY)

    def do_HEAD(self) -> None:  # noqa: N802
        self._headers(200)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        """Silent.

        The default writes every request to stderr, which for a fixture that a
        test drives is noise the test output does not need.
        """
        return


def serve(port: int = 8099) -> ThreadingHTTPServer:
    """Start the lab on ``port`` and return the server (already serving)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), LabHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = serve(port)
    print(f"vulnerable lab listening on http://127.0.0.1:{port}", flush=True)
    print("press Ctrl-C to stop", flush=True)
    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
