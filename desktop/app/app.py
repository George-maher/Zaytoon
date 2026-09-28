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
import time
from enum import Enum

import customtkinter as ctk

from . import protocol, theme
from .audio import AudioPlayer, MicrophoneCapture
from .backend import BackendProcess
from .client import BackendClient
from .hotkeys import HotkeyManager, format_hotkey
from .orb import OrbWindow
from .panel import ChatPanel
from .tray import TrayIcon

logger = logging.getLogger(__name__)

#: How often the UI drains the event queue, in milliseconds.
TICK_MS = 30


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
        )
        self._position_panel()
        self.panel.withdraw()
        self.panel.protocol("WM_DELETE_WINDOW", self.toggle_mode)

        self._start_tray()

    # -- lifecycle --------------------------------------------------------
    def run(self) -> int:
        self._register_hotkeys()
        self.start_backend()
        self.client.start()
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
        self.panel.withdraw()
        self.orb.recenter(self.config.screen_margin)
        self.orb.show()

    def _enter_panel(self) -> None:
        self.mode = Mode.PANEL
        self.orb.hide()
        self._position_panel()
        self.panel.deiconify()
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
                self._poll_mic()
                try:
                    self.root.after(TICK_MS, self._pump)
                except Exception:  # pragma: no cover - Tk shutting down
                    pass

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
            return

        if isinstance(item, protocol.ServerEvent):
            self._handle_event(item)

    # -- server events ----------------------------------------------------
    def _handle_event(self, event: protocol.ServerEvent) -> None:
        if event.type is protocol.EventType.CONNECTED:
            self._set_status("connected")
            self._set_hint("")
            return
        if event.type is protocol.EventType.DISCONNECTED:
            self._set_status("disconnected")
            self._set_hint("Lost the backend; reconnecting…")
            return
        if event.type is protocol.EventType.STATUS:
            self._set_status(event.status or "thinking")
            return
        if event.type is protocol.EventType.INTENT:
            self.language = event.data.get("language") or self.language
            return
        if event.type is protocol.EventType.TOKEN:
            self._set_status("speaking")
            self.panel.append_token(event.text)
            return
        if event.type is protocol.EventType.SUBTITLE:
            self.panel.set_transcript(event.text)
            return
        if event.type is protocol.EventType.MESSAGE:
            self.panel.set_transcript(event.text)
            self.panel.set_busy(False)
            self._set_status("listening" if self.mic is not None else "connected")
            return
        if event.type is protocol.EventType.STT_RESULT:
            detected = event.data.get("language")
            if detected in ("ar", "en"):
                self.language = detected
            return
        if event.type is protocol.EventType.AUDIO_START:
            return
        if event.type is protocol.EventType.AUDIO_CHUNK:
            import base64

            try:
                self.player.play(base64.b64decode(event.data.get("data", "")))
            except Exception as exc:  # pragma: no cover
                logger.debug("bad audio chunk: %s", exc)
            return
        if event.type is protocol.EventType.WAKE_WORD:
            if event.data.get("detected"):
                self._set_status("awaiting_command")
            else:
                self.panel.add_message(
                    "system",
                    "I only act on commands that start with “Zaytoon” — "
                    "say “Zaytoon, what time is it?”.",
                )
            return
        if event.type is protocol.EventType.REQUIRE_CONFIRMATION:
            self.panel.show_confirmation(event.data)
            self._set_status("awaiting_confirmation")
            return
        if event.type is protocol.EventType.CONFIRMATION_RESULT:
            if not event.data.get("resolved", True):
                self.panel.dismiss_confirmation()
            return
        if event.type is protocol.EventType.ERROR:
            self.panel.add_message("error", event.text or "The assistant could not answer.")
            self.panel.set_busy(False)
            self._set_status("error")
            return
        if event.type is protocol.EventType.REMINDER:
            message = event.data.get("message", "Reminder")
            self.panel.add_message("system", f"⏰ {message}")
            if self.tray.available:
                self.tray.notify("Zaytoon Reminder", message)
            return
        if event.type is protocol.EventType.REMINDER_LIST:
            reminders = event.data.get("reminders", [])
            self.panel.show_reminders(reminders)
            return
        if event.type is protocol.EventType.REMINDER_DELETED:
            reminder_id = event.data.get("id")
            if reminder_id is not None:
                self.panel.remove_reminder(reminder_id)
            return
        if event.type is protocol.EventType.REMINDER_DONE:
            reminder_id = event.data.get("id")
            if reminder_id is not None:
                self.panel.remove_reminder(reminder_id)
            return

    def _set_status(self, status: str) -> None:
        self.status = status
        self.orb.set_status(status)
        self.panel.set_status(status, self.language)

    def _set_hint(self, text: str) -> None:
        try:
            self.panel.set_hint(text)
        except Exception:  # pragma: no cover
            pass

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
        self._set_status("thinking")

    def interrupt(self) -> None:
        self.client.interrupt()
        if self.player is not None:
            self.player.clear()
        if self.mic is not None:
            self.mic.discard()
        self.panel.set_transcript("")
        self.panel.set_busy(False)
        self._set_status("listening" if self.mic is not None else "connected")

    def respond_to_confirmation(self, request_id: str, approved: bool) -> None:
        self.client.respond_to_confirmation(request_id, approved)
        self.panel.set_status("thinking", self.language)
        self._set_status("thinking")

    def toggle_mic(self) -> None:
        if self.mic is not None:
            self.mic.stop()
            self.mic = None
            self.panel.set_mic(False)
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
