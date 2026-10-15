/**
 * The i18n context object and its consumer hook.
 *
 * In a `.js` file rather than beside the provider: the lint rule this project
 * enforces (`react-refresh/only-export-components`) requires a component file
 * to export *only* components, and a hook export from a `.jsx` file defeats
 * fast refresh for every component in it. The split is mechanical, not
 * architectural — one provider (`./I18nProvider.jsx`), one consumer (here),
 * one table (`./strings.js`).
 */

import { createContext, useContext } from "react";
import { directionFor, translate } from "./strings";

export const I18nContext = createContext(null);

/**
 * The shell's language, or a usable default outside it.
 *
 * Deliberately not an exception when no provider is present: every component
 * that takes a `language` prop must also render in isolation (the tests do
 * exactly that), and a translated panel throwing because it is not wrapped
 * would be a worse failure than falling back to English.
 */
export function useI18n() {
  const ctx = useContext(I18nContext);
  return (
    ctx || {
      language: "en",
      dir: directionFor("en"),
      t: (key, vars) => translate("en", key, vars),
    }
  );
}
