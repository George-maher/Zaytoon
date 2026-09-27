import { useState, useEffect, useRef } from "react";

export default function SidePanelRight({ messages = [], language = "en" }) {
  const [notifications, setNotifications] = useState([]);
  const scrollRef = useRef(null);

  useEffect(() => {
    const systemMsgs = messages
      .filter((m) => m.role === "assistant" || m.type === "reminder")
      .slice(-8)
      .map((m, i) => ({
        id: i,
        text: m.content?.slice(0, 80) || "",
        time: new Date().toLocaleTimeString(language === "ar" ? "ar-EG" : "en-US", { hour: "2-digit", minute: "2-digit" }),
        type: m.type || "response",
      }));
    setNotifications(systemMsgs);
  }, [messages, language]);

  return (
    <div className="w-48 flex flex-col gap-3 p-3 animate-fade-in" style={{ animationDelay: "0.2s" }}>
      {/* Weather placeholder */}
      <div className="hud-glass-light rounded-sm p-3">
        <div className="text-[8px] font-display tracking-[0.2em] mb-1" style={{ color: "var(--text-muted)" }}>
          {language === "ar" ? "الطقس" : "WEATHER"}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-2xl">&#9728;</span>
          <div>
            <div className="text-sm font-display font-bold" style={{ color: "#00d4ff" }}>28&deg;C</div>
            <div className="text-[9px] font-mono" style={{ color: "var(--text-secondary)" }}>
              {language === "ar" ? "القاهرة" : "Cairo"}
            </div>
          </div>
        </div>
      </div>

      {/* Notifications / Activity Feed */}
      <div className="hud-glass-light rounded-sm p-3 flex-1 flex flex-col min-h-0">
        <div className="text-[8px] font-display tracking-[0.2em] mb-2" style={{ color: "var(--text-muted)" }}>
          {language === "ar" ? "آخر النشاطات" : "ACTIVITY LOG"}
        </div>
        <div ref={scrollRef} className="flex-1 overflow-y-auto space-y-1.5 min-h-0">
          {notifications.length === 0 ? (
            <div className="text-[9px] font-mono text-center py-4" style={{ color: "var(--text-muted)" }}>
              {language === "ar" ? "لا نشاط بعد" : "No activity yet"}
            </div>
          ) : (
            notifications.map((n) => (
              <div key={n.id} className="p-1.5 rounded-sm animate-fade-in" style={{
                background: n.type === "reminder" ? "rgba(0,255,136,0.04)" : "rgba(0,212,255,0.03)",
                borderLeft: `2px solid ${n.type === "reminder" ? "#00ff88" : "#00d4ff"}33`,
              }}>
                <p className="text-[9px] font-mono leading-tight" style={{ color: "var(--text-secondary)" }}>
                  {n.text}
                </p>
                <span className="text-[7px] font-mono" style={{ color: "var(--text-muted)" }}>{n.time}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
