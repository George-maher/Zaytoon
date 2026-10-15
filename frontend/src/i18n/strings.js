/**
 * The translation table — one place, two languages.
 *
 * Why this exists, given the app already switches language correctly: because
 * it did so with a ternary at every call site. `ar ? "نشط" : "ACTIVE"` repeated
 * 531 times across 31 components (plus 71 `language === "ar"` comparisons) is
 * correct and unmaintainable at the same time — a copy change means a search
 * for the English text, an Arabic wording fix means finding every place the
 * phrase appears, and nothing can tell you whether a string is missing in one
 * language. This module is the single source for user-visible text.
 *
 * Rules, so it stays one source:
 *
 *  * `t(key, vars)` looks up the *current* language, then falls back to English,
 *    then to the key itself. A missing translation renders as the English text
 *    (visible, wrong language) rather than as an empty node or a raw key
 *    (invisible) — a blank label in a HUD panel looks like a rendering bug, and
 *    the fallback is what a developer can actually act on.
 *  * Keys are `area.thing`, area-scoped so the file stays navigable.
 *  * Interpolation is `{name}` and is intentional: a message assembled by
 *    concatenating translated fragments is untranslatable in Arabic, where the
 *    clause order differs.
 *  * Components that already receive a `language` prop call
 *    `translate(language, key)` directly; components that read it from the shell
 *    use the provider in `./index.jsx`. Both call the same table — the context
 *    is a convenience, never a second dictionary.
 *
 * Migration status is tracked by `src/i18n/ratchet.test.js`, which fails if the
 * number of inline bilingual ternaries outside this module grows.
 */

export const SUPPORTED_LANGUAGES = ["en", "ar"];

/** The direction a language is written in. The shell applies it to `<html>`. */
export function directionFor(language) {
  return language === "ar" ? "rtl" : "ltr";
}

/**
 * Say what went wrong, once, instead of rendering nothing.
 *
 * A missing key in a language that is not English is almost always "someone
 * added a string and forgot this file", and it is worth a console line in
 * development. It is not worth an exception: a missing label must never take
 * a panel down.
 */
function warnMissing(key, language) {
  if (typeof console === "undefined" || !console.warn) return;
  console.warn(`[i18n] no '${language}' string for '${key}'`);
}

/** Fill `{placeholders}`. Unknown placeholders are left as written. */
function interpolate(text, vars) {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole,
  );
}

/**
 * The string for `key` in `language`.
 *
 * `language` is passed rather than read from a module-level variable on
 * purpose: two clients (browser and desktop shell) and a test suite all render
 * the same components at different languages, sometimes at the same time.
 */
export function translate(language, key, vars) {
  const table = STRINGS[language] || STRINGS.en;
  let text = table[key];
  if (text === undefined) {
    if (language !== "en") {
      text = STRINGS.en[key];
      if (text !== undefined) warnMissing(key, language);
    }
    if (text === undefined) {
      warnMissing(key, language);
      return key;
    }
  }
  return interpolate(text, vars);
}

/**
 * Every string the UI can show.
 *
 * `security.*` is migrated from the Security Workspace, which is where the
 * pattern was proven; the remaining areas are listed in the final report with
 * their exact counts and are still rendered by their own components.
 */
export const STRINGS = {
  en: {
    "common.retry": "Retry",
    "common.loading": "LOADING…",
    "common.language_en": "English",
    "common.language_ar": "العربية",

    // Security Workspace.
    "security.security_workspace": "SECURITY WORKSPACE",
    "security.active_testing_requires_an_authorised_scope_enfo": "Active testing requires an authorised scope. Enforced in the execution layer, not in model instructions.",
    "security.authorise_a_target": "AUTHORISE A TARGET",
    "security.active": "ACTIVE",
    "security.target": "Target",
    "security.a_wildcard_for_every_host_is_refused_an_authoris": "A wildcard for every host is refused — an authorisation for everything is not one.",
    "security.operations": "OPERATIONS",
    "security.expires_after_days": "Expires after (days)",
    "security.no_expiry": "no expiry",
    "security.note": "Note",
    "security.why_or_which_engagement": "why, or which engagement",
    "security.authorise": "AUTHORISE",
    "security.authorised_probe": "AUTHORISED PROBE",
    "security.one_non_destructive_request_no_payloads_no_explo": "One non-destructive request. No payloads, no exploitation, no guessing — every finding carries its evidence.",
    "security.url": "URL",
    "security.run_probe": "RUN PROBE",
    "security.authorisation": "authorisation",
    "security.capabilities": "CAPABILITIES",
    "security.denied": "DENIED",
    "security.deny_is_refused_at_the_tool_gate_before_any_appr": "DENY is refused at the tool gate before any approval prompt. ALLOW is recorded and shown, but a confirmation-tier action still asks.",
    "security.no_permissions_listed": "No permissions listed.",
    "security.recorded_authorisations": "RECORDED AUTHORISATIONS",
    "security.loading": "LOADING…",
    "security.none_active_testing_is_refused_until_one_exists": "None. Active testing is refused until one exists.",
    "security.revoked": "REVOKED",
    "security.expired": "EXPIRED",
    "security.active_2": "ACTIVE",
    "security.revoke": "REVOKE",
  },
  ar: {
    "common.retry": "أعد المحاولة",
    "common.loading": "جارٍ التحميل…",
    "common.language_en": "English",
    "common.language_ar": "العربية",

    // Security Workspace.
    "security.security_workspace": "مساحة الأمان",
    "security.active_testing_requires_an_authorised_scope_enfo": "الفحص النشط يتطلب نطاقًا مُصرَّحًا به. الفرض يتم في طبقة التنفيذ، لا في تعليمات النموذج.",
    "security.authorise_a_target": "تفويض هدف",
    "security.active": "نشط",
    "security.target": "الهدف",
    "security.a_wildcard_for_every_host_is_refused_an_authoris": "نطاق كامل للمضيف مرفوض: التفويض لكل شيء ليس تفويضًا.",
    "security.operations": "العمليات",
    "security.expires_after_days": "ينتهي بعد (أيام)",
    "security.no_expiry": "بدون انتهاء",
    "security.note": "ملاحظة",
    "security.why_or_which_engagement": "سبب التفويض / رقم المهمة",
    "security.authorise": "تفويض",
    "security.authorised_probe": "فحص مُصرَّح به",
    "security.one_non_destructive_request_no_payloads_no_explo": "طلب واحد غير مُدمِّر. لا حمولات، ولا تعطيل، ولا تخمين — كل نتيجة مصحوبة بدليلها.",
    "security.url": "الرابط",
    "security.run_probe": "تشغيل الفحص",
    "security.authorisation": "المعرّف",
    "security.capabilities": "الصلاحيات",
    "security.denied": "مرفوض",
    "security.deny_is_refused_at_the_tool_gate_before_any_appr": "«مرفوض» يُرفض عند بوابة الأدوات قبل أي طلب موافقة. «مسموح» يُسجَّل ويُعرض، لكن الأدوات التي تتطلب تأكيدًا تسأل دائمًا.",
    "security.no_permissions_listed": "لا توجد صلاحيات معروضة.",
    "security.recorded_authorisations": "التفويضات المسجّلة",
    "security.loading": "جارٍ التحميل…",
    "security.none_active_testing_is_refused_until_one_exists": "لا تفويضات. الفحص النشط مرفوض حتى تُضاف واحدة.",
    "security.revoked": "ملغى",
    "security.expired": "منتهٍ",
    "security.active_2": "نشط",
    "security.revoke": "إلغاء",
  },
};
