import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { navItems, VIEWS } from "../lib/viewState";
import { enterOverlay } from "../lib/motion";

/**
 * CommandPalette — the global command surface (Ctrl+K, Ctrl+Space).
 *
 * A compact, keyboard-driven palette for common actions:
 *   - Open Folder / Open File (through the native pickers when there are any)
 *   - Search files / Open Terminal / Toggle Explorer / New Task
 *   - Ask Zaytoon — puts the caret in the composer, wherever you are
 *   - Go to <destination> — generated from `navItems()`
 *   - Open Settings
 *
 * Two rules it keeps, both invisible in a screenshot:
 *
 * 1. **It offers nothing it cannot perform.** Every command is dropped unless
 *    it has an action, so a caller that forgets a handler produces a shorter
 *    list rather than a row that closes the palette and does nothing. That is
 *    the same honesty gate as the header nav, applied to a second surface —
 *    a palette is where phantom commands hide.
 * 2. **Destinations come from `navItems()`, not from a hand-written list.**
 *    Navigation is only offered for screens this build mounts, so the palette
 *    cannot promise a room the product does not have.
 *
 * Keyboard navigation: arrows to move, Enter to select, Escape to close.
 * Bilingual labels throughout.
 */
function CommandPalette({
  open,
  onClose,
  language = "en",
  onOpenFolder,
  onOpenFile,
  onSearch,
  onOpenTerminal,
  onToggleExplorer,
  onAsk,
  onOpenSettings,
  onNewTask,
  onNavigate,
}) {
  const ar = language === "ar";
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const scrimRef = useRef(null);
  const panelRef = useRef(null);

  const commands = useMemo(() => {
    // Destinations are generated, never hand-listed: `navItems()` already
    // refuses to return a screen this build does not mount, so the palette
    // and the header nav cannot disagree about what exists.
    const navigation = onNavigate
      ? navItems(language).map((item) => ({
          id: `go-${item.id}`,
          label:
            item.id === VIEWS.HOME
              ? ar
                ? "الرئيسية"
                : "Go Home"
              : ar
                ? `اذهب إلى ${item.label}`
                : `Go to ${item.label}`,
          icon: "▸",
          action: () => onNavigate(item.id),
          keywords: `${item.id} ${item.label} ${
            ar ? "اذهب شاشة الذهاب" : "go screen view navigate"
          }`,
        }))
      : [];

    return [
      { id: "open-folder", label: ar ? "فتح مجلد" : "Open Folder", icon: "▸", action: onOpenFolder, keywords: "folder directory project workspace" },
      { id: "open-file", label: ar ? "فتح ملف" : "Open File", icon: "▸", action: onOpenFile, keywords: "file editor code" },
      { id: "search", label: ar ? "بحث" : "Search Files", icon: "◈", action: onSearch, keywords: "find grep content" },
      { id: "open-terminal", label: ar ? "فتح الطرفية" : "Open Terminal", icon: "▸", action: onOpenTerminal, keywords: "terminal shell command" },
      { id: "toggle-explorer", label: ar ? "تبديل المستكشف" : "Toggle Explorer", icon: "◈", action: onToggleExplorer, keywords: "files tree panel" },
      // Not "toggle a panel": the composer is wherever the screen put it, and
      // this is how you reach it from the palette. The old label promised a
      // panel that never existed on three of the five screens.
      { id: "ask", label: ar ? "اسأل زيتون" : "Ask Zaytoon", icon: "◈", action: onAsk, keywords: "ask chat assistant ai message focus type" },
      { id: "new-task", label: ar ? "مهمة جديدة" : "New Task", icon: "▸", action: onNewTask, keywords: "task create" },
      ...navigation,
      { id: "open-settings", label: ar ? "الإعدادات" : "Open Settings", icon: "◈", action: onOpenSettings, keywords: "settings config preferences" },
    ].filter((cmd) => typeof cmd.action === "function");
  }, [
    ar,
    language,
    onOpenFolder,
    onOpenFile,
    onSearch,
    onOpenTerminal,
    onToggleExplorer,
    onAsk,
    onOpenSettings,
    onNewTask,
    onNavigate,
  ]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (cmd) =>
        cmd.label.toLowerCase().includes(q) ||
        cmd.keywords.toLowerCase().includes(q),
    );
  }, [query, commands]);

  // Reset selection when filter changes
  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  // Focus input on open
  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIndex(0);
      // Focus after render
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const cmd = filtered[selectedIndex];
        if (cmd) {
          cmd.action?.();
          onClose?.();
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose?.();
      }
    },
    [filtered, selectedIndex, onClose],
  );

  // Scroll selected into view
  useEffect(() => {
    const el = listRef.current?.children[selectedIndex];
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  // Enter motion. Keyed on `open`, not on `filtered`: re-running it on every
  // keystroke would re-animate the list under a user who is still typing, and
  // the palette is the one control people drive from the keyboard without
  // looking at it. Focus is a separate effect for the same reason — it must
  // never wait behind a tween.
  useLayoutEffect(() => {
    if (!open) return undefined;
    return enterOverlay(scrimRef.current, panelRef.current);
  }, [open]);

  if (!open) return null;

  return (
    <div
      ref={scrimRef}
      className="fixed inset-0 z-50 flex items-start justify-center"
      style={{
        paddingTop: "15vh",
        background: "var(--scrim)",
      }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={ar ? "لوحة الأوامر" : "Command palette"}
    >
      <div
        ref={panelRef}
        className="w-full max-w-lg"
        style={{
          background: "var(--surface-2)",
          border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 20%, transparent)",
          borderRadius: "var(--radius-panel)",
          boxShadow: "0 0 30px color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
          overflow: "hidden",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div
          className="flex items-center"
          style={{
            padding: "var(--space-3) var(--space-4)",
            borderBottom: "var(--border-w) solid var(--hud-edge-faint)",
            gap: "var(--space-2)",
          }}
        >
          <span style={{ color: "var(--hud-cyan)", fontSize: "var(--font-md)" }} aria-hidden="true">
            ◈
          </span>
          <input
            ref={inputRef}
            type="text"
            className="hud-input flex-1"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={ar ? "اكتب أمراً…" : "Type a command…"}
            aria-label={ar ? "بحث" : "Search commands"}
          />
          <kbd
            className="hud-micro flex-none"
            style={{
              padding: "var(--space-1) var(--space-2)",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text-mute)",
              fontSize: "var(--font-xs)",
            }}
          >
            ESC
          </kbd>
        </div>

        {/* Command list */}
        <ul
          ref={listRef}
          style={{
            listStyle: "none",
            margin: 0,
            padding: "var(--space-2)",
            maxHeight: "50vh",
            overflowY: "auto",
          }}
          role="listbox"
        >
          {filtered.map((cmd, i) => (
            <li key={cmd.id}>
              <button
                role="option"
                aria-selected={i === selectedIndex}
                onClick={() => {
                  cmd.action?.();
                  onClose?.();
                }}
                onMouseEnter={() => setSelectedIndex(i)}
                className="flex items-center w-full text-left"
                style={{
                  padding: "var(--space-2) var(--space-3)",
                  gap: "var(--space-2)",
                  background: i === selectedIndex ? "color-mix(in srgb, var(--hud-cyan) 8%, transparent)" : "transparent",
                  color: i === selectedIndex ? "var(--hud-cyan)" : "var(--hud-text)",
                  cursor: "pointer",
                  borderRadius: "var(--radius-sm)",
                  fontSize: "var(--font-sm)",
                  fontFamily: "var(--font-mono)",
                }}
              >
                <span
                  className="flex-none"
                  style={{
                    color: i === selectedIndex ? "var(--hud-cyan)" : "var(--hud-text-mute)",
                    width: "1.2em",
                  }}
                >
                  {cmd.icon}
                </span>
                <span className="flex-1">{cmd.label}</span>
              </button>
            </li>
          ))}
          {filtered.length === 0 && (
            <li
              style={{
                padding: "var(--space-4)",
                color: "var(--hud-text-mute)",
                fontSize: "var(--font-sm)",
                textAlign: "center",
              }}
            >
              {ar ? "لا نتائج" : "No commands found"}
            </li>
          )}
        </ul>
      </div>
    </div>
  );
}

export default memo(CommandPalette);
