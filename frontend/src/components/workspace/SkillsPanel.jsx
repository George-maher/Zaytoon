import { memo, useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../../lib/api";
import { MAX_ENABLED_SKILLS as MAX_ENABLED } from "../../lib/constants";

/**
 * Skills: install, create, enable, inspect, remove.
 *
 * The panel shows **load errors** next to the skills rather than only the ones
 * that worked, because a skill the user wrote that silently does not load is the
 * worst failure this subsystem has — the assistant just quietly does not know
 * about it.
 *
 * Creating a skill writes a valid, loadable package with a stub. It does not
 * *generate* the content: an instruction set the user has not read, injected
 * into every prompt, is a prompt-injection vector dressed up as a feature.
 */
function SkillsPanel({ language = "en" }) {
  const [skills, setSkills] = useState([]);
  const [errors, setErrors] = useState([]);
  const [error, setError] = useState(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", description: "" });
  const [detail, setDetail] = useState(null);

  const ar = language === "ar";

  const load = useCallback(async () => {
    const result = await api.getSkills();
    if (result.error) {
      setError(result.error);
      return;
    }
    setError(null);
    setSkills(result.skills || []);
    setErrors(result.errors || []);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const enabledCount = useMemo(() => skills.filter((s) => s.enabled).length, [skills]);

  const toggle = useCallback(
    async (name, enabled) => {
      const result = await api.setSkillEnabled(name, enabled);
      if (result.error) {
        // A 409 means the limit is reached. The message says so, and the fix —
        // disable one first — is in it.
        setError(result.error);
        return;
      }
      setError(null);
      await load();
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
    const result = await api.getSkill(name);
    if (result.error) {
      setError(result.error);
      return;
    }
    setError(null);
    setDetail(result);
  }, []);

  const remove = useCallback(
    async (name) => {
      const result = await api.removeSkill(name);
      if (result.error) setError(result.error);
      if (detail?.name === name) setDetail(null);
      await load();
    },
    [detail, load],
  );

  return (
    <div className="hud-panel flex-1 flex flex-col min-h-0 overflow-y-auto">
      <header className="hud-panel-head">
        <span>{ar ? "المهارات" : "SKILLS"}</span>
        <span className="hud-micro ml-auto">
          {enabledCount}/{MAX_ENABLED}
        </span>
        <button onClick={() => setCreating((v) => !v)} className="hud-micro">
          {ar ? "إنشاء" : "NEW"}
        </button>
      </header>

      {error && (
        <p role="alert" style={{ padding: "var(--space-2) var(--space-3)", color: "var(--hud-red)" }}>
          {error}
        </p>
      )}

      {creating && (
        <form
          onSubmit={create}
          className="flex flex-col"
          style={{ padding: "var(--space-3)", gap: "var(--space-2)" }}
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
          <button type="submit" className="hud-micro">
            {ar ? "إنشاء" : "CREATE"}
          </button>
        </form>
      )}

      {errors.length > 0 && (
        <div style={{ padding: "0 var(--space-3) var(--space-3)" }}>
          <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
            {ar ? `${errors.length} لم تُحمَّل` : `${errors.length} failed to load`}
          </p>
          {errors.map((e) => (
            <p key={e.path} className="hud-micro" style={{ color: "var(--hud-text-mute)" }}>
              {e.path.split(/[\\/]/).pop()}: {e.error}
            </p>
          ))}
        </div>
      )}

      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {skills.map((skill) => (
          <li
            key={skill.name}
            style={{ borderTop: "var(--border-w) solid var(--hud-edge-faint)", padding: "var(--space-3)" }}
          >
            <div className="flex items-center" style={{ gap: "var(--space-2)" }}>
              <div className="flex-1 min-w-0">
                <p style={{ fontSize: "var(--font-sm)" }}>{skill.name}</p>
                {skill.description && (
                  <p className="hud-micro truncate" style={{ color: "var(--hud-text-mute)" }}>
                    {skill.description}
                  </p>
                )}
                {skill.permissions?.length > 0 && (
                  <p className="hud-micro truncate" style={{ color: "var(--hud-amber)" }}>
                    {ar ? "يطلب" : "asks for"}: {skill.permissions.join(" · ")}
                  </p>
                )}
                {!skill.has_instructions && (
                  <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
                    {ar ? "لا تعليمات" : "no instructions.md"}
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
              <button onClick={() => inspect(skill.name)} className="hud-micro">
                {ar ? "عرض" : "VIEW"}
              </button>
              <button
                onClick={() => remove(skill.name)}
                className="hud-micro"
                style={{ color: "var(--hud-red)" }}
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

      {detail && (
        <div style={{ padding: "var(--space-3)", borderTop: "var(--border-w) solid var(--hud-edge)" }}>
          <p className="hud-micro" style={{ color: "var(--hud-cyan)" }}>
            {detail.name}
          </p>
          {/* What it asks for and what it ships — the two things to see
              before reading instructions you are about to trust. The
              declaration is inspection; the `confirm` floor on its tools is
              the enforcement. */}
          {detail.permissions?.length > 0 && (
            <p className="hud-micro" style={{ color: "var(--hud-amber)" }}>
              {ar ? "يطلب الأذونات" : "ASKS FOR"}: {detail.permissions.join(" · ")}
            </p>
          )}
          {detail.tools?.length > 0 && (
            <p className="hud-micro" style={{ color: "var(--hud-text-dim)" }}>
              {ar ? "ملفات الأدوات" : "TOOL FILES"}: {detail.tools.join(", ")}
            </p>
          )}
          <pre
            className="hud-mono"
            style={{
              fontSize: "var(--font-xs)",
              whiteSpace: "pre-wrap",
              color: "var(--hud-text-dim)",
              maxHeight: "40vh",
              overflowY: "auto",
            }}
          >
            {detail.content || (ar ? "(فارغ)" : "(empty)")}
          </pre>
        </div>
      )}
    </div>
  );
}

export default memo(SkillsPanel);