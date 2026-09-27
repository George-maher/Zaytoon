import { useCallback } from "react";

const ICONS = [
  {
    id: "mic",
    label: "MIC",
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
        <line x1="12" y1="19" x2="12" y2="23" />
        <line x1="8" y1="23" x2="16" y2="23" />
      </svg>
    ),
  },
  {
    id: "lang",
    label: "LANG",
    icon: <span className="text-[10px] font-display font-bold">AR/EN</span>,
  },
  {
    id: "settings",
    label: "CONF",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    ),
  },
  {
    id: "stop",
    label: "STOP",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
        <rect x="4" y="4" width="16" height="16" rx="2" />
      </svg>
    ),
  },
];

export default function BottomDock({
  onMicToggle,
  onLanguageToggle,
  onSettingsToggle,
  onStop,
  voiceMode,
  language,
  status,
  settingsOpen,
}) {
  const handleClick = useCallback(
    (id) => {
      switch (id) {
        case "mic":
          onMicToggle?.();
          break;
        case "lang":
          onLanguageToggle?.();
          break;
        case "settings":
          // This case did not exist, so the CONF button was a no-op.
          onSettingsToggle?.();
          break;
        case "stop":
          onStop?.();
          break;
        default:
          break;
      }
    },
    [onMicToggle, onLanguageToggle, onSettingsToggle, onStop],
  );

  const getActiveColor = (id) => {
    if (id === "mic" && voiceMode) return "#00ff88";
    if (id === "settings" && settingsOpen) return "#00ff88";
    if (id === "stop" && status === "speaking") return "#ff3355";
    return "#00d4ff";
  };

  return (
    <div className="flex items-center gap-3">
      {ICONS.map((icon) => {
        const color = getActiveColor(icon.id);
        const isActive =
          (icon.id === "mic" && voiceMode) ||
          (icon.id === "settings" && settingsOpen) ||
          (icon.id === "stop" && status === "speaking");
        return (
          <button
            key={icon.id}
            onClick={() => handleClick(icon.id)}
            type="button"
            aria-pressed={isActive}
            className="group relative w-11 h-11 rounded-full flex items-center justify-center transition-all duration-300"
            style={{
              background: isActive ? `${color}15` : "rgba(0,212,255,0.04)",
              border: `1px solid ${isActive ? `${color}44` : "rgba(0,212,255,0.12)"}`,
              color: isActive ? color : "var(--text-muted)",
              boxShadow: isActive ? `0 0 15px ${color}22` : "none",
            }}
            title={icon.label}
          >
            {icon.id === "lang" ? (
              <span className="text-[9px] font-display font-bold">{language === "ar" ? "EN" : "AR"}</span>
            ) : (
              icon.icon
            )}
            {/* Hover glow ring */}
            <span className="absolute inset-0 rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-300 pointer-events-none"
              style={{
                boxShadow: `0 0 20px ${color}22, inset 0 0 15px ${color}08`,
              }}
            />
          </button>
        );
      })}
    </div>
  );
}
