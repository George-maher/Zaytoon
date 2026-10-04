import { memo, useCallback } from "react";

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
    icon: <span className="font-display font-bold tracking-wider" style={{ fontSize: "var(--font-sm)" }}>AR/EN</span>,
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

function BottomDock({
  onMicToggle,
  onLanguageToggle,
  onSettingsToggle,
  onStop,
  voiceMode,
  language,
  status,
  settingsOpen,
  hideMic = false,
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

  // Palette tokens, not literals: the dock's active state is the same
  // cyan/green/red the rest of the instrument uses, and the alpha suffixes
  // become `color-mix` — `${hex}14` cannot be appended to a `var()`.
  const getActiveColor = (id) => {
    if (id === "mic" && voiceMode) return "var(--hud-green)";
    if (id === "settings" && settingsOpen) return "var(--hud-green)";
    if (id === "stop" && status === "speaking") return "var(--hud-red)";
    return "var(--hud-cyan)";
  };

  return (
    <div className="flex items-center" style={{ gap: "var(--space-1)" }}>
      {ICONS.filter((icon) => !(hideMic && icon.id === "mic")).map((icon) => {
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
            /* Square, hairline-framed system controls. No radius on purpose:
               rounded pills read as a consumer app, not as instrumentation. */
            className="relative flex items-center justify-center transition-all duration-200"
            style={{
              width: "var(--ctl-size)",
              height: "var(--ctl-size)",
              background: isActive
                ? `color-mix(in srgb, ${color} 6%, transparent)`
                : "color-mix(in srgb, var(--bg1) 72%, transparent)",
              border: `var(--border-w) solid ${isActive ? `color-mix(in srgb, ${color} 33%, transparent)` : "var(--hud-edge)"}`,
              color: isActive ? color : "var(--hud-text-mute)",
              boxShadow: isActive ? `0 0 10px color-mix(in srgb, ${color} 13%, transparent)` : "none",
            }}
            title={icon.label}
          >
            {icon.id === "lang" ? (
              <span className="font-display font-bold tracking-wider" style={{ fontSize: "var(--font-sm)" }}>
                {language === "ar" ? "EN" : "AR"}
              </span>
            ) : (
              icon.icon
            )}
          </button>
        );
      })}
    </div>
  );
}

export default memo(BottomDock);