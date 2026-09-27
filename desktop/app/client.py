"""WebSocket client for the desktop shell.

Threading contract
------------------
Tk is not thread-safe, so **no widget is ever touched from this module**. The
socket lives on a dedicated thread using the blocking ``websockets.sync``
client, decodes frames into :class:`ServerEvent`, and drops them onto a
:class:`queue.Queue`. The UI thread drains that queue on a timer and is the
only thing that mutates state.

A blocking client is a deliberate choice over asyncio here: the desktop shell
is the only consumer, there is no need to multiplex many concurrent streams,
and it removes an entire class of "loop stopped while awaiting" bugs.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import protocol

logger = logging.getLogger(__name__)

try:  # websockets ships a sync client from 12.x onwards
    from websockets.sync.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - very old websockets
    ws_connect = None

    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        pass


#: Reconnect backoff bounds, in seconds.
RECONNECT_MIN = 1.0
RECONNECT_MAX = 15.0


class BackendClient:
    """Maintains one WebSocket connection to the backend and pumps events."""

    def __init__(self, url: str, events: queue.Queue, name: str = "zaytoon-ws") -> None:
        self.url = url
        self.events = events
        self.name = name
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._socket = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        #: The response id of the request currently streaming, so a late frame
        #: from a cancelled turn can be discarded.
        self._active_response: str | None = None

    # -- state ------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._close_socket()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._set_connected(False)

    def _close_socket(self) -> None:
        with self._lock:
            socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("error closing socket: %s", exc)

    def _set_connected(self, value: bool) -> None:
        if self._connected.is_set() == value:
            return
        if value:
            self._connected.set()
        else:
            self._connected.clear()
        self.events.put(
            protocol.ServerEvent(
                protocol.EventType.CONNECTED if value else protocol.EventType.DISCONNECTED
            )
        )

    # -- the read loop ----------------------------------------------------
    def _run(self) -> None:
        if ws_connect is None:  # pragma: no cover
            logger.error("websockets.sync is unavailable; cannot connect")
            return
        backoff = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                self._session()
                backoff = RECONNECT_MIN
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("websocket error: %s: %s", type(exc).__name__, exc)
                # A failure right after connecting is usually a backend restart;
                # reset the backoff so we retry promptly.
                backoff = RECONNECT_MIN if not self._connected.is_set() else backoff
            finally:
                self._close_socket()
                self._set_connected(False)
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _session(self) -> None:
        assert ws_connect is not None
        socket = ws_connect(self.url, open_timeout=15, max_size=None)
        with self._lock:
            self._socket = socket
        self._set_connected(True)
        logger.info("connected to %s", self.url)
        while not self._stop.is_set():
            try:
                raw = socket.recv(timeout=1.0)
            except TimeoutError:
                continue
            except ConnectionClosed:
                logger.info("connection closed by peer")
                return
            if raw is None:
                continue
            self._dispatch(raw)

    def _dispatch(self, raw: str | bytes) -> None:
        event = protocol.ServerEvent.parse(raw)
        if event is None:
            logger.debug("ignoring unparseable frame")
            return
        if event.type is protocol.EventType.TOKEN:
            response_id = event.data.get("response_id")
            if response_id and self._active_response and response_id != self._active_response:
                # A stale stream from a turn the user already interrupted.
                return
        if event.type in (protocol.EventType.MESSAGE, protocol.EventType.ERROR):
            self._active_response = None
        self.events.put(event)

    # -- sending ----------------------------------------------------------
    def send(self, payload: dict) -> bool:
        """Send one frame. Returns ``False`` when not connected.

        Never raises and never blocks the UI thread for long: the sync client
        will raise if the socket died, which we report as a failed send so the
        caller can surface it instead of silently dropping the message.
        """
        with self._lock:
            socket = self._socket
        if socket is None:
            return False
        import json

        try:
            socket.send(json.dumps(payload))
        except Exception as exc:
            logger.warning("send failed: %s: %s", type(exc).__name__, exc)
            return False
        if payload.get("type") == "message":
            self._active_response = payload.get("response_id")
        return True

    def ask(self, text: str, response_id: str) -> bool:
        return self.send(protocol.chat_message(text, response_id))

    def send_audio(self, pcm: bytes, language: str = "") -> bool:
        return self.send(protocol.audio_message(pcm, language))

    def interrupt(self) -> bool:
        self._active_response = None
        return self.send(protocol.interrupt_message())

    def respond_to_confirmation(self, request_id: str, approved: bool) -> bool:
        return self.send(protocol.tool_confirm(request_id, approved))
