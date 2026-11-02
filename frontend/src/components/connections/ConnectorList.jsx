import { memo, useCallback, useMemo, useState } from "react";

const AUTH_LABELS = {
  oauth2: { en: "OAuth 2.0", ar: "أ OAuth 2.0" },
  api_key: { en: "API key", ar: "مفتاح API" },
  mcp: { en: "MCP server", ar: "خادم MCP" },
  none: { en: "None", ar: "بدون" },
};

const CATEGORY_LABELS = {
  development: { en: "Development", ar: "تطوير" },
  storage: { en: "Storage", ar: "تخزين" },
  communication: { en: "Communication", ar: "اتصال" },
  mcp: { en: "MCP", ar: "MCP" },
};

function ConnectorList({ connectors, loading, onConnect, t }) {
  const [search, setSearch] = useState("");

  const filtered = useMemo(
    () =>
      connectors.filter(
        (c) =>
          !search ||
          c.name.toLowerCase().includes(search.toLowerCase()) ||
          c.description.toLowerCase().includes(search.toLowerCase())
      ),
    [connectors, search]
  );

  const handleConnect = useCallback(
    (connectorId) => {
      onConnect(connectorId);
    },
    [onConnect]
  );

  if (loading) {
    return (
      <section className="hud-panel" style={{ minWidth: "320px" }}>
        <header className="hud-panel-head">
          <span>{t("connections.browse_connectors")}</span>
        </header>
        <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
          {t("common.loading")}
        </p>
      </section>
    );
  }

  return (
    <section className="hud-panel" style={{ minWidth: "320px" }}>
      <header className="hud-panel-head">
        <span>{t("connections.browse_connectors")}</span>
      </header>
      <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
        <label className="hud-micro" style={{ fontSize: "var(--font-xs)" }}>
          {t("connections.connector")}
        </label>
        <input
          className="hud-input w-full"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("connections.no_connectors")}
        />

        {filtered.length === 0 ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {t("connections.no_connectors")}
          </p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, overflowY: "auto", maxHeight: "60vh" }}>
            {filtered.map((connector) => (
              <li
                key={connector.id}
                style={{
                  borderTop: "var(--border-w) solid var(--hud-edge-faint)",
                  padding: "var(--space-2) var(--space-3)",
                  display: "flex",
                  flexDirection: "column",
                  gap: "var(--space-1)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <span
                    style={{ fontSize: "var(--font-sm)", color: "var(--hud-text)", fontWeight: 600 }}
                  >
                    {connector.name}
                  </span>
                  <span
                    className="hud-micro"
                    style={{
                      fontSize: "var(--font-xs)",
                      color: "var(--hud-text-mute)",
                      background: "color-mix(in srgb, var(--hud-cyan) 8%, transparent)",
                      border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 20%, transparent)",
                      padding: "var(--space-1)",
                      borderRadius: "var(--space-1)",
                    }}
                  >
                    {AUTH_LABELS[connector.auth_method]?.[
                      t("common.language_ar").startsWith("ع") ? "ar" : "en"
                    ] || connector.auth_method}
                  </span>
                </div>
                <p
                  className="hud-micro"
                  style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-dim)", margin: 0 }}
                >
                  {connector.description}
                </p>
                <p
                  className="hud-mono"
                  dir="ltr"
                  style={{
                    fontSize: "var(--font-xs)",
                    color: "var(--hud-text-mute)",
                    margin: 0,
                  }}
                >
                  {CATEGORY_LABELS[connector.category]?.en || connector.category}
                </p>
                {connector.scopes && connector.scopes.length > 0 && (
                  <p
                    className="hud-mono"
                    dir="ltr"
                    style={{
                      fontSize: "var(--font-xs)",
                      color: "var(--hud-text-mute)",
                      margin: "var(--space-1) 0 0 0",
                    }}
                  >
                    Scopes: {connector.scopes.join(", ")}
                  </p>
                )}
                <button
                  type="button"
                  onClick={() => handleConnect(connector.id)}
                  className="hud-micro"
                  style={{
                    padding: "var(--space-2) var(--space-3)",
                    cursor: "pointer",
                    background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
                    border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 30%, transparent)",
                    color: "var(--hud-cyan)",
                    marginTop: "var(--space-1)",
                    alignSelf: "flex-start",
                  }}
                >
                  {t("connections.connect")}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

ConnectorList.displayName = "ConnectorList";
export default memo(ConnectorList);
