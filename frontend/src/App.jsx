import { memo, useRef, useEffect } from "react";
import gsap from "gsap";
import useWebSocket from "./hooks/useWebSocket";
import ArcReactorCore from "./components/HUD/ArcReactorCore";
import RadarSweep from "./components/HUD/RadarSweep";
import StatusRing from "./components/HUD/StatusRing";
import Waveform from "./components/HUD/Waveform";
import SidePanelLeft from "./components/HUD/SidePanelLeft";
import SidePanelRight from "./components/HUD/SidePanelRight";
import BottomDock from "./components/HUD/BottomDock";
import ChatTranscript from "./components/Chat/ChatTranscript";
import ConfirmationDialog from "./components/ConfirmationDialog";

export default memo(function App() {
  const {
    messages, isConnected, isTyping, status, intent, language, setLanguage,
    subtitle, voiceMode, awaitingCommand, isListening, isSpeaking, reminders,
    sendMessage, sendReminderCmd, refreshReminders, stopAudio, clearChat,
    startVoiceMode, stopVoiceMode, pendingConfirmation, respondToConfirmation,
    speakingVoice,
  } = useWebSocket();

  const containerRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const ctx = gsap.context(() => {
      gsap.fromTo(".hud-panel-left",
        { opacity: 0, x: -30 },
        { opacity: 1, x: 0, duration: 0.6, delay: 0.2, ease: "power2.out" }
      );
      gsap.fromTo(".hud-panel-right",
        { opacity: 0, x: 30 },
        { opacity: 1, x: 0, duration: 0.6, delay: 0.3, ease: "power2.out" }
      );
      gsap.fromTo(".hud-center",
        { opacity: 0, scale: 0.9 },
        { opacity: 1, scale: 1, duration: 0.8, delay: 0.1, ease: "power2.out" }
      );
      gsap.fromTo(".hud-bottom",
        { opacity: 0, y: 20 },
        { opacity: 1, y: 0, duration: 0.5, delay: 0.5, ease: "power2.out" }
      );
    }, containerRef);
    return () => ctx.revert();
  }, []);

  const getStatusText = () => {
    if (!isConnected) return "OFFLINE";
    if (pendingConfirmation) return "AWAIT CONFIRM";
    if (awaitingCommand) return "SAY COMMAND";
    if (isListening) return "LISTENING";
    if (isTyping || status === "thinking") return "PROCESSING";
    if (isSpeaking) return "SPEAKING";
    if (status === "transcribing") return "TRANSCRIBING";
    return "AWAIT WAKE";
  };

  const getStatusColor = () => {
    if (!isConnected) return "#ff3355";
    if (pendingConfirmation) return "#ff3355";
    if (awaitingCommand) return "#f0b000";
    if (isListening) return "#00ff88";
    if (isTyping || status === "thinking") return "#00d4ff";
    if (isSpeaking) return "#00f0ff";
    return "var(--neon-blue)";
  };

  return (
    <div ref={containerRef} className="h-screen w-screen flex flex-col overflow-hidden"
      style={{ background: "var(--bg-primary)" }}>

      {/* Scan line overlay */}
      <div className="scan-line" />
      <div className="hud-corner hud-corner-tl" />
      <div className="hud-corner hud-corner-tr" />
      <div className="hud-corner hud-corner-bl" />
      <div className="hud-corner hud-corner-br" />

      {/* Header */}
      <header className="flex-none px-4 py-2 flex items-center justify-between z-20"
        style={{
          background: "rgba(5, 5, 12, 0.6)",
          borderBottom: "1px solid var(--border-color)",
          backdropFilter: "blur(12px)",
        }}>
        <div className="flex items-center gap-3">
          <div className="relative w-8 h-8 flex items-center justify-center" style={{ perspective: "600px" }}>
            <div className="absolute inset-0 rounded-sm border" style={{
              borderColor: "rgba(0, 212, 255, 0.3)",
              boxShadow: "0 0 12px rgba(0,212,255,0.15), inset 0 0 12px rgba(0,212,255,0.05)",
              transform: "rotateX(5deg) rotateY(-5deg)",
            }} />
            <span className="text-base font-bold font-display relative z-10 glow-text" style={{
              color: "#00d4ff",
              transform: "rotateX(5deg) rotateY(-5deg)",
            }}>Z</span>
          </div>
          <div>
            <h1 className="text-[11px] font-display font-bold tracking-[0.3em] glow-text" style={{ color: "#00d4ff" }}>
              ZAYTOON
            </h1>
            <div className="flex items-center gap-1.5 mt-0.5">
              <span className="text-[8px] font-mono tracking-widest" style={{ color: "#8888aa", opacity: 0.6 }}>
                J.A.R.V.I.S.-Class AI
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button onClick={() => setLanguage((l) => (l === "en" ? "ar" : "en"))}
            className="text-[9px] px-2 py-1 rounded-sm font-display font-bold tracking-wider transition-all"
            style={{
              background: language === "ar" ? "rgba(0, 255, 136, 0.08)" : "rgba(0, 212, 255, 0.08)",
              color: language === "ar" ? "#00ff88" : "#00d4ff",
              border: `1px solid ${language === "ar" ? "rgba(0, 255, 136, 0.2)" : "rgba(0, 212, 255, 0.2)"}`,
            }}
          >{language === "ar" ? "EN" : "AR"}</button>

          {isSpeaking && (
            <button onClick={stopAudio}
              className="text-[9px] px-2 py-1 rounded-sm font-mono tracking-wider"
              style={{
                background: "rgba(0, 240, 255, 0.08)",
                color: "#00f0ff",
                border: "1px solid rgba(0, 240, 255, 0.2)",
              }}
            >STOP</button>
          )}

          {messages.length > 0 && (
            <button onClick={clearChat}
              className="text-[9px] px-2 py-1 rounded-sm font-mono tracking-wider transition-all"
              style={{
                color: "var(--text-muted)",
                border: "1px solid var(--border-color)",
              }}
            >CLS</button>
          )}

          <div className="flex items-center gap-2 px-3 py-1 rounded-sm" style={{
            background: "rgba(0, 212, 255, 0.04)",
            border: "1px solid rgba(0, 212, 255, 0.1)",
          }}>
            <span className="relative inline-flex items-center justify-center w-2 h-2">
              <span className="absolute inset-0 rounded-full" style={{
                background: getStatusColor(),
                opacity: 0.3,
                animation: !isConnected ? "pulse-ring-red 2s infinite" : "none",
              }} />
              <span className="relative w-1.5 h-1.5 rounded-full" style={{
                background: getStatusColor(),
                boxShadow: `0 0 6px ${getStatusColor()}`,
              }} />
            </span>
            <span className="text-[9px] font-mono tracking-wider" style={{
              color: getStatusColor(),
              textShadow: `0 0 8px ${getStatusColor()}33`,
            }}>{getStatusText()}</span>
            {isSpeaking && speakingVoice && (
              <span
                className="text-[8px] font-mono tracking-wider"
                style={{ color: "#8888aa", opacity: 0.75 }}
                title="Active TTS voice"
              >
                {speakingVoice.replace("Neural", "")}
              </span>
            )}
          </div>
        </div>
      </header>

      {/* Main HUD area */}
      <main className="flex-1 flex overflow-hidden">
        {/* Left panel */}
        <div className="hud-panel-left flex-none hidden lg:flex">
          <SidePanelLeft status={status} isConnected={isConnected} language={language} />
        </div>

        {/* Center HUD */}
        <div className="hud-center flex-1 flex flex-col items-center justify-center relative min-w-0">
          {/* Radar sweep behind everything */}
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <div className="w-[500px] h-[500px] relative">
              <RadarSweep status={status} />
            </div>
          </div>

          {/* Arc Reactor */}
          <div className="relative z-10 flex flex-col items-center">
            <StatusRing status={status} size={240} />
            <div className="absolute inset-0 flex items-center justify-center" style={{ top: "-24px" }}>
              <ArcReactorCore status={status} />
            </div>
          </div>

          {/* Waveform */}
          <div className="relative z-10 mt-2">
            <Waveform isListening={isListening} isSpeaking={isSpeaking} />
          </div>

          {/* Chat transcript overlay */}
          <div className="relative z-10 w-full max-w-2xl mt-2">
            <ChatTranscript
              messages={messages}
              isTyping={isTyping}
              subtitle={subtitle}
              language={language}
            />
          </div>
        </div>

        {/* Right panel */}
        <div className="hud-panel-right flex-none hidden lg:flex">
          <SidePanelRight messages={messages} language={language} />
        </div>
      </main>

      {/* High-risk tool approval gate */}
      <ConfirmationDialog
        request={pendingConfirmation}
        onRespond={respondToConfirmation}
        language={language}
      />

      {/* Bottom dock */}
      <footer className="hud-bottom flex-none px-4 py-3 flex items-center justify-center z-20" style={{
        background: "rgba(5, 5, 12, 0.6)",
        borderTop: "1px solid var(--border-color)",
        backdropFilter: "blur(12px)",
      }}>
        <BottomDock
          voiceMode={voiceMode}
          language={language}
          status={status}
          onMicToggle={() => voiceMode ? stopVoiceMode() : startVoiceMode()}
          onLanguageToggle={() => setLanguage((l) => (l === "en" ? "ar" : "en"))}
          onStop={stopAudio}
        />
      </footer>
    </div>
  );
});
