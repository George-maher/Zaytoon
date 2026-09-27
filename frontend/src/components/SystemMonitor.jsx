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
      style={{ background: "rgba(0, 212, 255, 0.04)", border: "1px solid rgba(0, 212, 255, 0.08)" }}
    >
      <span style={{ color: sysInfo.api_key_configured ? "#00ff88" : "#ff3355" }}>
        {sysInfo.api_key_configured ? "● API" : "○ API"}
      </span>
      <span style={{ color: "rgba(0, 212, 255, 0.3)" }}>|</span>
      <span style={{ color: "#00d4ff" }}>TOOLS:{sysInfo.tools_loaded}</span>
      <span style={{ color: "rgba(0, 212, 255, 0.3)" }}>|</span>
      <span style={{ color: sysInfo.voice?.stt ? "#00ff88" : "#ffb347" }}>
        {sysInfo.voice?.stt ? "STT:ON" : "STT:OFF"}
      </span>
      <span style={{ color: "rgba(0, 212, 255, 0.3)" }}>|</span>
      <span style={{ color: sysInfo.voice?.tts ? "#00ff88" : "#ff3355" }}>
        {sysInfo.voice?.tts ? "TTS:ON" : "TTS:OFF"}
      </span>
    </span>
  );
}
