import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  getConnectors,
  getConnections,
  testConnection,
  refreshConnection,
  disconnectConnection,
  deleteConnection,
  discoverCapabilities,
} from "../../lib/api";
import { translate } from "../../i18n/strings";
import ConnectorList from "./ConnectorList";
import ConnectionList from "./ConnectionList";
import ConnectDialog from "./ConnectDialog";

/**
 * ConnectionsView — the Connections workspace.
 *
 * User flow: open Connections page → browse/search connectors → select service
 * → Connect → authenticate → review/approve permissions → complete setup → see
 * connection status → let assistant discover and use exposed tools →
 * disconnect/reconnect/refresh/edit.
 *
 * Architecture notes (mirror the backend):
 *   * No credential values ever reach this component. `has_credentials` is the
 *     only signal the backend sends about stored secrets.
 *   * OAuth happens in the system browser (desktop) or a popup (web); the
 *     callback is handled server-side and the frontend polls / gets a redirect
 *     result.
 *   * Capability execution is gated: confirm/dangerous capabilities return
 *     `requires_confirmation` and the caller shows a confirmation dialog before
 *     retrying with `approval: true`.
 *   * Every string comes from `src/i18n/strings.js` (connections.*).
 */
function ConnectionsView({ language = "en" }) {
  const t = useMemo(
    () => (key, vars) => translate(language, key, vars),
    [language]
  );

  const [connectors, setConnectors] = useState([]);
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [openConnector, setOpenConnector] = useState(null); // connector being connected
  const [activeConnectionId, setActiveConnectionId] = useState(null); // connection being viewed
  const [connectionError, setConnectionError] = useState(null);


  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const [connResult, connListResult] = await Promise.all([
      getConnectors(),
      getConnections(),
    ]);
    if (connResult.error) {
      setError(connResult.error);
    } else {
      setConnectors(connResult.connectors || []);
    }
    if (connListResult.error) {
      setError(connListResult.error);
    } else {
      setConnections(connListResult.connections || []);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleConnect = useCallback(
    (connectorId) => {
      setOpenConnector(connectorId);
    },
    []
  );

  const handleConnectionCreated = useCallback(
    async (connectionId) => {
      setOpenConnector(null);
      await load();
      setActiveConnectionId(connectionId);
    },
    [load]
  );

  const handleDeleteConnection = useCallback(
    async (id) => {
      const result = await deleteConnection(id);
      if (result.error) {
        setConnectionError(result.error);
      } else {
        setActiveConnectionId(null);
        await load();
      }
    },
    [load]
  );

  const handleTestConnection = useCallback(
    async (id) => {
      setConnectionError(null);
      const result = await testConnection(id);
      if (result.error) {
        setConnectionError(result.error);
      } else if (result.ok) {
        setConnectionError(null);
      }
    },
    []
  );

  const handleRefresh = useCallback(
    async (id) => {
      setConnectionError(null);
      const result = await refreshConnection(id);
      if (result.error) {
        setConnectionError(result.error);
      } else {
        await load();
      }
    },
    [load]
  );

  const handleDisconnect = useCallback(
    async (id) => {
      setConnectionError(null);
      const result = await disconnectConnection(id);
      if (result.error) {
        setConnectionError(result.error);
      } else {
        await load();
      }
    },
    [load]
  );

  const handleDiscover = useCallback(async () => {
    setConnectionError(null);
    const result = await discoverCapabilities();
    if (result.error) {
      setConnectionError(result.error);
    } else {
      setActiveConnectionId(null);
      // The discovery result is shown via a transient message in the active
      // connection panel; store it in state via a lightweight mechanism.
      window.dispatchEvent(
        new CustomEvent("zaytoon:discovery", {
          detail: { capabilities: result.capabilities || [], total: result.total },
        })
      );
    }
  }, []);

  return (
    <div
      className="flex-1 min-h-0 overflow-y-auto"
      style={{
        padding: "var(--space-4)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-4)",
      }}
    >
      <header>
        <h1 className="hud-label" style={{ fontSize: "var(--font-lg)" }}>
          {t("connections.title")}
        </h1>
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {t("connections.subtitle")}
        </p>
      </header>

      {error && (
        <p
          role="alert"
          className="hud-panel"
          style={{
            padding: "var(--space-3)",
            color: "var(--hud-red)",
            fontSize: "var(--font-sm)",
          }}
        >
          {error}
        </p>
      )}

      <div className="flex flex-wrap" style={{ gap: "var(--space-4)" }}>
        {/* ---- Browse connectors ---- */}
        <section className="hud-panel flex-1" style={{ minWidth: "320px" }}>
          <ConnectorList
            connectors={connectors}
            loading={loading}
            onConnect={handleConnect}
            t={t}
          />
        </section>

        {/* ---- My connections ---- */}
        <section className="hud-panel flex-2" style={{ minWidth: "420px" }}>
          <ConnectionList
            connections={connections}
            loading={loading}
            activeConnectionId={activeConnectionId}
            onSetActive={setActiveConnectionId}
            onTest={handleTestConnection}
            onRefresh={handleRefresh}
            onDisconnect={handleDisconnect}
            onDelete={handleDeleteConnection}
            onDiscover={handleDiscover}
            t={t}
            error={connectionError}
          />
        </section>
      </div>

      {/* ---- Connect dialog (OAuth / API key / MCP) ---- */}
      {openConnector && (
        <ConnectDialog
          connectorId={openConnector}
          onClose={() => setOpenConnector(null)}
          onSuccess={handleConnectionCreated}
          onError={() => {}}
          t={t}
        />
      )}
    </div>
  );
}

export default memo(ConnectionsView);
