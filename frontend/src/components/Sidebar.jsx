import { memo, useCallback, useEffect, useState } from "react";
import * as api from "../lib/api";
import { navItems } from "../lib/viewState";

/**
 * Sidebar — the product's primary navigation.
 *
 * Replaces the header nav with a collapsible left rail containing every
 * destination: Home, Chat, Computer, Workspace, Tasks, Skills, Models,
 * Settings. The sidebar owns:
 *
 * - **Active state** — the current screen is highlighted with an accent bar.
 * - **Collapse/expand** — a toggle switches between full labels and icon-only
 *   mode; the choice persists in localStorage so it survives a reload.
 * - **Recent workspaces** — the last few opened projects, loaded from the
 *   backend, shown under a "RECENT" section for quick re-entry.
 * - **AI status** — a compact presence indicator at the bottom so the sidebar
 *   carries the "is the AI alive" signal without a second status widget.
 *
 * The sidebar is the *only* navigation. There is no header nav — the header
 * shrinks to identity + status, and the sidebar owns every destination.
 */

const STORAGE_KEY = "zaytoon-sidebar-collapsed";
const NARROW_QUERY = "(max-width: 767px)";

function useSidebarCollapsed() {
  const [userCollapsed, setUserCollapsed] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(NARROW_QUERY).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const mq = window.matchMedia(NARROW_QUERY);
    const onChange = (e) => setNarrow(e.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  // On narrow screens the sidebar is always collapsed (icon-only); on wide
  // screens the user's toggle preference applies.
  const collapsed = narrow || userCollapsed;

  const toggle = useCallback(() => {
    setUserCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
      } catch {
        // A private-mode browser may refuse localStorage; the toggle still
        // works for this session.
      }
      return next;
    });
  }, []);

  return [collapsed, toggle];
}

