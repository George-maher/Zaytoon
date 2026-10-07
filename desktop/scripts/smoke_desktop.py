"""Live smoke test: open the real WebView window, verify the bridge, close.

    python desktop/scripts/smoke_desktop.py             # auto-closes
    python desktop/scripts/smoke_desktop.py --keep      # leave the window open
    python desktop/scripts/smoke_desktop.py --timeout 90

This is the check no unit test can make. Against the real production build it
proves that:

* pywebview + WebView2 actually open a window (the runtime is really there),
* the loaded page is Zaytoon's React app and the bundle **executed**
  (``#root`` has children — HTML parsing alone cannot produce that),
* the bridge is exposed to JavaScript and answers real calls
  (``get_system_info``, ``get_app_path``, ``get_backend_url``,
  ``get_backend_status``, ``get_window_state``),
* the backend lifecycle runs (adopt or spawn) and is released on close.

Exit code 0 only when every check passed. Prints one line per check so a
failure says which layer broke: window, page, bridge, or backend.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DesktopConfig  # noqa: E402
from app.host import WebViewHost  # noqa: E402

#: What the page must prove, evaluated inside the real document.
PROBE_JS = """(async () => {
    const out = {};
    out.title = document.title;
    const root = document.querySelector('#root');
    out.react_mounted = !!(root && root.childElementCount > 0);
    out.pywebview_ready = !!(window.pywebview && window.pywebview.api);
    if (out.pywebview_ready) {
        const api = window.pywebview.api;
        out.system = await api.get_system_info();
        out.app_path = await api.get_app_path();
        out.ws_url = await api.get_backend_url();
        out.backend = await api.get_backend_status();
        out.window_state = await api.get_window_state();
    }
    // The real app computes API_BASE as http://<hostname>:8000 — exercise the
    // same cross-origin call so a CORS rejection is caught here, not by the
    // reminders panel silently failing later.
    try {
        const r = await fetch('http://127.0.0.1:8000/health', {mode: 'cors'});
        out.rest = r.ok ? 'ok' : 'HTTP ' + r.status;
    } catch (e) {
        out.rest = 'BLOCKED: ' + e;
    }
    return out;
})()"""


class SmokeHost(WebViewHost):
    """WebViewHost that verifies the loaded page and then closes itself."""

    def __init__(self, config: DesktopConfig, keep_open: bool, timeout: float) -> None:
        super().__init__(config)
        self.keep_open = keep_open
        self.timeout = timeout
        self.probe: dict | None = None
        self.probe_error: str | None = None
        self.diag: dict = {}
        self._deadline = threading.Event()

    def _on_loaded(self) -> None:
        super()._on_loaded()
        threading.Thread(target=self._probe_page, name="smoke-probe", daemon=True).start()
        # Watchdog: never leave a window on screen if the probe never comes back.
        threading.Thread(target=self._watchdog, name="smoke-watchdog", daemon=True).start()

    def _watchdog(self) -> None:
        # wait() is True when the probe *set* the deadline (it finished);
        # False means the timeout actually elapsed.
        if not self._deadline.wait(self.timeout):
            self.probe_error = self.probe_error or "timed out waiting for the page"
        if not self.keep_open:
            self._destroy()

    def _eval_async(self, script: str, timeout: float = 3.0) -> object:
        """``evaluate_js`` for scripts that return a promise.

        pywebview only resolves promises on the *callback* form: the plain
        form runs ``pywebview.stringify(promise)`` which serialises to
        ``{}``, so an async probe silently returns an empty object. This is
        why the first smoke run reported a blank result for a page that had
        actually rendered.
        """
        done = threading.Event()
        box: dict = {}

        def _callback(value, _box=box, _done=done):
            _box["value"] = value
            _done.set()

        try:
            self.window.evaluate_js(script, callback=_callback)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not done.wait(timeout):
            return {"error": "promise never resolved"}
        return box.get("value")

    def _probe_page(self) -> None:
        """Poll until React mounts and the bridge appears (both need a moment).

        On failure the diagnostics (plumbing check, landed URL, document
        state) are kept so the report says *how far* the load got.
        """
        import time

        # Stage 1: does evaluate_js work at all, and what did we land on?
        self.diag = {}
        for name, expr in (
            ("js_plumbing", "1+1"),
            ("href", "location.href"),
            ("title", "document.title"),
            ("ready_state", "document.readyState"),
            ("root_html_len", "document.querySelector('#root') ? "
                              "document.querySelector('#root').innerHTML.length : -1"),
            ("script_count", "document.scripts.length"),
            ("body_len", "document.body ? document.body.innerHTML.length : -1"),
            ("api_keys", "window.pywebview && window.pywebview.api ? "
                         "Object.keys(window.pywebview.api).length : -1"),
        ):
            try:
                self.diag[name] = self.window.evaluate_js(expr)
            except Exception as exc:
                self.diag[name] = f"{type(exc).__name__}: {exc}"
        try:
            self.diag["current_url"] = self.window.get_current_url()
        except Exception as exc:
            self.diag["current_url"] = f"{type(exc).__name__}: {exc}"
        # Async stages need the callback form (see _eval_async).
        self.diag["async_plain"] = self._eval_async("(async () => ({title: document.title}))()")
        self.diag["bridge_call"] = self._eval_async(
            "(async () => { try {"
            " const r = await window.pywebview.api.get_system_info();"
            " return 'ok:' + (r && r.platform);"
            " } catch (e) { return 'REJECTED:' + e; } })()"
        )

        attempts = 12  # ~36s worst case; the page normally answers on try 1
        result: object = None
        for _ in range(attempts):
            result = self._eval_async(PROBE_JS)
            if isinstance(result, dict) and result.get("react_mounted") and result.get(
                "pywebview_ready"
            ):
                self.probe = result
                self._deadline.set()
                if not self.keep_open:
                    self._destroy()
                return
            time.sleep(0.2)
        # Remember whatever we saw — it tells us how far the load got.
        self.probe = result if isinstance(result, dict) else None
        self.diag["probe_result_repr"] = repr(result)
        self.probe_error = "page never reported React mounted AND the bridge ready"
        self._deadline.set()
        if not self.keep_open:
            self._destroy()

    def _destroy(self) -> None:
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:
            pass


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="leave the window open")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds before giving up")
    parser.add_argument("--dev", action="store_true", help="point at the Vite dev server")
    args = parser.parse_args(argv)

    # pywebview logs the URL it actually navigates to at DEBUG — the single
    # most useful line when the page does not appear.
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("webview").setLevel(logging.DEBUG)

    config = DesktopConfig()
    if args.dev:
        config.dev_mode = True

    print(f"Zaytoon desktop smoke test — {config.backend_addr}")
    print(f"  url: {'dev server' if config.dev_mode else config.frontend_dist / 'index.html'}")
    if not args.dev and not (config.frontend_dist / "index.html").is_file():
        print("  FAIL  production build missing — run `npm run build` in frontend/")
        return 1

    host = SmokeHost(config, keep_open=args.keep, timeout=args.timeout)
    backend_before = host.backend.status().running

    # run() blocks until the window closes; on_close shuts the backend down
    # only if this process spawned it.
    rc = host.run()

    probe = host.probe or {}
    print()
    print("results")
    ok = rc == 0
    ok = _check("window opened and closed cleanly", rc == 0, f"rc={rc}") and ok
    if host.probe_error:
        print(f"       {host.probe_error}")
    if not probe.get("react_mounted"):
        print("  diagnostics:")
        for key, value in host.diag.items():
            print(f"    {key} = {value!r}")

    title = probe.get("title", "")
    ok = _check("page loaded (title)", bool(title), title) and ok
    ok = _check(
        "React bundle executed (#root mounted)",
        bool(probe.get("react_mounted")),
    ) and ok

    ready = bool(probe.get("pywebview_ready"))
    ok = _check("bridge visible to JavaScript (window.pywebview.api)", ready) and ok
    if ready:
        system = probe.get("system") or {}
        ok = _check(
            "get_system_info",
            bool(system.get("platform")),
            f"{system.get('os')} {system.get('os_version')}".strip(),
        ) and ok
        app_path = probe.get("app_path") or ""
        ok = _check("get_app_path", app_path == str(config.repo_root), app_path) and ok
        ws_url = probe.get("ws_url") or ""
        ok = _check("get_backend_url", ws_url == config.ws_url, ws_url) and ok
        backend = probe.get("backend") or {}
        ok = _check(
            "get_backend_status",
            "running" in backend,
            f"running={backend.get('running')} (before={backend_before})",
        ) and ok
        state = probe.get("window_state") or {}
        ok = _check("get_window_state", "width" in state, json.dumps(state)) and ok
        rest = probe.get("rest") or ""
        ok = _check("REST /health from the page origin (CORS)", rest == "ok", rest) and ok

    # The backend must not have been killed if it was already ours to begin with,
    # and must not be left behind if we spawned it (both covered by shutdown()).
    backend_after = host.backend.status()
    print(
        f"  info  backend spawned_by_us={host.backend.managed} "
        f"running_after_close={backend_after.running}"
    )

    print()
    print("verdict:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
