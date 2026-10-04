/**
 * Full-app runtime smoke test.
 *
 * No desktop browser is attached to this session, so the "does it actually
 * render and respond" question is answered by mounting the real <App/> against
 * a fake WebSocket and driving a complete turn through the documented frame
 * sequence. This is execution, not inspection: if App throws during mount, or
 * a state transition wedges, or the activity feed stops updating, it fails
 * here.
 *
 * It also checks the wake-word contract (section 19/20) at the wire level --
 * the flag the client sends is what the backend gates on, so if the client
 * stops sending it, manual voice silently requires "Zaytoon" again.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import App from "./App";

class FakeSocket {
  static OPEN = 1;
  static last = null;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeSocket.last = this;
    setTimeout(() => {
      this.readyState = 1;
      this.onopen && this.onopen({});
    }, 0);
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
  }
  emit(frame) {
    this.onmessage && this.onmessage({ data: JSON.stringify(frame) });
  }
}

/**
 * jsdom implements neither <canvas> nor scrollIntoView, both of which the HUD
 * and transcript use. These stubs fill the *environment* gap only -- no app
 * code is replaced, so the components under test are the real ones.
 */
function stubBrowserApis() {
  const ctx2d = new Proxy(
    {
      canvas: { width: 300, height: 60 },
      measureText: () => ({ width: 10 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      getImageData: () => ({ data: new Uint8ClampedArray(4) }),
    },
    { get: (t, k) => (k in t ? t[k] : () => {}) },
  );
  window.HTMLCanvasElement.prototype.getContext = () => ctx2d;
  window.Element.prototype.scrollIntoView = function scrollIntoView() {};
}

describe("App runtime", () => {
  let consoleErrors;

  beforeEach(() => {
    stubBrowserApis();
    consoleErrors = [];
    vi.spyOn(console, "error").mockImplementation((...a) => consoleErrors.push(a.join(" ")));
    global.WebSocket = FakeSocket;
    // Default to the wide layout; individual tests override it to exercise the
    // narrow drawer. jsdom's own matchMedia reports matches:false for
    // everything, which would make every layout assertion test the phone.
    global.matchMedia = vi.fn((q) => ({
      matches: /min-width:\s*1280px/.test(q),
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    window.matchMedia = global.matchMedia;
    // jsdom has no audio output; the hook must not crash on playback.
    global.Audio = class {
      constructor() { this.src = ""; }
      play() { return Promise.resolve(); }
      pause() {}
    };
    global.URL.createObjectURL = () => "blob:stub";
    global.URL.revokeObjectURL = () => {};
    global.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({ root: "C:/ws", available: true, enforced: true, entries: [] }),
    }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("mounts without throwing and renders the primary chrome", async () => {
    await act(async () => { render(<App />); });
    // The input and the mic are the two things the app is useless without.
    expect(screen.getByLabelText(/your message/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/activate voice mode/i)).toBeInTheDocument();
    // Dock buttons. The mic is no longer one of them -- see the
    // "exactly one microphone" test.
    expect(screen.getByTitle("CONF")).toBeInTheDocument();
    expect(screen.getByTitle("STOP")).toBeInTheDocument();
    // No React exception during mount.
    const fatal = consoleErrors.filter((e) => /Uncaught|Cannot read|is not a function|undefined is not/i.test(e));
    expect(fatal).toEqual([]);
  });

  it("has exactly one microphone in the product", async () => {
    // InputBar and the dock each used to carry one, so there were two
    // controls for a single action and no way to tell which was live.
    await act(async () => { render(<App />); });
    expect(screen.getAllByLabelText(/activate voice mode/i)).toHaveLength(1);
    expect(screen.queryByTitle("MIC")).not.toBeInTheDocument();
  });

  it("renders the command centre regions at desktop width", async () => {
    // Pin the breakpoint: jsdom's matchMedia reports matches:false for
    // everything, which would silently test the narrow drawer instead of the
    // three-column command deck.
    global.matchMedia = vi.fn((q) => ({
      matches: /min-width:\s*1280px/.test(q),
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    window.matchMedia = global.matchMedia;
    await act(async () => { render(<App />); });

    expect(screen.getAllByText("ZAYTOON").length).toBeGreaterThan(0);
    // The core exposes itself as an image with the resolved state in its name.
    expect(screen.getByRole("img", { name: /ai core/i })).toBeInTheDocument();
    expect(screen.getByText(/transcript/i)).toBeInTheDocument();
    expect(screen.getByText(/command console/i)).toBeInTheDocument();
    // Both rails, exactly one copy of each (discriminated by the panel code
    // chips, because the drawer tabs reuse the same words).
    expect(screen.getAllByText(/system status/i)).toHaveLength(1);
    expect(screen.getAllByText(/^MSN$/)).toHaveLength(1);
    // The tabbed drawer must not also be present at this width.
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    // Full-size core: the shrink to 180px is the phone's, not ours.
    expect(screen.getByRole("img", { name: /ai core/i }).style.width).toBe("260px");
  });

  it("collapses the rails into one tabbed drawer at narrow width", async () => {
    global.matchMedia = vi.fn((q) => ({
      matches: false,
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    window.matchMedia = global.matchMedia;
    await act(async () => { render(<App />); });

    expect(screen.getByRole("tablist")).toBeInTheDocument();
    // The SYSTEM tab is open, so SystemStatus is mounted once...
    expect(screen.getAllByText(/system status/i)).toHaveLength(1);
    // ...and the mission panel is behind the other tab rather than duplicated
    // alongside it. Discriminate on the panel's code chip: the tab button is
    // itself labelled "MISSION", so the title text cannot be used.
    expect(screen.queryByText(/^MSN$/)).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /mission/i })).toBeInTheDocument();
    // The core and the console survive the collapse.
    expect(screen.getByRole("img", { name: /ai core/i })).toBeInTheDocument();
    expect(screen.getByText(/command console/i)).toBeInTheDocument();
  });

  it("keeps the whole deck usable in a 360-430px phone viewport", async () => {
    // Narrow means narrow: same jsdom matchMedia pin as the drawer test, i.e.
    // the layout a 360px phone gets. Nothing about the collapse may cost the
    // user the input, the transcript or the console -- and the core must step
    // down in size rather than be the reason the page scrolls sideways.
    global.matchMedia = vi.fn((q) => ({
      matches: false,
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    window.matchMedia = global.matchMedia;
    await act(async () => { render(<App />); });

    // The core shrinks from 260px (desktop) to 180px at phone widths.
    const core = screen.getByRole("img", { name: /ai core/i });
    expect(core.style.width).toBe("180px");

    // Every load-bearing region is present...
    expect(screen.getByLabelText(/your message/i)).toBeInTheDocument();
    expect(screen.getByText(/transcript/i)).toBeInTheDocument();
    expect(screen.getByText(/command console/i)).toBeInTheDocument();
    expect(screen.getByTitle("CONF")).toBeInTheDocument();
    expect(screen.getByTitle("STOP")).toBeInTheDocument();

    // ...exactly once each: the drawer renders one rail, not a second deck.
    expect(screen.getAllByRole("img", { name: /ai core/i })).toHaveLength(1);
    expect(screen.getAllByLabelText(/your message/i)).toHaveLength(1);
    expect(screen.getAllByText(/command console/i)).toHaveLength(1);
    expect(screen.getAllByText(/system status/i)).toHaveLength(1);
    expect(screen.getByRole("tablist")).toBeInTheDocument();
  });

  it("announces what the engine is doing on the status line", async () => {
    // The line is derived from live hook state (lib/statusLabels), shown under
    // the console as a polite live region, and reused by the transcript's
    // typing indicator — so the two can never disagree.
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const ws = FakeSocket.last;

    // Open socket, no server status yet: the stale initial "disconnected"
    // must read as Ready, not as a phantom reconnect.
    expect(screen.getAllByText("Ready").length).toBeGreaterThan(0);

    await act(async () => { ws.emit({ type: "plan", steps: ["Create the folder", "Write the file"] }); });
    expect(screen.getAllByText("Planning…").length).toBeGreaterThan(0);
    expect(screen.getByText("Create the folder")).toBeInTheDocument();

    await act(async () => { ws.emit({ type: "status", content: "thinking" }); });
    expect(screen.getAllByText("Thinking…").length).toBeGreaterThan(0);

    // A pending human approval outranks every working state.
    await act(async () => {
      ws.emit({
        type: "require_confirmation",
        request_id: "cfm_status",
        action: "file_write",
        params: { path: "a.txt" },
        level: "HIGH",
        reasons: ["writes a file"],
        summary: "Write a.txt",
        text: "Write a.txt?",
        session_id: "s1",
      });
    });
    expect(screen.getAllByText("Waiting for your approval…").length).toBeGreaterThan(0);
  });

  it("sends a typed message over the socket", async () => {
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const input = screen.getByLabelText(/your message/i);
    await act(async () => {
      fireEvent.change(input, { target: { value: "what time is it" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    const sent = FakeSocket.last.sent.filter((m) => m.type === "message");
    expect(sent.length).toBe(1);
    expect(sent[0].message).toBe("what time is it");
  });

  it("drives a full turn through every state without wedging", async () => {
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const ws = FakeSocket.last;

    // Message 1, for real.
    const input = screen.getByLabelText(/your message/i);
    await act(async () => {
      fireEvent.change(input, { target: { value: "what time is it" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(ws.sent.filter((m) => m.type === "message").length).toBe(1);

    // The exact sequence observed on the live backend, including a tool call.
    const frames = [
      { type: "status", content: "thinking" },
      { type: "intent", intent: "tools" },
      { type: "status", content: "executing" },
      { type: "plan", steps: ["Checking the time"] },
      { type: "tool_started", name: "get_time", label: "Getting the time" },
      { type: "tool_finished", name: "get_time", success: true, summary: "14:32" },
      { type: "message", content: "It is 14:32.", response_id: "r1" },
      { type: "status", content: "speaking" },
      { type: "audio_start", language: "en", voice: "en-GB-RyanNeural", format: "mpeg" },
      { type: "audio_chunk", data: btoa("ID3fake"), index: 0 },
      { type: "audio_end" },
      { type: "status", content: "done", response_id: "r1" },
      { type: "status", content: "listening" },
    ];
    for (const f of frames) {
      await act(async () => { ws.emit(f); });
    }

    // The reply reached the transcript (it is also surfaced in the subtitle
    // strip, so assert at least one match rather than exactly one).
    await waitFor(() =>
      expect(screen.getAllByText(/It is 14:32/).length).toBeGreaterThan(0),
    );
    // The plan rendered.
    expect(screen.getByText("Checking the time")).toBeInTheDocument();

    // Nothing is stuck in "processing": a second message goes straight out.
    await act(async () => {
      fireEvent.change(input, { target: { value: "again" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(ws.sent.filter((m) => m.type === "message").length).toBe(2);
  });

  it("recovers to idle after a backend error", async () => {
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const ws = FakeSocket.last;
    await act(async () => {
      ws.emit({ type: "status", content: "thinking" });
      ws.emit({ type: "error", content: "All available free AI models are temporarily unavailable." });
    });
    await waitFor(() =>
      expect(screen.getByText(/temporarily unavailable|error/i)).toBeInTheDocument(),
    );
    const input = screen.getByLabelText(/your message/i);
    expect(input).not.toBeDisabled();
  });

  it("renders a tool confirmation dialog and resolves it", async () => {
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const ws = FakeSocket.last;
    await act(async () => {
      ws.emit({
        type: "require_confirmation",
        request_id: "cfm_1",
        action: "file_write",
        params: { path: "notes.txt", content: "hi" },
        level: "HIGH",
        reasons: ["writes a file"],
        summary: "Write notes.txt",
        text: "Write notes.txt?",
        session_id: "s1",
      });
    });
    const approve = await screen.findByRole("button", { name: /approve|allow|confirm/i });
    await act(async () => { fireEvent.click(approve); });
    const confirmations = ws.sent.filter((m) => m.type === "tool_confirm");
    expect(confirmations.length).toBe(1);
    expect(confirmations[0].request_id).toBe("cfm_1");
    expect(confirmations[0].approved).toBe(true);
  });

  it("manual microphone sends isManualVoice so the wake gate is skipped", async () => {
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const ws = FakeSocket.last;

    const track = { stop: vi.fn(), kind: "audio" };
    const stream = { getTracks: () => [track] };
    global.navigator.mediaDevices = { getUserMedia: vi.fn(async () => stream) };

    // A MediaRecorder that actually yields data, so the real transmit path runs.
    let recorder = null;
    global.MediaRecorder = class {
      static isTypeSupported() { return true; }
      constructor(s, opts) {
        this.mimeType = opts?.mimeType || "audio/webm";
        this.state = "inactive";
        this.ondataavailable = null;
        recorder = this;
      }
      start(timeslice) {
        this.state = "recording";
        // The real recorder emits a slice every `timeslice` ms. stopVoiceMode
        // deliberately nulls ondataavailable before calling stop(), so the
        // chunks have to have arrived beforehand -- emitting only from stop()
        // would test a path the app never takes.
        this._emit = setInterval(() => {
          this.ondataavailable?.({ data: new Blob([new Uint8Array(9000)]) });
        }, 10);
      }
      stop() {
        this.state = "inactive";
        clearInterval(this._emit);
      }
    };

    // The VAD path needs an AudioContext; jsdom has none.
    global.AudioContext = class {
      constructor() { this.sampleRate = 48000; }
      createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
      createAnalyser() {
        return {
          fftSize: 2048,
          connect() {}, disconnect() {},
          getByteTimeDomainData(a) { a.fill(200); },  // loud -> counts as speech
        };
      }
      close() { return Promise.resolve(); }
    };

    const sent = [];
    const origSend = ws.send.bind(ws);
    ws.send = (d) => { sent.push(JSON.parse(d)); origSend(d); };

    // Press the mic, let the recorder emit at least one slice, then stop.
    await act(async () => { fireEvent.click(screen.getByLabelText(/activate voice mode/i)); });
    await waitFor(() => expect(global.navigator.mediaDevices.getUserMedia).toHaveBeenCalled());
    await waitFor(() => expect(recorder).not.toBeNull());
    // A user always speaks for a moment before releasing. Without this the
    // second click can land before the recorder's first 10ms slice, and
    // stopVoiceMode correctly sends nothing because nothing was captured.
    await waitFor(() => expect(sent.length).toBeGreaterThanOrEqual(0));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 60));
    });
    // The mic lives in the console input, so that is what toggles it off.
    await act(async () => { fireEvent.click(screen.getByLabelText(/activate voice mode/i)); });
    await waitFor(() => expect(sent.filter((m) => m.type === "audio").length).toBe(1));

    const audio = sent.find((m) => m.type === "audio");
    // The decisive assertion: the backend gates the wake word on this flag,
    // so if it is absent or false, the user must say "Zaytoon" to be heard.
    expect(audio.isManualVoice).toBe(true);
    expect(recorder).not.toBeNull();
    expect(audio.audio.length).toBeGreaterThan(100);
  });

  it("switches to Arabic and mirrors the document direction", async () => {
    await act(async () => { render(<App />); });
    await waitFor(() => expect(FakeSocket.last.readyState).toBe(1));
    const lang = screen.getByTitle("LANG");
    await act(async () => { fireEvent.click(lang); });
    await waitFor(() =>
      expect(document.documentElement.getAttribute("dir")).toBe("rtl"),
    );
    expect(document.documentElement.getAttribute("lang")).toBe("ar");
  });
});


describe("STOP control", () => {
  let consoleErrors;

  beforeEach(() => {
    stubBrowserApis();
    consoleErrors = [];
    vi.spyOn(console, "error").mockImplementation((...a) => consoleErrors.push(a.join(" ")));
    global.WebSocket = FakeSocket;
    FakeSocket.last = null;
    global.matchMedia = vi.fn((q) => ({
      matches: /min-width:\s*1280px/.test(q),
      media: q,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
    }));
    window.matchMedia = global.matchMedia;
    global.Audio = class {
      constructor() { this.src = ""; }
      play() { return Promise.resolve(); }
      pause() {}
    };
    global.URL.createObjectURL = () => "blob:stub";
    global.URL.revokeObjectURL = () => {};
    global.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({ root: "C:/ws", available: true, enforced: true, entries: [] }),
    }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("cancels the in-flight turn on the wire and unlocks the input", async () => {
    // The full path the STOP button is supposed to take: dock -> App -> hook
    // -> WebSocket. Before it was wired, the button only cleared local flags
    // and the backend kept generating.
    await act(async () => { render(<App />); });
    await waitFor(() => FakeSocket.last && FakeSocket.last.readyState === 1);
    const socket = FakeSocket.last;
    const sent = (type) => socket.sent.filter((m) => m.type === type);

    const input = screen.getByLabelText(/your message/i);
    fireEvent.change(input, { target: { value: "what time is it" } });
    fireEvent.submit(input.closest("form"));
    expect(sent("message")).toHaveLength(1);

    // Latched while the backend works: a second send is refused.
    fireEvent.change(input, { target: { value: "and again" } });
    fireEvent.submit(input.closest("form"));
    expect(sent("message")).toHaveLength(1);

    fireEvent.click(screen.getByTitle("STOP"));

    // The backend is asked to cancel the turn...
    expect(sent("interrupt")).toEqual([{ type: "interrupt" }]);
    // ...and the input is usable again immediately, before the ack lands.
    fireEvent.change(input, { target: { value: "hello there" } });
    fireEvent.submit(input.closest("form"));
    expect(sent("message")).toHaveLength(2);

    // The server ack resets the transient state.
    act(() => socket.emit({ type: "interrupted" }));
    expect(screen.getByLabelText(/your message/i)).toBeInTheDocument();

    const fatal = consoleErrors.filter((e) =>
      /Uncaught|Cannot read|is not a function|undefined is not/i.test(e)
    );
    expect(fatal).toEqual([]);
  });
});
