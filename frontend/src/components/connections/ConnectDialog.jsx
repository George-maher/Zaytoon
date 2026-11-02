import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  createConnection,
  startAuth,
  completeAuth,
} from "../../lib/api";

/**
 * ConnectDialog — the connect/auth flow for one connector.
 *
 * Three auth methods are rendered depending on what the connector declares:
 *   * oauth2 — shows the auth URL (desktop opens it in the system browser;
 *     web shows a link). After the user approves on the provider's consent
 *     page, the callback is handled server-side; the user pastes back the
 *     result or the desktop host delivers it via the bridge.
 *   * api_key — shows a masked password field. The value is posted to the
 *     backend once and stored server-side; it is never echoed back.
 *   * mcp — shows server config fields (name, command, args, env).
 *
 * No secret value is ever stored in component state longer than needed, and
 * the backend never returns a credential value — only `has_credentials`.
 */
function ConnectDialog({ connectorId, onClose, onSuccess, onError, t }) {
  const [loading, setLoading] = useState(false);
  const [step, setStep] = useState("config"); // config | auth | complete
  const [authResult, setAuthResult] = useState(null);
  const [form, setForm] = useState({});

  const handleCreate = useCallback(async () => {
    setLoading(true);
    try {
      const result = await createConnection(connectorId, {});
      if (result.error) {
        onError(result.error);
        setLoading(false);
        return;
      }
      setStep("auth");
      const auth = await startAuth(result.id, window.location.origin);
      if (auth.error) {
        onError(auth.error);
        setLoading(false);
        return;
      }
      setAuthResult(auth);
      setLoading(false);
    } catch (e) {
      onError(e.message || "Connection creation failed");
      setLoading(false);
    }
  }, [connectorId, onError]);

  const handleComplete = useCallback(async () => {
    setLoading(true);
    try {
      const result = await completeAuth(authResult.connection_id, {
        ...form,
      });
      if (result.error) {
        onError(result.error);
        setLoading(false);
        return;
      }
      setStep("complete");
      onSuccess(authResult.connection_id);
      setLoading(false);
    } catch (e) {
      onError(e.message || "Authentication failed");
      setLoading(false);
    }
  }, [authResult, form, onError, onSuccess]);

  const handleOpenAuthUrl = useCallback(() => {
    if (!authResult?.auth_url) return;
    // Desktop host opens the system browser; web opens the URL in the same tab
    // so the OAuth callback can reach the localhost redirect URI.
    window.location.href = authResult.auth_url;
  }, [authResult]);

  useEffect(() => {
    if (step === "config") {
      handleCreate();
    }
  }, [step, handleCreate]);

  const renderAuthMethod = useMemo(() => {
    if (step === "complete") {
      return (
        <div style={{ padding: "var(--space-3)", textAlign: "center", color: "var(--hud-green)" }}>
          {t("connections.connected")}
        </div>
      );
    }

    if (!authResult) return null;

    if (authResult.auth_method === "oauth2") {
      return (
        <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
            {t("connections.api_key_stored")}
          </p>
          <a
            href={authResult.auth_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => {
              e.preventDefault();
              handleOpenAuthUrl();
            }}
            className="hud-micro"
            style={{
              padding: "var(--space-2) var(--space-3)",
              cursor: "pointer",
              background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 30%, transparent)",
              color: "var(--hud-cyan)",
              display: "inline-block",
            }}
          >
            {t("connections.connect")}
          </a>
          <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-dim)" }}>
            After approving on the provider's page, the connection completes
            automatically. If you are on the desktop, the system browser is
            used and the result is delivered back here.
          </p>
          <button
            type="button"
            onClick={handleComplete}
            disabled={loading}
            className="hud-micro"
            style={{
              padding: "var(--space-2) var(--space-3)",
              cursor: loading ? "default" : "pointer",
              background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 30%, transparent)",
              color: "var(--hud-cyan)",
            }}
          >
            {loading ? t("common.loading") : t("connections.complete")}
          </button>
        </div>
      );
    }

    if (authResult.auth_method === "api_key") {
      return (
        <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <label
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)" }}
            htmlFor="connect-dialog-api-key"
          >
            {t("connections.api_key")}
          </label>
          <input
            id="connect-dialog-api-key"
            className="hud-input w-full"
            type="password"
            value={form.api_key || ""}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            placeholder={t("connections.api_key_placeholder")}
          />
          <p className="hud-micro" style={{ fontSize: "var(--font-xs)", color: "var(--hud-text-mute)" }}>
            {t("connections.api_key_stored")}
          </p>
          <button
            type="button"
            onClick={() => handleComplete()}
            disabled={loading || !form.api_key?.trim()}
            className="hud-micro"
            style={{
              padding: "var(--space-2) var(--space-3)",
              cursor: loading || !form.api_key?.trim() ? "default" : "pointer",
              background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 30%, transparent)",
              color: "var(--hud-cyan)",
            }}
          >
            {loading ? t("common.loading") : t("connections.connect")}
          </button>
        </div>
      );
    }

    if (authResult.auth_method === "mcp") {
      return (
        <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <label
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)" }}
            htmlFor="mcp-server-name"
          >
            {t("connections.server_name")}
          </label>
          <input
            id="mcp-server-name"
            className="hud-input w-full"
            value={form.name || ""}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="local-filesystem"
          />
          <label
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)" }}
            htmlFor="mcp-command"
          >
            {t("connections.command")}
          </label>
          <input
            id="mcp-command"
            className="hud-input w-full"
            dir="ltr"
            value={form.command || ""}
            onChange={(e) => setForm({ ...form, command: e.target.value })}
            placeholder="npx @modelcontextprotocol/server-filesystem"
          />
          <label
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)" }}
            htmlFor="mcp-args"
          >
            {t("connections.args")}
          </label>
          <input
            id="mcp-args"
            className="hud-input w-full"
            dir="ltr"
            value={form.args || ""}
            onChange={(e) => setForm({ ...form, args: e.target.value })}
            placeholder="/path/to/allowed/root"
          />
          <label
            className="hud-micro"
            style={{ fontSize: "var(--font-xs)" }}
            htmlFor="mcp-env"
          >
            {t("connections.env")}
          </label>
          <input
            id="mcp-env"
            className="hud-input w-full"
            dir="ltr"
            value={form.env || ""}
            onChange={(e) => setForm({ ...form, env: e.target.value })}
            placeholder='{"KEY": "value"}'
          />
          <button
            type="button"
            onClick={() => handleComplete()}
            disabled={loading || !form.name?.trim() || !form.command?.trim()}
            className="hud-micro"
            style={{
              padding: "var(--space-2) var(--space-3)",
              cursor:
                loading || !form.name?.trim() || !form.command?.trim() ? "default" : "pointer",
              background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
              border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 30%, transparent)",
              color: "var(--hud-cyan)",
            }}
          >
            {loading ? t("common.loading") : t("connections.connect")}
          </button>
        </div>
      );
    }

    return null;
  }, [step, authResult, form, loading, t, handleOpenAuthUrl, handleComplete]);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
        background: "rgba(0,0,0,0.45)",
      }}
    >
      <div
        className="hud-panel"
        style={{
          width: "min(560px, 92vw)",
          maxHeight: "90vh",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-3)",
        }}
      >
        <header className="hud-panel-head">
          <span>{t("connections.connect")}</span>
          <button
            type="button"
            onClick={onClose}
            className="hud-micro"
            style={{
              padding: "var(--space-1)",
              cursor: "pointer",
              background: "transparent",
              border: "var(--border-w) solid var(--hud-edge-faint)",
              color: "var(--hud-text-mute)",
            }}
          >
            ✕
          </button>
        </header>

        {step === "config" && (
          <div style={{ padding: "var(--space-3)", display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
              {t("connections.api_key_stored")}
            </p>
            <button
              type="button"
              onClick={handleCreate}
              disabled={loading}
              className="hud-micro"
              style={{
                padding: "var(--space-2) var(--space-3)",
                cursor: loading ? "default" : "pointer",
                background: "color-mix(in srgb, var(--hud-cyan) 10%, transparent)",
                border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 30%, transparent)",
                color: "var(--hud-cyan)",
              }}
            >
              {loading ? t("common.loading") : t("connections.connect")}
            </button>
          </div>
        )}

        {step === "auth" && renderAuthMethod}

        {step === "complete" && (
          <div style={{ padding: "var(--space-3)", textAlign: "center", color: "var(--hud-green)" }}>
            {t("connections.connected")}
          </div>
        )}
      </div>
    </div>
  );
}

ConnectDialog.displayName = "ConnectDialog";
export default memo(ConnectDialog);
