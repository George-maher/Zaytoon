import { memo } from "react";
import { VIEWS } from "../../lib/viewState";
import { translate } from "../../i18n/strings";

/**
 * NexusDock — the bottom app dock. Every cell is a REAL destination from the
 * view vocabulary (`lib/viewState.js`); there is no decorative cell in it.
 * The reference's icon set maps by function, not by mere shape: brain → this
 * dashboard, terminal → the live conversation, globe → the machine view,
 * charts → the task flow, folder → the workspace, sparkles → skills,
 * shield → security, board → models, box → machines, gear → settings, and
 * the grid opens the command palette (which carries open folder / file /
 * commands — Home's original three actions).
 */

const CELLS = [
  {
    id: "home",
    dest: VIEWS.HOME,
    titleKey: "nexus.dock_core",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="3" />
        <path d="M12 2a10 10 0 0 1 10 10" strokeDasharray="2 4" />
        <path d="M12 22A10 10 0 0 1 2 12a10 10 0 0 1 4-8" />
        <circle cx="12" cy="12" r="7.5" strokeOpacity="0.5" />
      </svg>
    ),
  },
  {
    id: "chat",
    dest: VIEWS.CHAT,
    titleKey: "nexus.dest_chat",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3 3h18v12H9l-6 5z" />
        <path d="M7.5 8h9M7.5 11h5.5" strokeOpacity="0.7" />
      </svg>
    ),
  },
  {
    id: "computer",
    dest: VIEWS.COMPUTER,
    titleKey: "nexus.dest_computer",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M3 12h18" />
        <path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z" />
      </svg>
    ),
  },
  {
    id: "tasks",
    dest: VIEWS.TASKS,
    titleKey: "nexus.dest_tasks",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <line x1="4" y1="20" x2="9" y2="15" />
        <line x1="10" y1="14" x2="14" y2="10" />
        <line x1="16" y1="9" x2="20" y2="5" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="9" y="9" width="7" height="7" rx="1" opacity="0.6" />
        <rect x="15" y="4" width="7" height="7" rx="1" opacity="0.3" />
      </svg>
    ),
  },
  {
    id: "workspace",
    dest: VIEWS.WORKSPACE,
    titleKey: "nexus.dest_workspace",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
    ),
  },
  {
    id: "skills",
    dest: VIEWS.SKILLS,
    titleKey: "nexus.dest_skills",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 3l2.4 5.8L20 10l-5.6 2.2L12 18l-2.4-5.8L4 10l5.6-2.2z" />
        <path d="M18 15l1 2.4L21 18l-2 1-1 2-1-2-2-1 2-1z" opacity="0.6" />
      </svg>
    ),
  },
  {
    id: "security",
    dest: VIEWS.SECURITY,
    titleKey: "nexus.dest_security",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        <path d="M9 11l2 2 4-4" />
      </svg>
    ),
  },
  {
    id: "models",
    dest: VIEWS.MODELS,
    titleKey: "nexus.dest_models",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="5" y="5" width="14" height="14" rx="2" />
        <circle cx="9" cy="9" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="15" cy="9" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="9" cy="15" r="1.4" fill="currentColor" stroke="none" />
        <circle cx="15" cy="15" r="1.4" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  {
    id: "machines",
    dest: VIEWS.MACHINES,
    titleKey: "nexus.dest_machines",
    icon: (
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="3" y="4" width="18" height="12" rx="2" />
        <path d="M7 20h10" />
        <path d="M12 16v4" />
        <rect x="6" y="7" width="6" height="6" rx="1" opacity="0.6" />
      </svg>
    ),
  },
];

function NexusDock({ language, screen, onNavigate, onOpenCommands, onSettingsToggle }) {
  const t = (key) => translate(language, key);
  return (
    <nav className="nx-dock" aria-label={t("nexus.app_dock")}>
      {CELLS.map((cell) => (
        <button
          key={cell.id}
          type="button"
          className="nx-dc"
          aria-current={screen === cell.dest ? "true" : undefined}
          title={t(cell.titleKey)}
          aria-label={t(cell.titleKey)}
          onClick={() => onNavigate(cell.dest)}
        >
          {cell.icon}
        </button>
      ))}
      <span className="nx-dc-sep" aria-hidden="true" />
      <button
        type="button"
        className="nx-dc"
        data-dest="palette"
        title={t("nexus.all_commands")}
        aria-label={t("nexus.all_commands")}
        onClick={onOpenCommands}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
        </svg>
      </button>
      <button
        type="button"
        className="nx-dc"
        data-dest="settings"
        title={t("nexus.settings")}
        aria-label={t("nexus.settings")}
        onClick={onSettingsToggle}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="3" />
          <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1" />
        </svg>
      </button>
    </nav>
  );
}

export default memo(NexusDock);
