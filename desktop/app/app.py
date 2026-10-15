"""The Zaytoon desktop shell.

Owns the orb, the panel, the tray, the global hotkeys, the backend child
process, the WebSocket client and the microphone — and keeps them consistent
with one another.

Threading
---------
Tk runs on the main thread. Everything else (the socket, PortAudio, the OS
hotkey hook, pystray) runs on its own thread and communicates only through
queues, which the main thread drains in :meth:`ZaytoonApp._pump`. No widget is
ever mutated off the main thread.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from enum import Enum

import customtkinter as ctk

from . import protocol, states
from .audio import AudioPlayer, MicrophoneCapture
from .backend import BackendProcess
from .client import BackendClient
from .hotkeys import HotkeyManager, format_hotkey
from .orb import OrbWindow
from .panel import ChatPanel
from .sidepanel import (
    SystemSampler,
    fetch_json,
    format_percent,
    parse_audit,
    parse_model,
    parse_processes,
    parse_tools,
)
from .tray import TrayIcon

logger = logging.getLogger(__name__)

#: How often the UI drains the event queue, in milliseconds.
TICK_MS = 30

#: How often the worker refreshes the optional REST snapshots, in seconds.
SNAPSHOT_INTERVAL = 10.0

#: Newest actions kept in the side panel.
MAX_RECENT_ACTIONS = 8


class TelemetryWorker(threading.Thread):
    """Everything network-y or OS-y that the shell needs to *display*.

    REST calls (``/tools``, ``/health``, ``/audit``, ``/processes``) and the
    ctypes CPU/RAM sampling all run here, never on the Tk thread, and every
    result is pushed back through the app's own event queue as a
    ``("telemetry", {...})`` tuple — the same queue the WebSocket client uses,
    so there is exactly one path into widget code.

    Missing endpoints are normal: ``fetch_json`` returns ``None``, the parsers
    return an empty list, and the panel shows ``—`` or hides the card.
    """

    def __init__(self, base_url: str, events: queue.Queue, *, sampler=None) -> None:
        super().__init__(name="zaytoon-telemetry", daemon=True)
        self.base_url = str(base_url).rstrip("/")
        self.events = events
        self.sampler = sampler or SystemSampler()
        self._stop = threading.Event()
        self._want_snapshot = threading.Event()
        self._have_model = False
        self._next_optional = 0.0

    # -- control ----------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def request_snapshot(self) -> None:
        """Ask for the tools/model snapshot (called on connect)."""
        self._want_snapshot.set()

    # -- loop -------------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - a worker must not die
                logger.exception("telemetry tick failed")
            self._stop.wait(0.5)

    def _tick(self) -> None:
        if self.sampler.due():
            self._emit({"system": self.sampler.sample()})

        if self._want_snapshot.is_set():
            tools = fetch_json(f"{self.base_url}/tools")
            if tools is not None:
                self._want_snapshot.clear()
                self._emit({"tools": parse_tools(tools)})
            health = fetch_json(f"{self.base_url}/health")
            if health is not None:
                model = parse_model(health)
                if model:
                    self._have_model = True
                    self._emit({"model": model})

        now = time.monotonic()
        if now >= self._next_optional:
            self._next_optional = now + SNAPSHOT_INTERVAL
            self._emit({
                "audit": parse_audit(fetch_json(f"{self.base_url}/audit")),
                "processes": parse_processes(fetch_json(f"{self.base_url}/processes")),
            })

    def _emit(self, data: dict) -> None:
        self.events.put(("telemetry", data))


class Mode(str, Enum):
    ORB = "orb"
    PANEL = "panel"


class ZaytoonApp:
    """The application object; ``run()`` blocks on the Tk mainloop."""

    def __init__(self, config, *, start_visible: bool = True) -> None:
        self.config = config
        self.events: queue.Queue = queue.Queue()
        self.mode = Mode.ORB
        self.shutting_down = False
        self.status = "disconnected"
        self.language = "en"
        self._ids = itertools.count(1)
        self._status_from_voice: str | None = None
        #: The turn the shell is following and whether its audio is being
        #: suppressed. Declared here rather than created on first use: a shell
        #: that receives `audio_chunk` before any anchor frame would otherwise
        #: be reading an attribute that does not exist yet.
        self._latest_response_id: str | None = None
        self._ignore_audio = False

        #: One state machine feeding the header pill, the core and the status
        #: line (see ``app/states.py``).
        self.tracker = states.StateTracker()
        #: The last resolved state, so a caller can read it without re-deriving.
        self.ui_state = states.UiState.OFFLINE
        #: Newest-first log of finished tool calls for the side panel.
        self.recent_actions: list[str] = []
        self.model_name = ""
        #: Worker thread: REST snapshots + CPU/RAM sampling (never the Tk thread).
        self.telemetry = TelemetryWorker(f"http://{config.backend_addr}", self.events)

        # customtkinter 6 removed the bundled named colour themes, so only the
        # appearance mode is set; every colour is supplied explicitly in theme.py.
        ctk.set_appearance_mode("dark")

        self.root = ctk.CTk()
        self.root.withdraw()
        self.root.title("Zaytoon AI")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.backend = BackendProcess(config)
        self.client = BackendClient(config.ws_url, self.events)
        self.hotkeys = HotkeyManager()
        self.tray = TrayIcon("zaytoon")
        self.player: AudioPlayer | None = None
        self.mic: MicrophoneCapture | None = None

        self.orb = OrbWindow(self.root, size=config.orb_size, on_click=self.toggle_mode)
        self.orb.bind("<Motion>", self._on_orb_motion)
        self.orb.bind("<Leave>", lambda _e: self.orb.set_hovered(False))
        self.orb.recenter(config.screen_margin)

        self.panel = ChatPanel(
            self.root,
            width=config.panel_width,
            height=config.panel_height,
            on_send=self.send_text,
            on_mic_toggle=self.toggle_mic,
            on_interrupt=self.interrupt,
            on_collapse=self.toggle_mode,
            on_confirm=self.respond_to_confirmation,
            on_quit=self.quit,
            on_settings=self.show_settings,
            config=config,
        )
        self._position_panel()
        self._panel_positioned = True
        self._panel_saved_geometry: str | None = None
        self._panel_saved_state: str | None = None
        self.panel.withdraw()
        self.panel.protocol("WM_DELETE_WINDOW", self.toggle_mode)

        self._start_tray()

    # -- lifecycle --------------------------------------------------------
    def run(self) -> int:
        self._register_hotkeys()
        self.start_backend()
        self.client.start()
        self.telemetry.start()
        self.player = self._open_player()

        self.orb.set_status("connected")
        self.orb.start()
        if self.mode is Mode.ORB:
            self.orb.show()
        else:
            self.panel.show()
        self._pump()
        self.root.mainloop()
        return 0

    def start_backend(self) -> None:
        """Bring up FastAPI in the background so the UI appears immediately."""
        status = self.backend.ensure_running()
        if not status.running:
            logger.warning("backend not running: %s", status.hint)
            self._set_hint(status.hint)
        elif status.managed:
            logger.info("started backend pid=%s", status.pid)
        else:
            logger.info("using the already-running backend on %s", status.address)

    def _open_player(self):
        try:
            player = AudioPlayer(
                sample_rate=self.config.sample_rate, device=self.config.output_device or None
            )
            player.start()
            return player
        except Exception as exc:
            logger.warning("no audio output: %s", exc)
            self._set_hint(f"Audio output unavailable: {exc}")
            return None

    def quit(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        logger.info("shutting down")
        self.hotkeys.unregister_all()
        self.tray.stop()
        self.telemetry.stop()
        self.orb.stop()
        if self.mic is not None:
            self.mic.stop()
        if self.player is not None:
            self.player.stop()
        self.client.stop()
        # Only a backend this process started is terminated.
        self.backend.shutdown()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # pragma: no cover
            pass

    # -- hotkeys ----------------------------------------------------------
    def _register_hotkeys(self) -> None:
        # Route hotkeys through the event queue so Tk work stays on the main
        # thread: the OS hook fires on the keyboard thread.
        self.hotkeys.register(self.config.toggle_hotkey, self._queue_hotkey_toggle)
        self.hotkeys.register(self.config.expand_hotkey, self._queue_hotkey_expand)
        hint = format_hotkey(self.config.toggle_hotkey)
        if not self.hotkeys.bound():
            self._set_hint("Global hotkeys unavailable; use the tray menu.")
        else:
            self._set_hint(f"{hint} toggles · {format_hotkey(self.config.expand_hotkey)} expands")

    def _queue_hotkey_toggle(self) -> None:
        self.events.put(("hotkey", "toggle"))

    def _queue_hotkey_expand(self) -> None:
        self.events.put(("hotkey", "expand"))

    # -- tray -------------------------------------------------------------
    def _start_tray(self) -> None:
        if not self.tray.available:
            return
        import pystray

        def toggle(_icon, _item):
            self.events.put(("hotkey", "toggle"))

        def show(_icon, _item):
            self.events.put(("ui", "show"))

        def panel(_icon, _item):
            self.events.put(("hotkey", "expand"))

        def restart(_icon, _item):
            self.events.put(("backend", "restart"))

        def quit_(_icon, _item):
            self.events.put(("ui", "quit"))

        self.tray.start(
            pystray.Menu(
                pystray.MenuItem("Show / hide", toggle, default=True),
                pystray.MenuItem("Open panel", panel),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Restart backend", restart),
                pystray.MenuItem("Quit", quit_),
            )
        )

    # -- mode switching ---------------------------------------------------
    def toggle_mode(self) -> None:
        if self.mode is Mode.ORB:
            self._enter_panel()
        else:
            self._enter_orb()

    def _enter_orb(self) -> None:
        self.mode = Mode.ORB
        # Remember how the user left the panel. On Windows ``withdraw`` drops
        # both the geometry and a maximized state, and the old code re-ran
        # _position_panel() on every return, so a resized or maximized panel
        # snapped back to the default box each time it was hidden.
        try:
            self._panel_saved_geometry = self.panel.geometry()
            self._panel_saved_state = self.panel.state()
        except Exception:  # pragma: no cover - Tk already torn down
            pass
        self.panel.withdraw()
        self.orb.recenter(self.config.screen_margin)
        self.orb.show()

    def _enter_panel(self) -> None:
        self.mode = Mode.PANEL
        self.orb.hide()
        if self._panel_positioned:
            # Position once, at startup; afterwards restore what the user had.
            if self._panel_saved_geometry:
                try:
                    self.panel.geometry(self._panel_saved_geometry)
                except Exception:  # pragma: no cover
                    pass
        else:
            self._position_panel()
            self._panel_positioned = True
        self.panel.deiconify()
        if self._panel_saved_state == "zoomed":
            try:
                self.panel.state("zoomed")
            except Exception:  # pragma: no cover - WM refused the request
                pass
        self.panel.lift()
        self.panel.attributes("-topmost", True)
        self.panel.after(200, lambda: self.panel.attributes("-topmost", self.config.always_on_top))
        self.panel.focus_force()

    def _position_panel(self) -> None:
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:  # pragma: no cover
            return
        width = min(self.config.panel_width, sw - 40)
        height = min(self.config.panel_height, sh - 80)
        x = max(20, (sw - width) // 2)
        y = max(20, (sh - height) // 3)
        self.panel.geometry(f"{width}x{height}+{x}+{y}")

    def _on_orb_motion(self, _event=None) -> None:
        self.orb.set_hovered(True)

    # -- the event pump ---------------------------------------------------
    def _pump(self) -> None:
        if self.shutting_down:
            return
        try:
            for _ in range(64):  # bound the work per tick so the UI stays live
                item = self.events.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        finally:
            if not self.shutting_down:
                self._poll_audio()
                self._poll_mic()
                try:
                    self.root.after(TICK_MS, self._pump)
                except Exception:  # pragma: no cover - Tk shutting down
                    pass

    def _poll_audio(self) -> None:
        """Track whether TTS is actually leaving the speaker.

        Cheap enough for every tick (a flag read), and it is what makes the
        ``SPEAKING`` state mean *speaking* rather than *some status frame said
        so a moment ago*.
        """
        playing = self.player is not None and self.player.playing
        if playing != self.tracker.inputs.speaking:
            self.tracker.set_speaking(playing)
            self._refresh_state()

    def _handle(self, item) -> None:
        if isinstance(item, tuple):
            kind, value = item
            if kind == "hotkey":
                if value == "toggle":
                    self.orb.toggle_visible()
                    if not self.orb.visible and self.mode is Mode.ORB:
                        self.orb.show()
                else:
                    if not self.orb.visible:
                        self.orb.show()
                    self.toggle_mode()
            elif kind == "ui":
                if value == "quit":
                    self.quit()
                else:
                    self.orb.show()
                    if self.mode is Mode.PANEL:
                        self.panel.deiconify()
            elif kind == "backend":
                self.backend.shutdown()
                self.client.stop()
                self.start_backend()
                self.client.start()
            elif kind == "telemetry":
                self._apply_telemetry(value)
            return

        if isinstance(item, protocol.ServerEvent):
            self._handle_event(item)

    # -- server events ----------------------------------------------------
    def _handle_event(self, event: protocol.ServerEvent) -> None:
        """Fold one frame into the tracker, the widgets, and back to a state.

        Every path ends in :meth:`_refresh_state`, so the header pill, the core
        and the status line can never disagree about what just happened — the
        previous version re-derived each of them independently from a raw
        status string and they drifted apart.
        """
        self.tracker.on_event(event)
        # Which turn the shell is following. Updated only by the frames that
        # *establish* a turn, so a superseded turn's tail cannot move it back
        # and the audio guard below compares against the right id.
        if event.is_anchor:
            self._latest_response_id = event.response_id
        try:
            self._route_event(event)
        finally:
            self._refresh_state()

    def _route_event(self, event: protocol.ServerEvent) -> None:
        etype = event.type
        if etype is protocol.EventType.CONNECTED:
            self._set_connection(states.CONNECTION_CONNECTED)
            self._set_hint("")
            # Clear any pending confirmation state on reconnect. The panel and
            # the tracker must be cleared together: dismissing only the dialog
            # left the tracker holding the dead request id, so the header still
            # read "Awaiting confirmation" for a request the backend has
            # forgotten.
            if self.panel.awaiting_confirmation:
                self.panel.dismiss_confirmation()
            self.tracker.set_confirmation(None)
            self.telemetry.request_snapshot()
            return
        if etype is protocol.EventType.DISCONNECTED:
            self._set_connection(states.CONNECTION_RECONNECTING)
            self._set_hint("Lost the backend; reconnecting…")
            return
        if etype is protocol.EventType.STATUS:
            self.status = event.status or "thinking"
            return
        if etype is protocol.EventType.INTENT:
            self.language = event.data.get("language") or self.language
            intent = str(event.data.get("intent") or "")
            self.panel.set_task(intent, self.status)
            return
        if etype is protocol.EventType.TOKEN:
            self.panel.append_token(event.text, trace_id=event.trace_id)
            self.panel.set_typing(True)
            return
        if etype is protocol.EventType.SUBTITLE:
            self.panel.set_transcript(event.text)
            return
        if etype is protocol.EventType.PLAN:
            steps = event.plan_steps
            if steps:
                self.panel.show_plan(steps)
            return
        if etype is protocol.EventType.TOOL_STARTED:
            name, label = event.tool_name, event.tool_label
            self.panel.tool_started(name, label)
            self._note_action(label or name, ok=True, running=True)
            return
        if etype is protocol.EventType.TOOL_PROGRESS:
            self.panel.tool_progress(event.tool_name, event.tool_label)
            return
        if etype is protocol.EventType.TOOL_FINISHED:
            name = event.tool_name
            ok = bool(event.data.get("success", True))
            summary = str(event.data.get("summary") or "")
            self.panel.tool_finished(name, ok, summary)
            self._note_action(summary or name, ok=ok)
            return
        if etype is protocol.EventType.MESSAGE:
            self._finish_stream(event.text, event.trace_id)
            return
        if etype is protocol.EventType.MESSAGE_DONE:
            # Some turns end with ``message_done`` alone; it carries the same
            # final content and must not be dropped on the floor.
            if event.text:
                self._finish_stream(event.text, event.trace_id)
            else:
                self.panel.set_typing(False)
                self.panel.set_busy(False)
            return
        if etype is protocol.EventType.INTERRUPTED:
            self.panel.set_busy(False)
            self.panel.set_typing(False)
            self.panel.activity.reset()
            self.status = (
                "listening" if self.mic is not None
                else ("connected" if self.client.connected else "disconnected")
            )
            return
        if etype is protocol.EventType.STT_RESULT:
            detected = event.data.get("language")
            if detected in ("ar", "en"):
                self.language = detected
            return
        if etype is protocol.EventType.AUDIO_START:
            # Speech belonging to a turn that has been replaced is dropped
            # whole. The server stops synthesising at the next chunk boundary,
            # but chunks already on the socket still arrive, and the shell plays
            # them as they land — so without this the previous answer is spoken
            # over the one the user is waiting for.
            turn = event.response_id
            latest = getattr(self, "_latest_response_id", None)
            self._ignore_audio = bool(turn and latest and turn != latest)
            return
        if etype is protocol.EventType.AUDIO_CHUNK:
            import base64

            if getattr(self, "_ignore_audio", False):
                return
            try:
                if self.player is not None:
                    self.player.play(base64.b64decode(event.data.get("data", "")))
            except Exception as exc:  # pragma: no cover
                logger.debug("bad audio chunk: %s", exc)
            return
        if etype is protocol.EventType.AUDIO_END:
            # The utterance is over either way: a dropped one must not leave the
            # next turn's audio ignored.
            self._ignore_audio = False
            return
        if etype is protocol.EventType.WAKE_WORD:
            if event.data.get("detected"):
                self.status = "awaiting_command"
            else:
                self.panel.add_message(
                    "system",
                    "I only act on commands that start with “Zaytoon” — "
                    "say “Zaytoon, what time is it?”.",
                )
            return
        if etype is protocol.EventType.REQUIRE_CONFIRMATION:
            self.panel.show_confirmation(event.data)
            return
        if etype is protocol.EventType.CONFIRMATION_RESULT:
            self.panel.resolve_confirmation(bool(event.data.get("resolved", True)))
            return
        if etype is protocol.EventType.ERROR:
            self.panel.add_message("error", event.text or "The assistant could not answer.")
            self.panel.set_busy(False)
            self.panel.set_typing(False)
            self.panel.activity.reset()
            return
        if etype is protocol.EventType.REMINDER:
            message = event.data.get("message", "Reminder")
            self.panel.add_message("system", f"⏰ {message}")
            if self.tray.available:
                self.tray.notify("Zaytoon Reminder", message)
            return
        if etype is protocol.EventType.REMINDER_LIST:
            reminders = event.data.get("reminders", [])
            self.panel.show_reminders(reminders)
            return
        if etype is protocol.EventType.REMINDER_DELETED:
            self._resolve_reminder(event, "deleted")
            return
        if etype is protocol.EventType.REMINDER_DONE:
            self._resolve_reminder(event, "completed")
            return

    def _finish_stream(self, text: str, trace_id=None) -> None:
        self.panel.set_transcript(text, trace_id=trace_id)
        # Explicit rather than relying on set_transcript to do it: the typing
        # dots belong to the turn, and the turn is over here.
        self.panel.set_typing(False)
        self.panel.set_busy(False)
        self.status = "listening" if self.mic is not None else (
            "connected" if self.client.connected else "disconnected"
        )

    def _resolve_reminder(self, event, verb: str) -> None:
        """Drop a reminder from the panel, but only if the backend did it.

        The row was removed unconditionally, so a request for an id that no
        longer existed made the row vanish and the app implied a deletion that
        never happened. The backend now reports ``success`` honestly; honour it
        and surface the reason when it declines.
        """
        reminder_id = event.data.get("id")
        if reminder_id is None:
            return
        if event.data.get("success") is False:
            self.panel.add_message("system", event.data.get("message", "No such reminder."))
            return
        self.panel.remove_reminder(reminder_id)

    def _set_status(self, status: str) -> None:
        """Record a raw HUD status and push the derived state to both views."""
        self.status = status
        self.tracker.set_status(status)
        self._refresh_state()

    def _set_connection(self, value: str) -> None:
        """Transport-level connection: feeds the tracker and the header pill."""
        self.tracker.set_connection(value)
        try:
            self.panel.set_connection(value)
        except Exception:  # pragma: no cover - panel half-built
            pass

    def _refresh_state(self) -> None:
        """One resolution, three consumers — they cannot drift apart."""
        state, text = self.tracker.resolve(self.language)
        self.ui_state = state
        try:
            self.orb.set_status(state)
        except Exception:  # pragma: no cover - orb destroyed mid-quit
            pass
        try:
            self.panel.set_ui_state(
                state, self.language, self.tracker.inputs.tool, text=text
            )
        except Exception:  # pragma: no cover
            pass
        self.status = states.orb_status(state)

    def _set_hint(self, text: str) -> None:
        try:
            self.panel.set_hint(text)
        except Exception:  # pragma: no cover
            pass

    # -- telemetry ---------------------------------------------------------
    def _apply_telemetry(self, data: dict) -> None:
        """Fold a worker snapshot into the side panel. Missing keys are normal."""
        if not isinstance(data, dict):
            return
        system = data.get("system")
        if isinstance(system, dict):
            cpu, ram = system.get("cpu"), system.get("ram")
            self.panel.set_system(
                format_percent(cpu) if isinstance(cpu, (int, float)) else "—",
                format_percent(ram) if isinstance(ram, (int, float)) else "—",
            )
        if "tools" in data:
            names = data.get("tools") or []
            if names:
                self.panel.set_tools([str(n) for n in names])
        if data.get("model"):
            self.model_name = str(data["model"])
            self.panel.set_model(self.model_name)
        if "audit" in data:
            items = data.get("audit") or []
            if items:
                self._replace_actions([str(a) for a in items])
        if "processes" in data:
            items = data.get("processes") or []
            if items:
                self.panel.set_processes([str(p) for p in items])

    def _note_action(self, text: str, *, ok: bool = True, running: bool = False) -> None:
        """Push one finished (or running) tool row onto the recent-actions log."""
        if not text:
            return
        mark = "…" if running else ("✓" if ok else "✕")
        self.recent_actions = ([f"{mark} {text}"] + self.recent_actions)[:MAX_RECENT_ACTIONS]
        self.panel.set_actions(self.recent_actions)

    def _replace_actions(self, items: list[str]) -> None:
        self.recent_actions = [str(i) for i in items][:MAX_RECENT_ACTIONS]
        self.panel.set_actions(self.recent_actions)

    def show_settings(self) -> None:
        self.panel.open_settings()

    # -- actions ----------------------------------------------------------
    def send_text(self, text: str) -> None:
        if not self.client.connected:
            self.panel.add_message("system", "Not connected to the backend yet.")
            return
        response_id = f"desktop-{next(self._ids)}"
        if not self.client.ask(text, response_id):
            self.panel.add_message("system", "Could not send that; still reconnecting.")
            return
        self.panel.add_message("user", text)
        self.panel.set_busy(True)
        self.tracker.reset_turn()
        self.panel.activity.reset()
        self._set_status("thinking")

    def interrupt(self) -> None:
        self.client.interrupt()
        if self.player is not None:
            self.player.clear()
        if self.mic is not None:
            self.mic.discard()
        self.panel.set_transcript("")
        self.panel.set_busy(False)
        self.panel.activity.reset()
        self.tracker.reset_turn()
        self._set_status("listening" if self.mic is not None else "connected")

    def respond_to_confirmation(self, request_id: str, approved: bool) -> None:
        """One decision per request: the dialog is already gone when we get here."""
        self.client.respond_to_confirmation(request_id, approved)
        self.tracker.set_confirmation(None)
        self._set_status("thinking")

    def toggle_mic(self) -> None:
        if self.mic is not None:
            self.mic.stop()
            self.mic = None
            self.panel.set_mic(False)
            self.tracker.set_mic(False)
            self._set_status("connected" if self.client.connected else "disconnected")
            self._set_hint("")
            return
        try:
            mic = MicrophoneCapture(
                sample_rate=self.config.sample_rate,
                device=self.config.input_device or None,
                vad_threshold=self.config.vad_threshold,
                vad_silence_frames=self.config.vad_silence_frames,
                min_speech_ms=self.config.min_speech_ms,
                max_utterance_s=self.config.max_utterance_s,
            )
            mic.start()
        except Exception as exc:
            self.panel.add_message("error", f"Could not open the microphone: {exc}")
            return
        self.mic = mic
        self.panel.set_mic(True)
        self.tracker.set_mic(True)
        self._set_status("listening")
        self._set_hint("Listening — tap ■ to stop the assistant talking")

    def _poll_mic(self) -> None:
        """Called from the pump: advance the VAD and upload finished utterances."""
        if self.mic is None or self.shutting_down:
            return
        utterance = self.mic.poll()
        if utterance is not None:
            self.client.send_audio(utterance.pcm, "")
            self.panel.set_busy(True)
            self._set_status("thinking")
        self.orb.set_level(self.mic.level)
        # The core reacts to level only while it is actually animating; when the
        # state is static it drew once and scheduled nothing, so this is a
        # property write, never a wake-up.
        try:
            self.panel.core.set_level(self.mic.level)
        except Exception:  # pragma: no cover - panel half-built
            pass
