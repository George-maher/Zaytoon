import { memo, useCallback, useEffect, useMemo, useState } from "react";

const STATE_LABELS = {
  not_connected: { en: "Not connected", ar: "غير متصل" },
  connecting: { en: "Connecting", ar: "جاري الاتصال" },
  connected: { en: "Connected", ar: "متصل" },
  needs_reauthorization: { en: "Needs reauthorization", ar: "يحتاج إلى تفويض مجدد" },
  error: { en: "Error", ar: "خطأ" },
  disabled: { en: "Disabled", ar: "معطل" },
  disconnecting: { en: "Disconnecting", ar: "جاري قطع الاتصال" },
};

const STATE_TONE = {
  connected: "var(--hud-green)",
  not_connected: "var(--hud-text-mute)",
  connecting: "var(--hud-amber)",
  needs_reauthorization: "var(--hud-amber)",
  error: "var(--hud-red)",
  disabled: "var(--hud-text-mute)",
  disconnecting: "var(--hud-amber)",
};

function ConnectionList({
  connections,
  loading,
  _activeConnectionId,
  onSetActive,
  onTest,
  onRefresh,
  onDisconnect,
  onDelete,
  onDiscover,
  t,
  error,
}) {
  const [discovery, setDiscovery] = useState(null);

  const connectedCount = useMemo(
    () => connections.filter((c) => c.state === "connected").length,
    [connections]
  );

  const handleDiscovery = useCallback(() => {
    onDiscover();
  }, [onDiscover]);

  // Listen for discovery events dispatched from the parent view.
  useEffect(() => {
    const onDiscovery = (event) => {
      setDiscovery(event.detail);
    };
    window.addEventListener("zaytoon:discovery", onDiscovery);
    return () => window.removeEventListener("zaytoon:discovery", onDiscovery);
  }, []);

  if (loading) {
    return (
      <section className="hud-panel" style={{ minWidth: "420px" }}>
        <header className="hud-panel-head">
          <span>{t("connections.my_connections")}</span>
          <span className="hud-micro">{connectedCount} / {connections.length}</span>
        </header>
        <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
          {t("common.loading")}
        </p>
      </section>
    );
  }

  return (
    <section className="hud-panel" style={{ minWidth: "420px" }}>
      <header className="hud-panel-head">
        <span>{t("connections.my_connections")}</span>
        <span className="hud-micro">{connectedCount} / {connections.length}</span>
      </header>
      <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>

        {error && (
          <p role="alert" className="hud-micro" style={{ color: "var(--hud-red)" }}>
            {error}
          </p>
        )}

        <div className="flex" style={{ gap: "var(--space-2)" }}>
          <button
            type="button"
            onClick={handleDiscovery}
            disabled={connections.length === 0}
            className="hud-micro"
            style={{
              padding: "var(--space-2) var(--space-3)",
              cursor: "pointer",
              background: "transparent",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text-dim)",
            }}
          >
            {t("connections.capabilities")}
          </button>
        </div>

        {discovery && (
          <div
            style={{
              border: "var(--border-w) solid var(--hud-edge-faint)",
              padding: "var(--space-2) var(--space-3)",
              background: "color-mix(in srgb, var(--hud-cyan) 5%, transparent)",
            }}
          >
            <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)", margin: "0 0 var(--space-1) 0" }}>
              {t("connections.capabilities")} — {discovery.total} found
            </p>
            <ul
              style={{
                listStyle: "none",
                margin: 0,
                padding: 0,
                maxHeight: "200px",
                overflowY: "auto",
              }}
            >
              {discovery.capabilities.map((cap) => (
                <li key={cap.capability.id} style={{ padding: "var(--space-1) 0" }}>
                  <span
                    className="hud-mono"
                    dir="ltr"
                    style={{ fontSize: "var(--font-xs)", color: "var(--hud-text)" }}
                  >
                    {cap.capability.name}
                  </span>
                  <span
                    className="hud-micro"
                    style={{
                      fontSize: "var(--font-xs)",
                      color: cap.authorized ? "var(--hud-green)" : "var(--hud-text-mute)",
                    }}
                  >
                    — {cap.authorized ? t("connections.connected") : t("connections.error")}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {connections.length === 0 && !discovery ? (
          <p className="hud-micro" style={{ padding: "var(--space-3)", color: "var(--hud-text-mute)" }}>
            {t("connections.no_connections")}
          </p>
        ) : null}

        <ul style={{ listStyle: "none", margin: 0, padding: 0, overflowY: "auto", maxHeight: "45vh" }}>
          {connections.map((connection) => (
            <li
              key={connection.id}
              style={{
                borderTop: "var(--border-w) solid var(--hud-edge-faint)",
                padding: "var(--space-2) var(--space-3)",
              }}
            >
              <div
                onClick={() => onSetActive(connection.id)}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  cursor: "pointer",
                }}
              >
                <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-0_5)" }}>
                  <span
                    style={{ fontSize: "var(--font-sm)", color: "var(--hud-text)" }}
                  >
                    {connection.connector_id}
                  </span>
                  <span
                    className="hud-micro"
                    style={{
                      fontSize: "var(--font-xs)",
                      color: STATE_TONE[connection.state] || "var(--hud-text-mute)",
                    }}
                  >
                    {STATE_LABELS[connection.state]?.en || connection.state}
                  </span>
                </div>
                <span
                  style={{
                    fontSize: "var(--font-xs)",
                    color: "var(--hud-text-mute)",
                    fontWeight: 600,
                  }}
                >
                  {connection.has_credentials ? t("connections.has_credentials") : t("connections.no_credentials")}
                </span>
                {connection.error && (
                  <span className="hud-micro" style={{ color: "var(--hud-red)" }}>
                    {connection.error}
                  </span>
                )}
              </div>

              <div
                style={{
                  display: "flex",
                  gap: "var(--space-1)",
                  marginTop: "var(--space-1)",
                  flexWrap: "wrap",
                }}
              >
                {connection.state === "connected" && (
                  <>
                    <button
                      type="button"
                      onClick={() => onTest(connection.id)}
                      className="hud-micro"
                      style={{
                        padding: "var(--space-1) var(--space-2)",
                        cursor: "pointer",
                        background: "transparent",
                        border: "var(--border-w) solid var(--hud-edge-faint)",
                        color: "var(--hud-text-dim)",
                        fontSize: "var(--font-xs)",
                      }}
                    >
                      {t("connections.test")}
                    </button>
                    <button
                      type="button"
                      onClick={() => onRefresh(connection.id)}
                      className="hud-micro"
                      style={{
                        padding: "var(--space-1) var(--space-2)",
                        cursor: "pointer",
                        background: "transparent",
                        border: "var(--border-w) solid var(--hud-edge-faint)",
                        color: "var(--hud-text-dim)",
                        fontSize: "var(--font-xs)",
                      }}
                    >
                      {t("connections.refresh")}
                    </button>
                    <button
                      type="button"
                      onClick={() => onDisconnect(connection.id)}
                      className="hud-micro"
                      style={{
                        padding: "var(--space-1) var(--space-2)",
                        cursor: "pointer",
                        background: "transparent",
                        border: "var(--border-w) solid var(--hud-edge-faint)",
                        color: "var(--hud-red)",
                        fontSize: "var(--font-xs)",
                      }}
                    >
                      {t("connections.disconnect")}
                    </button>
                  </>
                )}
                {connection.state === "needs_reauthorization" && (
                  <button
                    type="button"
                    onClick={() => onSetActive(connection.id)}
                    className="hud-micro"
                    style={{
                      padding: "var(--space-1) var(--space-2)",
                      cursor: "pointer",
                      background: "color-mix(in srgb, var(--hud-amber) 10%, transparent)",
                      border: "var(--border-w) solid color-mix(in srgb, var(--hud-amber) 30%, transparent)",
                      color: "var(--hud-amber)",
                      fontSize: "var(--font-xs)",
                    }}
                  >
                    {t("connections.reconnect")}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onDelete(connection.id)}
                  className="hud-micro"
                  style={{
                    padding: "var(--space-1) var(--space-2)",
                    cursor: "pointer",
                    background: "transparent",
                    border: "var(--border-w) solid var(--hud-edge-faint)",
                    color: "var(--hud-text-dim)",
                    fontSize: "var(--font-xs)",
                  }}
                >
                  {t("connections.delete")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

ConnectionList.displayName = "ConnectionList";
export default memo(ConnectionList);
