import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/api";
import { MAX_ENABLED_SKILLS as MAX_ENABLED } from "../lib/constants";

/**
 * SkillsView — the full skills lifecycle as a top-level page.
 *
 * Promoted from a Workspace tab to a first-class destination. The page
 * covers the complete lifecycle:
 *
 *   create → write instructions → save to disk → enable → persist → restart-survives
 *
 * Every action goes through the real backend API. The UI never bypasses
 * ToolRegistry, permission floors, or filesystem containment — those are
 * enforced by the backend, not the frontend.
 *
 * The page is a two-pane layout: the skill list on the left, the editor
 * (or detail view) on the right. This is the same information architecture
 * as the Workspace tab, expanded to full-page width.
 */

function SkillsView({ language = "en" }) {
  const ar = language === "ar";
  const [skills, setSkills] = useState([]);
  const [errors, setErrors] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", description: "" });

  const enabledCount = useMemo(() => skills.filter((s) => s.enabled).length, [skills]);

  const load = useCallback(async () => {
    setLoading(true);
    const result = await api.getSkills();
    if (result.error) {
      setError(result.error);
    } else {
      setError(null);
      setSkills(result.skills || []);
      setErrors(result.errors || []);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = useCallback(
    async (name, enabled) => {
      const result = await api.setSkillEnabled(name, enabled);
      if (result.error) {
        setError(result.error);
        return;
      }
      setError(null);
      await load();
      // Refresh detail if the toggled skill is selected.
      setSelected((prev) => {
        if (prev === name) {
          // Force re-fetch
          api.getSkill(name).then((d) => {
            if (!d.error) setDetail(d);
          });
        }
        return prev;
      });
    },
    [load],
  );

  const create = useCallback(
    async (event) => {
      event.preventDefault();
      const result = await api.createSkill(form.name, { description: form.description });
      if (result.error) {
        setError(result.error);
        return;
      }
      setError(null);
      setForm({ name: "", description: "" });
      setCreating(false);
      await load();
    },
    [form, load],
  );

  const inspect = useCallback(async (name) => {
    setSelected(name);
    setEditing(false);
    const result = await api.getSkill(name);
    if (result.error) {
      setError(result.error);
      return;
    }
    setError(null);
    setDetail(result);
    setEditContent(result.content || "");
  }, []);

  const save = useCallback(async () => {
    if (!selected) return;
    setSaving(true);
    setError(null);
    try {
      // Write instructions to disk via the backend.
      const result = await api.writeFile(
        `${detail.path}/instructions.md`,
        editContent,
      );
      if (result.error) {
        setError(result.error);
      } else {
        setEditing(false);
        await inspect(selected);
      }
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setSaving(false);
    }
  }, [selected, detail, editContent, inspect]);

  const remove = useCallback(
    async (name) => {
      const result = await api.removeSkill(name);
      if (result.error) {
        setError(result.error);
      } else {
        setError(null);
        if (selected === name) {
          setSelected(null);
          setDetail(null);
        }
        await load();
      }
    },
    [selected, load],
  );

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center" style={{ padding: "var(--space-5)" }}>
        <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
          {ar ? "جارٍ التحميل…" : "LOADING…"}
        </p>
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex" style={{ padding: "var(--space-4)", gap: "var(--space-4)" }}>
      {/* Left: skill list */}
      <div
        className="hud-panel flex-none flex flex-col min-h-0"
        style={{ width: "280px", maxWidth: "40%" }}
      >
        <header className="hud-panel-head">
          <span>{ar ? "المهارات" : "SKILLS"}</span>
          <span className="hud-micro ml-auto">
            {enabledCount}/{MAX_ENABLED}
          </span>
          <button
            onClick={() => setCreating((v) => !v)}
            className="hud-micro"
            style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", border: "var(--border-w) solid var(--hud-edge-faint)", background: "transparent" }}
          >
            {ar ? "جديد" : "NEW"}
          </button>
        </header>

        {error && (
          <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)", fontSize: "var(--font-sm)" }}>
            {error}
          </p>
        )}

        {creating && (
          <form
            onSubmit={create}
            className="flex flex-col"
            style={{ padding: "var(--space-3)", gap: "var(--space-2)", borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}
          >
            <label className="hud-micro" htmlFor="skill-name">
              {ar ? "الاسم" : "Name"}
            </label>
            <input
              id="skill-name"
              className="hud-input w-full"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="laravel-security"
              required
            />
            <label className="hud-micro" htmlFor="skill-description">
              {ar ? "الوصف" : "Description"}
            </label>
            <input
              id="skill-description"
              className="hud-input w-full"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="Laravel security review"
            />
            <button
              type="submit"
              className="hud-micro"
              style={{ padding: "var(--space-2) var(--space-3)", cursor: "pointer", background: "color-mix(in srgb, var(--hud-cyan) 8%, transparent)", border: "var(--border-w) solid color-mix(in srgb, var(--hud-cyan) 25%, transparent)", color: "var(--hud-cyan)" }}
            >
              {ar ? "إنشاء" : "CREATE"}
            </button>
          </form>
        )}

        {errors.length > 0 && (
          <div style={{ padding: "var(--space-2) var(--space-3)", borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}>
            <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
              {ar ? `${errors.length} لم تُحمَّل` : `${errors.length} failed to load`}
            </p>
            {errors.map((e) => (
              <p key={e.path} className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                {e.path.split(/[\\/]/).pop()}: {e.error}
              </p>
            ))}
          </div>
        )}

        <div className="flex-1 overflow-y-auto">
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {skills.map((skill) => (
              <li
                key={skill.name}
                style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)" }}
              >
                <div className="flex items-center" style={{ padding: "var(--space-2) var(--space-3)", gap: "var(--space-2)" }}>
                  <div className="flex-1 min-w-0">
                    <p
                      className="flex items-center"
                      style={{ gap: "var(--space-2)", fontSize: "var(--font-sm)", color: selected === skill.name ? "var(--hud-cyan)" : "var(--hud-text)" }}
                    >
                      {/* The reference's node marker, carrying the enabled
                          state. A hex reads as an instrument node where a dot
                          read as a bullet. */}
                      <span
                        aria-hidden="true"
                        className={skill.enabled ? "hud-hex hud-hex--ok" : "hud-hex hud-hex--hollow hud-hex--idle"}
                      />
                      {skill.name}
                    </p>
                    {skill.description && (
                      <p className="hud-micro truncate" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                        {skill.description}
                      </p>
                    )}
                    {skill.permissions?.length > 0 && (
                      <p className="hud-micro truncate" style={{ color: "var(--hud-amber)", fontSize: "var(--font-xs)" }}>
                        {ar ? "يطلب" : "asks for"}: {skill.permissions.join(" · ")}
                      </p>
                    )}
                  </div>
                  <label style={{ display: "flex", alignItems: "center", gap: "var(--space-1)" }}>
                    <input
                      type="checkbox"
                      checked={skill.enabled}
                      onChange={(e) => toggle(skill.name, e.target.checked)}
                    />
                    <span className="sr-only">{ar ? "مفعّل" : "Enabled"}</span>
                  </label>
                  <button
                    onClick={() => inspect(skill.name)}
                    className="hud-micro"
                    style={{ padding: "var(--space-1) var(--space-2)", fontSize: "var(--font-xs)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-text-dim)" }}
                  >
                    {ar ? "عرض" : "VIEW"}
                  </button>
                  <button
                    onClick={() => remove(skill.name)}
                    className="hud-micro"
                    style={{ padding: "var(--space-1) var(--space-2)", fontSize: "var(--font-xs)", cursor: "pointer", background: "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: "var(--hud-red)" }}
                  >
                    {ar ? "حذف" : "DEL"}
                  </button>
                </div>
              </li>
            ))}
            {skills.length === 0 && (
              <li className="hud-micro" style={{ padding: "var(--space-4)", color: "var(--hud-text-mute)" }}>
                {ar
                  ? "لا مهارات. أنشئ واحدة أو انسخ مجلدًا إلى دليل المهارات."
                  : "No skills. Create one, or copy a directory into the skills folder."}
              </li>
            )}
          </ul>
        </div>
      </div>

      {/* Right: detail / editor */}
      <div className="hud-panel flex-1 flex flex-col min-h-0">
        {detail ? (
          <>
            <header className="hud-panel-head">
              <span
                aria-hidden="true"
                className={detail.enabled ? "hud-hex hud-hex--ok" : "hud-hex hud-hex--hollow hud-hex--idle"}
              />
              <span>{detail.name}</span>
              <span className="hud-micro ml-auto">
                {detail.enabled ? (ar ? "مفعّل" : "ENABLED") : (ar ? "معطّل" : "DISABLED")}
              </span>
              <button
                onClick={() => setEditing((v) => !v)}
                className="hud-micro"
                style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", background: editing ? "color-mix(in srgb, var(--hud-cyan) 8%, transparent)" : "transparent", border: "var(--border-w) solid var(--hud-edge-faint)", color: editing ? "var(--hud-cyan)" : "var(--hud-text-dim)" }}
              >
                {editing ? (ar ? "إلغاء" : "CANCEL") : (ar ? "تعديل" : "EDIT")}
              </button>
              {editing && (
                <button
                  onClick={save}
                  disabled={saving}
                  className="hud-micro"
                  style={{ padding: "var(--space-1) var(--space-2)", cursor: "pointer", background: "color-mix(in srgb, var(--hud-green) 8%, transparent)", border: "var(--border-w) solid color-mix(in srgb, var(--hud-green) 25%, transparent)", color: "var(--hud-green)" }}
                >
                  {saving ? "…" : (ar ? "حفظ" : "SAVE")}
                </button>
              )}
            </header>

            {/* Metadata */}
            <div style={{ padding: "var(--space-3)", borderBottom: "var(--border-w) solid var(--hud-edge-faint)" }}>
              {detail.description && (
                <p style={{ fontSize: "var(--font-sm)", color: "var(--hud-text-dim)", marginBottom: "var(--space-2)" }}>
                  {detail.description}
                </p>
              )}
              {detail.permissions?.length > 0 && (
                <p className="hud-micro" style={{ color: "var(--hud-amber)", fontSize: "var(--font-xs)" }}>
                  {ar ? "يطلب الأذونات" : "ASKS FOR"}: {detail.permissions.join(" · ")}
                </p>
              )}
              {detail.tools?.length > 0 && (
                <p className="hud-micro" style={{ color: "var(--hud-text-dim)", fontSize: "var(--font-xs)" }}>
                  {ar ? "ملفات الأدوات" : "TOOL FILES"}: {detail.tools.join(", ")}
                </p>
              )}
              {detail.version && (
                <p className="hud-micro" style={{ color: "var(--hud-text-mute)", fontSize: "var(--font-xs)" }}>
                  v{detail.version}
                </p>
              )}
            </div>

            {/* Instructions editor / viewer */}
            <div className="flex-1 overflow-y-auto" style={{ padding: "var(--space-3)" }}>
              {editing ? (
                <textarea
                  className="w-full hud-mono"
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  spellCheck={false}
                  dir="ltr"
                  style={{
                    minHeight: "300px",
                    resize: "vertical",
                    background: "var(--input-bg)",
                    border: "var(--border-w) solid var(--input-border)",
                    color: "var(--input-text)",
                    padding: "var(--space-3)",
                    fontSize: "var(--font-sm)",
                    lineHeight: 1.6,
                    fontFamily: "var(--font-mono)",
                  }}
                  aria-label={ar ? "تعليمات المهارة" : "Skill instructions"}
                />
              ) : (
                <pre
                  className="hud-mono"
                  style={{
                    fontSize: "var(--font-sm)",
                    whiteSpace: "pre-wrap",
                    color: "var(--hud-text-dim)",
                    margin: 0,
                    lineHeight: 1.6,
                  }}
                >
                  {detail.content || (ar ? "(فارغ)" : "(empty)")}
                </pre>
              )}
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center">
            <p className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {ar ? "اختر مهارة من القائمة لعرضها أو تعديلها." : "Select a skill from the list to view or edit it."}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(SkillsView);
