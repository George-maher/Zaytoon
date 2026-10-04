import { useState, useEffect, useRef } from "react";

export default function SystemMonitor() {
  const [sysInfo, setSysInfo] = useState(null);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    const fetchSys = async () => {
      try {
        const res = await fetch("/health");
        const data = await res.json();
        if (mounted.current) setSysInfo(data);
      } catch {}
    };
    fetchSys();
    const interval = setInterval(fetchSys, 15000);
    return () => { mounted.current = false; clearInterval(interval); };
  }, []);

  if (!sysInfo) return null;

  return (
    <span className="text-[8px] font-mono tracking-wider hidden md:inline-flex items-center gap-1.5 ml-2 px-2 py-0.5 rounded-sm"
      style={{ background: "color-mix(in srgb, var(--hud-cyan) 4%, transparent)", border: "1px solid color-mix(in srgb, var(--hud-cyan) 8%, transparent)" }}
    >
      <span style={{ color: sysInfo.api_key_configured ? "var(--hud-green)" : "var(--hud-red)" }}>
        {sysInfo.api_key_configured ? "● API" : "○ API"}
      </span>
      <span style={{ color: "color-mix(in srgb, var(--hud-cyan) 30%, transparent)" }}>|</span>
      <span style={{ color: "var(--hud-cyan)" }}>TOOLS:{sysInfo.tools_loaded}</span>
      <span style={{ color: "color-mix(in srgb, var(--hud-cyan) 30%, transparent)" }}>|</span>
      <span style={{ color: sysInfo.voice?.stt ? "var(--hud-green)" : "var(--hud-amber)" }}>
        {sysInfo.voice?.stt ? "STT:ON" : "STT:OFF"}
      </span>
      <span style={{ color: "color-mix(in srgb, var(--hud-cyan) 30%, transparent)" }}>|</span>
      <span style={{ color: sysInfo.voice?.tts ? "var(--hud-green)" : "var(--hud-red)" }}>
        {sysInfo.voice?.tts ? "TTS:ON" : "TTS:OFF"}
      </span>
    </span>
  );
}