function useRecentProjects() {
  const [recent, setRecent] = useState([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.getRecentProjects(6);
      if (result?.error) {
        setRecent([]);
      } else {
        setRecent(Array.isArray(result?.projects) ? result.projects : []);
      }
    } catch {
      setRecent([]);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { recent, loaded, reload: load };
}

export default memo(function Sidebar({
  view,
  language = "en",
  onNavigate,
  onOpenProject,
  isConnected,
}) {
  const ar = language === "ar";
  const [collapsed, toggleCollapsed] = useSidebarCollapsed();
  const { recent, loaded: recentLoaded } = useRecentProjects();

  const items = navItems(language);

  return (
    <aside
      className="flex-none flex flex-col min-h-0"
      style={{
        width: collapsed ? "52px" : "220px",
        background: "var(--zt-bg-sidebar)",
        borderRight: "var(--border-w) solid var(--zt-border-subtle)",
        transition: "width var(--zt-duration-base) var(--zt-ease)",
      }}
      aria-label={ar ? "التنقل الرئيسي" : "Main navigation"}
    >
      {/* Collapse toggle */}
      <button
        type="button"
        onClick={toggleCollapsed}
        className="flex-none flex items-center justify-center"
        style={{
          height: "var(--header-h)",
          borderBottom: "var(--border-w) solid var(--zt-border-subtle)",
          color: "var(--zt-text-muted)",
          cursor: "pointer",
          background: "transparent",
          gap: "var(--space-2)",
        }}
        title={ar ? (collapsed ? "توسيع" : "طي") : collapsed ? "Expand" : "Collapse"}
        aria-label={ar ? (collapsed ? "توسيع الشريط" : "طي الشريط") : collapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-expanded={!collapsed}
      >
        <span style={{ fontSize: "var(--font-md)" }} aria-hidden="true">
          {collapsed ? "◂" : "▸"}
        </span>
        {!collapsed && (
          <span
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)", letterSpacing: ar ? "0" : "0.16em" }}
          >
            {ar ? "طي" : "COLLAPSE"}
          </span>
        )}
      </button>

      {/* Navigation items */}
      <nav
        className="flex-1 overflow-y-auto"
        style={{ padding: "var(--space-2) 0" }}
        aria-label={ar ? "التنقل الرئيسي" : "Main navigation"}
      >
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "1px" }}>
          {items.map((item) => {
            const active = view === item.id;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  onClick={() => onNavigate(item.id)}
                  aria-current={active ? "page" : undefined}
                  className="flex items-center w-full hud-glowable"
                  style={{
                    padding: collapsed ? "var(--space-3) 0" : "var(--space-2) var(--space-3)",
                    gap: "var(--space-3)",
                    justifyContent: collapsed ? "center" : "flex-start",
                    background: active
                      ? "color-mix(in srgb, var(--zt-accent) 8%, transparent)"
                      : "transparent",
                    border: "none",
                    borderLeft: active
                      ? "2px solid var(--zt-accent)"
                      : "2px solid transparent",
                    color: active ? "var(--zt-accent)" : "var(--zt-text-secondary)",
                    cursor: "pointer",
                    transition: "color var(--zt-duration-fast), background-color var(--zt-duration-fast), border-color var(--zt-duration-fast)",
                    textAlign: "start",
                  }}
                  title={collapsed ? item.label : undefined}
                >
                  <span
                    aria-hidden="true"
                    style={{
                      fontSize: "var(--font-md)",
                      width: "1.2em",
                      textAlign: "center",
                      flex: "none",
                    }}
                  >
                    {item.glyph}
                  </span>
                  <span
                    className={collapsed ? "sr-only" : "hud-micro"}
                    style={{
                      fontSize: collapsed ? undefined : "var(--font-xs)",
                      letterSpacing: collapsed ? undefined : ar ? "0" : "0.14em",
                      textTransform: collapsed ? undefined : "uppercase",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {item.label}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>

        {/* Recent workspaces */}
        {!collapsed && recentLoaded && recent.length > 0 && (
          <div style={{ marginTop: "var(--space-4)", padding: "0 var(--space-3)" }}>
            <p
              className="hud-micro"
              style={{
                fontSize: "var(--font-xs)",
                letterSpacing: ar ? "0" : "0.14em",
                color: "var(--zt-text-muted)",
                marginBottom: "var(--space-2)",
              }}
            >
              {ar ? "مشاريع حديثة" : "RECENT"}
            </p>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "1px" }}>
              {recent.map((project) => (
                <li key={project.id || project.path}>
                  <button
                    type="button"
                    onClick={() => onOpenProject?.(project.path)}
                    className="flex items-center w-full"
                    style={{
                      padding: "var(--space-1) var(--space-2)",
                      gap: "var(--space-2)",
                      background: "transparent",
                      border: "none",
                      borderLeft: "2px solid transparent",
                      color: "var(--zt-text-muted)",
                      cursor: "pointer",
                      textAlign: "start",
                      transition: "color var(--zt-duration-fast), border-color var(--zt-duration-fast)",
                    }}
                  >
                    <span aria-hidden="true" style={{ fontSize: "var(--font-sm)", flex: "none" }}>
                      ▸
                    </span>
                    <span
                      style={{
                        fontSize: "var(--font-xs)",
                        fontFamily: "var(--zt-font-mono)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        direction: "ltr",
                      }}
                    >
                      {project.name || project.path}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </nav>

      {/* AI status indicator */}
      <div
        className="flex-none flex items-center"
        style={{
          padding: collapsed ? "var(--space-3) 0" : "var(--space-3)",
          borderTop: "var(--border-w) solid var(--zt-border-subtle)",
          justifyContent: collapsed ? "center" : "flex-start",
          gap: "var(--space-2)",
        }}
      >
        <span
          aria-hidden="true"
          className={isConnected ? "hud-hex hud-hex--ok" : "hud-hex hud-hex--error"}
        />
        {!collapsed && (
          <span
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)", color: "var(--zt-text-muted)", letterSpacing: ar ? "0" : "0.12em" }}
          >
            {isConnected ? (ar ? "متصل" : "ONLINE") : (ar ? "غير متصل" : "OFFLINE")}
          </span>
        )}
      </div>
    </aside>
  );
});
