import { memo } from "react";

/**
 * Shared empty state — one consistent look for "no data here" across panels.
 *
 * Uses theme tokens only (--zt-*, --hud-*, --space-*, --font-*), so adopting
 * it in a panel is a swap of inline styles for a single component, not a
 * restyle. No animation: an empty panel is static information, not a process.
 *
 * @param {Object} props
 * @param {string} [props.title] — heading, e.g. "No commands yet"
 * @param {string} [props.description] — one line of context, e.g. "Run a command from the console"
 * @param {string} [props.glyph] — single glyph shown left of the title, e.g. "○"
 * @param {string} [props._language] — "en" | "ar", reserved for call-site translation
 * @param {string} [props.className] — extra Tailwind classes
 * @param {string} [props.ariaLabel] — accessible name for the region; defaults to `title`
 * @param {React.ReactNode} [props.action] — optional interactive child (a Retry button, a link)
 */
function EmptyState({
  title,
  description,
  glyph,
  _language,
  className = "",
  ariaLabel,
  action,
}) {
  return (
    <div
      role="region"
      aria-label={ariaLabel ?? title}
      className={`flex flex-col items-center justify-center gap-2 text-center ${className}`}
      style={{
        padding: `var(--space-6) var(--space-4)`,
        color: "var(--zt-text-muted)",
        fontFamily: "var(--zt-font-ui)",
        fontSize: "var(--font-sm)",
      }}
    >
      {glyph ? (
        <span
          aria-hidden="true"
          style={{
            fontFamily: "var(--zt-font-mono)",
            fontSize: "var(--font-md)",
            letterSpacing: "0.2em",
            opacity: 0.5,
          }}
        >
          {glyph}
        </span>
      ) : null}
      {title ? (
        <h3
          style={{
            margin: 0,
            color: "var(--zt-text-secondary)",
            fontFamily: "var(--zt-font-ui)",
            fontSize: "var(--font-md)",
            fontWeight: 600,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
          }}
        >
          {title}
        </h3>
      ) : null}
      {description ? (
        <p
          style={{
            margin: 0,
            color: "var(--zt-text-muted)",
            fontFamily: "var(--zt-font-ui)",
            fontSize: "var(--font-sm)",
            lineHeight: 1.5,
          }}
        >
          {description}
        </p>
      ) : null}
      {action ? (
        <div style={{ marginTop: "var(--space-4)" }}>{action}</div>
      ) : null}
    </div>
  );
}

export default memo(EmptyState);
